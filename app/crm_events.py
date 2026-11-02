"""CRM deadline events use the existing recipients, locks and daily deduplication."""
from hashlib import sha256
import json
from zoneinfo import ZoneInfo

from flask import current_app
from sqlalchemy.exc import IntegrityError

from .extensions import db
from .models import CrmEvent, Task, TaskEvent, TaskNotification, User
from .notifications import reminder_notification_recipients
from .permissions import ACTIVE, is_task_dispatched
from .reminders import as_utc, utc_now, _lock_reminder_accounts, _lock_reminder_task, _queue_reminders
from .workflow import create_task, make_creation_key


class CrmEventError(ValueError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.status = code, status


def _existing(payload, digest):
    receipt = CrmEvent.query.filter_by(source=payload["source"], event_id=payload["event_id"]).first()
    if receipt is not None and receipt.request_hash != digest:
        raise CrmEventError("event_conflict", "Этот event_id уже использован с другим содержимым")
    return receipt


def ingest_deadline_event(payload, *, now=None):
    """Validate a deadline snapshot; commit the receipt and notifications together.

    Callers validate the flat payload. Only current eligible platform accounts
    receive messages; CRM cannot choose arbitrary users or supply notification text.
    """
    moment = as_utc(now or utc_now())
    normalized = {**payload, "due_at": as_utc(payload["due_at"]).isoformat()}
    digest = sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    previous = _existing(payload, digest)
    if previous is not None:
        return previous, True
    task = db.session.get(Task, payload["task_id"])
    if task is None:
        raise CrmEventError("task_not_found", "Задача не найдена", 404)
    # Same lock order as workflow / scheduled reminders: accounts, departments, task.
    accounts = _lock_reminder_accounts([task], multi=True)
    task = _lock_reminder_task(task, multi=True)
    if task is None:
        raise CrmEventError("task_changed", "Задача изменилась; получите её актуальные данные")
    # Another request may have committed the same event while locks were acquired.
    previous = _existing(payload, digest)
    if previous is not None:
        return previous, True
    if task.status not in ACTIVE or not is_task_dispatched(task):
        raise CrmEventError("task_not_active", "Для закрытой или неотправленной задачи напоминание недоступно")
    if task.due_at is None or as_utc(task.due_at) != as_utc(payload["due_at"]) or task.rework_cycle != payload["rework_cycle"]:
        raise CrmEventError("deadline_changed", "Срок или цикл доработки изменился; получите актуальную задачу")
    if as_utc(task.created_at) > moment or as_utc(task.sent_at) > moment:
        raise CrmEventError("task_not_active", "Задача ещё не отправлена")
    zone = ZoneInfo(current_app.config["TASK_TIMEZONE"])
    today = moment.astimezone(zone).date()
    if (as_utc(task.due_at).astimezone(zone).date() - today).days > 2:
        raise CrmEventError("reminder_not_due", "Напоминания доступны за два дня до срока и позднее")
    receipt = CrmEvent(source=payload["source"], event_id=payload["event_id"], event_type=payload["event_type"],
        task_id=task.id, due_at=as_utc(task.due_at), rework_cycle=task.rework_cycle,
        request_hash=digest, notification_count=0, received_at=moment)
    db.session.add(receipt)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        previous = _existing(payload, digest)
        if previous is None:
            raise
        return previous, True
    recipients = reminder_notification_recipients(task, candidates=accounts, assignee_ids=task.assignee_ids)
    receipt.notification_count = _queue_reminders(task, recipients, moment, zone, today, cycle=task.rework_cycle)
    db.session.flush()
    return receipt, False


def ingest_task_creation(payload):
    """Create through the same workflow as the form, with an atomic CRM receipt.

    The shared (source, event_id) constraint arbitrates concurrent requests,
    including requests with different authors. Losing transactions roll back
    their task, history and notifications before returning the winning receipt.
    """
    normalized = {**payload, "event_type": "task_created",
        "due_at": as_utc(payload["due_at"]).isoformat()}
    digest = sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    previous = _existing(payload, digest)
    if previous is not None:
        return db.session.get(Task, previous.task_id), previous, True
    author = db.session.get(User, payload["author_id"])
    if author is None:
        raise CrmEventError("author_not_found", "Автор не найден", 404)
    try:
        task = create_task(actor=author, recipient_department_id=payload["recipient_department_id"],
            title=payload["title"], description=payload["description"], due_at=payload["due_at"],
            priority=payload["priority"], creation_key=make_creation_key(author))
        event = TaskEvent.query.filter_by(task_id=task.id, event_type="sent").one()
        event.details = {**event.details, "crm": {"source": payload["source"], "event_id": payload["event_id"]}}
        receipt = CrmEvent(source=payload["source"], event_id=payload["event_id"], event_type="task_created",
            task_id=task.id, due_at=as_utc(task.due_at), rework_cycle=task.rework_cycle,
            request_hash=digest, notification_count=TaskNotification.query.filter_by(task_id=task.id).count())
        db.session.add(receipt)
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        previous = _existing(payload, digest)
        if previous is None:
            raise
        return db.session.get(Task, previous.task_id), previous, True
    return task, receipt, False
