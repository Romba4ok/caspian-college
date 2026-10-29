"""Internal event delivery and permission-filtered notification queries."""

from flask import has_request_context
from flask_login import current_user
from sqlalchemy import and_, inspect, or_

from .extensions import db
from .models import Task, TaskNotification, User
from .permissions import can_access_task, is_platform_user, task_visibility_clause
from .i18n import t

NOTIFICATION_LABELS = {
    "task_created": "Новая задача для отдела", "edited": "Описание задачи изменено",
    "assigned": "Задача принята исполнителем", "reassigned": "Исполнитель задачи изменён",
    "status_changed": "Рабочий статус задачи изменён", "completed": "Результат задачи подтверждён",
    "returned_for_revision": "Задача возвращена на доработку", "clarification_answered": "Получен ответ на уточнение",
    "cancelled": "Задача отменена", "deadline_changed": "Изменён срок задачи",
    "comment_added": "Добавлен комментарий", "attachment_added": "Добавлено вложение",
    "reminder_two_days": "До срока осталось два дня", "reminder_one_day": "До срока остался один день",
    "reminder_due_today": "Срок задачи сегодня", "reminder_overdue": "Задача просрочена",
}


def notification_text(notification):
    if notification.deduplication_key and notification.deduplication_key.startswith("event:"):
        if notification.event_type == "rejected":
            import re
            from .models import TaskEvent
            match = re.fullmatch(r"event:(\d+):user:(\d+)", notification.deduplication_key)
            if match and int(match[2]) == notification.user_id:
                event = db.session.get(TaskEvent, int(match[1]))
                if event is not None and event.task_id == notification.task_id and event.event_type == "rejected":
                    title = event.details.get("after", {}).get("title", notification.task.title)
                    return f"Задача отклонена получателем: {title}. Причина: {event.details.get('comment', '')}"
        return notification.message
    if notification.task is None or notification.event_type not in NOTIFICATION_LABELS:
        return notification.message
    title = notification.task.title
    message = f"{t(NOTIFICATION_LABELS[notification.event_type])}: {title}"
    if notification.event_type.startswith("reminder_") and ". Срок: " in notification.message:
        message += f". {t('Срок')}: {notification.message.rsplit('. Срок: ', 1)[1]}"
    return message


def task_notification_recipients(task, *, creation=False):
    if creation:
        candidates = User.query.filter_by(
            role="teacher", department_id=task.recipient_department_id, is_blocked=False,
        ).all()
    else:
        ids = {task.author_id} | (set(task.assignee_ids) if inspect(db.session.connection()).has_table("task_assignees")
            else {task.legacy_assignee_id} - {None})
        candidates = User.query.filter(User.id.in_(ids)).all()
    # Only the pre-upgrade, request-free reminder CLI may still read revision 04.
    # Never use this bridge in HTTP authorization or on the new workflow schema.
    if not creation and not has_request_context() and any(user.role == "teacher" for user in candidates):
        columns = {col["name"] for col in inspect(db.session.connection()).get_columns("tasks")}
        if "is_legacy_dispatched" not in columns:
            return [user for user in candidates if user.is_active and user.role == "teacher"
                    and user.department_id in (task.sender_department_id, task.recipient_department_id)]
    return [user for user in candidates if can_access_task(user, task)]


def notify_task_participants(task, event_type, message, *, creation=False):
    for user in task_notification_recipients(task, creation=creation):
        db.session.add(TaskNotification(
            task_id=task.id, user_id=user.id, event_type=event_type,
            message=f"{message}: {task.title}"[:500],
        ))


def visible_notifications_query(user):
    query = TaskNotification.query.outerjoin(Task, TaskNotification.task_id == Task.id)
    if not is_platform_user(user):
        return query.filter(db.false())
    query = query.filter(TaskNotification.user_id == user.id)
    return query.filter(or_(
        TaskNotification.task_id.is_(None),
        and_(Task.id.is_not(None), task_visibility_clause(user)),
    ))


def init_app(app):
    @app.context_processor
    def notification_context():
        allowed = is_platform_user(current_user)
        count = visible_notifications_query(current_user).filter(TaskNotification.is_read.is_(False)).count() if allowed else 0
        return {"unread_notification_count": count}


