"""Department lifecycle, linked-data protection and HTTP authorization."""
import sys
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.departments import create_department
from app.extensions import db
from app.models import AdminAuditEvent, Department, TASK_STATUSES, Task, TaskComment, TaskEvent, User
from support.portal_fixtures import PortalCase


class DepartmentTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.login()

    def create(self, name="Новый отдел"):
        response = self.client.post("/admin/departments/new", data={"name": name})
        self.assertEqual(302, response.status_code)
        with self.app.app_context():
            return Department.query.filter_by(normalized_name=name.strip().casefold()).one().id

    def department(self, department_id):
        with self.app.app_context():
            d = db.session.get(Department, department_id)
            return None if d is None else dict(id=d.id, name=d.name, slug=d.slug, normalized_name=d.normalized_name, is_archived=d.is_archived)

    def audits(self):
        with self.app.app_context():
            return [dict(action=e.action, actor=e.actor_snapshot, object_id=e.object_id, details=e.details)
                    for e in AdminAuditEvent.query.order_by(AdminAuditEvent.id)]

    def test_seed_departments_are_preserved_and_admin_navigation_is_precise(self):
        with self.app.app_context():
            self.assertEqual({"marketing", "methodical", "academic", "upbringing", "it", "practice"},
                             {d.slug for d in Department.query.all()})
        page = self.client.get("/admin/departments").get_data(as_text=True)
        self.assertIn("Добавить отдел", page)
        self.assertIn('href="/admin/departments" aria-current="page"', page)
        self.assertNotIn('href="/admin/users" aria-current="page"', page)
        self.create()
        with self.app.app_context():
            self.assertEqual(7, Department.query.count())

    def test_complete_empty_department_lifecycle_is_audited(self):
        department_id = self.create("  Служба качества  ")
        original = self.department(department_id)
        self.assertEqual("Служба качества", original["name"])
        self.assertLessEqual(len(original["slug"]), 40)
        self.assertEqual(302, self.client.post(f"/admin/departments/{department_id}/edit", data={"name": "Контроль качества"}).status_code)
        self.assertEqual(original["slug"], self.department(department_id)["slug"])
        for action in ("archive", "archive", "restore", "restore"):
            self.assertEqual(302, self.client.post(f"/admin/departments/{department_id}/{action}").status_code)
        self.assertFalse(self.department(department_id)["is_archived"])
        self.assertEqual(302, self.client.post(f"/admin/departments/{department_id}/delete").status_code)
        self.assertIsNone(self.department(department_id))
        events = self.audits()
        self.assertEqual(["department_created", "department_renamed", "department_archived", "department_restored", "department_deleted"], [e["action"] for e in events])
        self.assertTrue(all(e["actor"]["role"] == "admin" for e in events))
        self.assertEqual("Служба качества", events[1]["details"]["before"]["name"])
        self.assertEqual("Контроль качества", events[-1]["details"]["before"]["name"])

    def test_duplicate_names_include_archived_unicode_casefold_and_whitespace(self):
        department_id = self.create("Straße")
        self.client.post(f"/admin/departments/{department_id}/archive")
        before = self.audits()
        for name in (" STRASSE ", " it ", "МАРКЕТИНГ", "", "   ", "x" * 101):
            response = self.client.post("/admin/departments/new", data={"name": name})
            self.assertEqual(400, response.status_code)
            self.assertIn('aria-invalid="true"', response.get_data(as_text=True))
        self.assertEqual(before, self.audits())

    def test_rename_error_preserves_slug_archive_status_and_old_audit(self):
        department_id = self.create()
        self.client.post(f"/admin/departments/{department_id}/archive")
        before, events = self.department(department_id), self.audits()
        self.assertEqual(400, self.client.post(f"/admin/departments/{department_id}/edit", data={"name": " IT "}).status_code)
        self.assertEqual(before, self.department(department_id))
        self.assertEqual(events, self.audits())
        self.client.post(f"/admin/departments/{department_id}/edit", data={"name": "Переименован"})
        self.assertTrue(self.department(department_id)["is_archived"])
        self.assertEqual(before["slug"], self.department(department_id)["slug"])
        self.assertEqual(events, self.audits()[:len(events)])

    def test_any_member_including_blocked_prevents_physical_delete(self):
        for role in ("head", "employee"):
            for blocked in (False, True):
                department_id = self.create(f"Связанный {role} {blocked}")
                with self.app.app_context():
                    u = User(full_name="Связанный", username=f"linked-{role}-{blocked}", role=role,
                        department_id=department_id, is_blocked=blocked, password_hash="synthetic")
                    db.session.add(u)
                    db.session.commit()
                    user_id = u.id
                before = self.audits()
                response = self.client.post(f"/admin/departments/{department_id}/delete", follow_redirects=True)
                self.assertIn("Используйте архивирование".encode(), response.data)
                self.assertIsNotNone(self.department(department_id))
                with self.app.app_context():
                    self.assertEqual(department_id, db.session.get(User, user_id).department_id)
                self.assertEqual(before, self.audits())

    def test_both_task_directions_and_all_states_prevent_deletion_without_members(self):
        for direction in ("sender_department_id", "recipient_department_id"):
            for status in TASK_STATUSES:
                department_id = self.create(f"Задачи {direction[:3]} {status}")
                task_id = self.task(**{direction: department_id}, status=status)
                before = self.audits()
                response = self.client.post(f"/admin/departments/{department_id}/delete", follow_redirects=True)
                self.assertIn("Используйте архивирование".encode(), response.data)
                self.assertIsNotNone(self.department(department_id))
                with self.app.app_context():
                    self.assertEqual(department_id, getattr(db.session.get(Task, task_id), direction))
                self.assertEqual(before, self.audits())

    def test_archive_preserves_tasks_history_and_existing_user_access(self):
        task_id = self.task(status="in_progress", assignee_id=self.ids["head"])
        with self.app.app_context():
            db.session.add(TaskComment(task_id=task_id, author_id=self.ids["employee"], body="Сохранить обсуждение"))
            db.session.add(TaskEvent(task_id=task_id, actor_id=self.ids["head"], event_type="started", details={"keep": True}))
            db.session.commit()
            before = {table: db.session.execute(select(model.__table__)).all() for table, model in
                      (("tasks", Task), ("comments", TaskComment), ("events", TaskEvent), ("users", User))}
        self.client.post(f"/admin/departments/{self.department_id}/archive")
        with self.app.app_context():
            after = {table: db.session.execute(select(model.__table__)).all() for table, model in
                     (("tasks", Task), ("comments", TaskComment), ("events", TaskEvent), ("users", User))}
        self.assertEqual(before, after)
        member_client = self.app.test_client()
        self.assertEqual(200, self.login("employee", client=member_client).status_code)
        self.assertEqual(200, member_client.get("/tasks/received").status_code)

    def test_archive_restricts_new_assignments_and_restore_reenables_them(self):
        department_id = self.create("Для назначения")
        self.client.post(f"/admin/departments/{department_id}/archive")
        self.assertNotIn("Для назначения".encode(), self.client.get("/admin/users/new").data)
        self.assertEqual(400, self.client.post("/admin/users/new", data=self.form(department_id=str(department_id))).status_code)
        self.assertEqual(400, self.client.post(f'/admin/users/{self.ids["employee"]}/edit', data=self.form("employee", department_id=str(department_id), password="")).status_code)
        self.client.post(f"/admin/departments/{department_id}/restore")
        self.assertIn("Для назначения".encode(), self.client.get("/admin/users/new").data)
        self.assertEqual(302, self.client.post("/admin/users/new", data=self.form(department_id=str(department_id))).status_code)
        self.assertEqual(department_id, self.account("created")["department_id"])

    def test_direct_get_post_and_service_calls_require_administrator(self):
        for role in ("employee", "head"):
            client = self.app.test_client()
            self.login(role, client=client)
            for url in ("/admin/departments", "/admin/departments/new", f"/admin/departments/{self.department_id}/edit"):
                self.assertEqual(403, client.get(url).status_code)
            for suffix in ("edit", "archive", "restore", "delete"):
                self.assertEqual(403, client.post(f"/admin/departments/{self.department_id}/{suffix}", data={"name": "Подмена"}).status_code)
            self.assertEqual(403, client.post("/admin/departments/new", data={"name": "Подмена"}).status_code)
            self.assertNotIn('href="/admin/departments"', client.get("/tasks/received").get_data(as_text=True))
            with self.app.app_context():
                with self.assertRaises(PermissionError):
                    create_department(actor=db.session.get(User, self.ids[role]), name="Подмена")
                db.session.rollback()
        self.assertEqual([], self.audits())

    def test_mutations_require_post_csrf_and_valid_department_id(self):
        self.app.config["WTF_CSRF_ENABLED"] = True
        for suffix in ("archive", "restore", "delete", "edit"):
            self.assertEqual(400, self.client.post(f"/admin/departments/{self.department_id}/{suffix}").status_code)
        self.assertEqual(400, self.client.post("/admin/departments/new", data={"name": "Новый"}).status_code)
        self.app.config["WTF_CSRF_ENABLED"] = False
        for suffix in ("archive", "restore", "delete"):
            self.assertEqual(405, self.client.get(f"/admin/departments/{self.department_id}/{suffix}").status_code)
            self.assertEqual(404, self.client.post(f"/admin/departments/999999/{suffix}").status_code)
        self.assertEqual(404, self.client.get("/admin/departments/999999/edit").status_code)
        self.assertEqual([], self.audits())

    def test_conditional_delete_rechecks_members_without_detaching_them(self):
        before = self.audits()
        with patch("app.departments._has_department_links", return_value=False):
            response = self.client.post(f"/admin/departments/{self.department_id}/delete", follow_redirects=True)
        self.assertIn("появились связанные данные".encode(), response.data)
        self.assertEqual(self.department_id, self.account("employee")["department_id"])
        self.assertIsNotNone(self.department(self.department_id))
        self.assertEqual(before, self.audits())

    def test_delete_integrity_error_rolls_back_and_session_remains_usable(self):
        department_id = self.create("Ошибка удаления")
        before = self.audits()
        # Simulate a reference rejected by the database after service checks.
        from app import departments as service
        actual_execute = db.session.execute
        def fail_delete(statement, *args, **kwargs):
            if getattr(statement, "is_delete", False):
                raise IntegrityError("synthetic delete", {}, Exception("reference conflict"))
            return actual_execute(statement, *args, **kwargs)
        with patch.object(service.db.session, "execute", side_effect=fail_delete):
            response = self.client.post(f"/admin/departments/{department_id}/delete", follow_redirects=True)
        self.assertNotIn(b"IntegrityError", response.data)
        self.assertIsNotNone(self.department(department_id))
        self.assertEqual(before, self.audits())
        self.assertEqual(200, self.client.get("/admin/departments").status_code)

    def test_commit_error_rolls_back_department_and_its_audit(self):
        department_id = self.create("Ошибка сохранения")
        before, events = self.department(department_id), self.audits()
        with patch("app.admin.db.session.commit", side_effect=IntegrityError("synthetic", {}, Exception("conflict"))):
            response = self.client.post(f"/admin/departments/{department_id}/archive", follow_redirects=True)
        self.assertEqual(200, response.status_code)
        self.assertEqual(before, self.department(department_id))
        self.assertEqual(events, self.audits())

    def test_name_save_integrity_errors_keep_input_without_partial_audit(self):
        conflict = IntegrityError("synthetic save", {}, Exception("unique conflict"))
        with patch("app.admin.db.session.commit", side_effect=conflict):
            response = self.client.post("/admin/departments/new", data={"name": "Новый конфликт"})
        self.assertEqual(400, response.status_code)
        self.assertIn('value="Новый конфликт"', response.get_data(as_text=True))
        self.assertEqual([], self.audits())
        with self.app.app_context():
            self.assertEqual(6, Department.query.count())
        department_id = self.create("До конфликта")
        before, events = self.department(department_id), self.audits()
        with patch("app.admin.db.session.commit", side_effect=conflict):
            response = self.client.post(f"/admin/departments/{department_id}/edit", data={"name": "После конфликта"})
        self.assertEqual(400, response.status_code)
        self.assertIn('value="После конфликта"', response.get_data(as_text=True))
        self.assertEqual(before, self.department(department_id))
        self.assertEqual(events, self.audits())

    def test_names_are_escaped_and_counts_include_all_references(self):
        department_id = self.create('<script>alert(1)</script>')
        self.task()
        page = self.client.get("/admin/departments").get_data(as_text=True)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn('<script>alert(1)</script>', page)
        self.assertIn('data-label="Пользователи">2</td>', page)
        self.assertIn('data-label="Задачи">1</td>', page)
        self.assertEqual('<script>alert(1)</script>', self.department(department_id)["name"])
