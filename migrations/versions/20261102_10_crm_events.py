"""Add CRM delivery receipts without rewriting existing working data."""
from alembic import op
import sqlalchemy as sa

revision = "20261102_10"
down_revision = "20261028_09"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "crm_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("event_id", sa.String(160), nullable=False),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rework_cycle", sa.Integer(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("notification_count", sa.Integer(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("source", "event_id", name="uq_crm_events_source_event"),
        sa.CheckConstraint("notification_count >= 0", name="ck_crm_events_count"),
        sa.CheckConstraint("rework_cycle >= 0", name="ck_crm_events_cycle"),
    )
    op.create_index("ix_crm_events_task_id", "crm_events", ["task_id"])


def downgrade():
    bind = op.get_bind()
    # Older clean-start migrations refuse nonempty workflow tables. Preflight
    # that boundary here too: SQLite must not drop this table and advance the
    # version before an older step refuses the rest of a multi-step downgrade.
    names = ("crm_events", "tasks", "task_comments", "task_events", "task_attachments",
             "task_notifications", "task_translations")
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql("LOCK TABLE " + ", ".join('"'+name+'"' for name in names) + " IN ACCESS EXCLUSIVE MODE")
    elif bind.dialect.name == "sqlite" and not bind.connection.driver_connection.in_transaction:
        bind.exec_driver_sql("BEGIN IMMEDIATE")
    for name in names:
        if bind.execute(sa.text('SELECT count(*) FROM "'+name+'"')).scalar():
            raise RuntimeError("Downgrade refused: working data or CRM receipts are not empty")
    op.drop_index("ix_crm_events_task_id", table_name="crm_events")
    op.drop_table("crm_events")