EVENT_LABELS = {
    "submitted_for_approval": "Заявка передана на согласование",
    "approval_withdrawn": "Автор отозвал заявку",
    "approval_returned": "Заявка возвращена автору",
    "approved_and_sent": "Задача одобрена и отправлена",
    "sent": "Задача отправлена",
    "assigned": "Состав исполнителей назначен",
    "reassigned": "Состав исполнителей изменён",
    "claimed": "Сотрудник взял задачу",
    "released": "Исполнитель снял задачу с себя",
    "work_started": "Работа начата",
    "clarification_requested": "Запрошено уточнение",
    "clarification_answered": "Получен ответ на уточнение",
    "result_submitted": "Результат поступил на внутреннюю проверку",
    "result_forwarded": "Результат поступил на окончательную приёмку",
    "returned_for_revision": "Результат возвращён на доработку",
    "completed": "Задача выполнена",
    "cancelled": "Задача отменена",
    "rejected": "Задача отклонена получателем",
    "deadline_changed": "Срок задачи изменён",
    "comment_added": "Добавлен комментарий",
    "attachment_added": "Добавлено вложение",
    "edited": "Черновик изменён",
}
NOTIFICATION_LABELS.update(EVENT_LABELS)


def _department_heads(task):
    users = User.query.filter(User.role == "head", User.is_blocked.is_(False),
        User.department_id.in_((task.sender_department_id, task.recipient_department_id))).populate_existing().all()
    return ({u.id for u in users if u.department_id == task.sender_department_id},
            {u.id for u in users if u.department_id == task.recipient_department_id})


def _stage_heads(task, sender_heads, recipient_heads):
    from .permissions import is_task_dispatched
    if not is_task_dispatched(task) or task.status == "sender_acceptance":
        return sender_heads
    if task.status == "awaiting_clarification":
        return sender_heads | recipient_heads
    return recipient_heads


REMINDER_STATUSES = ("new", "in_progress")


def reminder_notification_recipients(task, *, candidates=None, assignee_ids=None):
    """Author, receiving heads and available executor; no system actor to exclude.

    The job supplies locked accounts. Direct callers get fresh account records.
    An assignee who moved out of the receiving department is no longer an owner,
    even when their new sender membership still permits reading the task.
    """
    from .permissions import is_task_dispatched
    if not is_task_dispatched(task) or task.status not in REMINDER_STATUSES or task.due_at is None:
        return []
    roster = set(task.assignee_ids if assignee_ids is None else assignee_ids)
    if candidates is None:
        candidates = User.query.filter(or_(User.id.in_({task.author_id} | roster),
            and_(User.role == "head", User.department_id.in_(
                (task.sender_department_id, task.recipient_department_id))))).populate_existing().all()
    ids = {task.author_id} | {u.id for u in candidates if u.role == "head" and u.department_id == task.recipient_department_id}
    ids |= {u.id for u in candidates if u.id in roster and u.department_id == task.recipient_department_id}
    return [u for u in candidates if u.id in ids and u.role in ("head", "employee") and can_access_task(u, task)]


def event_notification_recipients(task, event):
    """Explicit current workflow matrix; a cached delivery never gains new owners."""
    from .permissions import is_task_dispatched, has_available_assignee
    if not is_task_dispatched(task):
        return []
    sender_heads, recipient_heads = _department_heads(task)
    author = {task.author_id}
    assignee = {u.id for u in task.assignees if u.is_active and u.role in ("employee", "head")
                and u.department_id == task.recipient_department_id}
    kind = event.event_type
    if kind == "sent":
        ids = recipient_heads | author
    elif kind in ("assigned", "claimed", "reassigned"):
        previous = set(event.details.get("before", {}).get("assignee_ids", []))
        ids = assignee | author | recipient_heads | (previous if kind == "reassigned" else set())
    elif kind == "released":
        ids = assignee | author | recipient_heads
    elif kind == "completed":
        ids = assignee | author | sender_heads
    elif kind == "returned_for_revision":
        ids = assignee | recipient_heads
    elif kind in ("cancelled", "deadline_changed", "rejected"):
        ids = author | assignee | sender_heads | recipient_heads
    elif kind in ("comment_added", "attachment_added"):
        ids = author | assignee | recipient_heads
        if (event.actor_snapshot or {}).get("department_id") == task.recipient_department_id:
            ids |= sender_heads
    else:
        return []
    ids -= {None, event.actor_id}
    if kind in ("sent", "rejected"):
        ids.add(task.author_id)
    return [u for u in User.query.filter(User.id.in_(ids)).populate_existing().all()
            if u.role in ("head", "employee") and can_access_task(u, task)]


