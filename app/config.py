"""Application configuration with safe environment-based overrides."""

import os
import secrets


class Config:
    TASK_UPLOAD_DIR = os.environ.get("TASK_UPLOAD_DIR")
    TASK_TIMEZONE = "Asia/Qyzylorda"
    MAX_CONTENT_LENGTH = 21 * 1024 * 1024
    SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_urlsafe(32)
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL") or "sqlite:///portal.sqlite3"
    ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
    ADMIN_FULL_NAME = os.environ.get("ADMIN_FULL_NAME", "Системный администратор")
    WTF_CSRF_ENABLED = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"


def apply_database_setting(app):
    """Map the existing test configuration to SQLAlchemy's database URI."""
    database = app.config.get("DATABASE")
    if database is None:
        return
    if database == ":memory:":
        from sqlalchemy.pool import StaticPool

        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            "connect_args": {"check_same_thread": False},
            "poolclass": StaticPool,
        }
    elif database.startswith(("sqlite:", "postgresql:", "postgresql+")):
        app.config["SQLALCHEMY_DATABASE_URI"] = database
    else:
        app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{database.replace(chr(92), '/')}"
