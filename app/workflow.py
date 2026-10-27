"""Task creation and shared workflow validation primitives."""

from datetime import datetime, timezone

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


def _locked_context(actor, task=None, recipient_department_id=None, *, user_ids=(), lock_settings=False):
    """Lock in user -> departments by ID -> task order; then re-read authority.

    The preliminary task read only discovers lock targets. A concurrent draft edit
    changing them causes a conflict, never a second lock acquired out of order.
    """
    from sqlalchemy import select, or_
    from sqlalchemy.orm import undefer
    from .models import Department
    from .permissions import is_platform_user, can_access_task
    if actor is None or getattr(actor, "id", None) is None:
        raise PermissionError("Требуется действующий сотрудник отдела")
    with db.session.no_autoflush:
        preliminary = None
        previous_assignee_id = None
        if task is not None:
            preliminary = db.session.execute(select(Task).where(Task.id == task.id)
                .options(undefer("*")).execution_options(populate_existing=True)).scalar_one_or_none()
            if preliminary is None:
                raise LookupError("Задача не найдена")
            targets = {preliminary.sender_department_id, preliminary.recipient_department_id}
            previous_assignee_id = preliminary.assignee_id
        else:
            targets = set()
        # Include the old and proposed assignee before any department/task lock.
        # This serializes assignments with account blocking, role and department changes.
        account_ids = {actor.id, *user_ids}
        if previous_assignee_id is not None:
            account_ids.add(previous_assignee_id)
        # FOR NO KEY UPDATE serializes role edits while allowing notification FK KEY SHARE.
        accounts = db.session.execute(select(User).where(or_(User.id.in_(account_ids), User.role == "admin") if lock_settings else User.id.in_(account_ids)).order_by(User.id)
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
            if task.assignee_id != previous_assignee_id:
                raise WorkflowConflict("Исполнитель уже изменён. Обновите страницу")
            if not can_access_task(actor, task):
                raise PermissionError("Задача недоступна")
        return actor, task, departments


def _draft_fields(*, actor, departments, recipient_department_id, title, description,
                  due_at, priority="normal", category_id=None, contact_person=None):
    from sqlalchemy import select
    from .models import TaskCategory
    if departments[actor.department_id].is_archived:
        raise PermissionError("Архивный отдел не создаёт и не изменяет черновики")
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


def create_task(*, actor, recipient_department_id, title, description, due_at=None,
                priority="normal", category_id=None, contact_person=None, prepared_files=()):
    """Saving always creates a private draft. Description is its first comment."""
    from .audit import record_task_event
    recipient_id = _integer(recipient_department_id, "Выберите отдел-получатель")
    actor, _, departments = _locked_context(actor, recipient_department_id=recipient_id, lock_settings=bool(prepared_files))
    values = _draft_fields(actor=actor, departments=departments, recipient_department_id=recipient_id,
        title=title, description=description, due_at=due_at, priority=priority,
        category_id=category_id, contact_person=contact_person)
    task = Task(**values, author_id=actor.id, sender_department_id=actor.department_id,
        status="draft", revision=1, assignee_id=None, sent_at=None,
        legacy_missing_due=False, is_legacy_dispatched=False)
    db.session.add(task)
    db.session.flush()
    event = record_task_event(task=task, actor=actor, event_type="created", comment=values["description"])
    _save_action_files(task, actor, event, prepared_files)
    deliver_event_notifications(task, event)
    return task


def _version(expected_revision, task):
    expected = _integer(expected_revision, "Обновите страницу: отсутствует корректная версия задачи", allow_zero=True)
    if task.revision != expected:
        raise WorkflowConflict("Задача уже изменена другим сотрудником. Обновите страницу")
    return expected


def _apply_task_change(*, task, actor, expected_revision, values, comment, event_type, prepared_files=()):
    from .audit import record_task_event, task_snapshot
    before = task_snapshot(task)
    old_status = task.status
    result = db.session.execute(update(Task).where(Task.id == task.id,
        Task.revision == expected_revision, Task.status == old_status)
        .values(**values, revision=expected_revision + 1, updated_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise WorkflowConflict("Задача уже изменена другим сотрудником. Обновите страницу")
    db.session.refresh(task)
    event = record_task_event(task=task, actor=actor, event_type=event_type, comment=comment, before=before)
    _save_action_files(task, actor, event, prepared_files)
    deliver_event_notifications(task, event)
    return task


def update_draft(*, task, actor, expected_revision, comment, recipient_department_id, prepared_files=(), **fields):
    from .permissions import can_edit_draft
    recipient_id = _integer(recipient_department_id, "Выберите отдел-получатель")
    actor, task, departments = _locked_context(actor, task, recipient_id, lock_settings=bool(prepared_files))
    if not can_edit_draft(actor, task):
        raise PermissionError("Редактировать можно только собственный черновик действующего отдела")
    expected = _version(expected_revision, task)
    comment = _required_comment(comment, "Поясните изменения черновика")
    values = _draft_fields(actor=actor, departments=departments, recipient_department_id=recipient_id, **fields)
    return _apply_task_change(task=task, actor=actor, expected_revision=expected,
        values=values, comment=comment, event_type="edited", prepared_files=prepared_files)


def update_task_before_acceptance(*, task, actor, expected_revision=None, comment=None, **fields):
    """Compatibility entry point has exactly the new draft rules; no sent editing."""
    return update_draft(task=task, actor=actor, expected_revision=expected_revision, comment=comment, **fields)


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
    actor, task, departments = _locked_context(actor, task, lock_settings=bool(prepared_files))
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
            Task.assignee_id == task.assignee_id if task.assignee_id is not None else Task.assignee_id.is_(None),
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


def transition_execution(*, task, actor, action, expected_revision, comment, assignee_id=None, prepared_files=()):
    """Only explicit execution actions; every successful call owns one revision."""
    from .permissions import (can_assign_task, can_start_task, can_request_clarification,
                              can_answer_clarification)
    transitions = {
        "assign": (can_assign_task, "assigned", "assigned"),
        "reassign": (can_reassign_task, None, "reassigned"),
        "start": (can_start_task, "in_progress", "work_started"),
        "request_clarification": (can_request_clarification, "awaiting_clarification", "clarification_requested"),
        "answer_clarification": (can_answer_clarification, "in_progress", "clarification_answered"),
    }
    if action not in transitions:
        raise WorkflowError("Неизвестное рабочее действие")
    assignment = action in ("assign", "reassign")
    candidate_id = _integer(assignee_id, "Выберите одного действующего участника отдела-получателя") if assignment else None
    if not assignment and assignee_id is not None:
        raise WorkflowError("Исполнитель меняется только отдельным назначением")
    actor, task, _ = _locked_context(actor, task, user_ids=(candidate_id,) if assignment else (), lock_settings=bool(prepared_files))
    expected = _version(expected_revision, task)
    predicate, status, event_type = transitions[action]
    if not predicate(actor, task):
        raise PermissionError("Действие недоступно для вашей роли или текущего состояния задачи")
    comment = _required_comment(comment, "Обязателен комментарий к действию")
    values = {"status": status} if status is not None else {}
    if assignment:
        candidate = db.session.get(User, candidate_id)
        if not is_recipient_employee(candidate, task):
            raise WorkflowError("Выберите действующего сотрудника или начальника отдела-получателя", "assignee_id")
        if action == "reassign" and candidate.id == task.assignee_id:
            raise WorkflowError("Этот участник уже назначен исполнителем", "assignee_id")
        values["assignee_id"] = candidate.id
    return _apply_task_change(task=task, actor=actor, expected_revision=expected,
        values=values, comment=comment, event_type=event_type, prepared_files=prepared_files)


def assign_task(*, task, actor, assignee_id, expected_revision=None, comment=None):
    return transition_execution(task=task, actor=actor, action="assign",
        assignee_id=assignee_id, expected_revision=expected_revision, comment=comment)


def claim_task(*, task, actor):
    raise PermissionError("Самоназначение отключено. Исполнителя назначает начальник отдела-получателя")


def reassign_task(*, task, actor, assignee_id, expected_revision=None, comment=None):
    return transition_execution(task=task, actor=actor, action="reassign",
        assignee_id=assignee_id, expected_revision=expected_revision, comment=comment)


def change_working_status(*, task, actor, status, expected_revision=None, comment=None):
    # Compatibility endpoint maps only the two explicitly permitted actions.
    actions = {"in_progress": "start", "awaiting_clarification": "request_clarification"}
    if status not in actions:
        raise WorkflowError("Недопустимый переход рабочего статуса")
    return transition_execution(task=task, actor=actor, action=actions[status],
        expected_revision=expected_revision, comment=comment)


def request_task_clarification(*, task, actor, expected_revision=None, comment=None):
    return transition_execution(task=task, actor=actor, action="request_clarification",
        expected_revision=expected_revision, comment=comment)


def _required_comment(text, label):
    text = text.strip() if isinstance(text, str) else ""
    if not text:
        raise WorkflowError(label)
    return text


def transition_review(*, task, actor, action, expected_revision, comment, due_at=None, prepared_files=()):
    """Review, closing and deadline actions share the same locked transaction."""
    from .permissions import (can_submit_result, can_review_recipient_result, can_review_result,
        can_return_result, can_cancel_task, can_reject_task, is_task_dispatched)
    transitions = {
        "submit_result": (can_submit_result, "recipient_review", "result_submitted"),
        "forward_result": (can_review_recipient_result, "sender_acceptance", "result_forwarded"),
        "return_result": (can_return_result, "in_progress", "returned_for_revision"),
        "complete": (can_review_result, "completed", "completed"),
        "cancel": (can_cancel_task, "cancelled", "cancelled"),
        "reject": (can_reject_task, "rejected", "rejected"),
        "deadline": (can_change_deadline, None, "deadline_changed"),
    }
    if action not in transitions:
        raise WorkflowError("Неизвестное действие проверки результата")
    actor, task, _ = _locked_context(actor, task, lock_settings=bool(prepared_files))
    expected = _version(expected_revision, task)
    predicate, status, event_type = transitions[action]
    if not predicate(actor, task):
        raise PermissionError("Действие недоступно для вашей роли или текущего состояния задачи")
    comment = _required_comment(comment, "Обязателен комментарий к действию")
    values = {"status": status} if status else {}
    if action == "deadline":
        if not is_task_dispatched(task):
            raise PermissionError("До отправки срок изменяется автором в форме черновика")
        deadline = _utc_deadline(due_at)
        if deadline is None:
            raise WorkflowError("Срок обязателен: снять его нельзя", "due_at")
        if deadline == _utc_deadline(task.due_at):
            raise WorkflowError("Новый срок должен отличаться от текущего", "due_at")
        values.update(due_at=deadline, legacy_missing_due=False)
    elif due_at is not None:
        raise WorkflowError("Срок изменяется только отдельным действием")
    if status in ("completed", "cancelled", "rejected"):
        now = datetime.now(timezone.utc)
        values.update(closed_at=now, completed_at=now if status == "completed" else None)
    return _apply_task_change(task=task, actor=actor, expected_revision=expected,
        values=values, comment=comment, event_type=event_type, prepared_files=prepared_files)


def submit_task_result(*, task, actor, comment, expected_revision=None):
    return transition_review(task=task, actor=actor, action="submit_result",
        expected_revision=expected_revision, comment=comment)


def forward_task_result(*, task, actor, comment, expected_revision=None):
    return transition_review(task=task, actor=actor, action="forward_result",
        expected_revision=expected_revision, comment=comment)


def confirm_task_result(*, task, actor, comment=None, expected_revision=None):
    return transition_review(task=task, actor=actor, action="complete",
        expected_revision=expected_revision, comment=comment)


def return_task_for_revision(*, task, actor, comment, expected_revision=None):
    return transition_review(task=task, actor=actor, action="return_result",
        expected_revision=expected_revision, comment=comment)


def answer_task_clarification(*, task, actor, comment, expected_revision=None):
    return transition_execution(task=task, actor=actor, action="answer_clarification",
        expected_revision=expected_revision, comment=comment)


def cancel_task(*, task, actor, reason=None, comment=None, expected_revision=None):
    if reason is not None and comment is not None:
        raise WorkflowError("Укажите одно пояснение отмены")
    return transition_review(task=task, actor=actor, action="cancel",
        expected_revision=expected_revision, comment=comment if comment is not None else reason)


def reject_task(*, task, actor, comment, expected_revision=None):
    return transition_review(task=task, actor=actor, action="reject",
        expected_revision=expected_revision, comment=comment)


def _utc_deadline(value):
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise WorkflowError("Укажите корректный срок")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def change_task_deadline(*, task, actor, due_at, comment=None, expected_revision=None):
    return transition_review(task=task, actor=actor, action="deadline",due_at=due_at,
        expected_revision=expected_revision, comment=comment)


def add_task_comment(*, task, actor, comment, expected_revision=None, prepared_files=()):
    """Ordinary discussion never answers clarification or changes its status.

    HTTP discussion and attached comment files will be enabled together in T009.
    """
    actor, task, _ = _locked_context(actor, task, lock_settings=bool(prepared_files))
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
