"""Compatibility execution URLs never revive start/clarification/status stages."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import workflow as w
from app.extensions import db
from app.models import Task, TaskEvent
from support.portal_fixtures import PortalCase


class ExecutionCompatibilityTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.add_member("receiver",department_id=self.other_department_id)
        self.add_member("receiver-head",role="head",department_id=self.other_department_id)
        self.sent=self.task()
        self.client=self.app.test_client()
        with self.client.session_transaction() as session:session["_user_id"]=str(self.ids["receiver-head"])

    def test_assign_alias_starts_work_without_separate_start(self):
        r=self.client.post(f"/tasks/{self.sent}/execution/assign",data={"revision":"0","comment":"Назначаю","assignee_ids":str(self.ids["receiver"])})
        self.assertEqual(302,r.status_code)
        with self.app.app_context():
            self.assertEqual("in_progress",db.session.get(Task,self.sent).status)
        self.assertEqual(410,self.client.post(f"/tasks/{self.sent}/execution/start",data={"revision":"1","comment":"Start"}).status_code)

    def test_arbitrary_status_clarification_and_unsupported_aliases_stay_closed(self):
        for suffix,code in (("status",410),("clarification",410),("execution/request_clarification",410),
                            ("execution/answer_clarification",410),("execution/claim",400),("execution/release",400),("execution/complete",400)):
            self.assertEqual(code,self.client.post(f"/tasks/{self.sent}/{suffix}",data={"revision":"0","comment":"Try","status":"completed"}).status_code,suffix)
        with self.app.app_context():self.assertEqual(0,TaskEvent.query.count())

    def test_legacy_working_status_services_are_removed(self):
        with self.app.app_context():
            task=db.session.get(Task,self.sent)
            for service in (w.change_working_status,w.request_task_clarification,w.answer_task_clarification):
                with self.assertRaises(w.WorkflowRemoved):service(task=task,actor=task.author)
