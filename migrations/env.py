"""Alembic environment bound to the active Flask application."""

from alembic import context
from flask import current_app

config = context.config
target_metadata = current_app.extensions["migrate"].db.metadata


def run_migrations_offline():
    context.configure(
        url=current_app.config["SQLALCHEMY_DATABASE_URI"],
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    engine = current_app.extensions["migrate"].db.engine
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
