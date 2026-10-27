"""Execution stage uses fresh authority, one executor and explicit clarification."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from io import BytesIO
import re
import tempfile
from unittest.mock import patch

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models import Department, Task, TaskComment, TaskEvent, TaskAttachment, TaskNotification, User
from app.workflow import (transition_execution, assign_task, reassign_task, claim_task,
    change_working_status, request_task_clarification, answer_task_clarification, add_task_comment,
    WorkflowError, WorkflowConflict)
from support.portal_fixtures import PortalCase
from test_tasks import attachment_bytes


class ExecutionWorkflowTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory(prefix="portal_case_")
        self.app.config["TASK_UPLOAD_DIR"] = self.directory.name
        self.add_member("sender-peer")
        for name, role, blocked in (("receiver","employee",False),("peer","employee",False),
            ("receiver-head","head",False),("receiver-head-two","head",False),("blocked","employee",True)):
            self.add_member(name,role=role,department_id=self.other_department_id,blocked=blocked)
        self.sent = self.new_task()

    def tearDown(self):
        super().tearDown()
        self.directory.cleanup()

    def new_task(self, **changes):
        return self.task(**(dict(status="awaiting_assignment",sent_at=datetime.now(timezone.utc)) | changes))

    def login(self, username="root", password=None, **kwargs):
        self.client = self.app.test_client()
        response = super().login(username,password,**kwargs)
        self.assertEqual(200,response.status_code)
        return response

    def saved(self, task_id=None):
        with self.app.app_context():
            t=db.session.get(Task,task_id or self.sent)
            return {key:deepcopy(getattr(t,key)) for key in ("status","revision","assignee_id","due_at",
                "title","description","author_id","sender_department_id","recipient_department_id","sent_at",
                "priority","category","contact_person")}

    def snapshot(self):
        with self.app.app_context():
            return {m.__tablename__:deepcopy(db.session.execute(select(m.__table__).order_by(m.id)).all())
                for m in (Task,TaskComment,TaskEvent,TaskAttachment,TaskNotification)}

    def action(self, action, *, actor="receiver-head", task_id=None, assignee=None,
               comment="Пояснение действия", version=None, extra=None, alias=None):
        task_id=task_id or self.sent
        self.login(actor)
        data=dict(revision=str(self.saved(task_id)["revision"] if version is None else version),comment=comment)
        if assignee is not None:
            data["assignee_id"]=str(self.ids.get(assignee,assignee))
        if extra:
            data.update(extra)
        path=f"/tasks/{task_id}/{alias}" if alias else f"/tasks/{task_id}/execution/{action}"
        return self.client.post(path,data=data)

    def assign(self, **kwargs):
        response=self.action("assign",assignee=kwargs.pop("assignee","receiver"),**kwargs)
        self.assertEqual(302,response.status_code,response.get_data(as_text=True))

    def running(self):
        self.assign()
        self.assertEqual(302,self.action("start",actor="receiver").status_code)

    def test_any_recipient_head_assigns_one_user_and_start_is_separate(self):
        self.assign(actor="receiver-head-two")
        task=self.saved()
        self.assertEqual(("assigned",1,self.ids["receiver"]),tuple(task[k] for k in ("status","revision","assignee_id")))
        self.assertEqual(302,self.action("start",actor="receiver").status_code)
        self.assertEqual(("in_progress",2),tuple(self.saved()[k] for k in ("status","revision")))
        with self.app.app_context():
            event=TaskEvent.query.filter_by(task_id=self.sent,event_type="assigned").one()
            self.assertEqual(self.ids["receiver-head-two"],event.actor_id)
            self.assertEqual("head",event.actor_snapshot["role"])
            self.assertEqual(self.ids["receiver"],event.details["after"]["assignee_id"])
            self.assertIsNotNone(event.comment_id)
            self.assertEqual(2,TaskComment.query.filter_by(task_id=self.sent).count())

    def test_head_can_be_executor_without_self_claim_and_other_head_can_start(self):
        self.assign(assignee="receiver-head")
        self.assertEqual(302,self.action("start",actor="receiver-head-two").status_code)
        self.assertEqual(self.ids["receiver-head"],self.saved()["assignee_id"])

    def test_ordinary_employee_sender_and_admin_cannot_assign_or_self_claim(self):
        before=self.snapshot()
        for name in ("receiver","peer","employee","head","root"):
            self.assertEqual(403,self.action("assign",actor=name,assignee="receiver").status_code,name)
            self.assertEqual(403,self.action("claim",actor=name,alias="claim").status_code,name)
        self.assertEqual(before,self.snapshot())
        with self.app.app_context():
            with self.assertRaises(PermissionError):
                claim_task(task=db.session.get(Task,self.sent),actor=db.session.get(User,self.ids["receiver"]))
            db.session.rollback()
        self.assertEqual(before,self.snapshot())

    def test_invalid_blocked_other_department_admin_or_many_assignees_rejected(self):
        before=self.snapshot()
        for name in ("blocked","employee","outsider","root","99999","1,2","-1","9"*100):
            response=self.action("assign",assignee=name)
            self.assertEqual(400,response.status_code,name)
            self.assertEqual(before,self.snapshot())
        from werkzeug.datastructures import MultiDict
        self.login("receiver-head")
        data=MultiDict([("revision","0"),("comment","Назначить"),("assignee_id",str(self.ids["receiver"])),
            ("assignee_id",str(self.ids["peer"]))])
        self.assertEqual(400,self.client.post(f"/tasks/{self.sent}/assign",data=data).status_code)
        self.assertEqual(before,self.snapshot())

    def test_non_executor_cannot_work_but_any_recipient_head_can(self):
        self.assign()
        before=self.snapshot()
        for name in ("peer","employee","head","root"):
            self.assertEqual(403,self.action("start",actor=name).status_code,name)
        self.assertEqual(before,self.snapshot())
        self.assertEqual(302,self.action("start",actor="receiver-head-two").status_code)
        self.assertEqual(302,self.action("request_clarification",actor="receiver-head").status_code)
        self.assertEqual("awaiting_clarification",self.saved()["status"])

    def test_execution_does_not_skip_assignment_work_or_reviews(self):
        before=self.snapshot()
        for action in ("start","request_clarification","answer_clarification"):
            self.assertEqual(403,self.action(action).status_code)
        self.assertEqual(before,self.snapshot())
        self.assign()
        before=self.snapshot()
        self.assertEqual(403,self.action("request_clarification",actor="receiver").status_code)
        for target in ("completed","recipient_review","sender_acceptance","ready","assigned","draft"):
            self.assertEqual(400,self.action("start",actor="receiver",
                alias="status",extra={"status":target}).status_code,target)
        self.assertEqual(before,self.snapshot())

    def test_explicit_clarification_then_any_sender_member_answers(self):
        for name in ("employee","sender-peer","head"):
            task_id=self.new_task(status="in_progress",assignee_id=self.ids["receiver"])
            self.assertEqual(302,self.action("request_clarification",actor="receiver",task_id=task_id).status_code)
            self.assertEqual(302,self.action("answer_clarification",actor=name,task_id=task_id).status_code)
            self.assertEqual(("in_progress",self.ids["receiver"]),tuple(self.saved(task_id)[k] for k in ("status","assignee_id")))
            with self.app.app_context():
                event=TaskEvent.query.filter_by(task_id=task_id,event_type="clarification_answered").one()
                self.assertEqual(self.ids[name],event.actor_id)
                self.assertEqual("awaiting_clarification",event.details["before"]["status"])

    def test_recipient_cannot_answer_own_clarification_request(self):
        self.running()
        self.action("request_clarification",actor="receiver")
        before=self.snapshot()
        for name in ("receiver","peer","receiver-head","root"):
            self.assertEqual(403,self.action("answer_clarification",actor=name).status_code)
        self.assertEqual(before,self.snapshot())

    def test_ordinary_comment_does_not_answer_or_change_status(self):
        task_id=self.new_task(status="awaiting_clarification",assignee_id=self.ids["receiver"])
        with self.app.app_context():
            task=db.session.get(Task,task_id)
            entry=add_task_comment(task=task,actor=db.session.get(User,self.ids["employee"]),
                comment="Обычное обсуждение",expected_revision=0)
            db.session.commit()
            self.assertEqual("Обычное обсуждение",entry.body)
            self.assertEqual("awaiting_clarification",task.status)
            self.assertEqual(1,task.revision)
            self.assertEqual("comment_added",TaskEvent.query.filter_by(task_id=task_id).one().event_type)
        self.assertEqual(302,self.action("answer_clarification",actor="sender-peer",task_id=task_id).status_code)
        self.assertEqual("in_progress",self.saved(task_id)["status"])

    def test_reassign_preserves_status_deadline_all_materials_and_history(self):
        self.running()
        with self.app.app_context():
            body=attachment_bytes(".pdf")
            Path(self.directory.name,"b"*32).write_bytes(body)
            c=TaskComment(task_id=self.sent,author_id=self.ids["receiver"],body="Существующий материал")
            db.session.add(c);db.session.flush()
            f=TaskAttachment(task_id=self.sent,uploaded_by_id=self.ids["receiver"],comment_id=c.id,
                original_filename="saved.pdf",storage_key="b"*32,content_type="application/pdf",size_bytes=len(body))
            db.session.add(f);db.session.commit()
            previous_comments=[(r.id,r.body) for r in TaskComment.query.filter_by(task_id=self.sent).all()]
            previous_files=db.session.execute(select(TaskAttachment.__table__)).all()
            previous_events=db.session.execute(select(TaskEvent.__table__)).all()
        before=self.saved()
        self.assertEqual(302,self.action("reassign",actor="receiver-head-two",assignee="peer").status_code)
        after=self.saved()
        self.assertEqual(self.ids["peer"],after["assignee_id"])
        for field in before.keys()-{"assignee_id","revision"}:
            self.assertEqual(before[field],after[field],field)
        with self.app.app_context():
            self.assertEqual(previous_files,db.session.execute(select(TaskAttachment.__table__)).all())
            for row in previous_comments:
                self.assertIn(row,[(r.id,r.body) for r in TaskComment.query.filter_by(task_id=self.sent).all()])
            self.assertEqual(previous_events,db.session.execute(select(TaskEvent.__table__).where(TaskEvent.event_type!="reassigned")).all())
        self.assertEqual(body,Path(self.directory.name,"b"*32).read_bytes())
        self.assertEqual(403,self.action("request_clarification",actor="receiver").status_code)
        self.assertEqual(302,self.action("request_clarification",actor="peer").status_code)

    def test_reassignment_never_starts_resets_or_closes_a_stage(self):
        for status in ("assigned","in_progress","awaiting_clarification","recipient_review"):
            task_id=self.new_task(status=status,assignee_id=self.ids["receiver"])
            self.assertEqual(302,self.action("reassign",task_id=task_id,assignee="peer").status_code,status)
            self.assertEqual(status,self.saved(task_id)["status"])
        task_id=self.new_task(status="sender_acceptance",assignee_id=self.ids["receiver"])
        before=self.snapshot()
        self.assertEqual(403,self.action("reassign",task_id=task_id,assignee="peer").status_code)
        self.assertEqual(before,self.snapshot())

    def test_employee_cannot_reassign_and_same_assignee_is_not_an_action(self):
        self.assign()
        before=self.snapshot()
        for name in ("receiver","peer","employee","head","root"):
            self.assertEqual(403,self.action("reassign",actor=name,assignee="peer").status_code,name)
        self.assertEqual(400,self.action("reassign",assignee="receiver").status_code)
        self.assertEqual(before,self.snapshot())

    def test_unavailable_assignee_blocks_work_and_head_can_repair(self):
        for changes in ({"is_blocked":True},{"department_id":self.third_department_id}):
            task_id=self.new_task(status="assigned",assignee_id=self.ids["receiver"])
            with self.app.app_context():
                user=db.session.get(User,self.ids["receiver"])
                for key,value in changes.items():setattr(user,key,value)
                db.session.commit()
            before=self.snapshot()
            self.assertEqual(403,self.action("start",task_id=task_id).status_code)
            self.assertEqual(before,self.snapshot())
            self.login("receiver-head")
            self.assertIn("Исполнитель недоступен",self.client.get(f"/tasks/{task_id}").get_data(as_text=True))
            self.assertEqual(302,self.action("reassign",task_id=task_id,assignee="peer").status_code)
            self.assertEqual(302,self.action("start",task_id=task_id,actor="peer").status_code)
            with self.app.app_context():
                user=db.session.get(User,self.ids["receiver"])
                user.is_blocked=False;user.department_id=self.other_department_id
                db.session.commit()

    def test_null_historical_assignee_can_be_reassigned_before_work_resumes(self):
        task_id=self.new_task(status="in_progress",assignee_id=None)
        self.login("receiver-head")
        self.assertIn("Исполнитель недоступен",self.client.get(f"/tasks/{task_id}").get_data(as_text=True))
        self.assertEqual(403,self.action("request_clarification",task_id=task_id).status_code)
        self.assertEqual(302,self.action("reassign",task_id=task_id,assignee="peer").status_code)
        self.assertEqual("in_progress",self.saved(task_id)["status"])

    def test_archived_departments_finish_sent_work_without_new_sending(self):
        with self.app.app_context():
            for dep_id in (self.department_id,self.other_department_id):
                db.session.get(Department,dep_id).is_archived=True
            db.session.commit()
        self.assign()
        self.assertEqual(302,self.action("start",actor="receiver").status_code)
        self.assertEqual(302,self.action("reassign",assignee="peer").status_code)
        self.assertEqual(302,self.action("request_clarification",actor="peer").status_code)
        self.assertEqual(302,self.action("answer_clarification",actor="sender-peer").status_code)

    def test_legacy_without_deadline_cannot_assign_reassign_start_or_clarify(self):
        for state,action,assignee in (("awaiting_assignment","assign","receiver"),
            ("assigned","start",None),("in_progress","request_clarification",None),
            ("assigned","reassign","peer"),("awaiting_clarification","answer_clarification",None)):
            task_id=self.new_task(status=state,due_at=None,legacy_missing_due=True,is_legacy_dispatched=True,
                assignee_id=None if state=="awaiting_assignment" else self.ids["receiver"])
            before=self.snapshot()
            actor="employee" if action=="answer_clarification" else "receiver-head"
            self.assertEqual(403,self.action(action,task_id=task_id,actor=actor,assignee=assignee).status_code)
            self.assertEqual(before,self.snapshot())
        self.login("receiver-head")
        self.assertIn("У исторической задачи не указан срок",self.client.get(f"/tasks/{task_id}").get_data(as_text=True))

    def test_terminal_tasks_and_unsent_drafts_never_allow_execution(self):
        for status in ("completed","cancelled","rejected","draft","pending_approval"):
            task_id=self.new_task(status=status)
            before=self.snapshot()
            for action in ("start","request_clarification","answer_clarification"):
                actor="employee" if action=="answer_clarification" else "receiver-head"
                expected=403 if status not in ("draft","pending_approval") or actor=="employee" else 404
                self.assertEqual(expected,self.action(action,actor=actor,task_id=task_id).status_code,(status,action))
            self.assertEqual(before,self.snapshot())

    def test_all_execution_changes_require_comment_and_revision(self):
        cases=[("assign","awaiting_assignment","receiver-head","receiver"),("reassign","assigned","receiver-head","peer"),
            ("start","assigned","receiver",None),("request_clarification","in_progress","receiver",None),
            ("answer_clarification","awaiting_clarification","employee",None)]
        for action,status,actor,assignee in cases:
            task_id=self.new_task(status=status,assignee_id=None if status=="awaiting_assignment" else self.ids["receiver"])
            before=self.snapshot()
            self.assertEqual(400,self.action(action,actor=actor,task_id=task_id,assignee=assignee,comment=" \n ").status_code,action)
            self.assertEqual(400,self.action(action,actor=actor,task_id=task_id,assignee=assignee,version="bad").status_code,action)
            self.assertEqual(before,self.snapshot())

    def test_stale_and_duplicate_actions_create_no_second_assignment_or_history(self):
        self.assign(version=0)
        before=self.snapshot()
        self.assertEqual(409,self.action("assign",actor="receiver-head-two",assignee="peer",version=0).status_code)
        self.assertEqual(before,self.snapshot())
        self.assertEqual(302,self.action("start",actor="receiver",version=1).status_code)
        before=self.snapshot()
        self.assertEqual(409,self.action("start",actor="receiver-head",version=1).status_code)
        self.assertEqual(before,self.snapshot())

    def test_atomic_event_failure_rolls_back_status_and_comment(self):
        before=self.snapshot()
        self.login("receiver-head")
        with patch("app.audit.TaskEvent",side_effect=SQLAlchemyError("synthetic private detail")):
            response=self.client.post(f"/tasks/{self.sent}/assign",
                data={"assignee_id":str(self.ids["receiver"]),"revision":"0","comment":"Назначить"})
        self.assertEqual(500,response.status_code)
        self.assertNotIn("synthetic private detail",response.get_data(as_text=True))
        self.assertEqual(before,self.snapshot())

    def test_candidate_and_actor_are_reloaded_under_locks_even_with_stale_objects(self):
        with self.app.app_context():
            actor=db.session.get(User,self.ids["receiver-head"])
            candidate=db.session.get(User,self.ids["receiver"])
            task=db.session.get(Task,self.sent)
            db.session.execute(update(User).where(User.id==candidate.id).values(is_blocked=True)
                .execution_options(synchronize_session=False))
            with self.assertRaises(WorkflowError):
                assign_task(task=task,actor=actor,assignee_id=candidate.id,expected_revision=0,comment="Не назначить")
            db.session.rollback()
            db.session.execute(update(User).where(User.id==actor.id).values(role="employee")
                .execution_options(synchronize_session=False))
            with self.assertRaises(PermissionError):
                assign_task(task=task,actor=actor,assignee_id=candidate.id,expected_revision=0,comment="Старая роль")
            db.session.rollback()
        self.assertEqual("awaiting_assignment",self.saved()["status"])

    def test_http_forgery_and_foreign_task_are_rejected_before_changes(self):
        before=self.snapshot()
        for extra in ({"status":"completed"},{"author_id":"1"},{"sender_department_id":"1"},
            {"revision":["0","0"]},{"comment":["a","b"]}):
            self.assertEqual(400,self.action("assign",assignee="receiver",extra=extra).status_code,extra)
        self.assertEqual(404,self.action("assign",actor="outsider",assignee="receiver").status_code)
        self.assertEqual(403,self.action("assign",actor="root",assignee="receiver").status_code)
        self.assertEqual(before,self.snapshot())

    def test_legacy_status_and_clarification_aliases_have_same_guards(self):
        self.assign(alias="assign")
        self.assertEqual(302,self.action("start",actor="receiver",alias="status",extra={"status":"in_progress"}).status_code)
        self.assertEqual(302,self.action("request_clarification",actor="receiver",
            alias="status",extra={"status":"awaiting_clarification"}).status_code)
        before=self.snapshot()
        self.assertEqual(403,self.action("start",actor="receiver",alias="status",extra={"status":"in_progress"}).status_code)
        self.assertEqual(before,self.snapshot())
        self.assertEqual(302,self.action("answer_clarification",actor="employee",alias="clarification").status_code)
        self.assertEqual("in_progress",self.saved()["status"])

    def test_forms_show_only_role_actions_and_only_active_recipient_candidates(self):
        self.login("receiver-head")
        page=self.client.get(f"/tasks/{self.sent}").get_data(as_text=True)
        options=re.search(r'<select id="execution-assignee-assign".*?</select>',page,re.S).group()
        for name in ("receiver-head","receiver","peer"):
            self.assertIn(name,options)
        for name in ("blocked","employee","outsider","root"):
            self.assertNotIn('>'+name+' ·',options)
        self.login("receiver")
        page=self.client.get(f"/tasks/{self.sent}").get_data(as_text=True)
        self.assertNotIn("Назначить исполнителя",page)
        self.assertNotIn("Взять задачу",page)
        self.assign()
        self.login("peer")
        self.assertNotIn("Начать работу",self.client.get(f"/tasks/{self.sent}").get_data(as_text=True))
        self.login("receiver")
        self.assertIn("Начать работу",self.client.get(f"/tasks/{self.sent}").get_data(as_text=True))

    def test_execution_is_post_only_and_csrf_protected(self):
        self.login("receiver-head")
        self.app.config["WTF_CSRF_ENABLED"]=True
        page=self.client.get(f"/tasks/{self.sent}").get_data(as_text=True)
        before=self.snapshot()
        self.assertEqual(405,self.client.get(f"/tasks/{self.sent}/execution/assign").status_code)
        data={"revision":"0","assignee_id":str(self.ids["receiver"]),"comment":"Назначить"}
        self.assertEqual(400,self.client.post(f"/tasks/{self.sent}/execution/assign",data=data).status_code)
        self.assertEqual(before,self.snapshot())
        data["csrf_token"]=re.search(r'name="csrf_token" value="([^"]+)"',page).group(1)
        self.assertEqual(302,self.client.post(f"/tasks/{self.sent}/execution/assign",data=data).status_code)

