"""Add a database-enforced reminder deduplication key without changing old notices."""

from alembic import op
import sqlalchemy as sa

revision = "20261024_03"
down_revision = "20261024_02"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("task_notifications", sa.Column("deduplication_key", sa.String(64), nullable=True))
    op.create_index("ix_task_notifications_deduplication_key", "task_notifications", ["deduplication_key"], unique=True)


def downgrade():
    op.drop_index("ix_task_notifications_deduplication_key", table_name="task_notifications")
    with op.batch_alter_table("task_notifications") as batch_op:
        batch_op.drop_column("deduplication_key")
