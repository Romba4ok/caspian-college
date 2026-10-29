"""Removed approval/edit/start paths cannot bypass immediate dispatch."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.extensions import db
from app.models import Task, TaskComment, TaskEvent, TaskAttachment, TaskNotification
from app import workflow as w
from support.portal_fixtures import PortalCase


class RetiredApprovalTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.add_member("receiver", department_id=self.other_department_id)
        with self.client.session_transaction() as session:
            session["_user_id"] = str(self.ids["employee"])
            session["_fresh"] = True
        response = self.client.post("/tasks/new", data=self.creation_form())
        self.assertEqual(302, response.status_code)
        self.ident = int(response.location.rsplit("/",1)[1])

    def snapshot(self):
        with self.app.app_context():
            return [(m.__tablename__, m.query.count()) for m in
                    (Task, TaskComment, TaskEvent, TaskAttachment, TaskNotification)], db.session.get(Task,self.ident).revision

    def test_every_approval_and_edit_url_is_retired_after_real_dispatch(self):
        before = self.snapshot()
        for action in ("submit", "withdraw", "return_draft", "approve", "send"):
            self.assertEqual(410, self.client.post(f"/tasks/{self.ident}/approval/{action}",
                data={"comment":"Try again", "revision":"1"}).status_code)
        for method in (self.client.get,self.client.post):
            self.assertEqual(410, method(f"/tasks/{self.ident}/edit").status_code)
        self.assertEqual(before,self.snapshot())

    def test_old_status_review_and_confirm_cannot_close_directly_sent_task(self):
        before = self.snapshot()
        for suffix in ("status", "confirm", "clarification", "execution/start", "review/complete", "review/forward_result"):
            self.assertEqual(410,self.client.post(f"/tasks/{self.ident}/{suffix}",
                data={"comment":"Try", "revision":"1", "status":"completed"}).status_code)
        self.assertEqual(before,self.snapshot())

    def test_direct_compatibility_edit_and_approval_services_remain_removed(self):
        with self.app.app_context():
            task=db.session.get(Task,self.ident)
            for method in (w.update_draft,w.update_task_before_acceptance,w.transition_approval,
                           w.forward_task_result,w.confirm_task_result,w.change_working_status,w.answer_task_clarification):
                with self.assertRaises(w.WorkflowRemoved):method(task=task,actor=task.author)
            self.assertEqual("new",task.status)
            self.assertEqual(1,task.revision)
            self.assertEqual(1,TaskEvent.query.count())

    def test_ordinary_creation_form_cannot_change_an_existing_task(self):
        form=self.creation_form()
        form.update(task_id=str(self.ident),title="Overwrite")
        self.assertEqual(400,self.client.post("/tasks/new",data=form).status_code)
        with self.app.app_context():self.assertNotEqual("Overwrite",db.session.get(Task,self.ident).title)
