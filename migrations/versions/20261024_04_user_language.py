"""Persist account language while preserving existing users."""

from alembic import op
import sqlalchemy as sa

revision = "20261024_04"
down_revision = "20261024_03"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("language", sa.String(8), nullable=False, server_default="ru"))


def downgrade():
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("language")