def deliver_event_notifications(task, event):
    """One key per persisted event/user; no commit or delivery outside this DB."""
    if event.id is None or event.task_id != task.id or not inspect(event).persistent:
        raise ValueError("Уведомление должно относиться к сохранённому событию задачи")
    from sqlalchemy import select
    from sqlalchemy.orm import undefer
    from .models import TaskEvent
    # A delivery retry must freeze the first recipient set, even in parallel.
    # Acquire task before event, as workflow already does, never the inverse.
    task = db.session.execute(select(Task).where(Task.id == task.id).options(undefer("*"))
        .with_for_update(key_share=True).execution_options(populate_existing=True)).scalar_one()
    event = db.session.execute(select(TaskEvent).where(TaskEvent.id == event.id, TaskEvent.task_id == task.id)
        .options(undefer("*")).with_for_update().execution_options(populate_existing=True)).scalar_one()
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    factory = pg_insert if db.session.get_bind().dialect.name == "postgresql" else sqlite_insert
    delivered = 0
    recipient_ids = event.details.get("notification_recipient_ids")
    if recipient_ids is None:
        recipients = event_notification_recipients(task, event)
        event.details = {**event.details, "notification_recipient_ids": sorted(u.id for u in recipients)}
    else:
        recipients = [u for u in User.query.filter(User.id.in_(recipient_ids)).populate_existing().all()
            if u.role in ("head", "employee") and can_access_task(u, task)]
    for user in recipients:
        message = f"{EVENT_LABELS.get(event.event_type, 'Изменение задачи')}: {event.details.get('after', {}).get('title', task.title)}"
        if event.event_type == "rejected":
            message += f". Причина: {event.details.get('comment', '')}"
        statement = factory(TaskNotification).values(task_id=task.id, user_id=user.id,
            event_type=event.event_type, message=message[:500], is_read=False,
            deduplication_key=f"event:{event.id}:user:{user.id}")
        result = db.session.execute(statement.on_conflict_do_nothing(index_elements=["deduplication_key"])
            .returning(TaskNotification.id))
        delivered += result.scalar_one_or_none() is not None
    return delivered


def unread_notification_ids(user):
    return [row[0] for row in visible_notifications_query(user).filter(TaskNotification.is_read.is_(False))
            .with_entities(TaskNotification.id).order_by(TaskNotification.id).all()]


def notification_read_boundary(user):
    from flask import current_app
    from itsdangerous import URLSafeSerializer
    # Exact IDs, not a sequence maximum: an older uncommitted insert may become
    # visible later with a smaller ID. It must also stay unread.
    return URLSafeSerializer(current_app.secret_key, salt="notification-read-boundary").dumps(
        {"user_id": user.id, "ids": unread_notification_ids(user)})


def mark_all_notifications_read(user, boundary):
    from flask import current_app
    from itsdangerous import URLSafeSerializer, BadData
    from sqlalchemy import update
    if not is_platform_user(user):
        raise PermissionError("Требуется действующий пользователь")
    try:
        snapshot = URLSafeSerializer(current_app.secret_key, salt="notification-read-boundary").loads(boundary)
    except (BadData, TypeError):
        raise ValueError("Обновите страницу уведомлений") from None
    if not isinstance(snapshot, dict) or snapshot.get("user_id") != user.id:
        raise PermissionError("Недоступная граница прочтения")
    ids = snapshot.get("ids")
    if not isinstance(ids, list) or any(type(i) is not int or not 0 < i <= 2**31-1 for i in ids):
        raise ValueError("Некорректная граница прочтения")
    changed = 0
    for offset in range(0, len(ids), 500):
        visible = visible_notifications_query(user).with_entities(TaskNotification.id).statement.correlate(None)
        result = db.session.execute(update(TaskNotification).where(TaskNotification.user_id == user.id,
            TaskNotification.id.in_(ids[offset:offset+500]), TaskNotification.id.in_(visible),
            TaskNotification.is_read.is_(False)).values(is_read=True).execution_options(synchronize_session=False))
        changed += result.rowcount
    return changed

