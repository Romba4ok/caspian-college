"""Directory history, upload policy validation and administrative boundaries."""
from datetime import datetime, timezone
from pathlib import Path
import json
import sys
import tempfile
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.extensions import db
from app.models import AdminAuditEvent, SystemSettings, Task, TaskAttachment, TaskCategory, TaskEvent, User
from app.settings import (SettingsError, SUPPORTED_EXTENSIONS, active_categories, create_category,
                          get_upload_policy, update_upload_policy, validate_upload_policy)
from support.portal_fixtures import PortalCase


class SystemSettingsTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.login()

    def category(self, name="Оборудование"):
        self.assertEqual(302, self.client.post("/admin/categories/new", data={"name": name}).status_code)
        with self.app.app_context():
            return TaskCategory.query.filter_by(normalized_name=name.strip().casefold()).one().id

    def category_data(self, category_id):
        with self.app.app_context():
            row = db.session.get(TaskCategory, category_id)
            return dict(name=row.name, normalized_name=row.normalized_name, is_archived=row.is_archived)

    def policy(self):
        with self.app.app_context():
            return get_upload_policy().as_dict()

    def audits(self):
        with self.app.app_context():
            return [dict(action=e.action, object_id=e.object_id, actor=e.actor_snapshot, details=e.details)
                    for e in AdminAuditEvent.query.order_by(AdminAuditEvent.id)]

    def test_default_policy_is_read_only_and_has_known_byte_limit(self):
        before = self.audits()
        for _ in range(2):
            self.assertEqual(200, self.client.get("/admin/settings").status_code)
        self.assertEqual({"max_upload_mb": 20, "allowed_extensions": list(SUPPORTED_EXTENSIONS)}, self.policy())
        with self.app.app_context():
            self.assertEqual(0, SystemSettings.query.count())
            self.assertEqual(20*1024*1024, get_upload_policy().max_file_bytes)
        self.assertEqual(21*1024*1024, self.app.config["MAX_CONTENT_LENGTH"])
        self.assertEqual(before, self.audits())

    def test_create_rename_archive_and_noops_are_audited(self):
        category_id = self.category("  Оборудование  ")
        self.assertEqual("Оборудование", self.category_data(category_id)["name"])
        for name in ("Техника", "Техника"):
            self.assertEqual(302, self.client.post(f"/admin/categories/{category_id}/edit", data={"name": name}).status_code)
        for _ in range(2):
            self.assertEqual(302, self.client.post(f"/admin/categories/{category_id}/archive").status_code)
        self.assertTrue(self.category_data(category_id)["is_archived"])
        with self.app.app_context():
            self.assertEqual([], active_categories())
        entries = self.audits()
        self.assertEqual(["category_created", "category_renamed", "category_archived"], [e["action"] for e in entries])
        self.assertEqual("Оборудование", entries[1]["details"]["before"]["name"])
        self.assertEqual("Техника", entries[1]["details"]["after"]["name"])
        self.assertTrue(all(e["actor"]["role"] == "admin" for e in entries))

    def test_name_uniqueness_covers_archive_casefold_and_invalid_names(self):
        category_id = self.category("Straße")
        self.client.post(f"/admin/categories/{category_id}/archive")
        before = self.audits()
        for name in (" STRASSE ", "", "  ", "x"*81):
            response = self.client.post("/admin/categories/new", data={"name": name})
            self.assertEqual(400, response.status_code)
            self.assertIn('aria-invalid="true"', response.get_data(as_text=True))
        other = self.category("Другая")
        checkpoint = self.audits()
        response = self.client.post(f"/admin/categories/{other}/edit", data={"name": "STRASSE"})
        self.assertEqual(400, response.status_code)
        self.assertIn('value="STRASSE"', response.get_data(as_text=True))
        self.assertIn(' open>', response.get_data(as_text=True))
        self.assertEqual("Другая", self.category_data(other)["name"])
        self.assertEqual(checkpoint, self.audits())
        self.assertEqual(before, checkpoint[:-1])

    def test_sent_task_category_snapshot_history_and_free_text_are_preserved(self):
        category_id = self.category()
        task_id = self.task(status="completed", category=" Оборудование ", category_id=category_id,
            sent_at=datetime(2026, 1, 1, tzinfo=timezone.utc), completed_at=datetime(2026, 1, 2, tzinfo=timezone.utc))
        free_id = self.task(category="Ручная категория Қазақша", category_id=None)
        with self.app.app_context():
            db.session.add(TaskEvent(task_id=task_id, actor_id=self.ids["employee"], event_type="created",
                details={"category": " Оборудование "}))
            db.session.commit()
            before_tasks = db.session.execute(select(Task.__table__).order_by(Task.id)).all()
            before_events = db.session.execute(select(TaskEvent.__table__)).all()
        self.client.post(f"/admin/categories/{category_id}/edit", data={"name": "Новое название"})
        self.client.post(f"/admin/categories/{category_id}/archive")
        self.assertEqual(200, self.client.get("/admin/categories").status_code)
        with self.app.app_context():
            self.assertEqual(before_tasks, db.session.execute(select(Task.__table__).order_by(Task.id)).all())
            self.assertEqual(before_events, db.session.execute(select(TaskEvent.__table__)).all())
            self.assertEqual("Ручная категория Қазақша", db.session.get(Task, free_id).category)
            self.assertEqual(" Оборудование ", db.session.get(Task, task_id).category)

    def test_policy_updates_boundaries_canonical_order_and_audit_without_secrets(self):
        for limit, formats in ((1, ["PNG", "pdf", "pdf"]), (20, list(SUPPORTED_EXTENSIONS))):
            response = self.client.post("/admin/settings", data={"max_upload_mb": str(limit), "allowed_extensions": formats})
            self.assertEqual(302, response.status_code)
            expected = [ext for ext in SUPPORTED_EXTENSIONS if ext in {v.lower() for v in formats}]
            self.assertEqual({"max_upload_mb": limit, "allowed_extensions": expected}, self.policy())
        before = self.audits()
        self.client.post("/admin/settings", data={"max_upload_mb": "20", "allowed_extensions": list(SUPPORTED_EXTENSIONS)})
        self.assertEqual(before, self.audits())
        self.assertEqual(["upload_policy_updated", "upload_policy_updated"], [e["action"] for e in before])
        self.assertEqual(20, before[0]["details"]["before"]["max_upload_mb"])
        self.assertEqual(1, before[0]["details"]["after"]["max_upload_mb"])
        serialized = json.dumps(before)
        for secret in ("root-password", "member-password", "password_hash", "secret_key", "postgresql://"):
            self.assertNotIn(secret, serialized)

    def test_invalid_numbers_and_extensions_leave_policy_and_audit_unchanged(self):
        self.client.post("/admin/settings", data={"max_upload_mb": "5", "allowed_extensions": ["pdf"]})
        before, audit = self.policy(), self.audits()
        for limit in ("", "0", "-1", "21", "1.5", "1e1", "NaN", "True", "9"*5000):
            response = self.client.post("/admin/settings", data={"max_upload_mb": limit, "allowed_extensions": ["pdf"]})
            self.assertEqual(400, response.status_code)
            self.assertNotIn(b"Traceback", response.data)
        for ext in ("exe", "svg", "html", "jpeg", "../../png"):
            response = self.client.post("/admin/settings", data={"max_upload_mb": "5", "allowed_extensions": ["pdf", ext]})
            self.assertEqual(400, response.status_code)
        self.assertEqual(before, self.policy())
        self.assertEqual(audit, self.audits())
        for limit in (True, False, 2.0, None):
            with self.assertRaises(SettingsError):
                validate_upload_policy(limit, ["pdf"])
        for formats in ("pdf", {"pdf": True}, [None]):
            with self.assertRaises(SettingsError):
                validate_upload_policy(2, formats)

    def test_empty_format_subset_is_explicit_and_does_not_expand_allowed_types(self):
        self.assertEqual(302, self.client.post("/admin/settings", data={"max_upload_mb": "4"}).status_code)
        self.assertEqual({"max_upload_mb": 4, "allowed_extensions": []}, self.policy())
        with self.app.app_context():
            self.assertEqual(0, len(get_upload_policy().allowed_extensions))
        self.assertEqual(21*1024*1024, self.app.config["MAX_CONTENT_LENGTH"])

    def test_restricting_formats_preserves_old_attachment_metadata_and_bytes(self):
        task_id = self.task()
        with tempfile.TemporaryDirectory(prefix="portal_case_") as directory:
            old_file = Path(directory)/"existing-private-key"
            old_file.write_bytes(b"original private attachment")
            with self.app.app_context():
                db.session.add(TaskAttachment(task_id=task_id, uploaded_by_id=self.ids["employee"], storage_key=old_file.name,
                    original_filename="Прежний.png", content_type="image/png", size_bytes=27))
                db.session.commit()
                before = db.session.execute(select(TaskAttachment.__table__)).all()
            self.client.post("/admin/settings", data={"max_upload_mb": "1", "allowed_extensions": ["pdf"]})
            self.assertEqual(b"original private attachment", old_file.read_bytes())
            with self.app.app_context():
                self.assertEqual(before, db.session.execute(select(TaskAttachment.__table__)).all())

    def test_management_pages_and_services_require_admin(self):
        category_id = self.category()
        before = self.audits()
        for name in ("employee", "head"):
            client = self.app.test_client()
            self.login(name, client=client)
            for url in ("/admin/categories", "/admin/settings"):
                self.assertEqual(403, client.get(url).status_code)
            for url in ("/admin/categories/new", f"/admin/categories/{category_id}/edit", f"/admin/categories/{category_id}/archive", "/admin/settings"):
                self.assertEqual(403, client.post(url, data={"name": "Подмена", "actor_id": self.ids["root"], "max_upload_mb": "20"}).status_code)
            page = client.get("/tasks/received").get_data(as_text=True)
            self.assertNotIn('href="/admin/settings"', page)
            self.assertNotIn('href="/admin/categories"', page)
            with self.app.app_context():
                actor = db.session.get(User, self.ids[name])
                with self.assertRaises(PermissionError):
                    create_category(actor=actor, name="Подмена")
                db.session.rollback()
                with self.assertRaises(PermissionError):
                    update_upload_policy(actor=actor, max_upload_mb=1, allowed_extensions=[])
                db.session.rollback()
        self.assertEqual(before, self.audits())

    def test_csrf_methods_and_unknown_category_are_checked(self):
        category_id = self.category()
        self.app.config["WTF_CSRF_ENABLED"] = True
        for url in ("/admin/settings", "/admin/categories/new", f"/admin/categories/{category_id}/edit", f"/admin/categories/{category_id}/archive"):
            self.assertEqual(400, self.client.post(url).status_code)
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.assertEqual(405, self.client.get(f"/admin/categories/{category_id}/archive").status_code)
        self.assertEqual(404, self.client.post("/admin/categories/999999/edit", data={"name": "Missing"}).status_code)
        self.assertEqual(404, self.client.post("/admin/categories/999999/archive").status_code)

    def test_category_commit_conflicts_roll_back_and_keep_form_input(self):
        conflict = IntegrityError("synthetic", {}, Exception("conflict"))
        with patch("app.admin.db.session.commit", side_effect=conflict):
            response = self.client.post("/admin/categories/new", data={"name": "Не сохранено"})
        self.assertEqual(400, response.status_code)
        self.assertIn('value="Не сохранено"', response.get_data(as_text=True))
        with self.app.app_context():
            self.assertEqual(0, TaskCategory.query.count())
        self.assertEqual([], self.audits())
        category_id = self.category()
        before, events = self.category_data(category_id), self.audits()
        for action, data in (("edit", {"name": "Ошибка названия"}), ("archive", {})):
            with patch("app.admin.db.session.commit", side_effect=conflict):
                response = self.client.post(f"/admin/categories/{category_id}/{action}", data=data)
            self.assertIn(response.status_code, (400, 302))
            self.assertEqual(before, self.category_data(category_id))
            self.assertEqual(events, self.audits())
        self.assertEqual(200, self.client.get("/admin/categories").status_code)

    def test_policy_commit_conflict_rolls_back_and_preserves_submitted_fields(self):
        self.client.post("/admin/settings", data={"max_upload_mb": "8", "allowed_extensions": ["pdf"]})
        before, events = self.policy(), self.audits()
        with patch("app.admin.db.session.commit", side_effect=IntegrityError("synthetic", {}, Exception("conflict"))):
            response = self.client.post("/admin/settings", data={"max_upload_mb": "3", "allowed_extensions": ["png"]})
        self.assertEqual(400, response.status_code)
        self.assertIn('value="3"', response.get_data(as_text=True))
        self.assertIn('value="png" checked', response.get_data(as_text=True))
        self.assertEqual(before, self.policy())
        self.assertEqual(events, self.audits())

    def test_category_names_are_escaped_and_navigation_is_unambiguous(self):
        self.category('<script>alert(1)</script>')
        page = self.client.get("/admin/categories").get_data(as_text=True)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', page)
        self.assertNotIn('<script>alert(1)</script>', page)
        self.assertIn('href="/admin/categories" aria-current="page"', page)
        self.assertNotIn('href="/admin/users" aria-current="page"', page)
        page = self.client.get("/admin/settings").get_data(as_text=True)
        self.assertIn('href="/admin/settings" aria-current="page"', page)

    def test_invalid_persisted_policy_fails_closed_and_admin_can_repair_it(self):
        with self.app.app_context():
            db.session.add(SystemSettings(id=1, max_upload_mb=2, allowed_extensions=["exe"]))
            db.session.commit()
            with self.assertRaises(SettingsError):
                get_upload_policy()
        self.assertEqual(400, self.client.get("/admin/settings").status_code)
        self.assertEqual(302, self.client.post("/admin/settings", data={"max_upload_mb": "2", "allowed_extensions": ["pdf"]}).status_code)
        self.assertEqual(["pdf"], self.policy()["allowed_extensions"])
