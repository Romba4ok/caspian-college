"""Convert roles and workflow while preserving the original audit evidence."""
from alembic import op
import sqlalchemy as sa

revision = "20261024_06"
down_revision = "20261024_05"
branch_labels = depends_on = None


def upgrade():
    bind = op.get_bind()
    metadata = sa.MetaData()
    tables = {name: sa.Table(name, metadata, autoload_with=bind) for name in
              ("users", "departments", "tasks", "task_categories", "task_events", "admin_audit_events")}
    users, departments, tasks, categories, events, audit = (tables[n] for n in
        ("users", "departments", "tasks", "task_categories", "task_events", "admin_audit_events"))
    for dept in bind.execute(sa.select(departments)).mappings().all():
        bind.execute(departments.update().where(departments.c.id == dept["id"]).values(
            normalized_name=dept["name"].strip().casefold()))
    dept_ids = set(bind.execute(sa.select(departments.c.id)).scalars())
    for user in bind.execute(sa.select(users)).mappings().all():
        if user["role"] == "admin":
            continue
        valid = user["department_id"] in dept_ids
        if user["role"] == "teacher" and user["department_role"] in ("head", "employee") and valid:
            role, blocked = user["department_role"], user["is_blocked"]
        elif user["role"] in ("head", "employee") and valid:
            role, blocked = user["role"], user["is_blocked"]
        else:
            role, blocked = "employee", True
        before = {k: user[k] for k in ("role", "department_id", "department_role", "is_blocked")}
        after = dict(before, role=role, is_blocked=blocked)
        bind.execute(users.update().where(users.c.id == user["id"]).values(role=role, is_blocked=blocked))
        bind.execute(audit.insert().values(actor_id=None, actor_snapshot={"name": "Система", "role": "system"},
            action="account_migrated", object_type="user", object_id=user["id"],
            details={"before": before, "after": after, "revision": revision}))

    category_ids = {}
    for task in bind.execute(sa.select(tasks).order_by(tasks.c.id)).mappings().all():
        old_status = task["status"]
        status = {"new": "awaiting_assignment", "accepted": "assigned", "ready": "recipient_review"}.get(old_status, old_status)
        if status not in ("awaiting_assignment", "assigned", "in_progress", "awaiting_clarification", "recipient_review", "completed", "cancelled"):
            raise RuntimeError("Миграция остановлена: неизвестное историческое состояние задачи")
        dates = {}
        for event in bind.execute(sa.select(events.c.event_type, events.c.created_at).where(
                events.c.task_id == task["id"]).order_by(events.c.created_at, events.c.id)).mappings():
            if event["event_type"] == "created":
                dates.setdefault("created", event["created_at"])
            elif event["event_type"] in ("completed", "cancelled"):
                dates[event["event_type"]] = event["created_at"]
        category_id = None
        if task["category"] and task["category"].strip():
            key = task["category"].strip().casefold()
            if key not in category_ids:
                result = bind.execute(categories.insert().values(name=task["category"].strip(), normalized_name=key))
                category_ids[key] = result.inserted_primary_key[0]
            category_id = category_ids[key]
        completed = dates.get("completed") if status == "completed" else None
        closed = completed if status == "completed" else dates.get("cancelled") if status == "cancelled" else None
        bind.execute(tasks.update().where(tasks.c.id == task["id"]).values(status=status,
            is_legacy_dispatched=True, legacy_missing_due=task["due_at"] is None,
            sent_at=dates.get("created"), completed_at=completed, closed_at=closed,
            category_id=category_id))
        bind.execute(events.insert().values(task_id=task["id"], actor_id=None, event_type="workflow_migrated",
            actor_snapshot={"name": "Система", "role": "system"}, details={"revision": revision,
                "before": {"status": old_status}, "after": {"status": status},
                "comment": "Переход на согласование и приёмку отделами; прежняя история сохранена"}))


def downgrade():
    raise RuntimeError("Обратное преобразование истории не выполняется: используйте согласованную резервную копию")
