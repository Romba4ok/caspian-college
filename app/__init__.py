"""Flask application factory for the internal department platform."""

from pathlib import Path

from flask import Flask, abort, g, redirect, session, url_for
from flask_login import current_user, logout_user

from .config import Config, apply_database_setting
from .extensions import csrf, db, login_manager, migrate

ROLES = {"admin": "Администратор", "head": "Начальник отдела", "employee": "Сотрудник отдела"}
DEPARTMENT_SEEDS = (
    ("marketing", "Маркетинг"),
    ("methodical", "Методический"),
    ("academic", "Учебный"),
    ("upbringing", "Воспитательный"),
    ("it", "IT"),
    ("practice", "Практика"),
)


def create_app(test_config=None):
    app = Flask(
        __name__,
        template_folder="../templates",
        static_folder="../static",
        instance_relative_config=True,
    )
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)
    apply_database_setting(app)

    if app.config.get("DATABASE") != ":memory:":
        Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    if app.testing:
        app.config["WTF_CSRF_ENABLED"] = False

    db.init_app(app)
    migration_directory = str(Path(app.root_path).parent / "migrations")
    migrate.init_app(app, db, directory=migration_directory)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Войдите, чтобы продолжить"
    csrf.init_app(app)

    from .admin import admin_bp
    from .auth import auth_bp
    from .tasks import tasks_bp
    from .reporting import reports_bp
    from .notifications import init_app as init_notifications
    from .reminders import init_app as init_reminders
    from .i18n import init_app as init_i18n, t
    from .models import User

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(tasks_bp)
    app.register_blueprint(reports_bp)
    init_notifications(app)
    init_reminders(app)
    init_i18n(app)
    login_manager.localize_callback = t

    @login_manager.user_loader
    def load_user(user_id):
        try:
            user = db.session.get(User, int(user_id), populate_existing=True)
            if user and user.is_active and user.role in ROLES and (user.role == "admin" or user.department_id is not None):
                return user
            return None
        except (TypeError, ValueError):
            return None

    @app.before_request
    def load_current_user_context():
        if not current_user.is_authenticated and "_user_id" in session:
            logout_user()
        g.user = current_user if current_user.is_authenticated else None

    @app.context_processor
    def inject_roles():
        return {"roles": ROLES, "portal_roles": ROLES}

    @app.route("/")
    def index():
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login"))
        return redirect(url_for("tasks.received_tasks"))

    @app.errorhandler(403)
    def forbidden(_error):
        return t("Доступ запрещён"), 403

    @app.errorhandler(404)
    def not_found(_error):
        return t("Страница не найдена"), 404

    @app.errorhandler(400)
    @app.errorhandler(409)
    @app.errorhandler(413)
    @app.errorhandler(405)
    def localized_request_error(error):
        defaults = {
            400: "Не удалось проверить форму. Обновите страницу и повторите действие.",
            405: "Действие недоступно", 413: "Превышен допустимый размер запроса",
        }
        description = error.description
        if error.code in defaults and (not description or not any("А" <= c <= "я" for c in description)):
            description = defaults[error.code]
        return t(description), error.code

    with app.app_context():
        if app.testing:
            db.create_all()
            from .models import Department

            for slug, name in DEPARTMENT_SEEDS:
                if not Department.query.filter_by(slug=slug).first():
                    db.session.add(Department(slug=slug, name=name))
            if User.query.count() == 0:
                admin_password = app.config.get("ADMIN_PASSWORD")
                if not admin_password:
                    raise RuntimeError("Для тестовой базы задайте ADMIN_PASSWORD")
                admin = User(
                    full_name=app.config.get("ADMIN_FULL_NAME", "Системный администратор"),
                    username=app.config.get("ADMIN_USERNAME", "admin"),
                    role="admin",
                )
                admin.set_password(admin_password)
                db.session.add(admin)
            db.session.commit()

            if app.config.get("DATABASE") == ":memory:":
                app.extensions["memory_db"] = db.engine.raw_connection()

    return app
