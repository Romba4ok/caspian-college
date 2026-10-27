"""One policy for task queries, resource reads and workflow capabilities."""
from flask import abort
from sqlalchemy import and_, or_, true, false

from .models import Task, TaskAttachment, TaskComment, TaskEvent, TaskNotification

PRE_DISPATCH = ("draft", "pending_approval")
TERMINAL = ("completed", "cancelled", "rejected")


def is_platform_user(user):
    return bool(user is not None and getattr(user, "is_active", False) and
        (getattr(user, "role", None) == "admin" or
         getattr(user, "role", None) in ("head", "employee") and getattr(user, "department_id", None) is not None))


def is_department_member(user):
    return is_platform_user(user) and user.role in ("head", "employee")


def dispatched_clause():
    return and_(~Task.status.in_(PRE_DISPATCH), or_(Task.sent_at.is_not(None), Task.is_legacy_dispatched.is_(True)))


def is_task_dispatched(task):
    return bool(task is not None and task.status not in PRE_DISPATCH and
                (task.sent_at is not None or task.is_legacy_dispatched))


def task_visibility_clause(user):
    if not is_platform_user(user):
        return false()
    if user.role == "admin":
        return true()
    return or_(Task.sender_department_id == user.department_id,
               and_(Task.recipient_department_id == user.department_id, dispatched_clause()))


def can_access_task(user, task):
    if task is None or not is_platform_user(user):
        return False
    return (user.role == "admin" or user.department_id == task.sender_department_id or
            user.department_id == task.recipient_department_id and is_task_dispatched(task))


def require_task_access(user, task):
    if not can_access_task(user, task):
        abort(404)
    return task


def is_sender_employee(user, task):
    return bool(task is not None and is_department_member(user) and user.department_id == task.sender_department_id)


def is_recipient_employee(user, task):
    return bool(task is not None and is_department_member(user) and user.department_id == task.recipient_department_id
                and is_task_dispatched(task))


def is_sender_head(user, task):
    return is_sender_employee(user, task) and user.role == "head"


def is_recipient_head(user, task):
    return is_recipient_employee(user, task) and user.role == "head"


def _active_department(user):
    return is_department_member(user) and user.department is not None and not user.department.is_archived


def can_create_task(user):
    return _active_department(user)


def can_edit_draft(user, task):
    return (is_sender_employee(user, task) and user.id == task.author_id and task.status == "draft"
            and not is_task_dispatched(task) and _active_department(user))


def _send_destination_valid(task):
    return (task.recipient_department is not None and not task.recipient_department.is_archived
            and task.sender_department_id != task.recipient_department_id and task.due_at is not None)


def can_submit_for_approval(user, task):
    return can_edit_draft(user, task) and user.role == "employee" and _send_destination_valid(task)


def can_withdraw_approval(user, task):
    return (is_sender_employee(user, task) and task.author_id == user.id and task.status == "pending_approval"
            and _active_department(user))


def can_return_approval(user, task):
    return is_sender_head(user, task) and task.status == "pending_approval" and _active_department(user)


def can_approve_task(user, task):
    return can_return_approval(user, task) and _send_destination_valid(task)


def can_send_task(user, task):
    return is_sender_head(user, task) and _active_department(user) and _send_destination_valid(task) and (
        task.status == "draft" and task.author_id == user.id)


def can_claim_task(user, task):
    # Self-assignment was explicitly removed; the legacy URL cannot grant it.
    return False


def can_assign_task(user, task):
    return (is_recipient_head(user, task) and task.status == "awaiting_assignment"
            and task.assignee_id is None and task.due_at is not None)


def can_reassign_task(user, task):
    return (is_recipient_head(user, task) and task.status in
            ("assigned", "in_progress", "awaiting_clarification", "recipient_review") and task.due_at is not None)


def has_available_assignee(task):
    assignee = task.assignee if task is not None else None
    return bool(assignee is not None and is_department_member(assignee)
                and assignee.department_id == task.recipient_department_id)


def _working_actor(user, task):
    return (is_recipient_employee(user, task) and has_available_assignee(task) and
            (user.id == task.assignee_id or user.role == "head") and task.due_at is not None)


def can_change_working_status(user, task):
    return _working_actor(user, task) and task.status in ("assigned", "in_progress")


def can_start_task(user, task):
    return can_change_working_status(user, task) and task.status == "assigned"


def can_submit_result(user, task):
    return _working_actor(user, task) and task.status == "in_progress"


def can_request_clarification(user, task):
    return can_submit_result(user, task)


def can_review_recipient_result(user, task):
    return (is_recipient_head(user, task) and task.status == "recipient_review" and
            task.assignee_id is not None and task.due_at is not None)


def can_review_result(user, task):
    return is_sender_head(user, task) and task.status == "sender_acceptance" and task.due_at is not None


def can_return_result(user, task):
    # Historical work can lose its assignee. It must still be possible to return
    # it for reassignment, without forwarding an unassigned result.
    return can_review_result(user, task) or (is_recipient_head(user, task) and
        task.status == "recipient_review" and task.due_at is not None)


def can_answer_clarification(user, task):
    return (is_sender_employee(user, task) and is_task_dispatched(task) and
            task.status == "awaiting_clarification" and task.due_at is not None)


def can_cancel_task(user, task):
    if not is_sender_employee(user, task) or task.status in TERMINAL:
        return False
    if is_task_dispatched(task):
        return user.role == "head"
    return task.status in PRE_DISPATCH and (user.id == task.author_id or user.role == "head")


def can_reject_task(user, task):
    return (is_recipient_head(user, task) and task.status in ("awaiting_assignment", "assigned")
            and task.due_at is not None)


def can_change_deadline(user, task):
    if not is_sender_employee(user, task) or task.status in TERMINAL:
        return False
    return user.role == "head" if is_task_dispatched(task) else can_edit_draft(user, task)


def can_contribute_task(user, task):
    if not is_department_member(user) or not can_access_task(user, task) or task.status in TERMINAL:
        return False
    return is_task_dispatched(task) or task.status in PRE_DISPATCH and _active_department(user)


def get_visible_task_or_404(task_id, user):
    return require_task_access(user, db_get(Task, task_id))


def get_visible_comment_or_404(comment_id, user):
    comment = db_get(TaskComment, comment_id)
    require_task_access(user, comment.task)
    return comment


def get_visible_attachment_or_404(attachment_id, user):
    attachment = db_get(TaskAttachment, attachment_id)
    require_task_access(user, attachment.task)
    if attachment.comment_id is not None and attachment.comment is None:
        abort(404)
    return attachment


def get_visible_event_or_404(event_id, user):
    event = db_get(TaskEvent, event_id)
    require_task_access(user, event.task)
    if event.comment_id is not None and event.comment is None:
        abort(404)
    return event


def get_visible_notification_or_404(notification_id, user):
    notification = db_get(TaskNotification, notification_id)
    if not is_platform_user(user) or notification.user_id != user.id:
        abort(404)
    if notification.task_id is not None:
        require_task_access(user, notification.task)
    return notification


def db_get(model, object_id):
    from .extensions import db
    try:
        if isinstance(object_id, bool):
            abort(404)
        object_id = int(object_id)
        if not 0 < object_id <= 2**31 - 1:
            abort(404)
        value = db.session.get(model, object_id, populate_existing=True)
    except (TypeError, ValueError, OverflowError):
        value = None
    if value is None:
        abort(404)
    return value
