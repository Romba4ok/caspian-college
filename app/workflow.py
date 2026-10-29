"""Task creation and shared workflow validation primitives."""

from datetime import datetime, timezone
from functools import wraps
from hashlib import sha256
import hmac
import json
import re
from secrets import token_hex

from flask import current_app

from sqlalchemy import update

from .extensions import db
from .notifications import deliver_event_notifications
from .models import TASK_PRIORITIES, TASK_STATUSES, Task, TaskAttachment, TaskComment, TaskEvent, User
from .permissions import (
    can_change_deadline, can_change_working_status, can_contribute_task, can_reassign_task,
    is_recipient_employee, is_sender_employee,
)


class WorkflowError(ValueError):
    """A task action was rejected by a server-side workflow rule."""
    def __init__(self, message, field=None):
        super().__init__(message)
        self.field = field


class WorkflowUnavailable(WorkflowError):
    """Mutation stays closed until its separately tested implementation task."""


class WorkflowRemoved(WorkflowError):
    """The approval/review lifecycle is no longer a supported operation."""


def _pending_workflow(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        raise WorkflowUnavailable("Новый обработчик действия ещё не подключён")
    return guarded


def _removed_workflow(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        raise WorkflowRemoved("Этот этап удалён из процесса работы задач")
    return guarded


class WorkflowConflict(WorkflowError):
    """A stale page must never retry a business decision automatically."""


def _integer(value, label, *, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise WorkflowError(label)
    if isinstance(value, str) and (not value.isascii() or not value.isdigit()):
        raise WorkflowError(label)
    try:
        number = int(value)
    except (ValueError, OverflowError):
        raise WorkflowError(label) from None
    if not (0 if allow_zero else 1) <= number < 2**31 - 1:
        raise WorkflowError(label)
    return number


def _locked_context(actor, task=None, recipient_department_id=None, *, user_ids=()):
    """Lock in user -> departments by ID -> task order; then re-read authority.

    The preliminary task read only discovers lock targets. A concurrent task update
    changing them causes a conflict, never a second lock acquired out of order.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import undefer
    from .models import Department
    from .permissions import is_platform_user, can_access_task
    if actor is None or getattr(actor, "id", None) is None:
        raise PermissionError("Требуется действующий сотрудник отдела")
    with db.session.no_autoflush:
        preliminary = None
        previous_assignee_ids = ()
        if task is not None:
            preliminary = db.session.execute(select(Task).where(Task.id == task.id)
                .options(undefer("*")).execution_options(populate_existing=True)).scalar_one_or_none()
            if preliminary is None:
                raise LookupError("Задача не найдена")
            targets = {preliminary.sender_department_id, preliminary.recipient_department_id}
            from .models import TaskAssignee
            previous_assignee_ids = tuple(db.session.execute(select(TaskAssignee.user_id).where(
                TaskAssignee.task_id == preliminary.id).order_by(TaskAssignee.user_id)).scalars())
        else:
            targets = set()
        # Include the old and proposed assignee before any department/task lock.
        # This serializes assignments with account blocking, role and department changes.
        account_ids = {actor.id, *user_ids}
        account_ids.update(previous_assignee_ids)
        # FOR NO KEY UPDATE serializes role edits while allowing notification FK KEY SHARE.
        accounts = db.session.execute(select(User).where(User.id.in_(account_ids)).order_by(User.id)
            .with_for_update(key_share=True).execution_options(populate_existing=True)).scalars().all()
        actor = next((account for account in accounts if account.id == actor.id), None)
        if not is_platform_user(actor) or actor.role == "admin":
            raise PermissionError("Действие доступно только действующим участникам отдела")
        if preliminary is not None and not can_access_task(actor, preliminary):
            raise PermissionError("Задача недоступна")
        targets.add(actor.department_id)
        if recipient_department_id is not None:
            targets.add(recipient_department_id)
        departments = db.session.execute(select(Department).where(Department.id.in_(targets))
            .order_by(Department.id).options(undefer("*")).with_for_update()
            .execution_options(populate_existing=True)).scalars().all()
        departments = {item.id: item for item in departments}
        if actor.department_id not in departments:
            raise PermissionError("У сотрудника отсутствует отдел")
        if task is not None:
            task = db.session.execute(select(Task).where(Task.id == task.id).options(undefer("*"))
                .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
            if task is None or {task.sender_department_id, task.recipient_department_id} - targets:
                raise WorkflowConflict("Задача уже изменена. Обновите страницу")
            current_ids = tuple(db.session.execute(select(TaskAssignee.user_id).where(
                TaskAssignee.task_id == task.id).order_by(TaskAssignee.user_id)).scalars())
            if current_ids != previous_assignee_ids:
                raise WorkflowConflict("Состав исполнителей уже изменён. Обновите страницу")
            db.session.expire(task, ["assignments"])
            if not can_access_task(actor, task):
                raise PermissionError("Задача недоступна")
        return actor, task, departments


def _task_fields(*, actor, departments, recipient_department_id, title, description,
                  due_at, priority="normal", category_id=None, contact_person=None):
    from sqlalchemy import select
    from .models import TaskCategory
    if departments[actor.department_id].is_archived:
        raise PermissionError("Архивный отдел не создаёт новые поручения")
    recipient = departments.get(recipient_department_id)
    if recipient is None or recipient.is_archived:
        raise WorkflowError("Выберите действующий отдел-получатель", "recipient_department_id")
    if recipient.id == actor.department_id:
        raise WorkflowError("Нельзя отправить задачу в свой отдел", "recipient_department_id")
    title = title.strip() if isinstance(title, str) else ""
    description = description.strip() if isinstance(description, str) else ""
    contact_person = contact_person.strip() if isinstance(contact_person, str) else None
    if not title or len(title) > 200:
        raise WorkflowError("Название должно содержать от 1 до 200 символов", "title")
    if not description:
        raise WorkflowError("Описание обязательно", "description")
    if contact_person and len(contact_person) > 160:
        raise WorkflowError("Контактное лицо не должно превышать 160 символов", "contact_person")
    if priority not in TASK_PRIORITIES:
        raise WorkflowError("Неизвестный приоритет задачи", "priority")
    if due_at is None:
        raise WorkflowError("Срок выполнения обязателен", "due_at")
    due_at = _utc_deadline(due_at)
    category = None
    if category_id not in (None, ""):
        category_id = _integer(category_id, "Выберите действующую категорию")
        category = db.session.execute(select(TaskCategory).where(TaskCategory.id == category_id)
            .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
        if category is None or category.is_archived:
            raise WorkflowError("Выберите действующую категорию", "category_id")
    return dict(title=title, description=description, due_at=due_at, priority=priority,
        recipient_department_id=recipient.id, category_id=category.id if category else None,
        category=category.name if category else None, contact_person=contact_person or None)


def make_creation_key(actor):
    """64 hex characters: random nonce plus author-bound authentication tag."""
    nonce = token_hex(16)
    return nonce + _creation_tag(actor.id, nonce)


def _creation_tag(actor_id, nonce):
    secret = current_app.secret_key
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    if not secret:
        raise RuntimeError("SECRET_KEY обязателен для отправки задач")
    return hmac.new(secret, f"task-create:v1:{actor_id}:{nonce}".encode("ascii"), "sha256").hexdigest()[:32]


def _check_creation_key(key, actor):
    if (not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{64}", key) is None or
            not hmac.compare_digest(key[32:], _creation_tag(actor.id, key[:32]))):
        raise WorkflowError("Откройте новую форму: ключ отправки отсутствует или недействителен", "creation_key")
    return key


def _creation_payload(*, recipient_department_id, title, description, due_at, priority, category_id, contact_person):
    """Normalize request values before replay lookup, without directory snapshots."""
    recipient = _integer(recipient_department_id, "Выберите отдел-получатель")
    category = None if category_id in (None, "") else _integer(category_id, "Выберите действующую категорию")
    title = title.strip() if isinstance(title, str) else ""
    description = description.strip() if isinstance(description, str) else ""
    contact = contact_person.strip() if isinstance(contact_person, str) else None
    if not title or len(title) > 200:
        raise WorkflowError("Название должно содержать от 1 до 200 символов", "title")
    if not description:
        raise WorkflowError("Описание обязательно", "description")
    if contact and len(contact) > 160:
        raise WorkflowError("Контактное лицо не должно превышать 160 символов", "contact_person")
    if priority not in TASK_PRIORITIES:
        raise WorkflowError("Неизвестный приоритет задачи", "priority")
    if due_at is None:
        raise WorkflowError("Срок выполнения обязателен", "due_at")
    due_at = _utc_deadline(due_at)
    return dict(recipient_department_id=recipient, title=title, description=description,
        due_at=due_at, priority=priority, category_id=category, contact_person=contact or None)


def _creation_digest(values, prepared_files):
    payload = dict(values, due_at=values["due_at"].isoformat(timespec="microseconds"))
    payload["files"] = [dict(name=item.filename, size=item.size, sha256=item.content_sha256)
                        for item in prepared_files]
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def create_task(*, actor, recipient_department_id, title, description, due_at=None,
                priority="normal", category_id=None, contact_person=None, creation_key=None, prepared_files=()):
    """Atomically create and dispatch once; the caller owns commit/file cleanup."""
    from sqlalchemy import select
    from sqlalchemy.orm import undefer
    from .audit import record_task_event
    from .permissions import can_access_task
    prepared_files = tuple(prepared_files)
    values = _creation_payload(recipient_department_id=recipient_department_id, title=title,
        description=description, due_at=due_at, priority=priority, category_id=category_id, contact_person=contact_person)
    actor, _, departments = _locked_context(actor, recipient_department_id=values["recipient_department_id"])
    key = _check_creation_key(creation_key, actor)
    digest = _creation_digest(values, prepared_files)
    previous = db.session.execute(select(Task).where(Task.creation_key == key).options(undefer("*"))
        .execution_options(populate_existing=True)).scalar_one_or_none()
    if previous is not None:
        if previous.author_id != actor.id or not can_access_task(actor, previous):
            raise PermissionError("Отправка недоступна")
        event = TaskEvent.query.filter_by(task_id=previous.id, event_type="sent").order_by(TaskEvent.id).first()
        original = event.details.get("creation_digest") if event else None
        if not isinstance(original, str) or not hmac.compare_digest(original, digest):
            raise WorkflowConflict("Эта форма уже отправлена с другими данными. Откройте новую форму")
        return previous
    values = _task_fields(actor=actor, departments=departments, **values)
    now = datetime.now(timezone.utc)
    task = Task(**values, author_id=actor.id, sender_department_id=actor.department_id,
        status="new", revision=1, assignee_ids=[], sent_at=now, creation_key=key,
        is_rework=False, rework_cycle=0, legacy_missing_due=False, is_legacy_dispatched=False)
    db.session.add(task)
    db.session.flush()
    event = record_task_event(task=task, actor=actor, event_type="sent", comment=values["description"])
    event.details = {**event.details, "creation_digest": digest}
    _save_action_files(task, actor, event, prepared_files)
    deliver_event_notifications(task, event)
    return task


def _version(expected_revision, task):
    expected = _integer(expected_revision, "Обновите страницу: отсутствует корректная версия задачи", allow_zero=True)
    if task.revision != expected:
        raise WorkflowConflict("Задача уже изменена другим сотрудником. Обновите страницу")
    return expected


def _apply_task_change(*, task, actor, expected_revision, values, comment, event_type, prepared_files=(), assignee_ids=None):
    from .audit import record_task_event, task_snapshot
    before = task_snapshot(task)
    old_status = task.status
    if assignee_ids is not None and values.get("status") == "new":
        task.assignee_ids = assignee_ids
        db.session.flush()
    result = db.session.execute(update(Task).where(Task.id == task.id,
        Task.revision == expected_revision, Task.status == old_status)
        .values(**values, revision=expected_revision + 1, updated_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise WorkflowConflict("Задача уже изменена другим сотрудником. Обновите страницу")
    if assignee_ids is not None and values.get("status") != "new":
        # Update members only after the parent status/CAS succeeded.
        task.assignee_ids = assignee_ids
        db.session.flush()
    db.session.refresh(task)
    event = record_task_event(task=task, actor=actor, event_type=event_type, comment=comment, before=before)
    _save_action_files(task, actor, event, prepared_files)
    deliver_event_notifications(task, event)
    return task


@_removed_workflow
def update_draft(*, task, actor, expected_revision, comment, recipient_department_id, prepared_files=(), **fields):
    from .permissions import can_edit_draft
    recipient_id = _integer(recipient_department_id, "Выберите отдел-получатель")
    actor, task, departments = _locked_context(actor, task, recipient_id)
    if not can_edit_draft(actor, task):
        raise PermissionError("Редактировать можно только собственный черновик действующего отдела")
    expected = _version(expected_revision, task)
    comment = _required_comment(comment, "Поясните изменения черновика")
    values = _task_fields(actor=actor, departments=departments, recipient_department_id=recipient_id, **fields)
    return _apply_task_change(task=task, actor=actor, expected_revision=expected,
        values=values, comment=comment, event_type="edited", prepared_files=prepared_files)


@_removed_workflow
def update_task_before_acceptance(*, task, actor, expected_revision=None, comment=None, **fields):
    """Compatibility entry point has exactly the new draft rules; no sent editing."""
    return update_draft(task=task, actor=actor, expected_revision=expected_revision, comment=comment, **fields)


@_removed_workflow
def transition_approval(*, task, actor, action, expected_revision, comment, prepared_files=()):
    from .permissions import (can_submit_for_approval, can_withdraw_approval,
        can_return_approval, can_approve_task, can_send_task)
    transitions = {
        "submit": (can_submit_for_approval, "pending_approval", "submitted_for_approval"),
        "withdraw": (can_withdraw_approval, "draft", "approval_withdrawn"),
        "return_draft": (can_return_approval, "draft", "approval_returned"),
        "approve": (can_approve_task, "awaiting_assignment", "approved_and_sent"),
        "send": (can_send_task, "awaiting_assignment", "sent"),
    }
    if action not in transitions:
        raise WorkflowError("Неизвестное действие согласования")
    actor, task, departments = _locked_context(actor, task)
    expected = _version(expected_revision, task)
    predicate, status, event_type = transitions[action]
    if not predicate(actor, task):
        raise PermissionError("Действие недоступно для вашей роли или текущего состояния задачи")
    comment = _required_comment(comment, "Обязателен комментарий к действию")
    values = {"status": status}
    if status == "awaiting_assignment":
        # Locked departments, not the page's earlier recipient selection.
        if any(departments[dep].is_archived for dep in (task.sender_department_id, task.recipient_department_id)):
            raise PermissionError("Нельзя отправить задачу архивному отделу или от его имени")
        if task.due_at is None:
            raise WorkflowError("Срок выполнения обязателен", "due_at")
        values["sent_at"] = datetime.now(timezone.utc)
    return _apply_task_change(task=task, actor=actor, expected_revision=expected,
        values=values, comment=comment, event_type=event_type, prepared_files=prepared_files)


def validate_status(status):
    if status not in TASK_STATUSES:
        raise WorkflowError("Неизвестный статус задачи")
    return status


WORKING_TRANSITIONS = {
    "assigned": ("in_progress",),
    "in_progress": ("awaiting_clarification",),
}


def _update_task_atomically(task, values):
    """Change the row only if the status and assignment still match this snapshot.

    The update and audit event belong to the caller's transaction; no service commits.
    """
    result = db.session.execute(
        update(Task)
        .where(
            Task.id == task.id,
            Task.status == task.status,
            Task.revision == task.revision,
            Task.recipient_department_id == task.recipient_department_id,
            Task.sender_department_id == task.sender_department_id,
            Task.due_at == task.due_at if task.due_at is not None else Task.due_at.is_(None),
        )
        .values(**values, updated_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise WorkflowError("Задача уже изменена другим сотрудником. Обновите страницу")
    db.session.refresh(task)


def _assignee_list(values):
    if not isinstance(values, (list, tuple)) or not values:
        raise WorkflowError("Выберите одного или нескольких исполнителей", "assignee_ids")
    try:
        ids = [_integer(value, "Выберите действующих участников отдела-получателя") for value in values]
    except WorkflowError as error:
        raise WorkflowError(str(error), "assignee_ids") from None
    if len(set(ids)) != len(ids):
        raise WorkflowError("Один участник указан несколько раз", "assignee_ids")
    return sorted(ids)


def transition_execution(*, task, actor, action, expected_revision, comment, assignee_ids=None, prepared_files=()):
    """Atomically update one team; the caller owns commit and file cleanup."""
    from .permissions import can_assign_task, can_claim_task, can_release_task, is_recipient_head
    transitions = {
        "assign": (can_assign_task, ("new",), "assigned"),
        "claim": (can_claim_task, ("new", "in_progress"), "claimed"),
        "reassign": (can_reassign_task, ("in_progress",), "reassigned"),
        "release": (can_release_task, ("in_progress",), "released"),
    }
    if action not in transitions:
        raise WorkflowError("Неизвестное рабочее действие")
    assignment = action in ("assign", "reassign")
    proposed = _assignee_list(assignee_ids) if assignment else None
    if not assignment and assignee_ids is not None:
        raise WorkflowError("Нельзя подменять состав исполнителей", "assignee_ids")
    actor, task, _ = _locked_context(actor, task, user_ids=proposed or ())
    expected = _version(expected_revision, task)
    if not is_recipient_employee(actor, task) or assignment and not is_recipient_head(actor, task):
        raise PermissionError("Действие доступно только соответствующим участникам отдела-получателя")
    predicate, sources, event_type = transitions[action]
    current = task.assignee_ids
    if task.status not in sources or action == "claim" and actor.id in current:
        raise WorkflowConflict("Состав или состояние задачи уже изменились. Обновите страницу")
    if not predicate(actor, task):
        raise PermissionError("Действие недоступно для вашей роли или состояния задачи")
    comment = _required_comment(comment, "Обязателен комментарий к действию")
    if assignment:
        if any(not is_recipient_employee(db.session.get(User, ident), task) for ident in proposed):
            raise WorkflowError("Выберите действующих участников отдела-получателя", "assignee_ids")
        if proposed == current:
            raise WorkflowError("Этот состав уже назначен", "assignee_ids")
    elif action == "claim":
        proposed = sorted([*current, actor.id])
    else:
        proposed = [ident for ident in current if ident != actor.id]
    return _apply_task_change(task=task, actor=actor, expected_revision=expected,
        values={"status": "in_progress" if proposed else "new"}, assignee_ids=proposed,
        comment=comment, event_type=event_type, prepared_files=prepared_files)


def assign_task(*, task, actor, assignee_ids, expected_revision=None, comment=None, prepared_files=()):
    return transition_execution(task=task, actor=actor, action="assign", assignee_ids=assignee_ids,
        expected_revision=expected_revision, comment=comment, prepared_files=prepared_files)


def claim_task(*, task, actor, expected_revision=None, comment=None, prepared_files=()):
    return transition_execution(task=task, actor=actor, action="claim",
        expected_revision=expected_revision, comment=comment, prepared_files=prepared_files)


def reassign_task(*, task, actor, assignee_ids, expected_revision=None, comment=None, prepared_files=()):
    return transition_execution(task=task, actor=actor, action="reassign", assignee_ids=assignee_ids,
        expected_revision=expected_revision, comment=comment, prepared_files=prepared_files)


def release_task(*, task, actor, expected_revision=None, comment=None, prepared_files=()):
    return transition_execution(task=task, actor=actor, action="release",
        expected_revision=expected_revision, comment=comment, prepared_files=prepared_files)


@_removed_workflow
def change_working_status(*, task, actor, status, expected_revision=None, comment=None):
    # Compatibility endpoint maps only the two explicitly permitted actions.
    actions = {"in_progress": "start", "awaiting_clarification": "request_clarification"}
    if status not in actions:
        raise WorkflowError("Недопустимый переход рабочего статуса")
    return transition_execution(task=task, actor=actor, action=actions[status],
        expected_revision=expected_revision, comment=comment)


@_removed_workflow
def request_task_clarification(*, task, actor, expected_revision=None, comment=None):
    return transition_execution(task=task, actor=actor, action="request_clarification",
        expected_revision=expected_revision, comment=comment)


def _required_comment(text, label):
    text = text.strip() if isinstance(text, str) else ""
    if not text:
        raise WorkflowError(label)
    return text


def transition_completion(*, task, actor, action, expected_revision, comment, prepared_files=()):
    """Close/reopen one revision; old acceptance/review stages stay removed."""
    from .permissions import (can_complete_task, can_return_result, can_cancel_task, can_reject_task,
        is_recipient_head, is_sender_head, has_available_assignee)
    transitions = {
        "complete": (can_complete_task, ("in_progress",), "completed", "completed"),
        "return": (can_return_result, ("completed",), "in_progress", "returned_for_revision"),
        "cancel": (can_cancel_task, ("new", "in_progress"), "cancelled", "cancelled"),
        "reject": (can_reject_task, ("new", "in_progress"), "rejected", "rejected"),
    }
    if action not in transitions:
        raise WorkflowError("Неизвестное действие завершения задачи")
    actor, task, _ = _locked_context(actor, task)
    expected = _version(expected_revision, task)
    if action in ("complete", "reject"):
        allowed = is_recipient_employee(actor, task) and (is_recipient_head(actor, task) or actor.id in task.assignee_ids)
    else:
        allowed = is_sender_employee(actor, task) and (is_sender_head(actor, task) or actor.id == task.author_id)
    if not allowed:
        raise PermissionError("Действие недоступно для вашей стороны и роли")
    predicate, sources, target, event_type = transitions[action]
    if task.status not in sources:
        raise WorkflowConflict("Состояние задачи уже изменилось. Обновите страницу")
    if action == "complete" and not has_available_assignee(task):
        raise WorkflowConflict("Нужно назначить действующего исполнителя перед выполнением")
    if not predicate(actor, task):
        raise PermissionError("Действие недоступно для текущего состояния задачи")
    comment = _required_comment(comment, "Обязателен результат, замечание или причина действия")
    values = {"status": target}
    roster = None
    if action == "return":
        roster = [user.id for user in task.assignees if is_recipient_employee(user, task)]
        values.update(completed_at=None, closed_at=None, is_rework=True, rework_cycle=task.rework_cycle + 1)
    else:
        now = datetime.now(timezone.utc)
        values.update(closed_at=now, completed_at=now if action == "complete" else None, is_rework=False)
    return _apply_task_change(task=task, actor=actor, expected_revision=expected,
        values=values, comment=comment, event_type=event_type, prepared_files=prepared_files, assignee_ids=roster)


@_removed_workflow
def transition_review(*args, **kwargs):
    """The retired multi-stage review service is never an alias for completion."""


def complete_task(*, task, actor, comment, expected_revision=None, prepared_files=()):
    return transition_completion(task=task, actor=actor, action="complete",
        expected_revision=expected_revision, comment=comment, prepared_files=prepared_files)


@_removed_workflow
def submit_task_result(*args, **kwargs):
    """Use complete_task, not the old internal-review submission."""


@_removed_workflow
def forward_task_result(*args, **kwargs):
    """No recipient-to-sender acceptance stage remains."""


@_removed_workflow
def confirm_task_result(*args, **kwargs):
    """The sender no longer confirms completion."""


def return_task_for_revision(*, task, actor, comment, expected_revision=None, prepared_files=()):
    return transition_completion(task=task, actor=actor, action="return",
        expected_revision=expected_revision, comment=comment, prepared_files=prepared_files)


@_removed_workflow
def answer_task_clarification(*args, **kwargs):
    """Clarifications are ordinary discussion, without a separate status."""


def cancel_task(*, task, actor, reason=None, comment=None, expected_revision=None, prepared_files=()):
    if reason is not None and comment is not None:
        raise WorkflowError("Укажите одно пояснение отмены")
    return transition_completion(task=task, actor=actor, action="cancel",
        expected_revision=expected_revision, comment=comment if comment is not None else reason, prepared_files=prepared_files)


def reject_task(*, task, actor, comment, expected_revision=None, prepared_files=()):
    return transition_completion(task=task, actor=actor, action="reject",
        expected_revision=expected_revision, comment=comment, prepared_files=prepared_files)


def _utc_deadline(value):
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise WorkflowError("Укажите корректный срок")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def change_task_deadline(*, task, actor, due_at, comment=None, expected_revision=None, prepared_files=()):
    from .permissions import is_sender_head, ACTIVE
    actor, task, _ = _locked_context(actor, task)
    expected = _version(expected_revision, task)
    if not is_sender_head(actor, task):
        raise PermissionError("Срок меняет только начальник отдела-отправителя")
    if task.status not in ACTIVE:
        raise WorkflowConflict("Срок закрытой задачи менять нельзя. Обновите страницу")
    if due_at is None:
        raise WorkflowError("Срок выполнения обязателен", "due_at")
    due_at = _utc_deadline(due_at)
    if due_at == _utc_deadline(task.due_at):
        raise WorkflowError("Укажите новый срок", "due_at")
    comment = _required_comment(comment, "Объясните изменение срока")
    return _apply_task_change(task=task, actor=actor, expected_revision=expected,
        values={"due_at": due_at}, comment=comment, event_type="deadline_changed", prepared_files=prepared_files)


def add_task_comment(*, task, actor, comment, expected_revision=None, prepared_files=()):
    """One immutable explanation, optional files and event; no working-field edits."""
    actor, task, _ = _locked_context(actor, task)
    expected = _version(expected_revision, task)
    if not can_contribute_task(actor, task):
        raise PermissionError("Комментарии доступны действующим участникам незавершённой задачи")
    comment = _required_comment(comment, "Введите текст комментария")
    _apply_task_change(task=task, actor=actor, expected_revision=expected,
        values={}, comment=comment, event_type="comment_added", prepared_files=prepared_files)
    return TaskComment.query.filter_by(task_id=task.id).order_by(TaskComment.id.desc()).first()


def add_task_attachment(*, task, actor, comment=None, expected_revision=None, prepared_files=(), metadata=None):
    if metadata is not None:
        raise WorkflowError("Файл добавляется через новый комментарий, а не клиентские метаданные")
    if not prepared_files:
        raise WorkflowError("Выберите файл")
    return add_task_comment(task=task, actor=actor, comment=comment,
        expected_revision=expected_revision, prepared_files=prepared_files)


def _save_action_files(task, actor, event, prepared_files):
    if not prepared_files:
        return
    comment = db.session.get(TaskComment, event.comment_id)
    if (comment is None or comment.task_id != task.id or comment.author_id != actor.id
            or event.task_id != task.id or event.actor_id != actor.id):
        raise WorkflowError("Файл должен принадлежать комментарию текущего действия")
    attachments = []
    for prepared in prepared_files:
        metadata = prepared.install()
        entry = TaskAttachment(task_id=task.id, uploaded_by_id=actor.id, comment_id=comment.id, **metadata)
        db.session.add(entry)
        db.session.flush()
        attachments.append({"id": entry.id, "filename": entry.original_filename, "size_bytes": entry.size_bytes})
    event.details = {**event.details, "attachments": attachments}
    db.session.flush()
