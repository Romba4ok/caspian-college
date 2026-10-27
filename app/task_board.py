"""Complete board read model; callers supply the existing private task query."""

from types import SimpleNamespace

from sqlalchemy import func
from sqlalchemy.orm import joinedload, undefer

from .extensions import db
from .models import Task, TaskAttachment, TaskComment, User


BOARD_GROUPS = {
    "received": (
        ("to_do", "К выполнению", ("awaiting_assignment", "assigned")),
        ("working", "В работе", ("in_progress",)),
        ("clarification", "Уточнение", ("awaiting_clarification",)),
        ("recipient_review", "Проверка своего отдела", ("recipient_review",)),
        ("sender_acceptance", "Приёмка отправителя", ("sender_acceptance",)),
    ),
    "sent": (
        ("drafts", "Черновики", ("draft",)),
        ("approval", "На согласовании", ("pending_approval",)),
        ("dispatched", "Переданы отделу", ("awaiting_assignment", "assigned")),
        ("working", "Выполняются", ("in_progress", "awaiting_clarification")),
        ("review", "Проверка результата", ("recipient_review", "sender_acceptance")),
    ),
}


def load_board_projection(visible_query, direction, actions_for_task):
    """Read every visible active task without fetching its discussion or files.

    The query retains the route's visibility, direction and ordering. Capability
    checks remain in the existing actions_for_task policy, not this grouping.
    Nothing in this projection commits, locks or updates task records.
    """
    definitions = BOARD_GROUPS[direction]
    tasks = visible_query.options(
        undefer("*"),
        joinedload(Task.sender_department).undefer("*"),
        joinedload(Task.recipient_department).undefer("*"),
        joinedload(Task.author),
        joinedload(Task.assignee),
    ).all()
    groups = [dict(key=key, label=label, statuses=statuses, tasks=[], count=0)
              for key, label, statuses in definitions]
    group_for_status = {status: group for group in groups for status in group["statuses"]}
    card_counts = {task.id: dict(comments=0, attachments=0) for task in tasks}
    actions = {}
    assignment_departments = set()
    for task in tasks:
        group = group_for_status[task.status]
        group["tasks"].append(task)
        group["count"] += 1
        actions[task.id] = actions_for_task(task)
        if any(action["assignee"] for action in actions[task.id]):
            assignment_departments.add(task.recipient_department_id)

    if tasks:
        # Separate aggregates avoid comments × attachments JOIN multiplication.
        # A scoped subquery also avoids a parameter for every board task ID.
        visible_ids = visible_query.with_entities(Task.id).order_by(None).subquery()
        for model, field in ((TaskComment, "comments"), (TaskAttachment, "attachments")):
            rows = db.session.query(model.task_id, func.count(model.id)).join(
                visible_ids, model.task_id == visible_ids.c.id).group_by(model.task_id).all()
            for task_id, count in rows:
                if task_id in card_counts:
                    card_counts[task_id][field] = count

    candidates_by_department = {department_id: [] for department_id in assignment_departments}
    if assignment_departments:
        candidates = User.query.filter(
            User.department_id.in_(assignment_departments),
            User.role.in_(("head", "employee")),
            User.is_blocked.is_(False),
        ).order_by(User.full_name, User.id).all()
        for candidate in candidates:
            candidates_by_department[candidate.department_id].append(candidate)
    # Do not expose candidates on a card whose actions do not permit assignment,
    # even if another card in the same department needed that candidate query.
    task_candidates = {
        task.id: candidates_by_department[task.recipient_department_id]
        if any(action["assignee"] for action in actions[task.id]) else []
        for task in tasks
    }
    return SimpleNamespace(
        tasks=tasks, groups=groups, card_counts=card_counts, actions=actions,
        candidates=task_candidates,
        group_keys={status: group["key"] for status, group in group_for_status.items()},
        # Transitional compatibility for existing list.html. A board has one
        # complete result; only list mode has navigable pages of 25 tasks.
        pagination=SimpleNamespace(total=len(tasks), page=1, pages=1 if tasks else 0,
                                   has_prev=False, has_next=False),
    )
