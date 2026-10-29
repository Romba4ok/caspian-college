"""Shared role/department policy for the simplified five-status workflow."""
from flask import abort
from sqlalchemy import and_, or_, true, false

from .models import Task, TaskAttachment, TaskComment, TaskEvent, TaskNotification

# Historical labels only; no active transition may enter these states.
PRE_DISPATCH = ("draft", "pending_approval")
TERMINAL = ("completed", "cancelled", "rejected")
ACTIVE = ("new", "in_progress")


def is_platform_user(user):
    return bool(user is not None and getattr(user, "is_active", False) and
        (getattr(user, "role", None) == "admin" or
         getattr(user, "role", None) in ("head", "employee") and getattr(user, "department_id", None) is not None))


def is_department_member(user):
    return is_platform_user(user) and user.role in ("head", "employee")


def dispatched_clause():
    # Legacy flags alone never grant recipient access in the clean-start profile.
    return and_(~Task.status.in_(PRE_DISPATCH), Task.sent_at.is_not(None))


def is_task_dispatched(task):
    return bool(task is not None and task.status not in PRE_DISPATCH and task.sent_at is not None)


def task_visibility_clause(user):
    if not is_platform_user(user):
        return false()
    if user.role == "admin":
        return true()
    return and_(dispatched_clause(), or_(Task.sender_department_id == user.department_id,
                                        Task.recipient_department_id == user.department_id))


def can_access_task(user, task):
    if task is None or not is_platform_user(user):
        return False
    return user.role == "admin" or (is_task_dispatched(task) and
        user.department_id in (task.sender_department_id, task.recipient_department_id))


def require_task_access(user, task):
    if not can_access_task(user, task):
        abort(404)
    return task


def is_sender_employee(user, task):
    return bool(is_department_member(user) and is_task_dispatched(task)
                and user.department_id == task.sender_department_id)


def is_recipient_employee(user, task):
    return bool(is_department_member(user) and is_task_dispatched(task)
                and user.department_id == task.recipient_department_id)


def is_sender_head(user, task):
    return is_sender_employee(user, task) and user.role == "head"


def is_recipient_head(user, task):
    return is_recipient_employee(user, task) and user.role == "head"


def _active_department(user):
    return is_department_member(user) and user.department is not None and not user.department.is_archived


def can_create_task(user):
    return _active_department(user)


def can_assign_task(user, task):
    return bool(is_recipient_head(user, task) and task.status == "new"
                and not task.assignee_ids and task.due_at is not None)


def can_claim_task(user, task):
    return bool(is_recipient_employee(user, task) and task.status in ACTIVE
                and user.id not in task.assignee_ids and task.due_at is not None)


def can_reassign_task(user, task):
    # Includes repair of a returned task whose former assignee is unavailable.
    return bool(is_recipient_head(user, task) and task.status == "in_progress" and task.due_at is not None)


def has_available_assignee(task):
    return bool(task is not None and any(is_recipient_employee(user, task) for user in task.assignees))


def can_release_task(user, task):
    return bool(is_recipient_employee(user, task) and task.status == "in_progress"
                and has_available_assignee(task) and user.id in task.assignee_ids)


def can_complete_task(user, task):
    return bool(is_recipient_employee(user, task) and task.status == "in_progress"
                and has_available_assignee(task) and task.due_at is not None
                and (user.id in task.assignee_ids or user.role == "head"))


def can_submit_result(user, task):
    # Capability alias only. The old two-step review service is retired.
    return can_complete_task(user, task)


def can_return_result(user, task):
    return bool(is_sender_employee(user, task) and task.status == "completed"
                and task.due_at is not None and (user.id == task.author_id or user.role == "head"))


def can_cancel_task(user, task):
    return bool(is_sender_employee(user, task) and task.status in ACTIVE
                and (user.id == task.author_id or user.role == "head"))


def can_reject_task(user, task):
    return bool(is_recipient_employee(user, task) and task.status in ACTIVE and
                (user.role == "head" or has_available_assignee(task) and user.id in task.assignee_ids))


def can_change_deadline(user, task):
    return bool(is_sender_head(user, task) and task.status in ACTIVE)


def can_contribute_task(user, task):
    return bool(is_department_member(user) and can_access_task(user, task) and task.status in ACTIVE)


def _retired_capability(user, task):
    return False


# Keep import compatibility while making every removed permission fail closed.
can_edit_draft = can_submit_for_approval = can_withdraw_approval = _retired_capability
can_return_approval = can_approve_task = can_send_task = _retired_capability
can_start_task = can_request_clarification = can_answer_clarification = _retired_capability
can_review_recipient_result = can_review_result = can_change_working_status = _retired_capability


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
        if isinstance(object_id, bool) or not isinstance(object_id, (str, int)):
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


def can_manage_staff(user):
    return bool(is_department_member(user) and user.role == "head")


def can_manage_employee(user, employee):
    return bool(can_manage_staff(user) and employee is not None and employee.role == "employee"
        and employee.department_id == user.department_id and employee.id != user.id)
