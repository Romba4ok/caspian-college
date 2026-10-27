"""Enforce the new workflow only after the data has been converted."""
from alembic import op
import sqlalchemy as sa

revision = "20261024_07"
down_revision = "20261024_06"
branch_labels = depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "sqlite" and bind.exec_driver_sql("PRAGMA foreign_keys").scalar():
        raise RuntimeError("Миграция SQLite остановлена: foreign_keys=ON; batch-пересоздание родительских таблиц небезопасно")
    with op.batch_alter_table("departments") as batch:
        batch.alter_column("normalized_name", existing_type=sa.String(300), nullable=False)
        batch.create_unique_constraint("uq_departments_normalized_name", ["normalized_name"])
        batch.create_check_constraint("ck_departments_name", "length(trim(normalized_name)) > 0")
    with op.batch_alter_table("users") as batch:
        batch.create_check_constraint("ck_users_role", "role IN ('admin', 'head', 'employee')")
        batch.create_check_constraint("ck_users_admin_active", "role <> 'admin' OR (NOT is_blocked AND department_id IS NULL)")
        batch.create_check_constraint("ck_users_department", "role = 'admin' OR is_blocked OR department_id IS NOT NULL")
    op.create_index("uq_users_single_admin", "users", ["role"], unique=True,
                    sqlite_where=sa.text("role = 'admin'"), postgresql_where=sa.text("role = 'admin'"))
    with op.batch_alter_table("tasks") as batch:
        batch.alter_column("status", existing_type=sa.String(32), server_default="draft")
        batch.create_check_constraint("ck_tasks_status",
            "status IN ('draft', 'pending_approval', 'awaiting_assignment', 'assigned', 'in_progress', "
            "'awaiting_clarification', 'recipient_review', 'sender_acceptance', 'completed', 'cancelled', 'rejected')")
        batch.create_check_constraint("ck_tasks_priority", "priority IN ('low', 'normal', 'high', 'urgent')")
        batch.create_check_constraint("ck_tasks_due_required", "due_at IS NOT NULL OR (legacy_missing_due AND is_legacy_dispatched)")
        batch.create_check_constraint("ck_tasks_revision", "revision >= 0")
    for table in ("task_attachments", "task_events"):
        with op.batch_alter_table(table) as batch:
            batch.create_foreign_key(f"fk_{table}_comment_task", "task_comments",
                ["comment_id", "task_id"], ["id", "task_id"], ondelete="RESTRICT")


def downgrade():
    raise RuntimeError("Понижение схемы не удаляет новые данные: восстановите согласованную резервную копию")
