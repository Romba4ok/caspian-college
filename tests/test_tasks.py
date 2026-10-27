import os
import sys
import unittest
import re
import tempfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import inspect
from sqlalchemy.orm import Session
from PIL import Image
from pypdf import PdfWriter

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app import create_app
from app.extensions import db
from app.models import Department, Task, TaskAttachment, TaskComment, TaskEvent, TaskNotification, User
from app.workflow import (
    WorkflowError, change_task_deadline, claim_task, create_task, return_task_for_revision, update_task_before_acceptance,
)
from app.storage import MAX_FILE_BYTES, MIME_TYPES, storage_root


def attachment_bytes(extension):
    output = BytesIO()
    if extension == ".pdf":
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.write(output)
    elif extension in (".png", ".jpg"):
        Image.new("RGB", (4, 4), "white").save(output, format="PNG" if extension == ".png" else "JPEG")
    else:
        parts = {
            ".docx": ("word/document.xml", "wordprocessingml.document.main+xml", "wordprocessingml", "document"),
            ".xlsx": ("xl/workbook.xml", "spreadsheetml.sheet.main+xml", "spreadsheetml", "workbook"),
            ".pptx": ("ppt/presentation.xml", "presentationml.presentation.main+xml", "presentationml", "presentation"),
        }
        path, content_suffix, namespace, tag = parts[extension]
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/{path}" ContentType="application/vnd.openxmlformats-officedocument.{content_suffix}"/></Types>')
            archive.writestr("_rels/.rels", f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="{path}"/></Relationships>')
            archive.writestr(path, f'<{tag} xmlns="http://schemas.openxmlformats.org/{namespace}/2006/main"/>')
    return output.getvalue()


class TaskModelTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": ":memory:",
                "SECRET_KEY": "task-test-secret",
                "ADMIN_USERNAME": "root",
                "ADMIN_PASSWORD": "root-password",
            }
        )
        self.context = self.app.app_context()
        self.context.push()
        self.sender = Department.query.filter_by(slug="marketing").one()
        self.recipient = Department.query.filter_by(slug="it").one()
        self.employee = User(
            full_name="Мария Сотрудник", username="task-author", role="employee",
            department_id=self.sender.id, department_role="employee",
        )
        self.employee.set_password("secure-pass")
        db.session.add(self.employee)
        db.session.commit()

    def tearDown(self):
        self.context.pop()
        self.app.extensions["memory_db"].close()
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def test_task_creation_derives_author_and_sender_from_account(self):
        task = create_task(actor=self.employee, recipient_department_id=self.recipient.id,
            title="  Подготовить компьютерный класс  ", description="  Установить нужные программы.  ",
            due_at=datetime(2030,1,1,tzinfo=timezone.utc), contact_person="Иван")
        db.session.commit()
        self.assertEqual(self.employee.id, task.author_id)
        self.assertEqual(self.sender.id, task.sender_department_id)
        self.assertEqual(self.recipient.id, task.recipient_department_id)
        self.assertEqual("draft", task.status)
        self.assertEqual("normal", task.priority)
        self.assertIsNone(task.assignee_id)
        self.assertIsNone(task.sent_at)
        self.assertEqual("Подготовить компьютерный класс", task.title)
        self.assertEqual("Установить нужные программы.", task.description)
        event=TaskEvent.query.filter_by(task_id=task.id,event_type="created").one()
        self.assertEqual(self.employee.id,event.actor_id)
        self.assertEqual(task.description,db.session.get(TaskComment,event.comment_id).body)
        self.assertEqual(0,TaskNotification.query.count())

    def test_task_creation_rejects_same_department_and_invalid_priority(self):
        for recipient_id, priority, deadline in (
            (self.sender.id,"normal",datetime(2030,1,1,tzinfo=timezone.utc)),
            (self.recipient.id,"critical",datetime(2030,1,1,tzinfo=timezone.utc)),
            (self.recipient.id,"normal",None)):
            with self.assertRaises(WorkflowError):
                create_task(actor=self.employee,recipient_department_id=recipient_id,
                    title="Задача",description="Текст",priority=priority,due_at=deadline)
            db.session.rollback()
        self.assertEqual(0,Task.query.count())

    def test_task_and_related_entities_are_mapped(self):
        task=create_task(actor=self.employee,recipient_department_id=self.recipient.id,
            title="Сохранить документы",description="Перенести документы в архив.",
            due_at=datetime(2030,1,1,tzinfo=timezone.utc),priority="urgent")
        db.session.commit()
        self.assertIsNotNone(task.due_at)
        self.assertEqual("urgent",task.priority)
        self.assertIsInstance(task,Task)
        self.assertEqual(1,task.revision)
        self.assertEqual(1,len(task.comments))
        self.assertEqual(1,len(task.events))

    def test_task_migration_creates_all_task_resource_tables(self):
        # Do not keep the model-test app context active while Flask-Migrate selects its app.
        self.context.pop()
        try:
            migration_app = create_app(
                {
                    "TESTING": False,
                    "DATABASE": ":memory:",
                    "SECRET_KEY": "task-migration-secret",
                    "ADMIN_USERNAME": "migration-admin",
                    "ADMIN_PASSWORD": "migration-password",
                }
            )
            result = migration_app.test_cli_runner().invoke(args=["db", "upgrade"], catch_exceptions=False)
            self.assertEqual(0, result.exit_code, f"{result.output}\n{result.exception!r}")
            repeated_result = migration_app.test_cli_runner().invoke(args=["db", "upgrade"], catch_exceptions=False)
            self.assertEqual(0, repeated_result.exit_code, repeated_result.output)
            with migration_app.app_context():
                tables = set(inspect(db.engine).get_table_names())
                db.engine.dispose()
            self.assertTrue(
                {
                    "tasks", "task_comments", "task_attachments", "task_events",
                    "task_notifications", "task_translations",
                }.issubset(tables)
            )
        finally:
            self.context.push()


