"""Mandatory deadlines, immutable discussion and revisions on isolated databases."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
import os
from pathlib import Path
import sys
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.datastructures import MultiDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.extensions import db
from app.models import Department, Task, TaskComment, TaskEvent, TaskAttachment, TaskNotification, User, TaskCategory
from app import workflow as w
from support.portal_fixtures import PortalCase
from support.migration_fixtures import MigrationDatabase
from test_tasks import attachment_bytes


class DeadlineCommentTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.files = tempfile.TemporaryDirectory(prefix="portal_materials_")
        self.app.config["TASK_UPLOAD_DIR"] = self.files.name
        for name, role, dept in (("sender-peer", "employee", self.department_id), ("head2", "head", self.department_id),
                ("receiver", "employee", self.other_department_id), ("receiver-peer", "employee", self.other_department_id),
                ("receiver-head", "head", self.other_department_id)):
            self.add_member(name, role=role, department_id=dept)
        self.work = self.task(status="in_progress", assignee_id=self.ids["receiver"])

    def tearDown(self):
        super().tearDown(); self.files.cleanup()

    def client_for(self, name):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(self.ids[name]); session["_fresh"] = True
        return client

    def saved(self):
        with self.app.app_context():
            task = db.session.get(Task, self.work)
            return {**{c.name: deepcopy(getattr(task, "legacy_assignee_id" if c.name=="assignee_id" else c.name)) for c in Task.__table__.columns}, "assignee_ids":task.assignee_ids}

    def snapshot(self):
        with self.app.app_context():
            return {m.__tablename__: deepcopy(db.session.execute(select(m.__table__).order_by(m.id)).all())
                for m in (Task, TaskComment, TaskEvent, TaskAttachment, TaskNotification)}

    def act(self, action="comments", *, actor="employee", revision=..., comment="Пояснение", extra=None):
        data = {"comment": comment}
        if revision is ...: revision = self.saved()["revision"]
        if revision is not None: data["revision"] = str(revision)
        if action == "deadline": data["due_at"] = "2030-05-02T00:00"
        data.update(extra or {})
        return self.client_for(actor).post(f"/tasks/{self.work}/{action}", data=data)

    def test_only_any_sender_head_changes_deadline_with_timezone_history_and_file(self):
        for actor in ("head", "head2"):
            before = self.saved()
            due = "2030-05-02T00:00" if actor == "head" else "2030-06-02T00:00"
            response = self.act("deadline", actor=actor, extra={"due_at": due,
                "attachment": (BytesIO(attachment_bytes(".pdf")), "reason.pdf")})
            self.assertEqual(302, response.status_code)
            current = self.saved()
            self.assertEqual(datetime(2030, 5 if actor == "head" else 6, 1, 19), current["due_at"])
            for key in before.keys() - {"due_at", "revision", "updated_at"}: self.assertEqual(before[key], current[key], key)
            with self.app.app_context():
                event = TaskEvent.query.order_by(TaskEvent.id.desc()).first()
                self.assertEqual("deadline_changed", event.event_type)
                self.assertEqual(self.ids[actor], event.actor_id)
                self.assertEqual(before["due_at"].replace(tzinfo=timezone.utc).isoformat(), event.details["before"]["due_at"])
                self.assertEqual(current["due_at"].replace(tzinfo=timezone.utc).isoformat(), event.details["after"]["due_at"])
                self.assertEqual(1, TaskAttachment.query.filter_by(comment_id=event.comment_id).count())

    def test_deadline_wrong_roles_and_sides_never_mutate(self):
        before = self.snapshot()
        for actor, expected in (("employee", 403), ("sender-peer", 403), ("receiver", 403),
                                ("receiver-head", 403), ("root", 403), ("outsider", 404)):
            self.assertEqual(expected, self.act("deadline", actor=actor).status_code)
            self.assertEqual(before, self.snapshot())

    def test_sender_head_can_change_new_task_deadline_without_assigning_executor(self):
        self.work = self.task()
        self.assertEqual(302, self.act("deadline", actor="head").status_code)
        task = self.saved()
        self.assertEqual(("new", [], None, None),
            (task["status"], task["assignee_ids"], task["completed_at"], task["closed_at"]))

    def test_deadline_and_comment_require_strict_fields_version_and_comment(self):
        for action, actor in (("deadline", "head"), ("comments", "receiver-peer"), ("attachments", "employee")):
            before = self.snapshot()
            files = {"attachment": (BytesIO(attachment_bytes(".pdf")), "x.pdf")} if action == "attachments" else {}
            for value in (None, "", "-1", "true", "1.2", "١", "9" * 80):
                extra = {"attachment": (BytesIO(attachment_bytes(".pdf")), "x.pdf")} if action == "attachments" else {}
                self.assertEqual(400, self.act(action, actor=actor, revision=value, extra=extra).status_code)
            for value in ("", " \t\n "):
                self.assertEqual(400, self.act(action, actor=actor, comment=value, extra=files).status_code)
                if files: files = {"attachment": (BytesIO(attachment_bytes(".pdf")), "x.pdf")}
            for field in ("comment_id", "task_id", "author_id", "assignee_ids", "status", "is_rework", "storage_key",
                          "title", "description", "priority", "category_id", "recipient_department_id", "contact_person"):
                self.assertEqual(400, self.act(action, actor=actor, extra={field: "1"}).status_code)
            form = MultiDict({"revision": "0", "comment": "one"}); form.add("revision", "1")
            self.assertEqual(400, self.client_for(actor).post(f"/tasks/{self.work}/{action}", data=form).status_code)
            self.assertEqual(before, self.snapshot())

    def test_deadline_cannot_clear_use_invalid_value_or_create_noop_history(self):
        before = self.snapshot()
        for value in ("", " ", "bad-date", "2030-99-01", "2030-01-01T12:00+99:00"):
            self.assertEqual(400, self.act("deadline", actor="head", extra={"due_at": value}).status_code)
            self.assertEqual(before, self.snapshot())
        with self.app.app_context():
            task = db.session.get(Task, self.work)
            with self.assertRaises(w.WorkflowError):
                w.change_task_deadline(task=task, actor=db.session.get(User, self.ids["head"]),
                    due_at=task.due_at, expected_revision=0, comment="No change")
            db.session.rollback()
        self.assertEqual(before, self.snapshot())

    def test_every_member_can_discuss_both_active_states_without_changing_work_fields(self):
        for status in ("new", "in_progress"):
            with self.app.app_context():
                task = db.session.get(Task, self.work)
                task.assignee_ids = []; db.session.flush(); task.status = status
                task.assignee_id = None if status == "new" else self.ids["receiver"]
                db.session.commit()
            for actor in ("employee", "sender-peer", "head", "receiver", "receiver-peer", "receiver-head"):
                before = self.saved()
                self.assertEqual(302, self.act(actor=actor, comment="Прошу перенести срок").status_code)
                current = self.saved()
                for key in before.keys() - {"revision", "updated_at"}: self.assertEqual(before[key], current[key], key)

    def test_closed_tasks_refuse_discussion_files_and_deadline_until_explicit_return(self):
        for status in ("completed", "cancelled", "rejected"):
            with self.app.app_context():
                task = db.session.get(Task, self.work); task.status = status; db.session.commit()
            before = self.snapshot()
            for action, actor, expected in (("comments", "employee", 403), ("comments", "receiver-peer", 403),
                    ("attachments", "receiver", 403), ("deadline", "head", 409)):
                extra = {"attachment": (BytesIO(attachment_bytes(".pdf")), "x.pdf")}
                self.assertEqual(expected, self.act(action, actor=actor, extra=extra).status_code)
                self.assertEqual(before, self.snapshot())
        with self.app.app_context():
            db.session.get(Task, self.work).status = "completed"; db.session.commit()
        self.assertEqual(302, self.act("return", actor="head").status_code)
        self.assertEqual(302, self.act(actor="receiver-peer").status_code)

    def test_admin_outsider_moved_blocked_and_demoted_cannot_bypass_permissions(self):
        before = self.snapshot()
        for action in ("comments", "attachments", "deadline"):
            for actor, code in (("root", 403), ("outsider", 404)):
                self.assertEqual(code, self.act(action, actor=actor).status_code)
        self.assertEqual(before, self.snapshot())
        for change, expected in (("role", 403), ("department", 404), ("blocked", 302)):
            client = self.client_for("head")
            with self.app.app_context():
                user = db.session.get(User, self.ids["head"])
                if change == "role": user.role = "employee"
                elif change == "department": user.department_id = self.third_department_id
                else: user.is_blocked = True
                db.session.commit()
            self.assertEqual(expected, client.post(f"/tasks/{self.work}/deadline", data={
                "revision": "0", "comment": "Reason", "due_at": "2030-05-02T00:00"}).status_code)
            with self.app.app_context():
                user = db.session.get(User, self.ids["head"])
                user.role = "head"; user.department_id = self.department_id; user.is_blocked = False; db.session.commit()
        self.assertEqual(before, self.snapshot())

    def test_stale_discussion_after_completion_retains_text_and_has_no_partial_file(self):
        self.assertEqual(302, self.act("complete", actor="receiver").status_code)
        before = self.snapshot()
        for action, actor in (("comments", "employee"), ("attachments", "receiver-peer"), ("deadline", "head")):
            response = self.act(action, actor=actor, revision=0, comment="UNSAVED-DISCUSSION",
                extra={"attachment": (BytesIO(attachment_bytes(".pdf")), "x.pdf")})
            self.assertEqual(409, response.status_code)
            self.assertIn(b"UNSAVED-DISCUSSION", response.data)
            self.assertEqual(before, self.snapshot())
        self.assertEqual([], list(Path(self.files.name).iterdir()))

    def test_stale_deadline_and_comment_cannot_duplicate_events(self):
        for action, actor in (("comments", "employee"), ("deadline", "head")):
            revision = self.saved()["revision"]
            extra = {"due_at": "2031-05-01T12:00"} if action == "deadline" else {}
            self.assertEqual(302, self.act(action, actor=actor, revision=revision, extra=extra).status_code)
            before = self.snapshot()
            self.assertEqual(409, self.act(action, actor=actor, revision=revision, extra=extra).status_code)
            self.assertEqual(before, self.snapshot())

    def test_failures_roll_back_deadline_comment_event_files_and_notifications(self):
        Path(self.files.name, "existing").write_bytes(b"keep")
        for action, actor in (("deadline", "head"), ("comments", "employee"), ("attachments", "receiver")):
            before = self.snapshot()
            for where in ("app.audit.record_task_event", "app.workflow.deliver_event_notifications", "app.tasks.db.session.commit"):
                with patch(where, side_effect=SQLAlchemyError("synthetic-secret")):
                    response = self.act(action, actor=actor, extra={"attachment": (BytesIO(attachment_bytes(".pdf")), "x.pdf")})
                self.assertEqual(500, response.status_code)
                self.assertNotIn(b"synthetic-secret", response.data)
                self.assertEqual(before, self.snapshot())
                self.assertEqual(["existing"], sorted(p.name for p in Path(self.files.name).iterdir()))

    def test_csrf_canonical_forms_and_comment_html_escaping(self):
        from flask import template_rendered
        contexts=[]
        def capture(sender,template,context,**kw):contexts.append(context)
        with template_rendered.connected_to(capture,self.app):
            page = self.client_for("head").get(f"/tasks/{self.work}").get_data(as_text=True)
        self.assertEqual(f"/tasks/{self.work}/comments",contexts[-1]["comment_url"])
        self.assertEqual(f"/tasks/{self.work}/attachments",contexts[-1]["upload_url"])
        with template_rendered.connected_to(capture,self.app):self.client_for("root").get(f"/tasks/{self.work}")
        self.assertIsNone(contexts[-1]["comment_url"]);self.assertIsNone(contexts[-1]["upload_url"])
        self.assertIn(f'action="/tasks/{self.work}/deadline"', page)
        self.assertIn(f'action="/tasks/{self.work}/comments"', page)
        for action, actor in (("deadline", "head"), ("comments", "employee"), ("attachments", "receiver")):
            self.app.config["WTF_CSRF_ENABLED"] = True
            self.assertEqual(400, self.act(action, actor=actor).status_code)
            self.app.config["WTF_CSRF_ENABLED"] = False
        self.assertEqual(302, self.act(comment='<script>alert("x")</script>').status_code)
        page = self.client_for("receiver-peer").get(f"/tasks/{self.work}").get_data(as_text=True)
        self.assertIn("&lt;script&gt;", page); self.assertNotIn('<script>alert("x")</script>', page)

    def test_archived_departments_still_discuss_and_adjust_existing_work(self):
        with self.app.app_context():
            for did in (self.department_id, self.other_department_id): db.session.get(Department, did).is_archived = True
            db.session.commit()
        self.assertEqual(302, self.act(actor="receiver-peer").status_code)
        self.assertEqual(302, self.act("deadline", actor="head").status_code)

    def test_notifications_address_only_author_executor_and_stage_heads(self):
        for action, actor, expected in (("comments", "employee", ("receiver", "receiver-head")),
                ("comments", "receiver-peer", ("employee", "receiver", "head", "head2", "receiver-head")),
                ("deadline", "head", ("employee", "receiver", "head2", "receiver-head"))):
            self.assertEqual(302, self.act(action, actor=actor).status_code)
            with self.app.app_context():
                event = TaskEvent.query.order_by(TaskEvent.id.desc()).first()
                self.assertEqual({self.ids[n] for n in expected}, {n.user_id for n in TaskNotification.query.filter(
                    TaskNotification.deduplication_key.like(f"event:{event.id}:user:%"))})

    def test_saved_categories_and_materials_are_unchanged_by_discussion(self):
        with self.app.app_context():
            category = TaskCategory(name="Категория", normalized_name="категория"); db.session.add(category); db.session.flush()
            task = db.session.get(Task, self.work); task.category_id = category.id; task.category = category.name
            db.session.commit(); category_id = category.id
        self.assertEqual(302, self.act().status_code)
        self.assertEqual(302, self.act("deadline", actor="head").status_code)
        with self.app.app_context():
            self.assertEqual("Категория", db.session.get(TaskCategory, category_id).name)
            self.assertEqual(category_id, db.session.get(Task, self.work).category_id)


@unittest.skipUnless(os.environ.get("PORTAL_TEST_DATABASE_URL"), "Use isolated local_portal.py verify")
class PostgreSQLDeadlineComments(MigrationDatabase, unittest.TestCase):
    postgres = True

    def setUp(self):
        super().setUp(); self.upgrade(); depts = self.rows("departments"); self.ids = {}
        for name, role, dept in (("author", "employee", depts[0]["id"]), ("head", "head", depts[0]["id"]),
                ("head2", "head", depts[0]["id"]), ("receiver", "employee", depts[1]["id"]), ("peer", "employee", depts[1]["id"])):
            user = User(username=name, full_name=name, role=role, department_id=dept, password_hash="synthetic")
            db.session.add(user); db.session.flush(); self.ids[name] = user.id
        task = Task(title="Synthetic", description="Required", author_id=self.ids["author"],
            sender_department_id=depts[0]["id"], recipient_department_id=depts[1]["id"],
            sent_at=datetime.now(timezone.utc), due_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
            status="in_progress", assignee_id=self.ids["receiver"])
        db.session.add(task); db.session.commit(); self.ident = task.id; db.session.remove()

    def race(self, operations):
        barrier = Barrier(2)
        def run(actor, action):
            with self.app.app_context():
                try:
                    user = db.session.get(User, self.ids[actor]); task = db.session.get(Task, self.ident)
                    barrier.wait(timeout=10)
                    kwargs = dict(task=task, actor=user, expected_revision=0, comment="Required")
                    if action == "deadline":
                        w.change_task_deadline(**kwargs, due_at=datetime(2030, 5 if actor == "head" else 6, 1, tzinfo=timezone.utc))
                    elif action == "complete": w.complete_task(**kwargs)
                    else: w.add_task_comment(**kwargs)
                    db.session.commit(); return "ok"
                except w.WorkflowConflict:
                    db.session.rollback(); return "conflict"
                finally: db.session.remove()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [f.result(timeout=25) for f in [pool.submit(run, *op) for op in operations]]
        self.assertEqual(["conflict", "ok"], sorted(results))
        self.assertEqual(1, db.session.get(Task, self.ident).revision)
        self.assertEqual(1, TaskEvent.query.count()); self.assertEqual(1, TaskComment.query.count())

    def test_two_deadline_changes_do_not_overwrite_history(self):
        self.race((("head", "deadline"), ("head2", "deadline")))

    def test_deadline_and_completion_share_one_revision(self):
        self.race((("head", "deadline"), ("receiver", "complete")))

    def test_two_comments_do_not_silently_accept_stale_context(self):
        self.race((("author", "comments"), ("peer", "comments")))

    def test_completion_and_discussion_cannot_leave_comment_on_closed_revision(self):
        self.race((("receiver", "complete"), ("peer", "comments")))
