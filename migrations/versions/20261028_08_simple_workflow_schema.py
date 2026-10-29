"""Install the simplified workflow only on empty working tables."""
from alembic import op
import sqlalchemy as sa

revision = "20261028_08"
down_revision = "20261024_07"
branch_labels = depends_on = None

WORKING_TABLES = (
    "tasks", "task_comments", "task_events", "task_attachments",
    "task_notifications", "task_translations",
)


def _require_empty_working_tables(bind):
    # Hold the exclusion until the schema/version transaction commits. Never
    # race the precondition against an old worker inserting another task.
    if bind.dialect.name == "postgresql":
        names = ", ".join('"' + name + '"' for name in WORKING_TABLES)
        bind.exec_driver_sql("LOCK TABLE " + names + " IN ACCESS EXCLUSIVE MODE")
    elif bind.dialect.name == "sqlite":
        if bind.exec_driver_sql("PRAGMA foreign_keys").scalar():
            raise RuntimeError("Миграция остановлена: foreign_keys=ON; пересоздание tasks небезопасно")
        raw = bind.connection.driver_connection
        if not raw.in_transaction:
            bind.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            # A no-row write acquires the writer lock even for an explicit
            # deferred transaction. It changes neither data nor the revision.
            bind.exec_driver_sql("UPDATE alembic_version SET version_num=version_num WHERE 0=1")
    else:
        raise RuntimeError("Миграция поддерживает только SQLite и PostgreSQL")
    nonempty = [name for name in WORKING_TABLES if bind.execute(
        sa.select(sa.literal(1)).select_from(sa.table(name)).limit(1)).first() is not None]
    if nonempty:
        raise RuntimeError("Нужна пустая рабочая БД. Найдены записи: " + ", ".join(nonempty)
                           + ". Очистка и перенос автоматически не выполняются.")


def upgrade():
    bind = op.get_bind()
    _require_empty_working_tables(bind)
    with op.batch_alter_table("tasks") as batch:
        batch.drop_constraint("ck_tasks_status", type_="check")
        batch.drop_constraint("ck_tasks_due_required", type_="check")
        batch.alter_column("status", existing_type=sa.String(32), nullable=False, server_default="new")
        batch.alter_column("due_at", existing_type=sa.DateTime(timezone=True), nullable=False)
        batch.alter_column("sent_at", existing_type=sa.DateTime(timezone=True), nullable=False)
        batch.add_column(sa.Column("creation_key", sa.String(64), nullable=False))
        batch.add_column(sa.Column("is_rework", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("rework_cycle", sa.Integer(), nullable=False, server_default="0"))
        batch.create_check_constraint("ck_tasks_status",
            "status IN ('new', 'in_progress', 'completed', 'cancelled', 'rejected')")
        batch.create_check_constraint("ck_tasks_due_required", "due_at IS NOT NULL")
        batch.create_check_constraint("ck_tasks_rework_cycle", "rework_cycle >= 0")
        batch.create_check_constraint("ck_tasks_creation_key",
            "length(trim(creation_key)) >= 32 AND length(creation_key) <= 64 "
            "AND length(trim(creation_key)) = length(creation_key)")
        batch.create_check_constraint("ck_tasks_new_unassigned", "status <> 'new' OR assignee_id IS NULL")
        batch.create_unique_constraint("uq_tasks_creation_key", ["creation_key"])


def downgrade():
    raise RuntimeError("Понижение схемы процесса не поддерживается автоматически; "
                       "используйте отдельно проверенный порядок возврата совместимой версии")
