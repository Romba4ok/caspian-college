"""Atomic, idempotent CRM mutations with the existing workflow authority checks."""
from datetime import datetime
from hashlib import sha256
import json

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from .crm_events import CrmEventError, _existing
from .extensions import db
from .models import CrmEvent,Task,TaskEvent,TaskNotification,User
from .reminders import as_utc
from .workflow import (WorkflowError,transition_execution,transition_completion,change_task_deadline,
    add_task_comment,edit_task_from_integration,delete_task_from_integration)


def _replay(payload,digest):
    receipt=_existing(payload,digest)
    return (db.session.get(Task,receipt.task_id),receipt,True) if receipt else None


def ingest_task_mutation(task_id,payload,*,deleting=False):
    normalized=dict(payload,task_id=task_id,operation="DELETE" if deleting else "PATCH")
    digest=sha256(json.dumps(normalized,sort_keys=True,separators=(",",":"),
        default=lambda value:as_utc(value).isoformat() if isinstance(value,datetime) else value).encode()).hexdigest()
    replay=_replay(payload,digest)
    if replay:return replay
    task=db.session.get(Task,task_id)
    if task is None:raise CrmEventError("task_not_found","Задача не найдена",404)
    if task.is_deleted:raise CrmEventError("task_deleted","Задача уже удалена",410)
    actor=db.session.get(User,payload["actor_id"])
    if actor is None:raise CrmEventError("actor_not_found","Инициатор не найден",404)
    arguments=dict(task=task,actor=actor,expected_revision=payload["revision"],comment=payload["comment"])
    action=payload["action"];changes=payload["changes"]
    try:
        if deleting:
            task=delete_task_from_integration(**arguments)
        elif action=="edit":
            task=edit_task_from_integration(**arguments,changes=changes)
        elif action in ("assign","reassign","claim","release"):
            task=transition_execution(**arguments,action=action,
                assignee_ids=changes.get("assignee_ids") if action in ("assign","reassign") else None)
        elif action in ("complete","return","cancel","reject"):
            task=transition_completion(**arguments,action=action)
        elif action=="deadline":
            task=change_task_deadline(**arguments,due_at=changes["due_at"])
        else:
            add_task_comment(**arguments)
        event=TaskEvent.query.filter_by(task_id=task.id,actor_id=actor.id).order_by(TaskEvent.id.desc()).first()
        event.details={**event.details,"crm":{"source":payload["source"],"event_id":payload["event_id"],"action":action}}
        count=TaskNotification.query.filter(or_(
            TaskNotification.deduplication_key.like(f"event:{event.id}:user:%"),
            TaskNotification.deduplication_key.like(f"task-delete:e:{event.id}:u:%"))).count()
        receipt=CrmEvent(source=payload["source"],event_id=payload["event_id"],
            event_type="task_deleted" if deleting else "task_updated",task_id=task.id,
            due_at=as_utc(task.due_at),rework_cycle=task.rework_cycle,request_hash=digest,notification_count=count)
        db.session.add(receipt);db.session.flush()
    except IntegrityError:
        db.session.rollback()
        replay=_replay(payload,digest)
        if replay:return replay
        raise
    except (PermissionError,WorkflowError):
        # A concurrent identical command may have committed while we waited on
        # workflow locks. Discard any local changes before consulting its receipt.
        db.session.rollback()
        replay=_replay(payload,digest)
        if replay:return replay
        current=db.session.get(Task,task_id)
        if current is not None and current.is_deleted:
            raise CrmEventError("task_deleted","Задача уже удалена",410) from None
        raise
    return task,receipt,False
