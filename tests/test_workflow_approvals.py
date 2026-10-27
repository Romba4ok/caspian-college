"""Approval stage on synthetic data, independently of T007-T009 work actions."""
from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import re
import tempfile
from unittest.mock import patch

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models import (Department, Task, TaskAttachment, TaskCategory, TaskComment,
                        TaskEvent, TaskNotification, User, SystemSettings)
from app.workflow import create_task, transition_approval, update_task_before_acceptance, WorkflowError
from support.portal_fixtures import PortalCase
from test_tasks import attachment_bytes


class ApprovalWorkflowTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory(prefix="portal_case_")
        self.app.config["TASK_UPLOAD_DIR"] = self.directory.name
        self.add_member("head-two", role="head")
        self.add_member("colleague")
        self.add_member("recipient", department_id=self.other_department_id)
        self.add_member("recipient-head", role="head", department_id=self.other_department_id)
        with self.app.app_context():
            category = TaskCategory(name="Документы")
            db.session.add(category)
            db.session.commit()
            self.category_id = category.id

    def tearDown(self):
        super().tearDown()
        self.directory.cleanup()

    def login(self, username="root", password=None, **kwargs):
        # Logging in again does not switch an authenticated Flask-Login session.
        self.client = self.app.test_client()
        response = super().login(username, password, **kwargs)
        self.assertEqual(200, response.status_code)
        return response

    def saved(self, task_id):
        with self.app.app_context():
            task = db.session.get(Task, task_id)
            return {key: deepcopy(getattr(task, key)) for key in (
                "id", "title", "description", "status", "revision", "author_id", "sender_department_id",
                "recipient_department_id", "assignee_id", "due_at", "sent_at", "priority",
                "category_id", "category", "contact_person", "legacy_missing_due", "is_legacy_dispatched")}

    def snapshot(self):
        with self.app.app_context():
            return {model.__tablename__: deepcopy(db.session.execute(select(model.__table__).order_by(model.id)).all())
                    for model in (Task, TaskComment, TaskEvent, TaskAttachment, TaskNotification)}

    def create(self, actor="employee", **fields):
        self.login(actor)
        response = self.client.post("/tasks/new", data=self.draft_form(**fields))
        self.assertEqual(302, response.status_code, response.get_data(as_text=True))
        return int(response.headers["Location"].rsplit("/", 1)[1])

    def act(self, task_id, action, actor="employee", version=None, comment="Проверено", **extra):
        self.login(actor)
        data = dict(revision=str(self.saved(task_id)["revision"] if version is None else version), comment=comment) | extra
        return self.client.post(f"/tasks/{task_id}/approval/{action}", data=data)

    def test_creation_is_private_draft_and_description_is_single_first_comment(self):
        task_id = self.create(title="  Документы  ", description="  Полный текст  ",
            category_id=str(self.category_id), priority="urgent", contact_person="  Анна  ")
        task = self.saved(task_id)
        self.assertEqual(("draft", 1, None, None), tuple(task[k] for k in ("status","revision","assignee_id","sent_at")))
        self.assertEqual(("Документы", "Полный текст", "urgent", "Документы", "Анна"),
            tuple(task[k] for k in ("title","description","priority","category","contact_person")))
        self.assertEqual(self.ids["employee"], task["author_id"])
        self.assertEqual(self.department_id, task["sender_department_id"])
        self.assertFalse(task["legacy_missing_due"] or task["is_legacy_dispatched"])
        with self.app.app_context():
            comment = TaskComment.query.filter_by(task_id=task_id).one()
            event = TaskEvent.query.filter_by(task_id=task_id).one()
            self.assertEqual("Полный текст", comment.body)
            self.assertEqual(comment.id, event.comment_id)
            self.assertEqual("draft", event.details["after"]["status"])
            self.assertEqual("employee", event.actor_snapshot["role"])
            self.assertEqual(0, TaskNotification.query.count())
        self.login("recipient")
        self.assertEqual(404, self.client.get(f"/tasks/{task_id}").status_code)
        self.assertNotIn("Полный текст", self.client.get("/tasks/received").get_data(as_text=True))

    def test_employee_submission_and_approval_by_other_head_sends_exact_version(self):
        task_id = self.create()
        self.assertEqual(302, self.act(task_id,"submit").status_code)
        self.assertEqual("pending_approval", self.saved(task_id)["status"])
        before = self.snapshot()
        self.assertEqual(403, self.act(task_id, "send", actor="head-two").status_code)
        self.assertEqual(before, self.snapshot())
        self.login("recipient")
        self.assertEqual(404,self.client.get(f"/tasks/{task_id}").status_code)
        self.assertEqual(302,self.act(task_id,"approve",actor="head-two",comment="Одобряю отправку").status_code)
        task=self.saved(task_id)
        self.assertEqual(("awaiting_assignment",3,None),tuple(task[k] for k in ("status","revision","assignee_id")))
        self.assertIsNotNone(task["sent_at"])
        for name in ("employee","head","recipient","recipient-head","root"):
            self.login(name)
            self.assertEqual(200,self.client.get(f"/tasks/{task_id}").status_code,name)
        with self.app.app_context():
            event=TaskEvent.query.filter_by(task_id=task_id,event_type="approved_and_sent").one()
            self.assertEqual(self.ids["head-two"],event.actor_id)
            self.assertEqual("head",event.actor_snapshot["role"])

    def test_head_first_saves_own_draft_then_sends_separately(self):
        task_id=self.create(actor="head")
        self.assertIsNone(self.saved(task_id)["sent_at"])
        self.assertEqual(403,self.act(task_id,"submit",actor="head").status_code)
        self.assertEqual(302,self.act(task_id,"send",actor="head").status_code)
        self.assertEqual("awaiting_assignment",self.saved(task_id)["status"])

    def test_head_cannot_send_someone_elses_draft_without_approval(self):
        task_id=self.create()
        before=self.snapshot()
        for name,action in (("head","send"),("head","approve"),("head-two","send"),("colleague","submit"),("employee","send"),("root","send")):
            self.assertEqual(403,self.act(task_id,action,actor=name).status_code,(name,action))
        self.assertEqual(before,self.snapshot())

    def test_withdraw_edit_and_resubmit_then_return_to_draft(self):
        task_id=self.create()
        self.act(task_id,"submit")
        self.assertEqual(403,self.client.get(f"/tasks/{task_id}/edit").status_code)
        self.assertEqual(302,self.act(task_id,"withdraw",comment="Нужно исправить").status_code)
        self.login("employee")
        response=self.client.post(f"/tasks/{task_id}/edit",data=self.draft_form(title="Исправленный текст",
            revision=str(self.saved(task_id)["revision"]),comment="Добавил сведения"))
        self.assertEqual(302,response.status_code)
        self.assertEqual("Исправленный текст",self.saved(task_id)["title"])
        self.assertEqual(302,self.act(task_id,"submit").status_code)
        self.assertEqual(302,self.act(task_id,"return_draft",actor="head-two",comment="Уточните объём").status_code)
        self.assertEqual(("draft",6),tuple(self.saved(task_id)[k] for k in ("status","revision")))

    def test_pending_payload_cannot_be_edited_by_author_or_head(self):
        task_id=self.create()
        self.act(task_id,"submit")
        before=self.snapshot()
        for name in ("employee","head","head-two","colleague","root"):
            self.login(name)
            self.assertEqual(403,self.client.get(f"/tasks/{task_id}/edit").status_code)
            self.assertEqual(403,self.client.post(f"/tasks/{task_id}/edit",
                data=self.draft_form(revision="2",comment="Bypass")).status_code)
        self.assertEqual(before,self.snapshot())

    def test_all_approval_actions_require_comment_without_partial_mutation(self):
        task_id=self.create()
        for action,actor in (("submit","employee"),):
            before=self.snapshot()
            self.assertEqual(400,self.act(task_id,action,actor,comment="   ").status_code)
            self.assertEqual(before,self.snapshot())
        self.act(task_id,"submit")
        for action,actor in (("withdraw","employee"),("return_draft","head"),("approve","head")):
            before=self.snapshot()
            self.assertEqual(400,self.act(task_id,action,actor,comment=" \n ").status_code)
            self.assertEqual(before,self.snapshot())
        other=self.create(actor="head")
        before=self.snapshot()
        self.assertEqual(400,self.act(other,"send","head",comment="").status_code)
        self.assertEqual(before,self.snapshot())

    def test_required_fields_invalid_priority_and_destination_do_not_create(self):
        self.login("employee")
        for fields in ({"title":" "},{"description":" "},{"due_at":""},{"due_at":"bad"},
            {"recipient_department_id":str(self.department_id)},{"recipient_department_id":"999999"},
            {"recipient_department_id":"9"*100},{"priority":"critical"},{"title":"x"*201},
            {"contact_person":"x"*161},{"category_id":"9999"},{"category_id":"bad"}):
            before=self.snapshot()
            response=self.client.post("/tasks/new",data=self.draft_form(**fields))
            self.assertEqual(400,response.status_code,fields)
            self.assertEqual(before,self.snapshot(),fields)

    def test_creation_edit_and_approval_reject_forged_or_duplicate_fields(self):
        self.login("employee")
        for extra in ({"author_id":self.ids["head"]},{"sender_department_id":self.third_department_id},
            {"status":"awaiting_assignment"},{"assignee_id":self.ids["head"]},{"legacy_missing_due":"1"},
            {"is_legacy_dispatched":"1"},{"category":"Свободная категория"},{"revision":"1"}):
            before=self.snapshot()
            self.assertEqual(400,self.client.post("/tasks/new",data=self.draft_form(**extra)).status_code)
            self.assertEqual(before,self.snapshot())
        task_id=self.create()
        before=self.snapshot()
        self.assertEqual(400,self.act(task_id,"submit",status="completed").status_code)
        self.login("employee")
        self.assertEqual(400,self.client.post(f"/tasks/{task_id}/edit",data=self.draft_form(
            revision="1",comment="Изменение",author_id=self.ids["head"])).status_code)
        from werkzeug.datastructures import MultiDict
        payload=MultiDict(self.draft_form())
        payload.add("title","Подмена")
        self.assertEqual(400,self.client.post("/tasks/new",data=payload).status_code)
        self.assertEqual(before,self.snapshot())

    def test_stale_approval_after_withdrawal_or_edit_is_conflict(self):
        task_id=self.create()
        self.act(task_id,"submit")
        old=self.saved(task_id)["revision"]
        self.act(task_id,"withdraw")
        before=self.snapshot()
        response=self.act(task_id,"approve","head",version=old)
        self.assertEqual(409,response.status_code)
        self.assertIn("Обновите страницу",response.get_data(as_text=True))
        self.assertEqual(before,self.snapshot())
        self.login("employee")
        self.assertEqual(409,self.client.post(f"/tasks/{task_id}/edit",
            data=self.draft_form(revision=str(old),comment="Устарело")).status_code)
        self.assertEqual(before,self.snapshot())

    def test_repeated_approval_has_no_duplicate_comments_or_events(self):
        task_id=self.create()
        self.act(task_id,"submit")
        version=self.saved(task_id)["revision"]
        self.assertEqual(302,self.act(task_id,"approve","head",version=version).status_code)
        before=self.snapshot()
        self.assertEqual(409,self.act(task_id,"approve","head-two",version=version).status_code)
        self.assertEqual(before,self.snapshot())

    def test_revision_is_required_and_strictly_integer(self):
        task_id=self.create()
        before=self.snapshot()
        self.login("employee")
        for version in (None,"","1.0","-1","true","9"*100,"١"):
            data={"comment":"Комментарий"}
            if version is not None:
                data["revision"]=version
            self.assertEqual(400,self.client.post(f"/tasks/{task_id}/approval/submit",data=data).status_code,version)
        self.assertEqual(before,self.snapshot())

    def test_archived_recipient_is_excluded_and_rechecked_before_send(self):
        task_id=self.create()
        self.act(task_id,"submit")
        with self.app.app_context():
            db.session.get(Department,self.other_department_id).is_archived=True
            db.session.commit()
        self.login("employee")
        page=self.client.get("/tasks/new").get_data(as_text=True)
        self.assertNotIn(f'<option value="{self.other_department_id}"',page)
        before=self.snapshot()
        self.assertEqual(403,self.act(task_id,"approve","head").status_code)
        self.login("employee")
        self.assertEqual(400,self.client.post("/tasks/new",data=self.draft_form()).status_code)
        self.assertEqual(before,self.snapshot())

    def test_archived_sender_can_only_read_unsent_not_create_edit_submit_or_send(self):
        draft=self.create()
        pending=self.create()
        self.act(pending,"submit")
        head_draft=self.create(actor="head")
        with self.app.app_context():
            db.session.get(Department,self.department_id).is_archived=True
            db.session.commit()
        before=self.snapshot()
        self.login("employee")
        self.assertEqual(403,self.client.get("/tasks/new").status_code)
        self.assertEqual(403,self.client.get(f"/tasks/{draft}/edit").status_code)
        self.assertEqual(200,self.client.get(f"/tasks/{draft}").status_code)
        for task_id,action,actor in ((draft,"submit","employee"),(pending,"withdraw","employee"),
            (pending,"approve","head"),(pending,"return_draft","head"),(head_draft,"send","head")):
            self.assertEqual(403,self.act(task_id,action,actor).status_code)
        self.assertEqual(before,self.snapshot())

    def test_sent_fields_frozen_for_every_role_and_compatibility_edit(self):
        task_id=self.create()
        self.act(task_id,"submit")
        self.act(task_id,"approve","head")
        before=self.snapshot()
        for name in ("employee","head","recipient","recipient-head","root"):
            self.login(name)
            self.assertEqual(403,self.client.get(f"/tasks/{task_id}/edit").status_code)
            self.assertEqual(403,self.client.post(f"/tasks/{task_id}/edit",
                data=self.draft_form(revision="3",comment="Изменить")).status_code)
        with self.app.app_context():
            with self.assertRaises(PermissionError):
                update_task_before_acceptance(task=db.session.get(Task,task_id),
                    actor=db.session.get(User,self.ids["employee"]),expected_revision=3,comment="Изменить",
                    recipient_department_id=self.other_department_id,title="Новое",description="Новое",
                    due_at=datetime(2031,1,1,tzinfo=timezone.utc))
            db.session.rollback()
        self.assertEqual(before,self.snapshot())

    def test_foreign_resources_actions_and_new_admin_task_creation_are_denied(self):
        task_id=self.create()
        before=self.snapshot()
        for name in ("recipient","outsider"):
            self.login(name)
            for path in (f"/tasks/{task_id}/edit",f"/tasks/{task_id}/approval/approve"):
                self.assertEqual(404,self.client.post(path,data={"revision":"1","comment":"x"}).status_code)
        self.login("root")
        self.assertEqual(403,self.client.get("/tasks/new").status_code)
        self.assertEqual(403,self.client.post("/tasks/new",data=self.draft_form()).status_code)
        self.assertEqual(before,self.snapshot())

    def test_service_rechecks_block_role_and_department_of_cached_actor(self):
        for changes in ({"is_blocked":True},{"role":"head"},{"department_id":self.third_department_id}):
            task_id=self.create()
            before=self.snapshot()
            with self.app.app_context():
                actor=db.session.get(User,self.ids["employee"])
                task=db.session.get(Task,task_id)
                # Keep a deliberately stale Python actor; authorization must re-read SQL.
                db.session.execute(update(User).where(User.id==actor.id).values(**changes)
                    .execution_options(synchronize_session=False))
                with self.assertRaises(PermissionError):
                    transition_approval(task=task,actor=actor,action="submit",expected_revision=1,comment="Старая сессия")
                db.session.rollback()
            self.assertEqual(before,self.snapshot())

    def test_initial_file_shares_description_comment_and_remains_private(self):
        self.login("employee")
        response=self.client.post("/tasks/new",data=self.draft_form(
            attachment=(BytesIO(attachment_bytes(".pdf")),"document.pdf")))
        self.assertEqual(302,response.status_code,response.get_data(as_text=True))
        task_id=int(response.headers["Location"].rsplit("/",1)[1])
        with self.app.app_context():
            comment=TaskComment.query.filter_by(task_id=task_id).one()
            attachment=TaskAttachment.query.filter_by(task_id=task_id).one()
            event=TaskEvent.query.filter_by(task_id=task_id).one()
            self.assertEqual((comment.id,comment.id),(attachment.comment_id,event.comment_id))
            attachment_id=attachment.id
            self.assertEqual(1,TaskEvent.query.filter_by(task_id=task_id).count())
        self.login("recipient")
        self.assertEqual(404,self.client.get(f"/tasks/attachments/{attachment_id}/download").status_code)
        self.login("root")
        response = self.client.get(f"/tasks/attachments/{attachment_id}/download")
        self.assertEqual(200,response.status_code)
        response.close()

    def test_bad_upload_or_commit_failure_rolls_back_entire_draft_and_only_new_files(self):
        self.login("employee")
        kept=Path(self.directory.name)/("a"*32)
        kept.write_bytes(b"existing synthetic file")
        before=self.snapshot()
        response=self.client.post("/tasks/new",data=self.draft_form(attachment=(BytesIO(b"forged"),"bad.pdf")))
        self.assertEqual(400,response.status_code)
        self.assertEqual(before,self.snapshot())
        with patch("app.tasks.db.session.commit",side_effect=SQLAlchemyError("synthetic failure")):
            response=self.client.post("/tasks/new",data=self.draft_form(
                attachment=(BytesIO(attachment_bytes(".pdf")),"document.pdf")))
        self.assertEqual(500,response.status_code)
        self.assertNotIn("synthetic failure",response.get_data(as_text=True))
        self.assertEqual(before,self.snapshot())
        self.assertEqual([kept],list(Path(self.directory.name).iterdir()))

    def test_initial_upload_uses_current_policy_not_open_form(self):
        self.login("employee")
        self.assertEqual(200,self.client.get("/tasks/new").status_code)
        with self.app.app_context():
            db.session.add(SystemSettings(id=1,max_upload_mb=1,allowed_extensions=["png"]))
            db.session.commit()
        before=self.snapshot()
        response=self.client.post("/tasks/new",data=self.draft_form(
            attachment=(BytesIO(attachment_bytes(".pdf")),"old-form.pdf")))
        self.assertEqual(400,response.status_code)
        self.assertEqual(before,self.snapshot())
        self.assertEqual([],list(Path(self.directory.name).iterdir()))

    def test_edit_and_transition_audit_failure_roll_back_row_and_comment(self):
        task_id=self.create()
        self.login("employee")
        before=self.snapshot()
        with patch("app.audit.TaskEvent",side_effect=SQLAlchemyError("synthetic audit failure")):
            response=self.client.post(f"/tasks/{task_id}/edit",
                data=self.draft_form(title="Не сохранять",revision="1",comment="Изменение"))
            self.assertEqual(500,response.status_code)
            response=self.client.post(f"/tasks/{task_id}/approval/submit",
                data={"revision":"1","comment":"Подать"})
            self.assertEqual(500,response.status_code)
        self.assertEqual(before,self.snapshot())

    def test_legacy_missing_deadline_is_not_a_way_to_submit_or_send(self):
        task_id = self.task(status="awaiting_assignment", due_at=None,
            legacy_missing_due=True, is_legacy_dispatched=True)
        before = self.snapshot()
        for name, action in (("employee", "submit"), ("head", "approve"), ("head", "send")):
            self.assertEqual(403, self.act(task_id, action, name).status_code)
        self.login("employee")
        self.assertEqual(200, self.client.get(f"/tasks/{task_id}").status_code)
        self.assertEqual(403, self.client.get(f"/tasks/{task_id}/edit").status_code)
        self.assertEqual(before, self.snapshot())

    def test_category_archived_after_opening_form_is_rejected_and_values_are_retained(self):
        self.login("employee")
        with self.app.app_context():
            db.session.get(TaskCategory,self.category_id).is_archived=True
            db.session.commit()
        before=self.snapshot()
        response=self.client.post("/tasks/new",data=self.draft_form(
            title="<script>сохранить текст</script>",category_id=str(self.category_id)))
        self.assertEqual(400,response.status_code)
        self.assertIn("&lt;script&gt;",response.get_data(as_text=True))
        self.assertNotIn("<script>сохранить текст</script>",response.get_data(as_text=True))
        self.assertEqual(before,self.snapshot())

    def test_actor_snapshot_is_not_rewritten_after_account_changes(self):
        task_id=self.create()
        self.act(task_id,"submit")
        self.act(task_id,"approve","head-two")
        with self.app.app_context():
            head=db.session.get(User,self.ids["head-two"])
            head.full_name="Новое имя"
            head.role="employee"
            head.department_id=self.third_department_id
            db.session.commit()
            event=TaskEvent.query.filter_by(task_id=task_id,event_type="approved_and_sent").one()
            self.assertEqual("head-two",event.actor_snapshot["name"])
            self.assertEqual("head",event.actor_snapshot["role"])
            self.assertEqual(self.department_id,event.actor_snapshot["department_id"])
        self.login("employee")
        page=self.client.get(f"/tasks/{task_id}").get_data(as_text=True)
        self.assertIn("head-two",page)
        self.assertIn("Начальник отдела",page)

    def test_action_forms_are_role_specific_post_only_and_csrf_protected(self):
        task_id=self.create()
        self.app.config["WTF_CSRF_ENABLED"]=True
        page=self.client.get(f"/tasks/{task_id}").get_data(as_text=True)
        self.assertIn("На согласование",page)
        self.assertNotIn("Одобрить и отправить",page)
        self.assertNotIn("Взять задачу",page)
        before=self.snapshot()
        self.assertEqual(405,self.client.get(f"/tasks/{task_id}/approval/submit").status_code)
        self.assertEqual(400,self.client.post(f"/tasks/{task_id}/approval/submit",
            data={"revision":"1","comment":"Подать"}).status_code)
        self.assertEqual(before,self.snapshot())
        token=re.search(r'name="csrf_token" value="([^"]+)"',page).group(1)
        self.assertEqual(302,self.client.post(f"/tasks/{task_id}/approval/submit",
            data={"csrf_token":token,"revision":"1","comment":"Подать"}).status_code)

