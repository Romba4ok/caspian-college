"""Transactional administrative audit; never commits its caller's operation."""
from flask_login import current_user

from datetime import date, datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo
from sqlalchemy import String, cast, func, literal, select, union_all

from .extensions import db
from .models import AdminAuditEvent, TaskComment, TaskEvent, User


def actor_snapshot(actor):
    return {"id": actor.id, "name": actor.full_name, "role": actor.role,
            "department_id": actor.department_id,
            "department_name": actor.department.name if actor.department is not None else None}


def account_snapshot(user):
    return {field: getattr(user, field) for field in
            ("full_name", "username", "role", "department_id", "is_blocked")}


def task_snapshot(task):
    """Capture values, including unknown dates, without keeping live ORM objects."""
    from datetime import datetime, timezone
    fields = ("title", "description", "author_id", "sender_department_id", "recipient_department_id",
              "assignee_ids", "status", "priority", "category_id", "category", "contact_person",
              "due_at", "sent_at", "completed_at", "closed_at", "revision", "is_rework", "rework_cycle")
    result = {}
    for field in fields:
        value = getattr(task, field)
        if isinstance(value, datetime):
            value = (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()
        result[field] = value
    result["assignees"] = [{"id": user.id, "name": user.full_name} for user in task.assignees]
    return result


def record_task_event(*, task, actor, event_type, comment, before=None):
    """A comment and immutable action snapshot belong to the caller's transaction."""
    entry = TaskComment(task_id=task.id, author_id=actor.id, body=comment)
    db.session.add(entry)
    db.session.flush()
    event = TaskEvent(task_id=task.id, actor_id=actor.id, actor_snapshot=actor_snapshot(actor),
        event_type=event_type, comment_id=entry.id,
        details={"before": before or {}, "after": task_snapshot(task), "comment": comment, "comment_id": entry.id})
    if event_type in ("assigned", "claimed", "reassigned", "released", "returned_for_revision"):
        old_ids = set((before or {}).get("assignee_ids") or [])
        new_ids = set(event.details["after"]["assignee_ids"])
        event.details = {**event.details, "added_assignee_ids": sorted(new_ids-old_ids),
                         "removed_assignee_ids": sorted(old_ids-new_ids)}
    db.session.add(event)
    db.session.flush()
    return event


def _safe(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key == "password_changed" and isinstance(item, bool):
                result[key] = item
            elif isinstance(key,str) and not any(part in key.casefold() for part in ("password", "secret", "token", "connection", "database", "cookie", "session", "auth_stamp", "authstamp", "api_key", "apikey", "api-key", "credential", "authorization")):
                result[key] = _safe(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    return value


def record_admin_event(action, object_type, object_id, *, before=None, after=None, snapshot=None, actor=None):
    initiator = actor if actor is not None else current_user
    entry = AdminAuditEvent(actor_id=initiator.id,
        actor_snapshot=snapshot if snapshot is not None else actor_snapshot(initiator),
        action=action, object_type=object_type, object_id=object_id,
        details=_safe({"before": before, "after": after}))
    db.session.add(entry)
    return entry


ADMIN_ACTIONS = {
    "account_created": "Учётная запись создана", "account_updated": "Учётная запись изменена",
    "account_blocked": "Учётная запись отключена", "account_unblocked": "Доступ к учётной записи восстановлен",
    "account_deleted": "Учётная запись удалена", "department_created": "Отдел создан",
    "department_renamed": "Отдел переименован", "department_archived": "Отдел архивирован",
    "department_restored": "Отдел восстановлен", "department_deleted": "Пустой отдел удалён",
    "category_created": "Категория создана", "category_renamed": "Категория переименована",
    "category_archived": "Категория архивирована", "upload_policy_updated": "Настройки файлов изменены",
}
OBJECTS = {"task": "Задача", "user": "Пользователь", "department": "Отдел",
           "category": "Категория", "system_settings": "Настройки файлов"}
ROLES = {"admin": "Администратор", "head": "Начальник отдела", "employee": "Сотрудник отдела", "system": "Система"}
ADMIN_FIELDS = {"full_name": "Имя", "username": "Логин", "role": "Роль", "department_id": "Отдел",
    "is_blocked": "Заблокирован", "password_changed": "Пароль изменён", "name": "Название",
    "is_archived": "В архиве", "max_upload_mb": "Лимит файла, МБ", "allowed_extensions": "Разрешённые форматы",
    "category_id": "Категория (ID)", "revision": "Версия"}


class AuditFilterError(ValueError):
    def __init__(self, message, field):
        super().__init__(message)
        self.field = field


def audit_text(value):
    """Never expose credential-shaped content, including in historical free text."""
    text = str(value)
    text = re.sub(r"(?im)^(\s*(?:set-)?cookie\s*:\s*)[^\r\n]+", r"\1[скрыто]", text)
    text = re.sub(r"(?i)\b(?:postgres(?:ql)?(?:\+[\w]+)?|mysql(?:\+[\w]+)?|mssql(?:\+[\w]+)?|sqlite|redis|mongodb(?:\+srv)?)://[^\s<>\"']+", "[строка подключения скрыта]", text)
    text = re.sub(r"(?i)\b(?:https?|[a-z][\w+.-]*)://[^\s/@]+:[^\s/@]+@[^\s<>\"']+", "[строка подключения скрыта]", text)
    text = re.sub(r"(?i)(\b(?:password(?:_hash)?|passwd|pwd|secret(?:_key)?|(?:access_|refresh_|session_|csrf_)?token|session(?:_id|_cookie|id)?|_?auth_stamp|cookie|authorization|api[_-]?key|пароль|токен)\b[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|(?:Bearer|Basic)\s+[^\s,;]+|[^\s,;]+)", r"\1[скрыто]", text)
    text = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [скрыто]", text)
    return re.sub(r"\$(?:scrypt|pbkdf2|2[aby])[^\s]+|\b(?:scrypt|pbkdf2):[^\s]+", "[хэш скрыт]", text)


def _positive(value, field):
    if not re.fullmatch(r"[1-9][0-9]{0,9}", str(value), flags=re.ASCII) or int(value) > 2147483647:
        raise AuditFilterError("Укажите положительный числовой идентификатор или номер страницы.", field)
    return int(value)


def _period(start, end):
    parsed = []
    for value, field in ((start, "date_from"), (end, "date_to")):
        try:
            if not value:
                parsed.append(None)
            elif not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                raise ValueError
            else:
                parsed.append(date.fromisoformat(value))
        except (ValueError, TypeError):
            raise AuditFilterError("Укажите существующую дату в формате ГГГГ-ММ-ДД.", field) from None
    if all(parsed) and parsed[0] > parsed[1]:
        raise AuditFilterError("Начало периода не может быть позже его окончания.", "date_to")
    bounds = []
    for value, field in zip(parsed, ("date_from", "date_to")):
        try:
            if value is not None and field == "date_to":
                value += timedelta(days=1)
            bounds.append(datetime.combine(value, datetime.min.time(), ZoneInfo("Asia/Qyzylorda"))
                          .astimezone(timezone.utc) if value else None)
        except (ValueError, OverflowError):
            raise AuditFilterError("Эта граница периода не поддерживается. Выберите другую дату.", field) from None
    return bounds


def _audit_source():
    """Union all preserves events with matching IDs/timestamps from both sources."""
    statements = []
    for model, source, action, kind, object_id in (
        (TaskEvent, "task", TaskEvent.event_type, literal("task"), TaskEvent.task_id),
        (AdminAuditEvent, "admin", AdminAuditEvent.action, AdminAuditEvent.object_type, AdminAuditEvent.object_id),
    ):
        statements.append(select(literal(source).label("source"), model.id.label("id"), model.created_at,
            cast(func.coalesce(cast(model.actor_id, String), model.actor_snapshot["id"].as_string()), String).label("actor_key"),
            model.actor_snapshot, model.details, action.label("action"), kind.label("object_type"), object_id.label("object_id")))
    return union_all(*statements).subquery("audit_events")


def _audit_value(field, value):
    from .tasks import PRIORITY_LABELS, STATUS_LABELS
    if value is None or value == "":
        return "Не указано"
    if type(value) is bool:
        return "Да" if value else "Нет"
    historical_statuses = STATUS_LABELS | {"accepted":"Принята (прежний процесс)", "ready":"Готова (прежний процесс)"}
    labels = {"role": ROLES, "status": historical_statuses, "priority": PRIORITY_LABELS}
    if field in labels and isinstance(value, str):
        return labels[field].get(value, "Историческое значение неизвестно")
    if field.endswith("_at") and isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
            moment = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
            return moment.astimezone(ZoneInfo("Asia/Qyzylorda")).strftime("%d.%m.%Y %H:%M")
        except (ValueError, OverflowError):
            return "Дата неизвестна"
    if isinstance(value, list) and field == "allowed_extensions":
        return ", ".join(audit_text(item) for item in value if isinstance(item, str)) or "Не указано"
    return audit_text(value) if isinstance(value, (str, int, float)) else "Составное значение скрыто"


def _audit_entry(row):
    from .tasks import HISTORY_FIELDS, HISTORY_LABELS
    snapshot = row["actor_snapshot"] if isinstance(row["actor_snapshot"], dict) else {}
    details = row["details"] if isinstance(row["details"], dict) else {}
    before = details.get("before") if isinstance(details.get("before"), dict) else {}
    after = details.get("after") if isinstance(details.get("after"), dict) else {}
    fields = HISTORY_FIELDS | ADMIN_FIELDS
    changes = [(label, _audit_value(key, before.get(key)), _audit_value(key, after.get(key)))
        for key, label in fields.items() if (key in before or key in after) and before.get(key) != after.get(key)]
    action = (ADMIN_ACTIONS if row["source"] == "admin" else HISTORY_LABELS).get(row["action"], "Историческое событие")
    if row["source"] == "task" and row["action"] in ("created", "edited") and after.get("status") == "draft":
        action = "Черновик создан" if row["action"] == "created" else "Черновик изменён"
    comment = details.get("comment") or details.get("reason")
    if not isinstance(comment,str) or not comment.strip():
        comment = "Системное пояснение: " + action if row["source"] == "admin" and row["action"] in ADMIN_ACTIONS else "Пояснение исторического события не сохранено"
    actor_key = row["actor_key"]
    if actor_key is not None and not re.fullmatch(r"[1-9][0-9]{0,9}", str(actor_key), flags=re.ASCII):
        actor_key = None
    saved_role = snapshot.get("role") if isinstance(snapshot.get("role"), str) else None
    saved_department = snapshot.get("department_name")
    department_name = audit_text(saved_department) if isinstance(saved_department, str) and saved_department else "Отдел на момент действия неизвестен"
    created_at = row["created_at"]
    created_at = created_at.astimezone(timezone.utc) if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
    return dict(source=row["source"], id=row["id"], created_at=created_at, actor_key=actor_key,
        actor_name=audit_text(snapshot.get("name")) if isinstance(snapshot.get("name"), str) and snapshot["name"] else "Имя инициатора неизвестно",
        actor_role=ROLES.get(saved_role, "Историческая роль неизвестна"),
        actor_department=None if saved_role in ("admin", "system") else department_name,
        action=action, object_label=OBJECTS.get(row["object_type"], "Исторический объект"),
        object_id=row["object_id"], task_id=row["object_id"] if row["source"] == "task" else None,
        comment=audit_text(comment) if isinstance(comment, str) else None, changes=changes)


def audit_page(actor, *, user_id=None, date_from="", date_to="", page=1, per_page=20):
    """Read-only journal; validate current admin before any filter or history query."""
    ident = getattr(actor, "id", None)
    if type(ident) is not int:
        raise PermissionError("Журнал доступен только администратору")
    with db.session.no_autoflush:
        current = db.session.get(User, ident, populate_existing=True)
    if current is None or current.role != "admin" or not current.is_active:
        raise PermissionError("Журнал доступен только администратору")
    user_id = _positive(user_id, "user_id") if user_id not in (None, "") else None
    page = _positive(page, "page")
    if type(per_page) is not int or not 1 <= per_page <= 100:
        raise AuditFilterError("Недопустимый размер страницы.", "page")
    start, end = _period(date_from, date_to)
    source = _audit_source()
    conditions = []
    if user_id is not None:
        conditions.append(source.c.actor_key == str(user_id))
    if start:
        conditions.append(source.c.created_at >= start)
    if end:
        conditions.append(source.c.created_at < end)
    total = db.session.scalar(select(func.count()).select_from(source).where(*conditions))
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    order = (source.c.created_at.desc(), source.c.source.asc(), source.c.id.desc())
    rows = db.session.execute(select(source).where(*conditions).order_by(*order)
        .limit(per_page).offset((page - 1) * per_page)).mappings()
    entries = [_audit_entry(row) for row in rows]
    # Latest saved identity per initiator, including deleted accounts; no live-name substitution.
    ranked = select(source.c.actor_key, source.c.actor_snapshot,
        func.row_number().over(partition_by=source.c.actor_key, order_by=order).label("rank")).subquery()
    choices = []
    for key, snapshot in db.session.execute(select(ranked.c.actor_key, ranked.c.actor_snapshot).where(ranked.c.rank == 1)):
        if key is not None and re.fullmatch(r"[1-9][0-9]{0,9}", str(key), flags=re.ASCII) and int(key) <= 2147483647:
            name = snapshot.get("name") if isinstance(snapshot, dict) else None
            choices.append(dict(id=int(key), name=audit_text(name) if isinstance(name, str) and name else "Имя неизвестно"))
    if user_id is not None and not any(item["id"] == user_id for item in choices):
        choices.append(dict(id=user_id, name="Инициатор без записей"))
    choices.sort(key=lambda item: (item["name"].casefold(), item["id"]))
    return dict(entries=entries, total=total, page=page, pages=pages, users=choices,
                filters=dict(user_id=str(user_id) if user_id else "", date_from=date_from, date_to=date_to))
