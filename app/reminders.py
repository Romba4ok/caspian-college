"""Idempotent daily reminder calculation; external schedulers invoke the Flask CLI."""

from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
from zoneinfo import ZoneInfo

import click
from flask import current_app, has_request_context
from sqlalchemy import and_, inspect, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import undefer

from .extensions import db
from .models import Department, Task, TaskNotification, User
from .notifications import REMINDER_STATUSES, reminder_notification_recipients, task_notification_recipients
from .permissions import dispatched_clause


def utc_now():
    return datetime.now(timezone.utc)


def as_utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _queue_reminders(task, recipients, now, zone, today):
    """Keep the existing slot key, including across the working schema upgrade."""
    due_at = as_utc(task.due_at)
    delta = (due_at.astimezone(zone).date() - today).days
    if delta > 2:
        return 0
    kind = {2: "reminder_two_days", 1: "reminder_one_day", 0: "reminder_due_today"}.get(delta, "reminder_overdue")
    labels = {"reminder_two_days": "До срока осталось два дня", "reminder_one_day": "До срока остался один день",
              "reminder_due_today": "Срок задачи сегодня", "reminder_overdue": "Задача просрочена"}
    message = f"{labels[kind]}: {task.title}. Срок: {due_at.astimezone(zone).strftime('%d.%m.%Y %H:%M')}"[:500]
    count = 0
    for user in recipients:
        raw_key = f"{task.id}|{user.id}|{due_at.isoformat()}|{today.isoformat()}|{kind}"
        key = sha256(raw_key.encode()).hexdigest()
        if TaskNotification.query.filter_by(deduplication_key=key).first():
            continue
        try:
            with db.session.begin_nested():
                db.session.add(TaskNotification(
                    task_id=task.id, user_id=user.id, event_type=kind, message=message,
                    created_at=now, deduplication_key=key,
                ))
                db.session.flush()
        except IntegrityError:
            if not TaskNotification.query.filter_by(deduplication_key=key).first():
                raise
        else:
            count += 1
    return count


def _run_legacy_reminders(now, zone, today):
    """Request-free schema 04 bridge for the still running Windows scheduler."""
    tasks = Task.query.filter(
        Task.due_at.is_not(None), ~Task.status.in_(("completed", "cancelled")), Task.created_at <= now,
    ).order_by(Task.id).all()
    count = 0
    for task in tasks:
        delta = (as_utc(task.due_at).astimezone(zone).date() - today).days
        if delta > 2:
            continue
        locked = db.session.execute(
            update(Task).where(
                Task.id == task.id, Task.due_at == task.due_at, Task.status == task.status,
                Task.assignee_id == task.assignee_id if task.assignee_id is not None else Task.assignee_id.is_(None),
            ).values(updated_at=Task.updated_at).execution_options(synchronize_session=False)
        )
        if locked.rowcount != 1:
            continue
        db.session.refresh(task)
        count += _queue_reminders(task, task_notification_recipients(task), now, zone, today)
    return count


def _lock_reminder_accounts(tasks):
    """Lock the whole batch's users before any department or task.

    Doing this per task would acquire later users after earlier task/department
    locks, inverting workflow's order when more than one task is due.
    """
    departments = {i for task in tasks for i in (task.sender_department_id, task.recipient_department_id)}
    ids = {i for task in tasks for i in (task.author_id, task.assignee_id)} - {None}
    # NO KEY UPDATE keeps the batch stable and permits workflow notification FK checks.
    accounts = db.session.execute(select(User).where(or_(
        User.id.in_(ids),
        and_(User.role == "head", User.department_id.in_(departments))))
        .order_by(User.id).with_for_update(key_share=True).execution_options(populate_existing=True)).scalars().all()
    db.session.execute(select(Department.id).where(Department.id.in_(departments))
        .order_by(Department.id).with_for_update()).all()
    return accounts


def _lock_reminder_task(task):
    """CAS skips changed lock targets; the no-op UPDATE also locks SQLite.

    Do not change revision, task dates or history. Account and department locks
    for all preliminary tasks have already been acquired in a single order.
    """
    locked = db.session.execute(update(Task).where(
        Task.id == task.id, Task.revision == task.revision, Task.status == task.status,
        Task.due_at == task.due_at, Task.author_id == task.author_id,
        Task.sender_department_id == task.sender_department_id, Task.recipient_department_id == task.recipient_department_id,
        Task.assignee_id == task.assignee_id if task.assignee_id is not None else Task.assignee_id.is_(None),
        dispatched_clause(),
    ).values(updated_at=Task.updated_at).execution_options(synchronize_session=False))
    if locked.rowcount != 1:
        return None
    current = db.session.execute(select(Task).where(Task.id == task.id).options(undefer("*"))
        .execution_options(populate_existing=True)).scalar_one()
    return current


def run_task_reminders(*, now=None):
    """Queue today's slot only, in the caller's transaction; never backfill."""
    now = as_utc(now or utc_now())
    zone = ZoneInfo(current_app.config["TASK_TIMEZONE"])
    today = now.astimezone(zone).date()
    columns = {col["name"] for col in inspect(db.session.connection()).get_columns("tasks")}
    if "is_legacy_dispatched" not in columns:
        if has_request_context():
            raise RuntimeError("Напоминания старой схемы доступны только фоновой команде")
        return _run_legacy_reminders(now, zone, today)
    tasks = Task.query.options(undefer("*")).filter(
        dispatched_clause(), Task.status.in_(REMINDER_STATUSES), Task.due_at.is_not(None),
        Task.created_at <= now, or_(Task.sent_at.is_(None), Task.sent_at <= now),
        Task.due_at < datetime.combine(today + timedelta(days=3), time.min, zone).astimezone(timezone.utc),
    ).order_by(Task.id).populate_existing().all()
    if not tasks:
        return 0
    accounts = _lock_reminder_accounts(tasks)
    count = 0
    for task in tasks:
        if (as_utc(task.due_at).astimezone(zone).date() - today).days > 2:
            continue
        task = _lock_reminder_task(task)
        if task is not None:
            count += _queue_reminders(task, reminder_notification_recipients(task, candidates=accounts), now, zone, today)
    return count


def init_app(app):
    @app.cli.command("send-task-reminders")
    def send_task_reminders():
        """Send today's reminders once per task deadline, recipient and local day."""
        try:
            count = run_task_reminders()
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        click.echo(f"Создано уведомлений: {count}")
