"""Expand the approval schema without erasing historical records."""
from alembic import op
import sqlalchemy as sa

revision = "20261024_05"
down_revision = "20261024_04"
branch_labels = depends_on = None


def upgrade():
    bind = op.get_bind()
    # SQLite batch recreation drops parent tables. With foreign_keys=ON that
    # could cascade-delete historical children; refuse before any DDL instead.
    if bind.dialect.name == "sqlite" and bind.exec_driver_sql("PRAGMA foreign_keys").scalar():
        raise RuntimeError("Миграция SQLite остановлена: foreign_keys=ON; используйте изолированную копию с отключёнными FK на время batch-миграции")
    admins = bind.execute(sa.text("SELECT id, is_blocked, department_id FROM users WHERE role='admin'")).mappings().all()
    if len(admins) != 1 or admins[0]["is_blocked"] or admins[0]["department_id"] is not None:
        raise RuntimeError("Миграция остановлена: требуется ровно один действующий администратор без отдела")
    normalized = set()
    for row in bind.execute(sa.text("SELECT name FROM departments")).mappings():
        name = row["name"].strip().casefold()
        if not name or name in normalized:
            raise RuntimeError("Миграция остановлена: пустые или конфликтующие нормализованные имена отделов")
        normalized.add(name)

    op.add_column("departments", sa.Column("normalized_name", sa.String(300), nullable=True))
    op.add_column("departments", sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table("task_categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("normalized_name", sa.String(240), nullable=False, unique=True),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("length(trim(normalized_name)) > 0", name="ck_task_categories_name"))
    op.create_table("system_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("max_upload_mb", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("allowed_extensions", sa.JSON(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_system_settings_singleton"),
        sa.CheckConstraint("max_upload_mb BETWEEN 1 AND 20", name="ck_system_settings_upload_limit"))
    settings = sa.table("system_settings", sa.column("id"), sa.column("max_upload_mb"), sa.column("allowed_extensions", sa.JSON()))
    bind.execute(settings.insert().values(id=1, max_upload_mb=20,
        allowed_extensions=["pdf", "docx", "xlsx", "pptx", "jpg", "png"]))
    op.create_table("admin_audit_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actor_snapshot", sa.JSON(), nullable=False),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("object_type", sa.String(40), nullable=False),
        sa.Column("object_id", sa.Integer(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    for col in ("actor_id", "created_at"):
        op.create_index(f"ix_admin_audit_events_{col}", "admin_audit_events", [col])
    with op.batch_alter_table("tasks") as batch:
        batch.drop_constraint("ck_tasks_status", type_="check")
        batch.drop_constraint("ck_tasks_priority", type_="check")
        for name in ("sent_at", "completed_at", "closed_at"):
            batch.add_column(sa.Column(name, sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("revision", sa.Integer(), nullable=False, server_default="0"))
        for name in ("legacy_missing_due", "is_legacy_dispatched"):
            batch.add_column(sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("category_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_tasks_category", "task_categories", ["category_id"], ["id"], ondelete="RESTRICT")
    with op.batch_alter_table("task_comments") as batch:
        batch.create_unique_constraint("uq_task_comments_id_task", ["id", "task_id"])
    op.add_column("task_attachments", sa.Column("comment_id", sa.Integer(), nullable=True))
    op.add_column("task_events", sa.Column("comment_id", sa.Integer(), nullable=True))
    op.add_column("task_events", sa.Column("actor_snapshot", sa.JSON(), nullable=True))
    for table in ("task_attachments", "task_events"):
        op.create_index(f"ix_{table}_comment_id", table, ["comment_id"])


def downgrade():
    raise RuntimeError("Безопасный откат требует восстановления согласованной резервной копии; данные не удалены")