# T012 moves the four list/filter scenarios to the current department workflow.
# T016 adapts legacy action scenarios below without restoring superseded permissions.
from support.portal_fixtures import PortalCase


class TaskListRouteTests(PortalCase):
    def test_create_draft_waits_for_approval_before_receiver_can_see_it(self):
        self.login("employee")
        response=self.client.post("/tasks/new",data=self.draft_form(title="Подготовить кабинет"))
        self.assertEqual(302,response.status_code)
        with self.app.app_context():
            task=Task.query.filter_by(title="Подготовить кабинет").one();ident=task.id
            self.assertEqual("draft",task.status);self.assertIsNotNone(task.due_at)
            self.assertEqual(self.ids["employee"],task.author_id)
        self.assertIn(f'data-task-id="{ident}"'.encode(),self.client.get("/tasks/sent").data)
        self.add_member("recipient",department_id=self.other_department_id)
        self.client=self.app.test_client();self.login("recipient")
        self.assertNotIn(f'data-task-id="{ident}"'.encode(),self.client.get("/tasks/received").data)
        self.assertEqual(404,self.client.get(f"/tasks/{ident}").status_code)

    def test_removed_status_priority_assignee_and_overdue_filters_do_not_hide_tasks(self):
        ids=[self.task(status="in_progress",sent_at=datetime.now(timezone.utc),priority=p) for p in ("low","urgent")]
        self.login("employee")
        source=self.client.get("/tasks/sent?status=completed&priority=low&assignee_id=unassigned&deadline_state=overdue").data
        for ident in ids:self.assertIn(f'data-task-id="{ident}"'.encode(),source)
        self.assertNotIn(b'name="priority"',source)

    def test_removed_date_range_parameters_are_ignored_in_both_directions(self):
        outgoing=self.task(status="in_progress",sent_at=datetime.now(timezone.utc))
        incoming=self.task(author_id=self.ids["head"],sender_department_id=self.other_department_id,
            recipient_department_id=self.department_id,status="in_progress",sent_at=datetime.now(timezone.utc))
        self.login("employee")
        for direction,ident in (("sent",outgoing),("received",incoming)):
            source=self.client.get(f"/tasks/{direction}?due_from=1990-01-01&due_to=1990-01-02").data
            self.assertIn(f'data-task-id="{ident}"'.encode(),source)

    def test_college_midnight_is_displayed_without_old_calendar_filter(self):
        ident=self.task(status="in_progress",sent_at=datetime.now(timezone.utc),due_at=datetime(2030,6,1,19,tzinfo=timezone.utc))
        self.login("employee")
        source=self.client.get("/tasks/sent?due_from=2030-06-01&due_to=2030-06-01").data
        self.assertIn(f'data-task-id="{ident}"'.encode(),source)
        self.assertIn("02.06.2030 00:00".encode(),source)


