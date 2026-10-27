"""Only Russian system UI; historical languages and original content survive."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from sqlalchemy import text
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from app.extensions import db
from app.i18n import get_language, t
from app.models import Task, TaskComment, TaskTranslation, User
from support.portal_fixtures import PortalCase


class RussianInterfaceTests(PortalCase):
    def test_language_endpoint_gone_and_saved_preference_is_preserved(self):
        with self.app.app_context():
            db.session.get(User, self.ids["employee"]).language = "kk"
            db.session.commit()
        self.login("employee")
        for language in ("ru", "kk", "en"):
            self.assertEqual(404, self.client.post("/language", data={"language":language,"user_id":self.ids["head"],"next":"https://example.invalid"}).status_code)
        page = self.client.get("/tasks/received").get_data(as_text=True)
        self.assertIn('<html lang="ru">', page)
        self.assertNotIn('name="language"', page)
        self.assertNotIn("/language", page)
        self.assertEqual("kk", self.account("employee")["language"])
        self.assertEqual("ru", self.account("head")["language"])

    def test_anonymous_errors_and_authentication_stay_russian(self):
        with self.client.session_transaction() as session:
            session["language"] = "en"
        self.assertIn("Вход".encode(), self.client.get("/login").data)
        self.assertIn("Неверный логин или пароль".encode(), self.login(password="wrong").data)
        self.assertIn("Страница не найдена".encode(), self.client.get("/missing").data)
        self.app.config["WTF_CSRF_ENABLED"] = True
        self.assertIn("Не удалось проверить форму".encode(), self.client.post("/login").data)
        self.assertEqual("ru", get_language())
        self.assertEqual("Имя: Иван", t("Имя: {name}", name="Иван"))

    def test_original_user_content_and_old_translation_cache_are_not_changed(self):
        task_id = self.task(title="Equipment Қазақша English", description="Оригинал <script>alert(1)</script>")
        with self.app.app_context():
            db.session.add(TaskComment(task_id=task_id, author_id=self.ids["employee"], body="Комментарий Қазақша"))
            db.session.add(TaskTranslation(source_type="task", source_id=task_id, source_hash="historical", target_language="en", translated_text="CACHE MUST NOT REPLACE ORIGINAL"))
            db.session.commit()
        self.login("employee")
        with patch("urllib.request.urlopen", side_effect=AssertionError("External translation is forbidden")):
            page = self.client.get(f"/tasks/{task_id}").get_data(as_text=True)
        self.assertIn("Equipment Қазақша English", page)
        self.assertIn("Комментарий Қазақша", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertNotIn("CACHE MUST NOT REPLACE ORIGINAL", page)
        outsider = self.app.test_client()
        self.login("outsider", client=outsider)
        self.assertEqual(404, outsider.get(f"/tasks/{task_id}").status_code)
        with self.app.app_context():
            self.assertEqual("Оригинал <script>alert(1)</script>", db.session.get(Task, task_id).description)
            self.assertEqual(1, TaskTranslation.query.count())

    def test_administrative_page_has_only_current_role_choices(self):
        self.login()
        page = self.client.get("/admin/users/new").get_data(as_text=True)
        self.assertIn("Сотрудник отдела", page)
        self.assertIn("Начальник отдела", page)
        self.assertIn('aria-label="Учётная запись: Системный администратор"', page)
        self.assertIn("<small>Администратор</small>", page)
        for obsolete in ('value="admin"', 'name="department_role"', "Преподаватель", "Студент", "Мои группы", 'name="language"'):
            self.assertNotIn(obsolete, page)


class LanguageMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_existing_accounts_with_russian_default(self):
        app = create_app({"DATABASE": ":memory:", "ADMIN_PASSWORD": "migration-password", "SECRET_KEY": "migration"})
        runner = app.test_cli_runner()
        self.assertEqual(0, runner.invoke(args=["db", "upgrade", "20261024_03"], catch_exceptions=False).exit_code)
        with app.app_context():
            before = db.session.execute(text("SELECT id, username, password_hash, role FROM users")).all()
        self.assertEqual(0, runner.invoke(args=["db", "upgrade"], catch_exceptions=False).exit_code)
        self.assertEqual(0, runner.invoke(args=["db", "upgrade"], catch_exceptions=False).exit_code)
        with app.app_context():
            self.assertEqual(before, db.session.execute(text("SELECT id, username, password_hash, role FROM users")).all())
            self.assertTrue(all(user.language == "ru" for user in User.query.all()))
            db.session.remove()
            db.engine.dispose()

