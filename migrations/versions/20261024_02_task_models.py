"""Create task records and task-related resources."""

from alembic import op
import sqlalchemy as sa


revision = "20261024_02"
down_revision = "20261024_01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("author_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("sender_department_id", sa.Integer(), sa.ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("recipient_department_id", sa.Integer(), sa.ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("assignee_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("priority", sa.String(length=16), server_default="normal", nullable=False),
        sa.Column("category", sa.String(length=80), nullable=True),
        sa.Column("contact_person", sa.String(length=160), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="new", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("priority IN ('low', 'normal', 'high')", name="ck_tasks_priority"),
        sa.CheckConstraint(
            "status IN ('new', 'accepted', 'in_progress', 'awaiting_clarification', 'ready', 'completed', 'cancelled')",
            name="ck_tasks_status",
        ),
        sa.CheckConstraint("sender_department_id <> recipient_department_id", name="ck_tasks_distinct_departments"),
    )
    for column in ("author_id", "sender_department_id", "recipient_department_id", "assignee_id", "due_at", "status"):
        op.create_index(f"ix_tasks_{column}", "tasks", [column])

    op.create_table(
        "task_comments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("author_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_task_comments_task_id", "task_comments", ["task_id"])
    op.create_index("ix_task_comments_author_id", "task_comments", ["author_id"])

    op.create_table(
        "task_attachments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("uploaded_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("storage_key", sa.String(length=255), nullable=False, unique=True),
        sa.Column("content_type", sa.String(length=120), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_task_attachments_task_id", "task_attachments", ["task_id"])
    op.create_index("ix_task_attachments_uploaded_by_id", "task_attachments", ["uploaded_by_id"])

    op.create_table(
        "task_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    for column in ("task_id", "actor_id", "event_type"):
        op.create_index(f"ix_task_events_{column}", "task_events", [column])

    op.create_table(
        "task_notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("message", sa.String(length=500), nullable=False),
        sa.Column("is_read", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_task_notifications_task_id", "task_notifications", ["task_id"])
    op.create_index("ix_task_notifications_user_id", "task_notifications", ["user_id"])

    op.create_table(
        "task_translations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_type", sa.String(length=24), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("target_language", sa.String(length=8), nullable=False),
        sa.Column("translated_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("source_type", "source_id", "source_hash", "target_language", name="uq_task_translation_version"),
    )


def downgrade():
    op.drop_table("task_translations")
    for index, table in (
        ("ix_task_notifications_user_id", "task_notifications"),
        ("ix_task_notifications_task_id", "task_notifications"),
    ):
        op.drop_index(index, table_name=table)
    op.drop_table("task_notifications")
    for index, table in (
        ("ix_task_events_event_type", "task_events"),
        ("ix_task_events_actor_id", "task_events"),
        ("ix_task_events_task_id", "task_events"),
    ):
        op.drop_index(index, table_name=table)
    op.drop_table("task_events")
    for index, table in (
        ("ix_task_attachments_uploaded_by_id", "task_attachments"),
        ("ix_task_attachments_task_id", "task_attachments"),
    ):
        op.drop_index(index, table_name=table)
    op.drop_table("task_attachments")
    for index, table in (("ix_task_comments_author_id", "task_comments"), ("ix_task_comments_task_id", "task_comments")):
        op.drop_index(index, table_name=table)
    op.drop_table("task_comments")
    for index in ("ix_tasks_status", "ix_tasks_due_at", "ix_tasks_assignee_id", "ix_tasks_recipient_department_id", "ix_tasks_sender_department_id", "ix_tasks_author_id"):
        op.drop_index(index, table_name="tasks")
    op.drop_table("tasks")
