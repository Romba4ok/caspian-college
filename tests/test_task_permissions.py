"""HTTP and related-resource isolation for the clean-start task model."""
from io import BytesIO
from pathlib import Path
import sys
import tempfile
from uuid import uuid4
from unittest.mock import patch

from sqlalchemy import select
from werkzeug.exceptions import NotFound
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.extensions import db
from app.models import Task, TaskAttachment, TaskComment, TaskEvent, TaskNotification, User
from app import permissions as p
from app.notifications import visible_notifications_query
from support.portal_fixtures import PortalCase


class TaskPermissionTests(PortalCase):
    def setUp(self):
        super().setUp()
        for name, role, dept in (("sender-colleague", "employee", self.department_id),
            ("sender-head2", "head", self.department_id), ("recipient", "employee", self.other_department_id),
            ("recipient-colleague", "employee", self.other_department_id), ("recipient-head", "head", self.other_department_id),
            ("recipient-head2", "head", self.other_department_id), ("outsider-head", "head", self.third_department_id)):
            self.add_member(name, role=role, department_id=dept)
        self.files = tempfile.TemporaryDirectory(prefix="portal_case_")
        self.app.config["TASK_UPLOAD_DIR"] = self.files.name
        self.sent = self.task(title="PRIVATE-TASK-" + uuid4().hex)
        self.closed = self.task(status="completed")
        self.resources = self.add_resources(self.sent)

    def tearDown(self):
        super().tearDown()
        self.files.cleanup()

    def client_for(self, name):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(self.ids[name])
            session["_fresh"] = True
        return client

    def add_resources(self, task_id):
        key = uuid4().hex
        Path(self.files.name, key).write_bytes(b"synthetic private PDF bytes")
        with self.app.app_context():
            comment = TaskComment(task_id=task_id, author_id=self.ids["employee"], body="PRIVATE-COMMENT")
            db.session.add(comment)
            db.session.flush()
            attachment = TaskAttachment(task_id=task_id, comment_id=comment.id, uploaded_by_id=self.ids["employee"],
                original_filename="PRIVATE-FILE.pdf", storage_key=key, content_type="application/pdf", size_bytes=27)
            event = TaskEvent(task_id=task_id, comment_id=comment.id, actor_id=self.ids["employee"], event_type="sent", details={"comment":"PRIVATE-HISTORY"})
            notification = TaskNotification(task_id=task_id, user_id=self.ids["recipient"], event_type="private_test", message="PRIVATE-NOTIFICATION")
            db.session.add_all([attachment, event, notification])
            db.session.commit()
            return dict(comment=comment.id, attachment=attachment.id, event=event.id, notification=notification.id)

    def snapshot(self):
        with self.app.app_context():
            tasks = [(t.id, t.status, t.revision, t.assignee_id, t.updated_at) for t in Task.query.order_by(Task.id)]
            return tasks, tuple(m.query.count() for m in (TaskComment, TaskEvent, TaskAttachment, TaskNotification))

    def test_python_and_sql_visibility_match_for_all_roles_and_statuses(self):
        for status in ("in_progress", "cancelled", "rejected"):
            self.task(status=status)
        with self.app.app_context():
            tasks = Task.query.all()
            for name, uid in self.ids.items():
                user = db.session.get(User, uid, populate_existing=True)
                expected = {t.id for t in tasks if p.can_access_task(user, t)}
                actual = set(db.session.execute(select(Task.id).where(p.task_visibility_clause(user))).scalars())
                self.assertEqual(expected, actual, name)
            user.is_blocked = True
            self.assertEqual([], db.session.execute(select(Task.id).where(p.task_visibility_clause(user))).all())
            self.assertFalse(p.can_access_task(user, tasks[0]))

    def test_both_departments_and_admin_can_read_task_panel_and_files(self):
        for name in ("employee", "sender-colleague", "head", "sender-head2", "recipient", "recipient-colleague", "recipient-head", "recipient-head2", "root"):
            client = self.client_for(name)
            for suffix in ("", "/panel"):
                response = client.get(f"/tasks/{self.sent}{suffix}")
                self.assertEqual(200, response.status_code, (name, suffix))
                self.assertIn(b"PRIVATE-COMMENT", response.data)
            response = client.get(f'/tasks/attachments/{self.resources["attachment"]}/download')
            self.assertEqual(200, response.status_code)
            self.assertEqual(b"synthetic private PDF bytes", response.data)
            self.assertIn("private, no-store", response.headers["Cache-Control"])
            self.assertEqual("nosniff", response.headers["X-Content-Type-Options"])
            response.close()

    def test_unrelated_department_has_identical_missing_resource_response(self):
        for name in ("outsider", "outsider-head"):
            client = self.client_for(name)
            missing = client.get("/tasks/999999").data
            self.assertEqual(missing, client.get(f"/tasks/{self.sent}").data)
            self.assertEqual(404, client.get(f"/tasks/{self.sent}/panel").status_code)
            response = client.get(f'/tasks/attachments/{self.resources["attachment"]}/download')
            self.assertEqual(404, response.status_code)
            self.assertNotIn("Content-Disposition", response.headers)
            for getter, key in ((p.get_visible_comment_or_404, "comment"), (p.get_visible_event_or_404, "event"),
                                (p.get_visible_attachment_or_404, "attachment"), (p.get_visible_notification_or_404, "notification")):
                with self.app.app_context(), self.assertRaises(NotFound):
                    getter(self.resources[key], db.session.get(User, self.ids[name]))

    def test_lists_fragments_and_notification_counts_do_not_leak_other_departments(self):
        client = self.client_for("outsider")
        with self.app.app_context():
            title = db.session.get(Task, self.sent).title.encode()
        for url in ("/tasks/received", "/tasks/sent", "/tasks/completed?scope=sent",
                    "/tasks/received?view=list", "/tasks/received?fragment=board", "/tasks/notifications"):
            response = client.get(url)
            self.assertEqual(200, response.status_code)
            self.assertNotIn(title, response.data)
            self.assertNotIn(b"PRIVATE-NOTIFICATION", response.data)
            self.assertNotIn(b"PRIVATE-FILE", response.data)

    def test_own_notification_rule_applies_even_to_admin(self):
        for name in ("employee", "head", "root", "outsider"):
            client = self.client_for(name)
            self.assertNotIn(b"PRIVATE-NOTIFICATION", client.get("/tasks/notifications").data)
            self.assertEqual(404, client.post(f'/tasks/notifications/{self.resources["notification"]}/read').status_code)
        client = self.client_for("recipient")
        self.assertIn(b"PRIVATE-NOTIFICATION", client.get("/tasks/notifications").data)
        self.assertEqual(302, client.post(f'/tasks/notifications/{self.resources["notification"]}/read').status_code)

    def test_existing_session_loses_old_department_and_blocked_access(self):
        client = self.client_for("recipient")
        self.assertEqual(200, client.get(f"/tasks/{self.sent}").status_code)
        with self.app.app_context():
            user = db.session.get(User, self.ids["recipient"])
            user.department_id = self.third_department_id
            db.session.commit()
        self.assertEqual(404, client.get(f"/tasks/{self.sent}").status_code)
        self.assertNotIn(b"PRIVATE-NOTIFICATION", client.get("/tasks/notifications").data)
        self.assertEqual(404, client.post(f'/tasks/notifications/{self.resources["notification"]}/read').status_code)
        with self.app.app_context():
            user = db.session.get(User, self.ids["recipient"])
            user.is_blocked = True
            db.session.commit()
        self.assertEqual(302, client.get(f"/tasks/{self.sent}").status_code)
        for url in (f"/tasks/{self.sent}/panel", f'/tasks/attachments/{self.resources["attachment"]}/download',
                    "/tasks/notifications"):
            self.assertEqual(302, client.get(url).status_code)

    def test_retired_routes_refuse_before_workflow_or_upload_preparation(self):
        client = self.client_for("head")
        urls = [f"/tasks/{self.sent}/" + action for action in
            ("approval/submit", "approval/withdraw", "approval/approve", "approval/send", "approval/return_draft",
             "review/submit_result", "review/forward_result", "review/complete", "review/cancel", "confirm",
             "clarification", "status", "execution/start", "execution/request_clarification", "execution/answer_clarification", "edit")]
        before = self.snapshot()
        files = set(Path(self.files.name).iterdir())
        with patch("app.tasks._prepare_uploads", side_effect=AssertionError("must not save uploads")):
            for url in urls:
                response = client.post(url, data={"revision": "0", "comment": "Required", "attachment": (BytesIO(b"bad"), "new.pdf")})
                self.assertEqual(410, response.status_code, url)
            self.assertEqual(410, client.get(f"/tasks/{self.sent}/edit").status_code)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(files, set(Path(self.files.name).iterdir()))

    def test_implemented_material_handlers_reject_untrusted_fields_and_scope(self):
        with self.app.app_context():
            task = db.session.get(Task, self.sent)
            task.status = "in_progress"
            task.assignee_id = self.ids["recipient"]
            db.session.commit()
        cases = (("head", f"/tasks/{self.sent}/deadline"),
                 ("employee", f"/tasks/{self.sent}/comments"), ("employee", f"/tasks/{self.sent}/attachments"))
        before = self.snapshot()
        with patch("app.tasks._prepare_uploads", side_effect=AssertionError("must not prepare files")):
            for name, url in cases:
                self.assertEqual(400, self.client_for(name).post(url, data={"comment":"Required", "revision":"0", "task_id":str(self.closed)}).status_code, url)
        self.assertEqual(before, self.snapshot())
        for name in ("root", "outsider"):
            self.assertEqual(403 if name == "root" else 404, self.client_for(name).post(f"/tasks/{self.sent}/cancel").status_code)

    def test_no_retired_write_forms_in_detail_panel_or_board(self):
        client = self.client_for("recipient-head")
        for url in (f"/tasks/{self.sent}", f"/tasks/{self.sent}/panel", "/tasks/received"):
            page = client.get(url).get_data(as_text=True)
            self.assertNotIn('action="/tasks/'+str(self.sent)+'/review/', page)
            self.assertNotIn('action="/tasks/'+str(self.sent)+'/execution/start', page)
            if url == "/tasks/received":
                self.assertIn('href="/tasks/new"', page)

    def test_csrf_and_anonymous_requests_remain_protected(self):
        self.assertEqual(302, self.client.get(f"/tasks/{self.sent}").status_code)
        self.assertEqual(302, self.client.post(f"/tasks/{self.sent}/cancel").status_code)
        client = self.client_for("head")
        self.app.config["WTF_CSRF_ENABLED"] = True
        self.assertEqual(400, client.post(f"/tasks/{self.sent}/cancel", data={"comment":"Required"}).status_code)
        self.assertEqual(400, client.post(f"/tasks/{self.sent}/approval/approve").status_code)

    def test_assignment_requires_revision_and_comment_without_partial_change(self):
        before = self.snapshot()
        for name, suffix in (("recipient-head", "assign"), ("recipient-head", "execution/assign"),
                             ("recipient", "claim")):
            response = self.client_for(name).post(f"/tasks/{self.sent}/{suffix}",
                data={"revision":"0", "comment":" ", **({"assignee_ids":str(self.ids["recipient"])} if suffix != "claim" else {})})
            self.assertEqual(400, response.status_code)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(400, self.client_for("recipient-head").post(f"/tasks/{self.sent}/execution/unknown").status_code)

    def test_department_members_cannot_enter_admin_pages_by_direct_url(self):
        urls = ("/admin/users", "/admin/users/new", "/admin/departments", "/admin/categories",
                "/admin/settings", "/admin/statistics", "/admin/audit")
        for name in ("employee", "head", "recipient", "recipient-head"):
            client = self.client_for(name)
            for url in urls:
                self.assertEqual(403, client.get(url).status_code, (name, url))
        self.assertEqual(403, self.client_for("employee").get("/reports/department").status_code)

    def test_generic_notifications_remain_personal_even_for_admin(self):
        with self.app.app_context():
            note = TaskNotification(user_id=self.ids["recipient"], event_type="generic", message="GENERIC-PRIVATE")
            db.session.add(note)
            db.session.commit()
            ident = note.id
        for name in ("root", "employee", "recipient-head", "outsider"):
            client = self.client_for(name)
            self.assertNotIn(b"GENERIC-PRIVATE", client.get("/tasks/notifications").data)
            self.assertEqual(404, client.post(f"/tasks/notifications/{ident}/read").status_code)
        self.assertIn(b"GENERIC-PRIVATE", self.client_for("recipient").get("/tasks/notifications").data)

    def test_invalid_identifiers_fail_as_missing_not_database_errors(self):
        with self.app.app_context():
            user = db.session.get(User, self.ids["employee"])
            for bad in (True, 0, -1, 1.9, [], {}, "not-an-id", "1 OR 1=1", str(2**40), float("inf")):
                for getter in (p.get_visible_task_or_404, p.get_visible_comment_or_404,
                               p.get_visible_attachment_or_404, p.get_visible_event_or_404,
                               p.get_visible_notification_or_404):
                    with self.subTest(bad=bad, getter=getter.__name__), self.assertRaises(NotFound):
                        getter(bad, user)
