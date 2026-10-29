import os
import sys
import unittest
import re
import tempfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile
from datetime import datetime, timezone

from sqlalchemy import inspect
from sqlalchemy.orm import Session
from PIL import Image
from pypdf import PdfWriter

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app import create_app
from app.extensions import db
from app.models import Department, Task, TaskAttachment, TaskComment, TaskEvent, TaskNotification, User
from app.workflow import (
    WorkflowError, update_task_before_acceptance,
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

    def model_task(self, **changes):
        return Task(**(dict(title="Подготовить класс", description="Нужны программы",
            author_id=self.employee.id, sender_department_id=self.sender.id,
            recipient_department_id=self.recipient.id, due_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
            sent_at=datetime(2029, 12, 1, tzinfo=timezone.utc)) | changes))

    def test_model_defaults_do_not_run_workflow(self):
        task = self.model_task()
        db.session.add(task)
        db.session.commit()
        self.assertEqual("new", task.status)
        self.assertEqual("normal", task.priority)
        self.assertIsNone(task.assignee_id)
        self.assertFalse(task.is_rework)
        self.assertEqual(0, task.rework_cycle)
        self.assertEqual(32, len(task.creation_key))
        self.assertEqual(0, task.revision)
        self.assertEqual(0, TaskEvent.query.count())
        self.assertEqual(0, TaskNotification.query.count())
        # Direct-send authorship and event delivery are workflow tests in T004/T008.

    def test_model_rejects_invalid_status_priority_deadline_and_sender(self):
        from sqlalchemy.exc import IntegrityError
        for changes in ({"status": "draft"}, {"priority": "critical"}, {"due_at": None},
                        {"sent_at": None}, {"recipient_department_id": self.sender.id},
                        {"rework_cycle": -1}):
            with self.subTest(changes=changes):
                task = self.model_task(**changes)
                db.session.add(task)
                with self.assertRaises(IntegrityError):
                    db.session.commit()
                db.session.rollback()
        self.assertEqual(0, Task.query.count())

    def test_task_and_related_entities_are_mapped(self):
        task = self.model_task(priority="urgent")
        db.session.add(task)
        db.session.flush()
        comment = TaskComment(task_id=task.id, author_id=self.employee.id, body="Описание")
        db.session.add(comment)
        db.session.flush()
        db.session.add(TaskEvent(task_id=task.id, actor_id=self.employee.id,
            event_type="sent", details={"comment": comment.body}, comment_id=comment.id))
        db.session.commit()
        self.assertEqual("urgent", task.priority)
        self.assertEqual(1, len(task.comments))
        self.assertEqual(1, len(task.events))
        self.assertEqual(comment.id, task.events[0].comment_id)

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


# Broad HTTP regressions use the current direct workflow; former stages stay forbidden.
from support.portal_fixtures import PortalCase


class TaskListRouteTests(PortalCase):
    def test_direct_send_is_immediately_visible_to_both_departments(self):
        self.login("employee")
        response=self.client.post("/tasks/new",data=self.creation_form(title="Подготовить кабинет"));self.assertEqual(302,response.status_code)
        with self.app.app_context():
            task=Task.query.filter_by(title="Подготовить кабинет").one();ident=task.id
            self.assertEqual("new",task.status);self.assertIsNotNone(task.due_at);self.assertIsNotNone(task.sent_at)
            self.assertEqual(self.ids["employee"],task.author_id)
        self.assertIn(f'data-task-id="{ident}"'.encode(),self.client.get("/tasks/sent").data)
        self.add_member("recipient",department_id=self.other_department_id)
        self.client=self.app.test_client();self.login("recipient")
        self.assertIn(f'data-task-id="{ident}"'.encode(),self.client.get("/tasks/received").data)
        self.assertEqual(200,self.client.get(f"/tasks/{ident}").status_code)

    def test_removed_status_priority_assignee_and_overdue_filters_do_not_hide_tasks(self):
        ids=[self.task(priority=p) for p in ("low","urgent")];self.login("employee")
        source=self.client.get("/tasks/sent?status=completed&priority=low&assignee_id=unassigned&deadline_state=overdue").data
        for ident in ids:self.assertIn(f'data-task-id="{ident}"'.encode(),source)
        self.assertNotIn(b'name="priority"',source)

    def test_removed_date_range_parameters_are_ignored_in_both_directions(self):
        outgoing=self.task();incoming=self.task(sender_department_id=self.other_department_id,recipient_department_id=self.department_id)
        self.login("employee")
        for direction,ident in (("sent",outgoing),("received",incoming)):
            self.assertIn(f'data-task-id="{ident}"'.encode(),self.client.get(f"/tasks/{direction}?due_from=1990-01-01&due_to=1990-01-02").data)

    def test_college_midnight_is_displayed_without_old_calendar_filter(self):
        ident=self.task(due_at=datetime(2030,6,1,19,tzinfo=timezone.utc));self.login("employee")
        source=self.client.get("/tasks/sent?due_from=2030-06-01&due_to=2030-06-01").data
        self.assertIn(f'data-task-id="{ident}"'.encode(),source);self.assertIn("02.06.2030 00:00".encode(),source)


class TaskRouteTests(PortalCase):
    def setUp(self):
        super().setUp();self.upload_directory=tempfile.TemporaryDirectory(prefix="portal_case_")
        self.app.config["TASK_UPLOAD_DIR"]=self.upload_directory.name
        for name,role in (("receiver","employee"),("receiver-two","employee"),("receiver-head","head"),("receiver-head-two","head")):
            self.add_member(name,role=role,department_id=self.other_department_id)
        self.author_id=self.ids["employee"];self.recipient_id=self.ids["receiver"]

    def tearDown(self):super().tearDown();self.upload_directory.cleanup()
    def _login(self,who="employee"):
        self.client=self.app.test_client();return self.login(who)
    def _new_task(self,title,status="new",assignee_id=None,**values):
        return self.task(**(dict(title=title,status=status,assignee_ids=[] if assignee_id is None else [assignee_id],
            completed_at=datetime(2029,12,31,tzinfo=timezone.utc) if status=="completed" else None)|values))
    def _state(self,task_id):
        with self.app.app_context():
            task=db.session.get(Task,task_id)
            return {k:getattr(task,k) for k in ("title","description","status","revision","assignee_ids","due_at","completed_at","is_rework","rework_cycle")}
    def _counts(self):
        with self.app.app_context():return tuple(m.query.count() for m in (TaskEvent,TaskComment,TaskAttachment,TaskNotification))
    def _post(self,task_id,suffix,who="employee",**data):
        self._login(who);payload=dict(revision=str(self._state(task_id)["revision"]),comment="Пояснение действия")|data
        response=self.client.post(f"/tasks/{task_id}/{suffix}",data=payload)
        response.get_data();response.close();response.request.input_stream.close();return response
    def _file(self,task_id,filename,payload,who="employee"):
        return self._post(task_id,"attachments",who,attachment=(BytesIO(payload),filename))

    def test_old_edit_and_approval_cannot_mutate_sent_content(self):
        ident=self._new_task("Зафиксированное описание");before=self._state(ident),self._counts()
        for endpoint in ("edit","approval/submit","approval/approve","approval/withdraw"):
            self.assertEqual(410,self._post(ident,endpoint,title="Подмена").status_code)
        self.assertEqual(before,(self._state(ident),self._counts()))

    def test_post_without_session_and_foreign_task_id_are_denied(self):
        self.assertEqual(302,self.client.post("/tasks/new").status_code)
        ident=self._new_task("Частная задача");self._login("outsider")
        self.assertEqual(404,self.client.get(f"/tasks/{ident}").status_code)
        self.assertEqual(404,self.client.post(f"/tasks/{ident}/claim",data=dict(revision=0,comment="Подмена")).status_code)

    def test_assignment_completion_return_and_recompletion_are_direct(self):
        ident=self._new_task("Рабочий процесс")
        for endpoint,actor,extra in (("assign","receiver-head",dict(assignee_ids=[self.recipient_id])),("complete","receiver",{}),("return","employee",{}),("complete","receiver",{})):
            self.assertEqual(302,self._post(ident,endpoint,actor,**extra).status_code)
        state=self._state(ident);self.assertEqual(("completed",4,1,False),(state["status"],state["revision"],state["rework_cycle"],state["is_rework"]))
        with self.app.app_context():self.assertEqual(["assigned","completed","returned_for_revision","completed"],[e.event_type for e in TaskEvent.query.order_by(TaskEvent.id)])

    def test_clarification_is_retired_and_route_authorization_is_preserved(self):
        ident=self._new_task("Уточнения",status="in_progress",assignee_id=self.recipient_id);before=self._counts()
        for suffix in ("claim","status","reassign"):
            self.assertEqual(404,self._post(ident,suffix,"outsider").status_code)
        for suffix in ("execution/request_clarification","execution/answer_clarification","clarification","status"):
            self.assertEqual(410,self._post(ident,suffix,"receiver").status_code)
        self.assertEqual(before,self._counts());self.assertEqual(405,self.client.get(f"/tasks/{ident}/claim").status_code)

    def test_actions_require_real_csrf_and_head_can_reassign_through_form(self):
        ident=self._new_task("CSRF",status="in_progress",assignee_id=self.recipient_id);self._login("receiver-head")
        page=self.client.get(f"/tasks/{ident}").data;self.app.config["WTF_CSRF_ENABLED"]=True
        payload=dict(revision="0",comment="Переназначаю",assignee_ids=[self.ids["receiver-two"]])
        self.assertEqual(400,self.client.post(f"/tasks/{ident}/reassign",data=payload).status_code)
        token=re.search(rb'name="csrf_token" value="([^"]+)"',page)[1].decode()
        self.assertEqual(302,self.client.post(f"/tasks/{ident}/reassign",data=payload|dict(csrf_token=token)).status_code)
        self.assertEqual([self.ids["receiver-two"]],self._state(ident)["assignee_ids"])

    def test_two_stale_assignments_leave_one_assignment_and_event(self):
        ident=self._new_task("Два назначения")
        self.assertEqual(302,self._post(ident,"assign","receiver-head",revision=0,assignee_ids=[self.recipient_id]).status_code)
        before=self._counts();self.assertEqual(409,self._post(ident,"assign","receiver-head-two",revision=0,assignee_ids=[self.ids["receiver-two"]]).status_code)
        self.assertEqual(before,self._counts());self.assertEqual([self.recipient_id],self._state(ident)["assignee_ids"])
        self.assertEqual(409,self._post(ident,"claim","receiver").status_code)
        self.assertEqual(302,self._post(ident,"claim","receiver-two").status_code)
        self.assertEqual([self.recipient_id,self.ids["receiver-two"]],self._state(ident)["assignee_ids"])

    def test_stale_edit_cannot_overwrite_a_changed_task_from_another_session(self):
        ident=self._new_task("Не перезаписать")
        with self.app.app_context():
            stale=db.session.get(Task,ident);actor=db.session.get(User,self.author_id)
            with Session(db.engine) as other:
                current=other.get(Task,ident);current.status="in_progress";current.assignee_id=self.recipient_id;current.revision=1;other.commit()
            with self.assertRaises(WorkflowError):update_task_before_acceptance(task=stale,actor=actor,expected_revision=0,comment="Поздняя правка",title="Подмена")
            db.session.rollback()
        self.assertEqual(("Не перезаписать",1),(self._state(ident)["title"],self._state(ident)["revision"]))

    def test_completion_requires_no_sender_acceptance_and_notifies_source(self):
        ident=self._new_task("Результат",status="in_progress",assignee_id=self.recipient_id)
        self.assertEqual(403,self._post(ident,"complete","head").status_code)
        self.assertEqual(302,self._post(ident,"complete","receiver").status_code)
        self.assertEqual(410,self._post(ident,"confirm","head").status_code)
        with self.app.app_context():
            event=TaskEvent.query.one();self.assertEqual(("completed",self.recipient_id),(event.event_type,event.actor_id))
            self.assertIn(self.author_id,{n.user_id for n in TaskNotification.query.all()})

    def test_return_requires_comment_and_preserves_actor_snapshot(self):
        ident=self._new_task("Возврат",status="completed",assignee_id=self.recipient_id);before=self._counts()
        self.assertEqual(400,self._post(ident,"return","head",comment=" ").status_code);self.assertEqual(before,self._counts())
        self.assertEqual(302,self._post(ident,"return","head",comment="Нужна доработка").status_code)
        with self.app.app_context():
            event=TaskEvent.query.one();self.assertEqual("head",event.actor_snapshot["role"]);self.assertEqual("Нужна доработка",event.details["comment"])
        self.assertEqual("in_progress",self._state(ident)["status"])

    def test_source_author_or_head_can_cancel_both_active_states(self):
        for status in ("new","in_progress"):
            for who in ("employee","head"):
                ident=self._new_task(status,status=status,assignee_id=self.recipient_id if status=="in_progress" else None)
                self.assertEqual(400,self._post(ident,"cancel",who,comment="").status_code)
                self.assertEqual(302,self._post(ident,"cancel",who).status_code);self.assertEqual("cancelled",self._state(ident)["status"])

    def test_current_mutations_reject_foreign_receiver_and_missing_csrf(self):
        ident=self._new_task("Защита")
        for endpoint in ("return","cancel","deadline"):
            self.assertEqual(404,self._post(ident,endpoint,"outsider").status_code);self.assertEqual(403,self._post(ident,endpoint,"receiver").status_code)
        self._login("head");self.app.config["WTF_CSRF_ENABLED"]=True
        for endpoint in ("return","cancel","deadline"):self.assertEqual(400,self.client.post(f"/tasks/{ident}/{endpoint}").status_code)

    def test_closed_states_are_immutable_except_explicit_return_of_completed(self):
        for status in ("completed","cancelled","rejected"):
            ident=self._new_task(status,status=status,assignee_id=self.recipient_id);before=self._counts()
            self.assertEqual(409,self._post(ident,"cancel","head").status_code)
            self.assertEqual(409,self._post(ident,"deadline","head",due_at="2030-06-01T10:00").status_code)
            if status!="completed":self.assertEqual(409,self._post(ident,"return","head").status_code)
            self.assertEqual(before,self._counts())
            if status=="completed":self.assertEqual(302,self._post(ident,"return","head").status_code)

    def test_stale_result_action_after_cancel_has_no_partial_side_effect(self):
        ident=self._new_task("Конфликт",status="in_progress",assignee_id=self.recipient_id)
        self.assertEqual(302,self._post(ident,"cancel","head").status_code);before=self._counts()
        self.assertEqual(409,self._post(ident,"complete","receiver",revision=0).status_code)
        self.assertEqual(before,self._counts());self.assertEqual("cancelled",self._state(ident)["status"])

    def test_head_changes_deadline_with_history_and_cannot_clear_it(self):
        ident=self._new_task("Срок");self.assertEqual(302,self._post(ident,"deadline","head",due_at="2030-06-01T12:00").status_code)
        saved=self._state(ident);self.assertEqual(7,saved["due_at"].hour);before=self._counts()
        self.assertEqual(400,self._post(ident,"deadline","head",due_at="").status_code)
        self.assertEqual(saved,self._state(ident));self.assertEqual(before,self._counts())

    def test_only_sender_head_can_change_sent_deadline(self):
        ident=self._new_task("Полномочия срока")
        for who in ("employee","receiver","receiver-head","root"):self.assertEqual(403,self._post(ident,"deadline",who,due_at="2030-06-01T12:00").status_code)
        self.assertEqual(302,self._post(ident,"deadline","head",due_at="2030-06-01T12:00").status_code)

    def test_discussion_escapes_html_and_does_not_change_work_fields(self):
        ident=self._new_task("Обсуждение",status="in_progress",assignee_id=self.recipient_id);before=self._state(ident)
        self.assertEqual(302,self._post(ident,"comments",comment="<script>alert(1)</script>").status_code)
        after=self._state(ident);self.assertEqual(before|dict(revision=1),after)
        page=self.client.get(f"/tasks/{ident}").data;self.assertIn(b"&lt;script&gt;",page);self.assertNotIn(b"<script>alert(1)</script>",page)

    def test_every_file_type_is_private_validated_and_downloadable(self):
        ident=self._new_task("Шесть форматов")
        for extension in MIME_TYPES:
            payload=attachment_bytes(extension);self.assertEqual(302,self._file(ident,"Материалы"+extension,payload).status_code)
            with self.app.app_context():aid=TaskAttachment.query.order_by(TaskAttachment.id.desc()).first().id
            with self.client.get(f"/tasks/attachments/{aid}/download") as response:
                self.assertEqual(200,response.status_code);self.assertEqual(payload,response.data);self.assertEqual("nosniff",response.headers["X-Content-Type-Options"]);self.assertIn("no-store",response.headers["Cache-Control"])
        with self.app.app_context():self.assertEqual(6,TaskAttachment.query.count());self.assertTrue(all(a.comment_id for a in TaskAttachment.query.all()))

    def test_invalid_forged_macro_uploads_leave_no_files_or_metadata(self):
        ident=self._new_task("Подмена");generic=BytesIO()
        with ZipFile(generic,"w") as archive:archive.writestr("readme.txt","not office")
        macro=BytesIO(attachment_bytes(".docx"))
        with ZipFile(macro,"a") as archive:archive.writestr("word/vbaProject.bin",b"macro")
        before=self._counts()
        for name,body in (("bad.exe",b"bad"),("bad.png",b"not image"),("bad.pdf",b"not pdf"),("bad.docx",generic.getvalue()),("macro.docx",macro.getvalue())):
            self.assertEqual(400,self._file(ident,name,body).status_code);self.assertEqual(before,self._counts())
        self.assertEqual([], [p for p in Path(self.upload_directory.name).rglob("*") if p.is_file()])

    def test_twenty_megabytes_allowed_but_one_byte_more_rejected(self):
        ident=self._new_task("Размер");data=attachment_bytes(".png");data+=b"x"*(MAX_FILE_BYTES-len(data))
        self.assertEqual(302,self._file(ident,"size.png",data).status_code);before=self._counts()
        self.assertEqual(413,self._file(ident,"size.png",data+b"x").status_code);self.assertEqual(before,self._counts())

    def test_private_download_and_mutation_reject_foreign_and_moved_users(self):
        ident=self._new_task("Приватно");self.assertEqual(302,self._file(ident,"private.pdf",attachment_bytes(".pdf")).status_code)
        with self.app.app_context():aid=TaskAttachment.query.one().id
        for who in ("outsider","receiver"):
            if who=="receiver":
                with self.app.app_context():db.session.get(User,self.recipient_id).department_id=self.third_department_id;db.session.commit()
            self._login(who);self.assertEqual(404,self.client.get(f"/tasks/attachments/{aid}/download").status_code)
            self.assertEqual(404,self.client.post(f"/tasks/{ident}/comments",data=dict(revision=1,comment="Подмена")).status_code)

    def test_file_commit_failure_cleans_new_storage_and_preserves_metadata(self):
        from sqlalchemy.exc import SQLAlchemyError
        ident=self._new_task("Ошибка файла");self._login();before=self._counts()
        with patch("app.tasks.db.session.commit",side_effect=SQLAlchemyError("Synthetic failure")):
            response=self.client.post(f"/tasks/{ident}/attachments",data=dict(revision=0,comment="Не сохранить",attachment=(BytesIO(attachment_bytes(".pdf")),"private.pdf")))
        self.assertEqual(500,response.status_code);self.assertEqual(before,self._counts())
        self.assertEqual([], [p for p in Path(self.upload_directory.name).rglob("*") if p.is_file()])

    def test_new_attachment_and_description_are_one_atomic_send(self):
        self._login();payload=self.creation_form(attachment=(BytesIO(b"forged"),"bad.pdf"))
        self.assertEqual(400,self.client.post("/tasks/new",data=payload).status_code)
        with self.app.app_context():self.assertEqual(0,Task.query.count());self.assertEqual(0,TaskEvent.query.count())
        payload=self.creation_form(attachment=(BytesIO(attachment_bytes(".pdf")),"good.pdf"))
        self.assertEqual(302,self.client.post("/tasks/new",data=payload).status_code)
        with self.app.app_context():self.assertEqual((1,1,1),(Task.query.count(),TaskComment.query.count(),TaskAttachment.query.count()))

    def test_download_path_traversal_and_public_storage_are_refused(self):
        ident=self._new_task("Путь");self.assertEqual(302,self._file(ident,"private.pdf",attachment_bytes(".pdf")).status_code)
        with self.app.app_context():row=TaskAttachment.query.one();row.storage_key="../outside.pdf";aid=row.id;db.session.commit()
        self.assertEqual(404,self.client.get(f"/tasks/attachments/{aid}/download").status_code)
        self.app.config["TASK_UPLOAD_DIR"]=str(Path(self.app.static_folder)/"uploads")
        with self.app.app_context(),self.assertRaisesRegex(RuntimeError,"вне static"):storage_root()

    def test_stale_deadline_cannot_overwrite_new_deadline_or_history(self):
        ident=self._new_task("Версия срока");self.assertEqual(302,self._post(ident,"deadline","head",revision=0,due_at="2030-06-01T12:00").status_code)
        before=self._state(ident),self._counts();self.assertEqual(409,self._post(ident,"deadline","head",revision=0,due_at="2030-07-01T12:00").status_code)
        self.assertEqual(before,(self._state(ident),self._counts()))

    def test_direct_send_notifies_author_and_recipient_heads_not_every_employee(self):
        self._login();self.assertEqual(302,self.client.post("/tasks/new",data=self.creation_form()).status_code)
        with self.app.app_context():
            self.assertEqual({self.author_id,self.ids["receiver-head"],self.ids["receiver-head-two"]},{n.user_id for n in TaskNotification.query.all()})
            self.assertEqual("sent",TaskEvent.query.one().event_type)

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