class TaskRouteTests(PortalCase):
    """Historical HTTP regression adapted to approval, assigned execution and two reviews."""
    def setUp(self):
        super().setUp()
        self.upload_directory=tempfile.TemporaryDirectory(prefix="portal_case_")
        self.app.config["TASK_UPLOAD_DIR"]=self.upload_directory.name
        for name,role in (("receiver","employee"),("receiver-two","employee"),("receiver-head","head"),("receiver-head-two","head")):
            self.add_member(name,role=role,department_id=self.other_department_id)
        self.author_id=self.ids["employee"];self.recipient_id=self.ids["receiver"]

    def tearDown(self):
        super().tearDown();self.upload_directory.cleanup()

    def _login(self,who="employee"):
        self.client=self.app.test_client();return self.login(who)

    def _new_task(self,title,status="awaiting_assignment",assignee_id=None,**values):
        with self.app.app_context():
            task=create_task(actor=db.session.get(User,self.author_id),recipient_department_id=self.other_department_id,
                title=title,description="Описание задачи",due_at=datetime(2030,1,1,tzinfo=timezone.utc))
            task.status=status;task.assignee_id=assignee_id
            task.sent_at=None if status in ("draft","pending_approval") else datetime.now(timezone.utc)
            for key,value in values.items():setattr(task,key,value)
            db.session.commit();return task.id

    def _state(self,task_id):
        with self.app.app_context():
            task=db.session.get(Task,task_id)
            return {key:getattr(task,key) for key in ("title","description","status","revision","assignee_id","due_at","completed_at")}

    def _counts(self):
        with self.app.app_context():return tuple(model.query.count() for model in (TaskEvent,TaskComment,TaskAttachment,TaskNotification))

    def _post(self,task_id,suffix,who="employee",**data):
        self._login(who)
        payload={"revision":str(self._state(task_id)["revision"]),"comment":"Пояснение действия"}|data
        if suffix=="cancel":payload["reason"]=payload.pop("comment")
        return self.client.post(f"/tasks/{task_id}/{suffix}",data=payload)

    def _file(self,task_id,filename,payload,who="employee"):
        return self._post(task_id,"attachments",who,attachment=(BytesIO(payload),filename))

    def test_author_can_edit_draft_but_not_after_sending(self):
        task_id=self._new_task("Черновик",status="draft")
        response=self._post(task_id,"edit",**self.draft_form(title="Уточнённое название",description="Обновлённое описание"))
        self.assertEqual(302,response.status_code);self.assertEqual("Уточнённое название",self._state(task_id)["title"])
        self.assertEqual(302,self._post(task_id,"approval/submit").status_code)
        self.assertEqual(302,self._post(task_id,"approval/approve","head").status_code)
        before=self._counts();self.assertEqual(403,self._post(task_id,"edit",**self.draft_form(title="Поздняя правка")).status_code)
        self.assertEqual(before,self._counts());self.assertEqual("Уточнённое название",self._state(task_id)["title"])

    def test_post_without_session_and_task_id_from_other_department_are_denied(self):
        self.assertEqual(302,self.client.post("/tasks/new").status_code)
        task_id=self._new_task("Частная задача");self._login("outsider")
        self.assertEqual(404,self.client.get(f"/tasks/{task_id}").status_code)

    def test_assignment_start_and_two_reviews_are_separate_actions(self):
        task_id=self._new_task("Рабочий процесс")
        self.assertEqual(403,self._post(task_id,"claim","receiver").status_code)
        self.assertEqual(302,self._post(task_id,"execution/assign","receiver-head",assignee_id=str(self.recipient_id)).status_code)
        self.assertEqual("assigned",self._state(task_id)["status"])
        self.assertEqual(403,self._post(task_id,"review/submit_result","receiver").status_code)
        self.assertEqual(302,self._post(task_id,"execution/start","receiver").status_code)
        self.assertEqual(302,self._post(task_id,"review/submit_result","receiver").status_code)
        self.assertEqual("recipient_review",self._state(task_id)["status"])
        self.assertEqual(403,self._post(task_id,"review/complete","head").status_code)
        self.assertEqual(302,self._post(task_id,"review/forward_result","receiver-head").status_code)
        self.assertEqual(302,self._post(task_id,"review/complete","head").status_code)
        with self.app.app_context():
            self.assertEqual(["created","assigned","work_started","result_submitted","result_forwarded","completed"],
                [e.event_type for e in TaskEvent.query.order_by(TaskEvent.id).all()])

    def test_clarification_and_route_authorization(self):
        task_id=self._new_task("Уточнение",status="in_progress",assignee_id=self.recipient_id)
        for suffix in ("claim","status","reassign"):
            self.assertEqual(404,self._post(task_id,suffix,"outsider",status="in_progress").status_code)
        self.assertEqual(302,self._post(task_id,"execution/request_clarification","receiver").status_code)
        self.assertEqual(403,self._post(task_id,"execution/answer_clarification","receiver-two").status_code)
        self.assertEqual(302,self._post(task_id,"execution/answer_clarification").status_code)
        self.assertEqual("in_progress",self._state(task_id)["status"])
        self.assertEqual(405,self.client.get(f"/tasks/{task_id}/claim").status_code)

    def test_actions_require_csrf_and_head_can_reassign_through_form(self):
        task_id=self._new_task("CSRF",status="assigned",assignee_id=self.recipient_id)
        self._login("receiver-head");page=self.client.get(f"/tasks/{task_id}").data
        self.app.config["WTF_CSRF_ENABLED"]=True
        self.assertEqual(400,self.client.post(f"/tasks/{task_id}/execution/reassign",data={"revision":"1","comment":"Переназначаю","assignee_id":str(self.ids["receiver-two"])}).status_code)
        token=re.search(rb'name="csrf_token" value="([^"]+)"',page)[1].decode()
        response=self.client.post(f"/tasks/{task_id}/execution/reassign",data={"csrf_token":token,"revision":"1","comment":"Переназначаю","assignee_id":str(self.ids["receiver-two"])})
        self.assertEqual(302,response.status_code);self.assertEqual(self.ids["receiver-two"],self._state(task_id)["assignee_id"])

    def test_two_stale_assignments_create_one_assignment_and_one_event(self):
        # Real simultaneous connections are verified separately by test_postgresql.py.
        task_id=self._new_task("Два назначения")
        self.assertEqual(302,self._post(task_id,"execution/assign","receiver-head",revision="1",assignee_id=str(self.recipient_id)).status_code)
        self.assertEqual(409,self._post(task_id,"execution/assign","receiver-head-two",revision="1",assignee_id=str(self.ids["receiver-two"])).status_code)
        for who in ("receiver","receiver-two"):self.assertEqual(403,self._post(task_id,"claim",who).status_code)
        with self.app.app_context():self.assertEqual(1,TaskEvent.query.filter_by(event_type="assigned").count())
        self.assertEqual(self.recipient_id,self._state(task_id)["assignee_id"])

    def test_stale_edit_cannot_overwrite_submitted_task_in_another_session(self):
        task_id=self._new_task("Не перезаписать",status="draft")
        with self.app.app_context():
            stale=db.session.get(Task,task_id);actor=db.session.get(User,self.author_id)
            with Session(db.engine) as other:
                current=other.get(Task,task_id);current.status="pending_approval";current.revision=2;other.commit()
            with self.assertRaises((WorkflowError,PermissionError)):
                update_task_before_acceptance(task=stale,actor=actor,expected_revision=1,comment="Поздняя правка",title="Нельзя",description="Нельзя",recipient_department_id=self.other_department_id,due_at=datetime(2030,1,1,tzinfo=timezone.utc))
            db.session.rollback()
        self.assertEqual("Не перезаписать",self._state(task_id)["title"])

    def test_only_sender_head_confirms_result_and_notifies_executor(self):
        task_id=self._new_task("Приёмка",status="sender_acceptance",assignee_id=self.recipient_id)
        self.assertEqual(403,self._post(task_id,"confirm").status_code)
        self.assertEqual(302,self._post(task_id,"confirm","head",revision="1").status_code)
        self.assertEqual(409,self._post(task_id,"confirm","head",revision="1").status_code)
        with self.app.app_context():
            event=TaskEvent.query.filter_by(event_type="completed").one();self.assertEqual(self.ids["head"],event.actor_id)
            self.assertEqual("sender_acceptance",event.details["before"]["status"])
            self.assertIsNotNone(db.session.get(Task,task_id).completed_at)
            self.assertIn(self.recipient_id,{n.user_id for n in TaskNotification.query.all()})

    def test_return_and_clarification_require_comment_and_keep_actor_snapshot(self):
        task_id=self._new_task("Возврат",status="sender_acceptance",assignee_id=self.recipient_id)
        before=self._counts();self.assertEqual(400,self._post(task_id,"return","head",comment=" ").status_code);self.assertEqual(before,self._counts())
        self.assertEqual(302,self._post(task_id,"return","head",comment="Нужна доработка").status_code)
        self.assertEqual(302,self._post(task_id,"execution/request_clarification","receiver",comment="Уточните результат").status_code)
        with self.app.app_context():
            event=TaskEvent.query.filter_by(event_type="returned_for_revision").one()
            self.assertEqual("head",event.actor_snapshot["role"]);self.assertEqual("Нужна доработка",event.details["comment"])

    def test_head_cancels_sent_states_and_author_only_unsent_request(self):
        for status in ("awaiting_assignment","assigned","in_progress","awaiting_clarification","recipient_review","sender_acceptance"):
            task_id=self._new_task(status,status=status,assignee_id=self.recipient_id)
            self.assertEqual(403,self._post(task_id,"cancel").status_code)
            self.assertEqual(400,self._post(task_id,"cancel","head",comment="").status_code)
            self.assertEqual(302,self._post(task_id,"cancel","head").status_code)
            self.assertEqual("cancelled",self._state(task_id)["status"])
        task_id=self._new_task("Своё",status="draft");self.assertEqual(302,self._post(task_id,"cancel").status_code)

    def test_new_actions_reject_foreign_receiver_and_missing_csrf(self):
        task_id=self._new_task("Защита",status="sender_acceptance",assignee_id=self.recipient_id)
        for action in ("confirm","return","cancel","deadline"):
            self.assertEqual(404,self._post(task_id,action,"outsider").status_code)
            self.assertEqual(403,self._post(task_id,action,"receiver").status_code)
        self._login("head");self.app.config["WTF_CSRF_ENABLED"]=True
        for action in ("confirm","return","cancel","deadline"):self.assertEqual(400,self.client.post(f"/tasks/{task_id}/{action}").status_code)

    def test_terminal_and_wrong_statuses_cannot_be_changed(self):
        for status in ("completed","cancelled","rejected"):
            task_id=self._new_task(status,status=status)
            before=self._counts()
            for action in ("confirm","return","cancel","deadline"):
                response=self._post(task_id,action,"head",**({"due_at":"2030-06-01T10:00"} if action=="deadline" else {}))
                self.assertEqual(403,response.status_code)
            self.assertEqual(before,self._counts())

    def test_stale_review_leaves_no_comment_event_or_notification_after_cancellation(self):
        task_id=self._new_task("Конфликт",status="sender_acceptance",assignee_id=self.recipient_id)
        self.assertEqual(302,self._post(task_id,"cancel","head").status_code);before=self._counts()
        with self.app.app_context(),self.assertRaises(WorkflowError):
            return_task_for_revision(task=db.session.get(Task,task_id),actor=db.session.get(User,self.ids["head"]),expected_revision=1,comment="Поздний возврат")
        self.assertEqual(before,self._counts());self.assertEqual("cancelled",self._state(task_id)["status"])

    def test_head_changes_mandatory_deadline_with_audit_and_cannot_clear_it(self):
        task_id=self._new_task("Срок")
        self.assertEqual(302,self._post(task_id,"deadline","head",due_at="2030-06-01T12:00").status_code)
        saved=self._state(task_id);self.assertEqual(7,saved["due_at"].hour)
        before=self._counts();self.assertEqual(400,self._post(task_id,"deadline","head",due_at="").status_code)
        self.assertEqual(saved,self._state(task_id));self.assertEqual(before,self._counts())
        with self.app.app_context():self.assertEqual(1,TaskEvent.query.filter_by(event_type="deadline_changed").count())

    def test_only_sender_head_can_change_sent_deadline(self):
        task_id=self._new_task("Полномочия срока")
        for who in ("employee","receiver","receiver-head","root"):
            self.assertEqual(403,self._post(task_id,"deadline",who,due_at="2030-06-01T12:00").status_code)
        self.assertEqual(302,self._post(task_id,"deadline","head",due_at="2030-06-01T12:00").status_code)

    def test_comments_escape_html_and_only_explicit_answer_resumes_work(self):
        task_id=self._new_task("Обсуждение",status="awaiting_clarification",assignee_id=self.recipient_id)
        self.assertEqual(302,self._post(task_id,"comments",comment="<script>alert(1)</script>").status_code)
        self.assertEqual("awaiting_clarification",self._state(task_id)["status"])
        page=self.client.get(f"/tasks/{task_id}").data;self.assertIn(b"&lt;script&gt;",page);self.assertNotIn(b"<script>alert(1)</script>",page)
        self.assertEqual(302,self._post(task_id,"execution/answer_clarification").status_code)
        self.assertEqual("in_progress",self._state(task_id)["status"])

    def test_every_file_type_is_private_validated_and_downloadable(self):
        task_id=self._new_task("Шесть форматов")
        for extension in MIME_TYPES:
            payload=attachment_bytes(extension);self.assertEqual(302,self._file(task_id,"Материалы"+extension,payload).status_code)
            with self.app.app_context():attachment_id=TaskAttachment.query.order_by(TaskAttachment.id.desc()).first().id
            download=self.client.get(f"/tasks/attachments/{attachment_id}/download")
            self.assertEqual(200,download.status_code);self.assertEqual(payload,download.data)
            self.assertEqual("nosniff",download.headers["X-Content-Type-Options"]);self.assertIn("no-store",download.headers["Cache-Control"]);download.close()
        with self.app.app_context():
            self.assertEqual(6,TaskAttachment.query.count());self.assertEqual(6,TaskEvent.query.filter_by(event_type="comment_added").count())
            self.assertTrue(all(item.comment_id is not None for item in TaskAttachment.query.all()))

    def test_invalid_forged_and_macro_uploads_leave_no_files_or_metadata(self):
        task_id=self._new_task("Подмена");generic=BytesIO()
        with ZipFile(generic,"w") as archive:archive.writestr("readme.txt","not office")
        macro=BytesIO(attachment_bytes(".docx"))
        with ZipFile(macro,"a") as archive:archive.writestr("word/vbaProject.bin",b"macro")
        before=self._counts()
        for filename,payload in (("program.exe",b"MZtest"),("fake.pdf",b"%PDF-1.4 not PDF"),("fake.png",attachment_bytes(".pdf")),
            ("fake.jpg",attachment_bytes(".png")),("wrong.xlsx",attachment_bytes(".docx")),("generic.docx",generic.getvalue()),
            ("macro.docx",macro.getvalue()),("broken.png",attachment_bytes(".png")[:20]),("empty.pdf",b"")):
            self.assertEqual(400,self._file(task_id,filename,payload).status_code,filename)
        self.assertEqual(before,self._counts());self.assertEqual([],list(Path(self.upload_directory.name).iterdir()))

    def test_file_limit_accepts_twenty_megabytes_and_rejects_one_byte_more(self):
        task_id=self._new_task("Лимит");image=attachment_bytes(".png");payload=image+b"\0"*(MAX_FILE_BYTES-len(image))
        response=self._file(task_id,"limit.png",payload);self.assertEqual(302,response.status_code);response.request.input_stream.close();response.close()
        response=self._file(task_id,"too-large.png",payload+b"x");self.assertEqual(413,response.status_code);response.request.input_stream.close();response.close()
        with self.app.app_context():self.assertEqual(1,TaskAttachment.query.count());self.assertEqual(MAX_FILE_BYTES,TaskAttachment.query.one().size_bytes)
        self.assertEqual(1,len(list(Path(self.upload_directory.name).iterdir())))

    def test_private_download_and_mutations_reject_foreign_and_moved_users(self):
        task_id=self._new_task("Секретный файл");self.assertEqual(302,self._file(task_id,"Секретный.pdf",attachment_bytes(".pdf")).status_code)
        with self.app.app_context():attachment_id=TaskAttachment.query.one().id
        self._login("outsider")
        for response in (self.client.get(f"/tasks/attachments/{attachment_id}/download"),self._post(task_id,"comments","outsider"),self._file(task_id,"fake.pdf",b"bad","outsider")):
            self.assertEqual(404,response.status_code);self.assertNotIn("Секретный.pdf".encode(),response.data)
        self._login("receiver");download=self.client.get(f"/tasks/attachments/{attachment_id}/download");self.assertEqual(200,download.status_code);download.close()
        with self.app.app_context():db.session.get(User,self.recipient_id).department_id=self.third_department_id;db.session.commit()
        self.assertEqual(404,self.client.get(f"/tasks/attachments/{attachment_id}/download").status_code)
        self._login("root");download=self.client.get(f"/tasks/attachments/{attachment_id}/download");self.assertEqual(200,download.status_code);download.close()

    def test_new_mutations_require_csrf_and_failure_cleans_storage(self):
        task_id=self._new_task("Откат файла");self._login();self.app.config["WTF_CSRF_ENABLED"]=True
        for suffix in ("deadline","comments","attachments"):self.assertEqual(400,self.client.post(f"/tasks/{task_id}/{suffix}").status_code)
        self.app.config["WTF_CSRF_ENABLED"]=False;before=self._counts()
        with patch("app.tasks.db.session.commit",side_effect=RuntimeError("synthetic failure")):
            with self.assertRaises(RuntimeError):self._file(task_id,"valid.png",attachment_bytes(".png"))
        self.assertEqual(before,self._counts());self.assertEqual([],list(Path(self.upload_directory.name).iterdir()))

    def test_create_form_optional_attachment_and_invalid_file_rolls_back(self):
        self._login();data=self.draft_form()
        self.assertEqual(302,self.client.post("/tasks/new",data=data|{"attachment":(BytesIO(attachment_bytes(".pdf")),"Инструкция.pdf")}).status_code)
        self.assertEqual(400,self.client.post("/tasks/new",data=data|{"title":"Не сохранять","attachment":(BytesIO(b"fake"),"fake.pdf")}).status_code)
        with self.app.app_context():self.assertIsNone(Task.query.filter_by(title="Не сохранять").first());self.assertEqual(1,TaskAttachment.query.count())

    def test_download_rejects_path_traversal_and_storage_refuses_public_directory(self):
        task_id=self._new_task("Безопасный путь");self.assertEqual(302,self._file(task_id,"../../folder\\Материалы.pdf",attachment_bytes(".pdf")).status_code)
        with self.app.app_context():
            row=TaskAttachment.query.one();self.assertEqual("Материалы.pdf",row.original_filename);attachment_id=row.id;row.storage_key="../outside.pdf";db.session.commit()
        self.assertEqual(404,self.client.get(f"/tasks/attachments/{attachment_id}/download").status_code)
        self.app.config["TASK_UPLOAD_DIR"]=self.app.static_folder
        with self.app.app_context(),self.assertRaises(RuntimeError):storage_root()

    def test_stale_deadline_cannot_overwrite_new_deadline_or_audit(self):
        task_id=self._new_task("Конфликт срока")
        self.assertEqual(302,self._post(task_id,"deadline","head",due_at="2030-06-01T12:00").status_code);before=self._counts()
        with self.app.app_context(),self.assertRaises(WorkflowError):
            change_task_deadline(task=db.session.get(Task,task_id),actor=db.session.get(User,self.ids["head"]),expected_revision=1,comment="Поздний перенос",due_at=datetime(2030,7,1,tzinfo=timezone.utc))
        self.assertEqual(before,self._counts());self.assertEqual(6,self._state(task_id)["due_at"].month)

    def test_sending_notifies_author_and_heads_not_entire_department(self):
        task_id=self._new_task("Точная отправка",status="pending_approval")
        self.assertEqual(302,self._post(task_id,"approval/approve","head").status_code)
        with self.app.app_context():
            owners={n.user_id for n in TaskNotification.query.all()}
            self.assertEqual({self.author_id,self.ids["receiver-head"],self.ids["receiver-head-two"]},owners)
            self.assertNotIn(self.recipient_id,owners)

    def _note(self,task_id,who="receiver",message="Личное сообщение"):
        with self.app.app_context():
            row=TaskNotification(task_id=task_id,user_id=self.ids[who],event_type="synthetic",message=message)
            db.session.add(row);db.session.commit();return row.id

    def test_notification_read_is_private_post_only_and_csrf_protected(self):
        task_id=self._new_task("Уведомление");note_id=self._note(task_id);self._login("receiver")
        page=self.client.get("/tasks/notifications").data
        self.assertEqual(405,self.client.get(f"/tasks/notifications/{note_id}/read").status_code)
        self.app.config["WTF_CSRF_ENABLED"]=True;self.assertEqual(400,self.client.post(f"/tasks/notifications/{note_id}/read").status_code)
        token=re.search(rb'name="csrf_token" value="([^"]+)"',page)[1].decode()
        self.assertEqual(302,self.client.post(f"/tasks/notifications/{note_id}/read",data={"csrf_token":token}).status_code)
        self.app.config["WTF_CSRF_ENABLED"]=False;self._login("receiver-two")
        self.assertEqual(404,self.client.post(f"/tasks/notifications/{note_id}/read").status_code)
        with self.app.app_context():self.assertTrue(db.session.get(TaskNotification,note_id).is_read)

    def test_moved_user_loses_notification_and_admin_reads_only_own_feed(self):
        task_id=self._new_task("Скрыть после перевода");note_id=self._note(task_id,message="Скрыть после перевода")
        self._login("receiver")
        with self.app.app_context():db.session.get(User,self.recipient_id).department_id=self.third_department_id;db.session.commit()
        self.assertNotIn("Скрыть после перевода".encode(),self.client.get("/tasks/notifications").data)
        self.assertEqual(404,self.client.post(f"/tasks/notifications/{note_id}/read").status_code)
        self._login("root");self.assertNotIn("Скрыть после перевода".encode(),self.client.get("/tasks/notifications").data)
        self.assertEqual(404,self.client.post(f"/tasks/notifications/{note_id}/read").status_code)
        self.assertEqual(200,self.client.get(f"/tasks/{task_id}").status_code)

    def test_notification_center_paginates_and_escapes_message_content(self):
        task_id=self._new_task("Пагинация")
        for i in range(27):self._note(task_id,message=f"Сообщение {i} <script>test</script>")
        self._login("receiver");page=self.client.get("/tasks/notifications").data
        self.assertIn("Страница 1 из 2".encode(),page);self.assertIn(b"&lt;script&gt;",page);self.assertNotIn(b"<script>",page)
        self.assertIn("Страница 2 из 2".encode(),self.client.get("/tasks/notifications?page=2").data)


if __name__ == "__main__":
    unittest.main()
