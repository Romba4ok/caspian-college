"""Both result reviews, immutable closure and mandatory versioned deadline changes."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import re
import unittest
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models import Department, Task, TaskComment, TaskEvent, TaskAttachment, TaskNotification, User
from app.workflow import (transition_review, add_task_comment, confirm_task_result, return_task_for_revision,
                          cancel_task, reject_task, change_task_deadline, WorkflowError)
from support.portal_fixtures import PortalCase


class ReviewWorkflowTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.add_member("sender-head-two",role="head")
        self.add_member("sender-peer")
        for name,role in (("receiver","employee"),("receiver-peer","employee"),
                          ("receiver-head","head"),("receiver-head-two","head")):
            self.add_member(name,role=role,department_id=self.other_department_id)
        self.work=self.new_task()

    def new_task(self, **changes):
        return self.task(**(dict(status="in_progress",assignee_id=self.ids["receiver"],
            sent_at=datetime.now(timezone.utc)) | changes))

    def login(self, username="root", password=None, **kwargs):
        self.client=self.app.test_client()
        response=super().login(username,password,**kwargs)
        self.assertEqual(200,response.status_code)
        return response

    def saved(self, task_id=None):
        with self.app.app_context():
            task=db.session.get(Task,task_id or self.work)
            return {key:deepcopy(getattr(task,key)) for key in ("status","revision","assignee_id",
                "due_at","completed_at","closed_at","sent_at","legacy_missing_due","title","description",
                "author_id","sender_department_id","recipient_department_id","priority","category")}

    def snapshot(self):
        with self.app.app_context():
            return {m.__tablename__:deepcopy(db.session.execute(select(m.__table__).order_by(m.id)).all())
                for m in (Task,TaskComment,TaskEvent,TaskAttachment,TaskNotification)}

    def action(self, action, *, actor="receiver", task_id=None, version=None, comment="Обоснование действия",
               extra=None, alias=None):
        task_id=task_id or self.work
        self.login(actor)
        data=dict(revision=str(self.saved(task_id)["revision"] if version is None else version),comment=comment)
        if extra:data.update(extra)
        path=f"/tasks/{task_id}/{alias}" if alias else f"/tasks/{task_id}/review/{action}"
        return self.client.post(path,data=data)

    def accept_stage(self):
        self.assertEqual(302,self.action("submit_result").status_code)
        self.assertEqual(302,self.action("forward_result",actor="receiver-head-two").status_code)

    def test_two_reviews_and_completion_dates_only_on_final_acceptance(self):
        self.assertEqual(302,self.action("submit_result",comment="Результат готов").status_code)
        task=self.saved()
        self.assertEqual("recipient_review",task["status"])
        self.assertIsNone(task["completed_at"]);self.assertIsNone(task["closed_at"])
        self.assertEqual(302,self.action("forward_result",actor="receiver-head-two").status_code)
        task=self.saved()
        self.assertEqual("sender_acceptance",task["status"])
        self.assertIsNone(task["completed_at"]);self.assertIsNone(task["closed_at"])
        self.assertEqual(302,self.action("complete",actor="sender-head-two").status_code)
        task=self.saved()
        self.assertEqual(("completed",3),tuple(task[k] for k in ("status","revision")))
        self.assertIsNotNone(task["completed_at"])
        self.assertEqual(task["completed_at"],task["closed_at"])
        with self.app.app_context():
            events=TaskEvent.query.filter_by(task_id=self.work).order_by(TaskEvent.id).all()
            self.assertEqual(["result_submitted","result_forwarded","completed"],[e.event_type for e in events])
            self.assertEqual(["employee","head","head"],[e.actor_snapshot["role"] for e in events])
            self.assertEqual(3,len({e.comment_id for e in events}))

    def test_head_executor_must_still_submit_then_separately_check(self):
        task_id=self.new_task(assignee_id=self.ids["receiver-head"])
        before=self.snapshot()
        self.assertEqual(403,self.action("forward_result",actor="receiver-head",task_id=task_id).status_code)
        self.assertEqual(before,self.snapshot())
        self.assertEqual(302,self.action("submit_result",actor="receiver-head",task_id=task_id).status_code)
        self.assertEqual("recipient_review",self.saved(task_id)["status"])
        self.assertEqual(302,self.action("forward_result",actor="receiver-head",task_id=task_id).status_code)
        self.assertEqual("sender_acceptance",self.saved(task_id)["status"])

    def test_no_skipping_or_finishing_by_employee_recipient_head_or_admin(self):
        before=self.snapshot()
        for action,actor in (("complete","head"),("forward_result","receiver-head"),("submit_result","receiver-peer"),
                              ("submit_result","employee"),("submit_result","root")):
            self.assertEqual(403,self.action(action,actor=actor).status_code,(action,actor))
        self.assertEqual(before,self.snapshot())
        self.accept_stage()
        before=self.snapshot()
        for actor in ("employee","sender-peer","receiver","receiver-head","root"):
            self.assertEqual(403,self.action("complete",actor=actor).status_code,actor)
        self.assertEqual(before,self.snapshot())

    def test_both_returns_restore_same_executor_and_require_both_reviews_again(self):
        for stage,actor in (("recipient_review","receiver-head"),("sender_acceptance","head")):
            task_id=self.new_task(status=stage)
            deadline=self.saved(task_id)["due_at"]
            self.assertEqual(302,self.action("return_result",actor=actor,task_id=task_id).status_code)
            task=self.saved(task_id)
            self.assertEqual(("in_progress",self.ids["receiver"],deadline),
                tuple(task[k] for k in ("status","assignee_id","due_at")))
            self.assertIsNone(task["completed_at"]);self.assertIsNone(task["closed_at"])
            self.assertEqual(403,self.action("complete",actor="head",task_id=task_id).status_code)
            self.assertEqual(302,self.action("submit_result",task_id=task_id).status_code)
            self.assertEqual(403,self.action("complete",actor="head",task_id=task_id).status_code)
            self.assertEqual(302,self.action("forward_result",actor="receiver-head-two",task_id=task_id).status_code)
            self.assertEqual(302,self.action("complete",actor="head",task_id=task_id).status_code)

    def test_return_unavailable_or_missing_executor_does_not_restore_work_rights(self):
        for stage,actor in (("recipient_review","receiver-head"),("sender_acceptance","head")):
            for missing in (False,True):
                task_id=self.new_task(status=stage,assignee_id=None if missing else self.ids["receiver"])
                if not missing:
                    with self.app.app_context():
                        db.session.get(User,self.ids["receiver"]).is_blocked=True
                        db.session.commit()
                self.assertEqual(302,self.action("return_result",actor=actor,task_id=task_id).status_code)
                self.assertEqual("in_progress",self.saved(task_id)["status"])
                self.login("receiver-head")
                self.assertIn("Исполнитель недоступен",self.client.get(f"/tasks/{task_id}").get_data(as_text=True))
                before=self.snapshot()
                self.assertEqual(403,self.action("submit_result",actor="receiver-head",task_id=task_id).status_code)
                self.assertEqual(before,self.snapshot())
                self.assertEqual(302,self.client.post(f"/tasks/{task_id}/execution/reassign",
                    data={"revision":str(self.saved(task_id)["revision"]),"comment":"Назначаю замену",
                          "assignee_id":str(self.ids["receiver-peer"])}).status_code)
                with self.app.app_context():
                    db.session.get(User,self.ids["receiver"]).is_blocked=False
                    db.session.commit()

    def test_cancel_before_dispatch_by_author_or_sender_head_preserves_privacy(self):
        for status,actor in (("draft","employee"),("pending_approval","employee"),("draft","sender-head-two"),
                             ("pending_approval","head")):
            task_id=self.task(status=status)
            self.assertEqual(302,self.action("cancel",actor=actor,task_id=task_id).status_code)
            task=self.saved(task_id)
            self.assertEqual("cancelled",task["status"])
            self.assertIsNotNone(task["closed_at"]);self.assertIsNone(task["completed_at"]);self.assertIsNone(task["sent_at"])
            self.login("receiver")
            self.assertEqual(404,self.client.get(f"/tasks/{task_id}").status_code)
        task_id=self.task()
        before=self.snapshot()
        self.assertEqual(403,self.action("cancel",actor="sender-peer",task_id=task_id).status_code)
        self.assertEqual(before,self.snapshot())

    def test_sent_cancellation_only_sender_head_at_all_active_stages(self):
        for status in ("awaiting_assignment","assigned","in_progress","awaiting_clarification","recipient_review","sender_acceptance"):
            task_id=self.new_task(status=status)
            before=self.snapshot()
            for name in ("employee","sender-peer","receiver-head","receiver","root"):
                self.assertEqual(403,self.action("cancel",actor=name,task_id=task_id).status_code,(status,name))
            self.assertEqual(before,self.snapshot())
            self.assertEqual(302,self.action("cancel",actor="sender-head-two",task_id=task_id).status_code)
            self.assertEqual("cancelled",self.saved(task_id)["status"])
            self.assertIsNone(self.saved(task_id)["completed_at"])

    def test_rejection_is_separate_final_state_only_before_work_and_recipient_head(self):
        for status in ("awaiting_assignment","assigned"):
            task_id=self.new_task(status=status)
            before=self.snapshot()
            for name in ("receiver","employee","head","root"):
                self.assertEqual(403,self.action("reject",actor=name,task_id=task_id).status_code)
            self.assertEqual(before,self.snapshot())
            self.assertEqual(302,self.action("reject",actor="receiver-head-two",task_id=task_id,
                comment="Отдел не выполняет такие работы").status_code)
            task=self.saved(task_id)
            self.assertEqual("rejected",task["status"])
            self.assertIsNone(task["completed_at"]);self.assertIsNotNone(task["closed_at"])
            with self.app.app_context():
                event=TaskEvent.query.filter_by(task_id=task_id,event_type="rejected").one()
                self.assertEqual("Отдел не выполняет такие работы",event.details["comment"])
        before=self.snapshot()
        self.assertEqual(403,self.action("reject",actor="receiver-head").status_code)
        self.assertEqual(before,self.snapshot())

    def test_deadline_changes_only_sender_head_with_old_and_new_values(self):
        before=self.snapshot()
        for name in ("employee","receiver","receiver-head","root"):
            self.assertEqual(403,self.action("deadline",actor=name,extra={"due_at":"2031-01-01T12:00"}).status_code)
        self.assertEqual(before,self.snapshot())
        self.assertEqual(302,self.action("deadline",actor="sender-head-two",
            extra={"due_at":"2031-01-01T12:00"},comment="Согласован перенос").status_code)
        task=self.saved()
        self.assertEqual("in_progress",task["status"])
        self.assertEqual(self.ids["receiver"],task["assignee_id"])
        self.assertEqual(datetime(2031,1,1,7,0),task["due_at"].replace(tzinfo=None))
        with self.app.app_context():
            event=TaskEvent.query.filter_by(task_id=self.work,event_type="deadline_changed").one()
            self.assertTrue(event.details["before"]["due_at"].startswith("2030-01-01"))
            self.assertTrue(event.details["after"]["due_at"].startswith("2031-01-01"))
            self.assertEqual("Согласован перенос",event.comment.body)

    def test_deadline_cannot_be_removed_or_unchanged_and_draft_uses_edit(self):
        before=self.snapshot()
        for date in ("","bad","2030-01-01T05:00"):
            self.assertEqual(400,self.action("deadline",actor="head",extra={"due_at":date}).status_code,date)
        self.assertEqual(before,self.snapshot())
        draft=self.task()
        before=self.snapshot()
        self.assertEqual(403,self.action("deadline",actor="employee",task_id=draft,
            extra={"due_at":"2031-01-01T12:00"}).status_code)
        self.assertEqual(before,self.snapshot())

    def test_historical_missing_deadline_allows_cancel_or_repair_but_not_review_or_reject(self):
        for stage,action,actor in (("in_progress","submit_result","receiver"),("recipient_review","forward_result","receiver-head"),
            ("sender_acceptance","complete","head"),("assigned","reject","receiver-head")):
            task_id=self.new_task(status=stage,due_at=None,legacy_missing_due=True,is_legacy_dispatched=True)
            before=self.snapshot()
            self.assertEqual(403,self.action(action,actor=actor,task_id=task_id).status_code)
            self.assertEqual(before,self.snapshot())
            self.assertEqual(302,self.action("deadline",actor="head",task_id=task_id,
                extra={"due_at":"2031-01-01T12:00"}).status_code)
            self.assertFalse(self.saved(task_id)["legacy_missing_due"])
            self.assertEqual(302,self.action(action,actor=actor,task_id=task_id).status_code)
        task_id=self.new_task(due_at=None,legacy_missing_due=True,is_legacy_dispatched=True)
        self.assertEqual(302,self.action("cancel",actor="head",task_id=task_id).status_code)

    def test_archived_departments_can_review_return_finish_cancel_and_reject(self):
        with self.app.app_context():
            for ident in (self.department_id,self.other_department_id):
                db.session.get(Department,ident).is_archived=True
            db.session.commit()
        self.accept_stage()
        self.assertEqual(302,self.action("return_result",actor="head").status_code)
        self.accept_stage()
        self.assertEqual(302,self.action("complete",actor="head").status_code)
        task_id=self.new_task(status="assigned")
        self.assertEqual(302,self.action("reject",actor="receiver-head",task_id=task_id).status_code)
        task_id=self.task(status="draft")
        self.assertEqual(302,self.action("cancel",actor="employee",task_id=task_id).status_code)

    def test_every_review_action_requires_comment_and_revision_without_partial_writes(self):
        cases=[("submit_result","in_progress","receiver"),("forward_result","recipient_review","receiver-head"),
            ("return_result","recipient_review","receiver-head"),("return_result","sender_acceptance","head"),
            ("complete","sender_acceptance","head"),("cancel","in_progress","head"),
            ("reject","assigned","receiver-head"),("deadline","in_progress","head")]
        for action,status,actor in cases:
            task_id=self.new_task(status=status)
            extra={"due_at":"2031-01-01T12:00"} if action=="deadline" else None
            before=self.snapshot()
            self.assertEqual(400,self.action(action,actor=actor,task_id=task_id,comment=" \n ",extra=extra).status_code,action)
            self.assertEqual(400,self.action(action,actor=actor,task_id=task_id,version="bad",extra=extra).status_code,action)
            self.assertEqual(before,self.snapshot())

    def test_repeated_completion_rejection_or_deadline_changes_are_conflicts(self):
        for action,status,actor in (("complete","sender_acceptance","head"),("reject","assigned","receiver-head"),
                                    ("cancel","in_progress","head"),("deadline","in_progress","head")):
            task_id=self.new_task(status=status)
            extra={"due_at":"2031-01-01T12:00"} if action=="deadline" else None
            self.assertEqual(302,self.action(action,actor=actor,task_id=task_id,version=0,extra=extra).status_code)
            before=self.snapshot()
            self.assertEqual(409,self.action(action,actor=actor,task_id=task_id,version=0,extra=extra).status_code)
            self.assertEqual(before,self.snapshot())

    def test_final_states_are_readonly_in_http_and_comment_service(self):
        for status in ("completed","cancelled","rejected"):
            task_id=self.new_task(status=status)
            before=self.snapshot()
            for action,actor in (("submit_result","receiver"),("forward_result","receiver-head"),("complete","head"),
                ("return_result","head"),("cancel","head"),("reject","receiver-head"),("deadline","head")):
                self.assertEqual(403,self.action(action,actor=actor,task_id=task_id).status_code,(status,action))
            for suffix in ("comments","attachments","edit"):
                self.assertEqual(403,self.client.post(f"/tasks/{task_id}/{suffix}",data={"comment":"Не менять"}).status_code)
            with self.app.app_context():
                with self.assertRaises(PermissionError):
                    add_task_comment(task=db.session.get(Task,task_id),actor=db.session.get(User,self.ids["employee"]),
                        expected_revision=0,comment="Нельзя")
                db.session.rollback()
            self.assertEqual(before,self.snapshot())

    def test_stale_acceptance_after_return_cannot_close_task(self):
        self.accept_stage()
        version=self.saved()["revision"]
        self.assertEqual(302,self.action("return_result",actor="head",version=version).status_code)
        before=self.snapshot()
        self.assertEqual(409,self.action("complete",actor="sender-head-two",version=version).status_code)
        self.assertEqual(before,self.snapshot())

    def test_direct_services_and_legacy_urls_have_identical_new_rules(self):
        self.accept_stage()
        self.login("head")
        self.assertEqual(302,self.client.post(f"/tasks/{self.work}/confirm",
            data={"revision":"2","comment":"Принимаю результат"}).status_code)
        task_id=self.new_task(status="sender_acceptance")
        self.assertEqual(302,self.action("return_result",actor="head",task_id=task_id,alias="return").status_code)
        task_id=self.new_task(status="assigned")
        self.assertEqual(302,self.action("reject",actor="receiver-head",task_id=task_id,alias="reject").status_code)
        task_id=self.new_task()
        self.login("head")
        self.assertEqual(302,self.client.post(f"/tasks/{task_id}/cancel",
            data={"revision":"0","reason":"Поручение потеряло актуальность"}).status_code)
        task_id=self.new_task()
        self.assertEqual(302,self.action("deadline",actor="head",task_id=task_id,alias="deadline",
            extra={"due_at":"2031-01-01T12:00"}).status_code)
        with self.app.app_context():
            with self.assertRaises(WorkflowError):
                confirm_task_result(task=db.session.get(Task,task_id),actor=db.session.get(User,self.ids["head"]))
            db.session.rollback()

    def test_atomic_failure_after_comment_insert_rolls_back_entire_result_action(self):
        task_id=self.new_task(status="sender_acceptance")
        self.login("head")
        before=self.snapshot()
        with patch("app.audit.TaskEvent",side_effect=SQLAlchemyError("synthetic private failure")):
            response=self.client.post(f"/tasks/{task_id}/review/complete",data={"revision":"0","comment":"Принимаю"})
        self.assertEqual(500,response.status_code)
        self.assertNotIn("synthetic private failure",response.get_data(as_text=True))
        self.assertEqual(before,self.snapshot())

    def test_foreign_ids_forged_or_duplicate_payload_and_admin_mutations_denied(self):
        before=self.snapshot()
        self.assertEqual(404,self.action("cancel",actor="outsider").status_code)
        self.assertEqual(403,self.action("cancel",actor="root").status_code)
        for extra in ({"status":"completed"},{"completed_at":"2020-01-01"},{"assignee_id":"1"},
                      {"comment":["a","b"]},{"revision":["0","0"]}):
            self.assertEqual(400,self.action("submit_result",extra=extra).status_code,extra)
        self.assertEqual(before,self.snapshot())

    def test_closed_card_displays_correct_date_and_historical_unknowns(self):
        self.accept_stage()
        self.action("complete",actor="head")
        self.login("employee")
        page=self.client.get(f"/tasks/{self.work}").get_data(as_text=True)
        self.assertIn("Завершена:",page)
        self.assertIn("Отправлена:",page)
        self.assertNotIn("review-actions-heading",page)
        task_id=self.new_task(status="rejected",sent_at=None,is_legacy_dispatched=True)
        page=self.client.get(f"/tasks/{task_id}").get_data(as_text=True)
        self.assertIn("Отклонена: дата и время неизвестны",page)
        self.assertIn("точная дата неизвестна",page)
        self.assertNotIn("Завершена:",page)

    def test_post_only_csrf_and_role_specific_review_forms(self):
        self.login("receiver")
        page=self.client.get(f"/tasks/{self.work}").get_data(as_text=True)
        self.assertIn("Передать на проверку",page)
        self.assertNotIn("Принять и завершить",page)
        self.assertNotIn("Передать результат отправителю",page)
        self.app.config["WTF_CSRF_ENABLED"]=True
        before=self.snapshot()
        self.assertEqual(405,self.client.get(f"/tasks/{self.work}/review/submit_result").status_code)
        self.assertEqual(400,self.client.post(f"/tasks/{self.work}/review/submit_result",
            data={"revision":"0","comment":"Готово"}).status_code)
        self.assertEqual(before,self.snapshot())
        token=re.search(r'name="csrf_token" value="([^"]+)"',page).group(1)
        self.assertEqual(302,self.client.post(f"/tasks/{self.work}/review/submit_result",
            data={"csrf_token":token,"revision":"0","comment":"Готово"}).status_code)

