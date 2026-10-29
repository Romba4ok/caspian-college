"""Immediate completion, versioned rework and final outcomes; isolated databases only."""
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

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.datastructures import MultiDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.extensions import db
from app.models import Department, Task, TaskComment, TaskEvent, TaskAttachment, TaskNotification, User
from app import workflow as w
from support.portal_fixtures import PortalCase
from support.migration_fixtures import MigrationDatabase
from test_tasks import attachment_bytes


class CompletionTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.files = tempfile.TemporaryDirectory(prefix="portal_completion_")
        self.app.config["TASK_UPLOAD_DIR"] = self.files.name
        self.add_member("sender-peer")
        self.add_member("sender-head2", role="head")
        for name, role in (("receiver", "employee"), ("peer", "employee"),
                           ("receiver-head", "head"), ("receiver-head2", "head")):
            self.add_member(name, role=role, department_id=self.other_department_id)
        self.work = self.work_task()

    def tearDown(self):
        super().tearDown()
        self.files.cleanup()

    def work_task(self, **changes):
        return self.task(**(dict(status="in_progress", assignee_ids=[self.ids["receiver"]],
            sent_at=datetime(2026, 1, 1, tzinfo=timezone.utc)) | changes))

    def client_for(self, name):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(self.ids[name])
            session["_fresh"] = True
        return client

    def saved(self):
        with self.app.app_context():
            task = db.session.get(Task, self.work)
            return {**{c.name: deepcopy(getattr(task, "legacy_assignee_id" if c.name=="assignee_id" else c.name)) for c in Task.__table__.columns}, "assignee_ids":task.assignee_ids}

    def snapshot(self):
        with self.app.app_context():
            return {m.__tablename__: deepcopy(db.session.execute(select(m.__table__).order_by(m.id)).all())
                for m in (Task, TaskComment, TaskEvent, TaskAttachment, TaskNotification)}

    def act(self, key, *, actor="receiver", revision=..., comment="Пояснение действия", extra=None):
        data = {"comment": comment}
        if revision is ...:
            revision = self.saved()["revision"]
        if revision is not None:
            data["revision"] = str(revision)
        data.update(extra or {})
        return self.client_for(actor).post(f"/tasks/{self.work}/{key}", data=data)

    def ok(self, response):
        self.assertEqual(302, response.status_code, response.get_data(as_text=True)[:250])

    def outcome(self, action):
        if action == "return":
            self.ok(self.act("complete"))
        return {"complete": "receiver", "return": "employee", "cancel": "employee", "reject": "receiver"}[action]

    def recipients(self):
        with self.app.app_context():
            event = TaskEvent.query.filter_by(task_id=self.work).order_by(TaskEvent.id.desc()).first()
            return {n.user_id for n in TaskNotification.query.filter(
                TaskNotification.deduplication_key.like(f"event:{event.id}:user:%"))}

    def test_two_rework_cycles_preserve_each_result_and_old_dates(self):
        original = self.saved()
        for cycle in range(3):
            self.ok(self.act("complete", comment=f"Результат {cycle}"))
            done = self.saved()
            self.assertEqual(("completed", False, cycle), tuple(done[k] for k in ("status", "is_rework", "rework_cycle")))
            self.assertEqual(done["completed_at"], done["closed_at"])
            self.assertIsNotNone(done["closed_at"])
            for key in ("assignee_ids", "due_at", "sent_at", "title", "description", "priority"):
                self.assertEqual(original[key], done[key], key)
            if cycle == 2:
                break
            self.ok(self.act("return", actor="employee" if cycle == 0 else "sender-head2", comment=f"Замечания {cycle}"))
            current = self.saved()
            self.assertEqual(("in_progress", True, cycle + 1, None, None), tuple(current[k] for k in
                ("status", "is_rework", "rework_cycle", "completed_at", "closed_at")))
            self.assertEqual(original["assignee_ids"], current["assignee_ids"])
            with self.app.app_context():
                event = TaskEvent.query.order_by(TaskEvent.id.desc()).first()
                for key in ("completed_at", "closed_at"):
                    self.assertEqual(done[key].replace(tzinfo=timezone.utc).isoformat(), event.details["before"][key])
                    self.assertIsNone(event.details["after"][key])
        with self.app.app_context():
            events = TaskEvent.query.order_by(TaskEvent.id).all()
            self.assertEqual(["completed", "returned_for_revision", "completed", "returned_for_revision", "completed"], [e.event_type for e in events])
            self.assertEqual(["Результат 0", "Замечания 0", "Результат 1", "Замечания 1", "Результат 2"],
                [db.session.get(TaskComment, e.comment_id).body for e in events])
            self.assertEqual(5, self.saved()["revision"])

    def test_real_send_claim_complete_and_rework_have_no_review_stages(self):
        sender = self.client_for("employee")
        response = sender.post("/tasks/new", data=self.creation_form(client=sender))
        self.ok(response)
        self.work = int(response.location.rsplit("/", 1)[1])
        self.ok(self.act("claim"))
        self.ok(self.act("complete"))
        self.ok(self.act("return", actor="head"))
        self.ok(self.act("complete", actor="receiver-head2"))
        self.assertEqual(("completed", 5, 1), tuple(self.saved()[k] for k in ("status", "revision", "rework_cycle")))
        with self.app.app_context():
            self.assertEqual(["sent", "claimed", "completed", "returned_for_revision", "completed"],
                [e.event_type for e in TaskEvent.query.filter_by(task_id=self.work).order_by(TaskEvent.id)])

    def test_completion_requires_executor_or_recipient_head_and_available_assignee(self):
        for actor, status in (("employee", 403), ("head", 403), ("peer", 403), ("root", 403),
                              ("outsider", 404), ("receiver", 302), ("receiver-head", 302), ("receiver-head2", 302)):
            with self.subTest(actor=actor):
                self.work = self.work_task()
                before = self.snapshot()
                self.assertEqual(status, self.act("complete", actor=actor).status_code)
                if status != 302:
                    self.assertEqual(before, self.snapshot())
        self.work = self.work_task(assignee_id=None)
        before = self.snapshot()
        self.assertEqual(409, self.act("complete", actor="receiver-head").status_code)
        self.assertEqual(before, self.snapshot())

    def test_only_author_and_any_sender_head_can_return(self):
        self.ok(self.act("complete"))
        before = self.snapshot()
        for actor, status in (("sender-peer", 403), ("receiver", 403), ("receiver-head", 403), ("root", 403), ("outsider", 404)):
            self.assertEqual(status, self.act("return", actor=actor).status_code, actor)
            self.assertEqual(before, self.snapshot())
        self.ok(self.act("return", actor="employee"))
        self.ok(self.act("complete"))
        self.ok(self.act("return", actor="sender-head2"))

    def test_cancel_and_reject_roles_and_both_active_states(self):
        for status in ("new", "in_progress"):
            for action, actors in (("cancel", ("employee", "head", "sender-head2")),
                                   ("reject", ("receiver-head", "receiver-head2", "receiver"))):
                for actor in actors:
                    self.work = self.work_task(status=status, assignee_id=None if status == "new" else self.ids["receiver"])
                    before = self.snapshot()
                    expected = 403 if action == "reject" and actor == "receiver" and status == "new" else 302
                    self.assertEqual(expected, self.act(action, actor=actor).status_code, (action, actor, status))
                    if expected != 302:
                        self.assertEqual(before, self.snapshot())
                        continue
                    result = self.saved()
                    self.assertEqual("cancelled" if action == "cancel" else "rejected", result["status"])
                    self.assertIsNone(result["completed_at"])
                    self.assertIsNotNone(result["closed_at"])
        self.work = self.work_task()
        before = self.snapshot()
        for action, actors in (("cancel", ("sender-peer", "receiver", "receiver-head", "root")),
                               ("reject", ("peer", "employee", "head", "root"))):
            for actor in actors:
                self.assertEqual(403, self.act(action, actor=actor).status_code)
                self.assertEqual(before, self.snapshot())

    def test_final_cancelled_and_rejected_outcomes_are_immutable(self):
        for action, actor in (("cancel", "employee"), ("reject", "receiver")):
            self.work = self.work_task()
            self.ok(self.act(action, actor=actor))
            before = self.snapshot()
            for key, user in (("complete", "receiver"), ("return", "employee"), ("cancel", "head"),
                              ("reject", "receiver-head"), ("release", "receiver"), ("reassign", "receiver-head")):
                extra = {"assignee_ids": str(self.ids["peer"])} if key == "reassign" else None
                self.assertEqual(409, self.act(key, actor=user, extra=extra).status_code, key)
                self.assertEqual(before, self.snapshot())

    def test_completed_can_only_reopen_not_cancel_reject_or_complete_again(self):
        self.ok(self.act("complete"))
        before = self.snapshot()
        for key, actor in (("cancel", "employee"), ("reject", "receiver-head"), ("complete", "receiver")):
            self.assertEqual(409, self.act(key, actor=actor).status_code)
            self.assertEqual(before, self.snapshot())

    def test_every_action_requires_comment_strict_version_and_no_extra_fields(self):
        for action in ("complete", "return", "cancel", "reject"):
            self.work = self.work_task()
            actor = self.outcome(action)
            before = self.snapshot()
            for comment in ("", " \n\t "):
                self.assertEqual(400, self.act(action, actor=actor, comment=comment).status_code)
            for revision in (None, "", "-1", "true", "1.5", "١", "9" * 80):
                self.assertEqual(400, self.act(action, actor=actor, revision=revision).status_code)
            for extra in ({"status": "completed"}, {"assignee_ids": "1"}, {"due_at": "2030-01-01"},
                          {"completed_at": "2030-01-01"}, {"rework_cycle": "9"}, {"reason": "alias"}):
                self.assertEqual(400, self.act(action, actor=actor, extra=extra).status_code)
            form = MultiDict({"revision": str(self.saved()["revision"]), "comment": "first"})
            form.add("comment", "override")
            self.assertEqual(400, self.client_for(actor).post(f"/tasks/{self.work}/{action}", data=form).status_code)
            self.assertEqual(before, self.snapshot())

    def test_stale_and_repeated_requests_do_not_duplicate_events_or_lose_comment(self):
        for action in ("complete", "return", "cancel", "reject"):
            self.work = self.work_task()
            actor = self.outcome(action)
            revision = self.saved()["revision"]
            self.ok(self.act(action, actor=actor, revision=revision))
            before = self.snapshot()
            response = self.act(action, actor=actor, revision=revision, comment="UNSAVED-RESULT")
            self.assertEqual(409, response.status_code)
            self.assertIn(b"UNSAVED-RESULT", response.data)
            self.assertEqual(before, self.snapshot())

    def test_unavailable_former_executor_is_preserved_for_reassignment_not_granted_access(self):
        for unavailable in ("blocked", "moved", "moved-to-sender", "missing"):
            self.work = self.work_task()
            self.ok(self.act("complete"))
            with self.app.app_context():
                user = db.session.get(User, self.ids["receiver"])
                if unavailable == "blocked": user.is_blocked = True
                elif unavailable == "moved": user.department_id = self.third_department_id
                elif unavailable == "moved-to-sender": user.department_id = self.department_id
                else: db.session.get(Task, self.work).assignee_id = None
                db.session.commit()
            self.ok(self.act("return", actor="employee"))
            self.assertEqual({self.ids[n] for n in ("receiver-head", "receiver-head2")}, self.recipients())
            self.assertEqual("in_progress", self.saved()["status"])
            before = self.snapshot()
            self.assertEqual(409, self.act("complete", actor="receiver-head").status_code)
            self.assertEqual(before, self.snapshot())
            from flask import template_rendered
            contexts = []
            def capture(sender, template, context, **kw): contexts.append(context)
            with template_rendered.connected_to(capture, self.app):
                self.client_for("receiver-head").get(f"/tasks/{self.work}")
            self.assertTrue(contexts[-1]["unavailable_assignee"])
            self.assertNotIn("complete", contexts[-1]["action_urls"])
            self.ok(self.act("claim", actor="peer"))
            self.ok(self.act("complete", actor="peer"))
            with self.app.app_context():
                user = db.session.get(User, self.ids["receiver"])
                self.assertEqual(unavailable == "blocked", user.is_blocked)
                self.assertEqual(self.third_department_id if unavailable == "moved" else (self.department_id if unavailable == "moved-to-sender" else self.other_department_id), user.department_id)
                self.assertEqual("employee", user.role)
                user.is_blocked = False; user.department_id = self.other_department_id; user.role = "employee"
                db.session.commit()

    def test_changed_role_department_and_blocked_session_cannot_complete(self):
        for change, expected in (("role", 403), ("department", 404), ("blocked", 302)):
            client = self.client_for("receiver-head")
            with self.app.app_context():
                user = db.session.get(User, self.ids["receiver-head"])
                if change == "role": user.role = "employee"
                elif change == "department": user.department_id = self.third_department_id
                else: user.is_blocked = True
                db.session.commit()
            before = self.snapshot()
            self.assertEqual(expected, client.post(f"/tasks/{self.work}/complete", data={"revision": "0", "comment": "Result"}).status_code)
            self.assertEqual(before, self.snapshot())
            with self.app.app_context():
                user = db.session.get(User, self.ids["receiver-head"])
                user.role = "head"; user.department_id = self.other_department_id; user.is_blocked = False
                db.session.commit()

    def test_archived_departments_can_complete_return_and_reject_old_work(self):
        with self.app.app_context():
            for did in (self.department_id, self.other_department_id): db.session.get(Department, did).is_archived = True
            db.session.commit()
        self.ok(self.act("complete"))
        self.ok(self.act("return", actor="head"))
        self.ok(self.act("reject", actor="receiver-head"))

    def test_event_recipients_are_responsible_users_not_all_employees(self):
        self.ok(self.act("complete", actor="receiver-head"))
        self.assertEqual({self.ids[n] for n in ("employee", "head", "sender-head2", "receiver")}, self.recipients())
        self.ok(self.act("return", actor="head"))
        self.assertEqual({self.ids[n] for n in ("receiver", "receiver-head", "receiver-head2")}, self.recipients())
        self.ok(self.act("cancel", actor="head"))
        self.assertEqual({self.ids[n] for n in ("employee", "sender-head2", "receiver", "receiver-head", "receiver-head2")}, self.recipients())
        self.work = self.work_task()
        self.ok(self.act("reject", actor="receiver", comment="Не наш профиль"))
        self.assertEqual({self.ids[n] for n in ("employee", "head", "sender-head2", "receiver-head", "receiver-head2")}, self.recipients())
        with self.app.app_context():
            event = TaskEvent.query.order_by(TaskEvent.id.desc()).first()
            notice = TaskNotification.query.filter_by(user_id=self.ids["employee"], event_type="rejected").one()
            self.assertIn("Не наш профиль", notice.message)
            before = TaskNotification.query.count()
            from app.notifications import deliver_event_notifications
            self.assertEqual(0, deliver_event_notifications(db.session.get(Task, self.work), event))
            db.session.commit()
            self.assertEqual(before, TaskNotification.query.count())

    def test_files_are_comment_linked_and_all_side_effects_roll_back_together(self):
        Path(self.files.name, "existing").write_bytes(b"keep")
        pdf = attachment_bytes(".pdf")
        for action in ("complete", "return", "cancel", "reject"):
            self.work = self.work_task()
            actor = self.outcome(action)
            before = self.snapshot()
            existing = sorted(p.name for p in Path(self.files.name).iterdir())
            for where, error in (("app.audit.record_task_event", SQLAlchemyError("synthetic")),
                    ("app.workflow.deliver_event_notifications", SQLAlchemyError("synthetic")),
                    ("app.tasks.db.session.commit", SQLAlchemyError("synthetic")),
                    ("app.workflow._save_action_files", OSError("synthetic"))):
                with patch(where, side_effect=error):
                    self.assertEqual(500, self.act(action, actor=actor, extra={"attachment": (BytesIO(pdf), "x.pdf")}).status_code)
                self.assertEqual(before, self.snapshot())
                self.assertEqual(existing, sorted(p.name for p in Path(self.files.name).iterdir()))
            self.ok(self.act(action, actor=actor, extra={"attachment": (BytesIO(pdf), "x.pdf")}))
            with self.app.app_context():
                event = TaskEvent.query.order_by(TaskEvent.id.desc()).first()
                attachment = TaskAttachment.query.filter_by(comment_id=event.comment_id).one()
                self.assertEqual(self.work, attachment.task_id)
                self.assertEqual(pdf, Path(self.files.name, attachment.storage_key).read_bytes())
                attachment_id = attachment.id
            for role, expected in (("head", 200), ("receiver-head", 200), ("outsider", 404)):
                with self.client_for(role).get(f"/tasks/attachments/{attachment_id}/download") as response:
                    self.assertEqual(expected, response.status_code)
                    if expected == 200:
                        self.assertEqual(pdf, response.data)

    def test_invalid_content_and_csrf_do_not_change_task(self):
        for action in ("complete", "return", "cancel", "reject"):
            self.work = self.work_task()
            actor = self.outcome(action)
            before = self.snapshot()
            self.assertEqual(400, self.act(action, actor=actor, extra={"attachment": (BytesIO(b"not-pdf"), "x.pdf")}).status_code)
            self.assertEqual(before, self.snapshot())
            self.app.config["WTF_CSRF_ENABLED"] = True
            self.assertEqual(400, self.act(action, actor=actor).status_code)
            self.app.config["WTF_CSRF_ENABLED"] = False
            self.assertEqual(before, self.snapshot())

    def test_canonical_action_urls_and_readonly_closed_board(self):
        from flask import template_rendered
        contexts = []
        def capture(sender, template, context, **kw): contexts.append(context)
        with template_rendered.connected_to(capture, self.app):
            response = self.client_for("receiver").get(f"/tasks/{self.work}")
        self.assertIn(f'action="/tasks/{self.work}/complete"', response.get_data(as_text=True))
        self.assertNotIn("/review/", response.get_data(as_text=True))
        self.assertTrue({"complete", "reject"}.issubset(contexts[-1]["action_urls"]))
        self.ok(self.act("complete"))
        with template_rendered.connected_to(capture, self.app):
            response = self.client_for("employee").get(f"/tasks/{self.work}/panel")
        self.assertEqual({"return"}, set(contexts[-1]["action_urls"]))
        self.assertIn(f'/tasks/{self.work}#task-permitted-actions', response.get_data(as_text=True))
        detail = self.client_for("employee").get(f"/tasks/{self.work}")
        self.assertIn(f'action="/tasks/{self.work}/return"', detail.get_data(as_text=True))
        self.assertNotIn("обе проверки", response.get_data(as_text=True))
        with template_rendered.connected_to(capture, self.app):
            self.client_for("employee").get("/tasks/completed?scope=sent&view=board")
        self.assertEqual([], contexts[-1]["board_data"][self.work]["actions"])
        with template_rendered.connected_to(capture, self.app):
            self.client_for("root").get(f"/tasks/{self.work}")
        self.assertEqual([], contexts[-1]["task_actions"])


