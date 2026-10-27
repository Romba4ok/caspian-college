"""Add departments and staff account controls while preserving portal data."""

from alembic import op
import sqlalchemy as sa
from flask import current_app
from werkzeug.security import generate_password_hash

revision = "20261024_01"
down_revision = None
branch_labels = None
depends_on = None

DEPARTMENTS = (
    ("marketing", "Маркетинг"),
    ("methodical", "Методический"),
    ("academic", "Учебный"),
    ("upbringing", "Воспитательный"),
    ("it", "IT"),
    ("practice", "Практика"),
)


def _has_table(bind, table_name):
    return sa.inspect(bind).has_table(table_name)


def _columns(bind, table_name):
    return {column["name"] for column in sa.inspect(bind).get_columns(table_name)}


def upgrade():
    bind = op.get_bind()

    if not _has_table(bind, "departments"):
        op.create_table(
            "departments",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("slug", sa.String(length=40), nullable=False, unique=True),
            sa.Column("name", sa.String(length=100), nullable=False, unique=True),
        )

    if not _has_table(bind, "users"):
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("full_name", sa.String(length=160), nullable=False),
            sa.Column("username", sa.String(length=80), nullable=False, unique=True),
            sa.Column("password_hash", sa.String(length=255), nullable=False),
            sa.Column("role", sa.String(length=24), nullable=False, server_default="teacher"),
            sa.Column("department_id", sa.Integer(), sa.ForeignKey("departments.id", ondelete="SET NULL"), nullable=True),
            sa.Column("department_role", sa.String(length=24), nullable=True),
            sa.Column("is_blocked", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        op.create_index("ix_users_username", "users", ["username"], unique=False)
    else:
        columns = _columns(bind, "users")
        missing_columns = {
            "department_id": sa.Column(
                "department_id",
                sa.Integer(),
                sa.ForeignKey(
                    "departments.id",
                    ondelete="SET NULL",
                    name="fk_users_department_id_departments",
                ),
                nullable=True,
            ),
            "department_role": sa.Column("department_role", sa.String(length=24), nullable=True),
            "is_blocked": sa.Column("is_blocked", sa.Boolean(), nullable=False, server_default=sa.false()),
        }
        if set(missing_columns).difference(columns):
            recreate = "always" if bind.dialect.name == "sqlite" else "auto"
            naming_convention = {
                "ix": "ix_%(column_0_label)s",
                "uq": "uq_%(table_name)s_%(column_0_name)s",
                "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
                "pk": "pk_%(table_name)s",
            }
            with op.batch_alter_table(
                "users", recreate=recreate, naming_convention=naming_convention
            ) as batch_op:
                for column_name, column in missing_columns.items():
                    if column_name not in columns:
                        batch_op.add_column(column)

    if not _has_table(bind, "groups"):
        op.create_table(
            "groups",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("title", sa.String(length=160), nullable=False),
            sa.Column("teacher_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        )
        op.create_index("ix_groups_teacher_id", "groups", ["teacher_id"], unique=False)

    if not _has_table(bind, "group_students"):
        op.create_table(
            "group_students",
            sa.Column("group_id", sa.Integer(), sa.ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("student_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        )

    for slug, name in DEPARTMENTS:
        existing = bind.execute(sa.text("SELECT id FROM departments WHERE slug = :slug"), {"slug": slug}).first()
        if existing is None:
            bind.execute(
                sa.text("INSERT INTO departments (slug, name) VALUES (:slug, :name)"),
                {"slug": slug, "name": name},
            )

    user_count = bind.execute(sa.text("SELECT COUNT(*) FROM users")).scalar_one()
    admin_count = bind.execute(sa.text("SELECT COUNT(*) FROM users WHERE role = 'admin'")).scalar_one()
    if user_count == 0 or admin_count == 0:
        password = current_app.config.get("ADMIN_PASSWORD")
        if not password:
            raise RuntimeError("Задайте ADMIN_PASSWORD для создания первого администратора")
        username = current_app.config.get("ADMIN_USERNAME", "admin")
        existing_username = bind.execute(
            sa.text("SELECT id FROM users WHERE username = :username"), {"username": username}
        ).first()
        if existing_username is not None:
            raise RuntimeError("ADMIN_USERNAME уже занят; задайте отдельный логин системного администратора")
        bind.execute(
            sa.text(
                "INSERT INTO users (full_name, username, password_hash, role) "
                "VALUES (:full_name, :username, :password_hash, 'admin')"
            ),
            {
                "full_name": current_app.config.get("ADMIN_FULL_NAME", "Системный администратор"),
                "username": username,
                "password_hash": generate_password_hash(password),
            },
        )


def downgrade():
    bind = op.get_bind()
    columns = _columns(bind, "users") if _has_table(bind, "users") else set()
    if "department_id" in columns:
        has_assignments = bind.execute(sa.text("SELECT COUNT(*) FROM users WHERE department_id IS NOT NULL")).scalar_one()
        if has_assignments:
            raise RuntimeError("Отмена миграции остановлена: сначала сохраните или перенесите назначения отделов")
    if "department_role" in columns:
        has_roles = bind.execute(sa.text("SELECT COUNT(*) FROM users WHERE department_role IS NOT NULL")).scalar_one()
        if has_roles:
            raise RuntimeError("Отмена миграции остановлена: сначала сохраните или перенесите роли отделов")
    if "is_blocked" in columns:
        has_blocked = bind.execute(sa.text("SELECT COUNT(*) FROM users WHERE is_blocked = 1")).scalar_one()
        if has_blocked:
            raise RuntimeError("Отмена миграции остановлена: сначала сохраните статус блокировки пользователей")

    if {"department_id", "department_role", "is_blocked"}.intersection(columns):
        naming_convention = {
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
        with op.batch_alter_table("users", naming_convention=naming_convention) as batch_op:
            for column in ("is_blocked", "department_role", "department_id"):
                if column in columns:
                    batch_op.drop_column(column)
    if _has_table(bind, "departments"):
        op.drop_table("departments")
