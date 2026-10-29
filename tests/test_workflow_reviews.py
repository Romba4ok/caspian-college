"""Retired two-stage acceptance stays unavailable after immediate completion."""
from pathlib import Path
import sys
from unittest.mock import patch

from sqlalchemy import select, update

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.extensions import db
from app.models import Task, TaskComment, TaskEvent, TaskAttachment, TaskNotification, User
from app import workflow as w
from support.portal_fixtures import PortalCase


class RetiredReviewTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.add_member("receiver", department_id=self.other_department_id)
        self.add_member("receiver-head", role="head", department_id=self.other_department_id)
        self.work = self.task(status="in_progress", assignee_id=self.ids["receiver"])

    def client_for(self, name):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(self.ids[name]); session["_fresh"] = True
        return client

    def snapshot(self):
        with self.app.app_context():
            return {m.__tablename__: db.session.execute(select(m.__table__).order_by(m.id)).all()
                for m in (Task, TaskComment, TaskEvent, TaskAttachment, TaskNotification)}

    def test_old_review_urls_never_prepare_uploads_or_modify_any_stage(self):
        for state in ("new", "in_progress", "completed", "cancelled", "rejected"):
            with self.app.app_context():
                task = db.session.get(Task, self.work)
                task.assignee_ids = []; db.session.flush()
                task.status = state; task.assignee_id = None if state == "new" else self.ids["receiver"]
                db.session.commit()
            before = self.snapshot()
            with patch("app.tasks._prepare_uploads", side_effect=AssertionError("No legacy uploads")):
                for actor in ("employee", "head", "receiver", "receiver-head"):
                    for suffix in ("review/submit_result", "review/forward_result", "review/return_result",
                                   "review/complete", "review/cancel", "review/reject", "confirm"):
                        response = self.client_for(actor).post(f"/tasks/{self.work}/{suffix}",
                            data={"revision": "0", "comment": "Old form"})
                        self.assertEqual(410, response.status_code, (state, actor, suffix))
            self.assertEqual(before, self.snapshot())

    def test_old_review_services_cannot_install_files_or_mutate(self):
        class ForbiddenFile:
            def install(self): raise AssertionError("No legacy files")
        before = self.snapshot()
        with self.app.app_context():
            task = db.session.get(Task, self.work); actor = db.session.get(User, self.ids["receiver-head"])
            for method in (w.transition_review, w.submit_task_result, w.forward_task_result, w.confirm_task_result):
                with self.assertRaises(w.WorkflowRemoved):
                    method(task=task, actor=actor, expected_revision=0, comment="Old action", prepared_files=(ForbiddenFile(),))
        self.assertEqual(before, self.snapshot())

    def test_direct_services_recheck_stale_role_from_database(self):
        before = self.snapshot()
        with self.app.app_context():
            task = db.session.get(Task, self.work); actor = db.session.get(User, self.ids["receiver-head"])
            db.session.execute(update(User).where(User.id == actor.id).values(role="employee")
                .execution_options(synchronize_session=False))
            self.assertEqual("head", actor.role)
            with self.assertRaises(PermissionError):
                w.complete_task(task=task, actor=actor, expected_revision=0, comment="Result")
            db.session.rollback()
        self.assertEqual(before, self.snapshot())

    def test_author_must_still_belong_to_sender_department(self):
        self.assertEqual(302, self.client_for("receiver").post(f"/tasks/{self.work}/complete",
            data={"revision": "0", "comment": "Result"}).status_code)
        with self.app.app_context():
            db.session.get(User, self.ids["employee"]).department_id = self.other_department_id
            db.session.commit()
        before = self.snapshot()
        self.assertEqual(403, self.client_for("employee").post(f"/tasks/{self.work}/return",
            data={"revision": "1", "comment": "Remarks"}).status_code)
        with self.app.app_context():
            task = db.session.get(Task, self.work); actor = db.session.get(User, self.ids["employee"])
            with self.assertRaises(PermissionError):
                w.return_task_for_revision(task=task, actor=actor, expected_revision=1, comment="Remarks")
            db.session.rollback()
        self.assertEqual(before, self.snapshot())