@unittest.skipUnless(os.environ.get("PORTAL_TEST_DATABASE_URL"), "Use isolated local_portal.py verify")
class PostgreSQLCompletion(MigrationDatabase, unittest.TestCase):
    postgres = True

    def setUp(self):
        super().setUp(); self.upgrade()
        depts = self.rows("departments"); self.ids = {}
        for name, role, dept in (("author", "employee", depts[0]["id"]), ("sender-head", "head", depts[0]["id"]),
                ("receiver", "employee", depts[1]["id"]), ("peer", "employee", depts[1]["id"]), ("head", "head", depts[1]["id"])):
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
                    method = w.transition_execution if action == "release" else w.transition_completion
                    method(task=task, actor=user, action=action, expected_revision=0, comment="Required")
                    db.session.commit(); return "ok"
                except w.WorkflowConflict:
                    db.session.rollback(); return "conflict"
                finally: db.session.remove()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [f.result(timeout=25) for f in [pool.submit(run, *op) for op in operations]]
        self.assertEqual(["conflict", "ok"], sorted(results))
        task = db.session.get(Task, self.ident)
        self.assertEqual(1, task.revision)
        self.assertEqual(1, TaskEvent.query.count()); self.assertEqual(1, TaskComment.query.count())
        return task

    def test_employee_and_head_complete_only_once(self):
        task = self.race((("receiver", "complete"), ("head", "complete")))
        self.assertEqual("completed", task.status)
        self.assertEqual(task.completed_at, task.closed_at)

    def test_completion_and_cancellation_cannot_overwrite_each_other(self):
        task = self.race((("receiver", "complete"), ("author", "cancel")))
        self.assertIn(task.status, ("completed", "cancelled"))
        self.assertIsNotNone(task.closed_at)
        self.assertEqual(task.status == "completed", task.completed_at is not None)

    def test_rejection_and_release_are_versioned_together(self):
        task = self.race((("head", "reject"), ("receiver", "release")))
        self.assertIn((task.status, task.assignee_id), (("new", None), ("rejected", self.ids["receiver"])))

    def test_two_returns_increment_cycle_only_once_and_clear_dates(self):
        task = db.session.get(Task, self.ident)
        task.status = "completed"; task.completed_at = task.closed_at = datetime.now(timezone.utc)
        db.session.commit(); db.session.remove()
        task = self.race((("author", "return"), ("sender-head", "return")))
        self.assertEqual(("in_progress", True, 1, None, None),
            (task.status, task.is_rework, task.rework_cycle, task.completed_at, task.closed_at))
        self.assertIsNotNone(TaskEvent.query.one().details["before"]["completed_at"])

    def test_native_rework_repair_and_repeat_completion(self):
        def act(actor, action):
            task = db.session.get(Task, self.ident)
            w.transition_completion(task=task, actor=db.session.get(User, self.ids[actor]),
                action=action, expected_revision=task.revision, comment="Required")
            db.session.commit()
        act("receiver", "complete")
        db.session.get(User, self.ids["receiver"]).is_blocked = True; db.session.commit()
        act("author", "return")
        task = db.session.get(Task, self.ident)
        w.reassign_task(task=task, actor=db.session.get(User, self.ids["head"]),
            assignee_ids=[self.ids["peer"]], expected_revision=task.revision, comment="Repair")
        db.session.commit(); act("peer", "complete")
        task = db.session.get(Task, self.ident)
        self.assertEqual(("completed", False, 1, 4), (task.status, task.is_rework, task.rework_cycle, task.revision))
        self.assertTrue(db.session.get(User, self.ids["receiver"]).is_blocked)
        self.assertEqual(["completed", "returned_for_revision", "reassigned", "completed"],
            [e.event_type for e in TaskEvent.query.order_by(TaskEvent.id)])
