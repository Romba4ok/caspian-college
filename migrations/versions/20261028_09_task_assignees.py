"""Multiple task assignees; clean working tables only."""
from alembic import op
import sqlalchemy as sa

revision = "20261028_09"
down_revision = "20261028_08"
branch_labels = None
depends_on = None

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
    if sa.inspect(op.get_bind()).has_table("task_assignees"):
        raise RuntimeError("Ожидалась схема 08 без таблицы назначений; обновление остановлено")
    _require_empty_working_tables(op.get_bind())
    with op.batch_alter_table("tasks") as batch:
        batch.drop_constraint("ck_tasks_new_unassigned", type_="check")
        batch.create_check_constraint("ck_tasks_legacy_assignee_unused", "assignee_id IS NULL")
    op.create_table("task_assignees",
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True))
    op.create_index("ix_task_assignees_user_id", "task_assignees", ["user_id"])
    from app.assignment_schema import install_guards
    install_guards(op.get_bind())


def downgrade():
    raise RuntimeError("Assignment downgrade requires a separately verified compatible release")
