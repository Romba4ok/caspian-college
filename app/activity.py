"""Emit small, credential-free business events only after the database commits."""
from datetime import datetime, timezone
import json
import logging
import re
import sys

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from .audit import audit_text
from .models import AdminAuditEvent, CrmEvent, TaskEvent

logger = logging.getLogger("caspian.activity")
PENDING = "caspian_pending_activity"
TASK_ACTIONS = {
    "sent": "Создал и отправил задачу", "created": "Создал задачу", "edited": "Изменил задачу",
    "assigned": "Назначил исполнителей задачи", "reassigned": "Изменил исполнителей задачи",
    "claimed": "Взял задачу в работу", "released": "Снял с себя задачу",
    "completed": "Выполнил задачу", "returned_for_revision": "Вернул задачу на доработку",
    "cancelled": "Отменил задачу", "rejected": "Отклонил задачу",
    "deadline_changed": "Изменил срок задачи", "comment_added": "Добавил комментарий к задаче",
    "attachment_added": "Добавил материалы к задаче",
}
ADMIN_ACTIONS = {
    "account_created": "Добавил пользователя", "account_updated": "Изменил пользователя",
    "account_blocked": "Отключил доступ пользователю", "account_unblocked": "Включил доступ пользователю",
    "account_deleted": "Удалил пользователя из списка", "account_restored": "Вернул пользователя в список",
    "department_created": "Создал отдел", "department_renamed": "Переименовал отдел",
    "department_archived": "Архивировал отдел", "department_restored": "Восстановил отдел",
    "department_deleted": "Удалил пустой отдел", "upload_policy_updated": "Изменил настройки вложений",
}


def clean(value, limit=200):
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\x00-\x1f\x7f]", " ", audit_text(value)).strip()[:limit]


def activity_payload(model, values):
    ident = values.get("id")
    if type(ident) is not int:
        return None
    snapshot = values.get("actor_snapshot") if isinstance(values.get("actor_snapshot"),dict) else {}
    details = values.get("details") if isinstance(values.get("details"),dict) else {}
    before = details.get("before") if isinstance(details.get("before"),dict) else {}
    after = details.get("after") if isinstance(details.get("after"),dict) else {}
    actor_id=snapshot.get("id",values.get("actor_id"))
    actor_id=actor_id if type(actor_id) is int and 0<actor_id<=2147483647 else None
    actor = dict(id=actor_id, name=clean(snapshot.get("name"),120) or "Неизвестный участник",
        role=snapshot.get("role") if snapshot.get("role") in ("admin","head","employee") else "system")
    if model is TaskEvent:
        source, action, target = "task", values.get("event_type"), values.get("task_id")
        title = clean(after.get("title") or before.get("title"))
        message = f"{TASK_ACTIONS.get(action, 'Изменил задачу')} №{target}"
        if title: message += f" «{title}»"
        if isinstance(details.get("crm"),dict):
            message += " — из CRM"
    elif model is AdminAuditEvent:
        source, action, target = "admin", values.get("action"), values.get("object_id")
        title = clean(after.get("full_name") or before.get("full_name") or after.get("name") or before.get("name"))
        if action == "account_updated" and after.get("password_changed") is True:
            message = "Сменил свой пароль" if actor["id"] == target else "Сменил пароль пользователя"
            if actor["id"] != target:
                message += f" «{title or 'ID '+str(target)}»"
        else:
            message = ADMIN_ACTIONS.get(action,"Изменил настройки платформы")
            if title: message += f" «{title}»"
            if target is not None: message += f" (ID {target})"
    else:
        if values.get("event_type") != "deadline_reminder":
            return None
        source, action, target = "crm", "deadline_reminder", values.get("task_id")
        actor = dict(id=None,name="CRM: "+clean(values.get("source"),64),role="system")
        message = f"Обработала напоминание о сроке задачи №{target}. Новых уведомлений: {values.get('notification_count',0)}"
    action=action if action in TASK_ACTIONS or action in ADMIN_ACTIONS or action=="deadline_reminder" else "data_changed"
    target=target if type(target) is int and 0<target<=2147483647 else None
    moment = values.get("created_at") or values.get("received_at") or datetime.now(timezone.utc)
    if moment.tzinfo is None: moment=moment.replace(tzinfo=timezone.utc)
    return dict(version=1,id=f"{source}:{ident}",timestamp=moment.astimezone(timezone.utc).isoformat(),
        actor=actor,action=action,object_id=target,message=message)


def _collect(session, flush_context):
    pending = session.info.setdefault(PENDING,{})
    for item in session.new:
        if isinstance(item,(TaskEvent,AdminAuditEvent,CrmEvent)):
            pending[id(item)] = item


def _committed(session):
    # A savepoint commit is not a committed business operation.
    if session.in_nested_transaction(): return
    pending = session.info.pop(PENDING,{})
    for item in pending.values():
        try:
            state=inspect(item)
            if not state.persistent: continue
            payload=activity_payload(type(item),state.dict)
            if payload is not None:
                logger.info(json.dumps({"caspian_activity":payload},ensure_ascii=False))
        except Exception:
            # Monitoring failures must never turn a committed user action into an error.
            logger.warning("Не удалось сформировать запись журнала действий")


def _rolled_back(session, previous_transaction):
    if previous_transaction.parent is None:
        session.info.pop(PENDING,None)
    else:
        pending=session.info.get(PENDING,{})
        for key,item in list(pending.items()):
            if inspect(item).transient or inspect(item).detached:
                pending.pop(key,None)


def init_app(app):
    if not logger.handlers:
        handler=logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate=False
    if not event.contains(Session,"after_flush",_collect):
        event.listen(Session,"after_flush",_collect)
        event.listen(Session,"after_commit",_committed)
        event.listen(Session,"after_soft_rollback",_rolled_back)
