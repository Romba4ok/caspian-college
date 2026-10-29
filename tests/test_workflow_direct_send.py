"""Immediate dispatch, authenticated retries and atomic first-comment files."""
from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.datastructures import MultiDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.extensions import db
from app.models import Department, Task, TaskAttachment, TaskCategory, TaskComment, TaskEvent, TaskNotification, User, SystemSettings
from app.workflow import create_task, make_creation_key, WorkflowError
from support.portal_fixtures import PortalCase
from support.migration_fixtures import MigrationDatabase
from test_tasks import attachment_bytes


class DirectSendTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.files = tempfile.TemporaryDirectory(prefix="portal_case_")
        self.app.config["TASK_UPLOAD_DIR"] = self.files.name
        for name, role, dept in (("colleague", "employee", self.department_id),
            ("receiver", "employee", self.other_department_id),
            ("receiver-head", "head", self.other_department_id), ("receiver-head2", "head", self.other_department_id)):
            self.add_member(name, role=role, department_id=dept)
        with self.app.app_context():
            category = TaskCategory(name="Документы")
            db.session.add(category)
            db.session.commit()
            self.category_id = category.id
        self.client = self.client_for("employee")

    def tearDown(self):
        super().tearDown()
        self.files.cleanup()

    def client_for(self, name):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(self.ids[name])
            session["_fresh"] = True
        return client

    def snapshot(self):
        with self.app.app_context():
            return {m.__tablename__: deepcopy(db.session.execute(select(m.__table__).order_by(m.id)).all())
                    for m in (Task, TaskComment, TaskEvent, TaskAttachment, TaskNotification)}

    def post(self, payload, *, client=None):
        return (client or self.client).post("/tasks/new", data=payload)

    def assert_sent(self, response):
        self.assertEqual(302, response.status_code, response.get_data(as_text=True)[:400])
        return int(response.location.rsplit("/", 1)[1])

    def installed(self):
        return {p.name: p.read_bytes() for p in Path(self.files.name).iterdir() if p.is_file()}

    def test_employee_and_head_send_immediately_with_one_description_comment(self):
        for name in ("employee", "head"):
            client = self.client_for(name)
            form = self.creation_form(client=client, title="  Документы  ", description="  Полный текст  ",
                priority="urgent", category_id=str(self.category_id), contact_person="  Анна  ")
            ident = self.assert_sent(self.post(form, client=client))
            with self.app.app_context():
                task = db.session.get(Task, ident)
                self.assertEqual(("new", 1, None, False, 0), (task.status, task.revision, task.assignee_id, task.is_rework, task.rework_cycle))
                self.assertEqual(self.ids[name], task.author_id)
                self.assertEqual(self.department_id, task.sender_department_id)
                self.assertEqual(("Документы", "Полный текст", "Документы", "Анна"), (task.title, task.description, task.category, task.contact_person))
                self.assertIsNotNone(task.sent_at)
                self.assertEqual(7, task.due_at.hour)  # 12:00 in Asia/Qyzylorda.
                event = TaskEvent.query.filter_by(task_id=ident).one()
                comment = TaskComment.query.filter_by(task_id=ident).one()
                self.assertEqual("sent", event.event_type)
                self.assertEqual("new", event.details["after"]["status"])
                self.assertFalse(event.details["after"]["is_rework"])
                self.assertEqual(0, event.details["after"]["rework_cycle"])
                self.assertEqual("Полный текст", comment.body)
                self.assertEqual(comment.id, event.comment_id)
                self.assertEqual(64, len(event.details["creation_digest"]))
                recipients = {n.user_id for n in TaskNotification.query.filter_by(task_id=ident)}
                self.assertEqual({self.ids[name], self.ids["receiver-head"], self.ids["receiver-head2"]}, recipients)
            self.assertEqual(200, self.client_for("receiver").get(f"/tasks/{ident}").status_code)
            self.assertEqual(200, self.client_for("root").get(f"/tasks/{ident}").status_code)
            self.assertEqual(404, self.client_for("outsider").get(f"/tasks/{ident}").status_code)

    def test_repeat_returns_same_task_without_duplicate_events_or_notifications(self):
        form = self.creation_form()
        first = self.assert_sent(self.post(form))
        before = self.snapshot()
        second = self.assert_sent(self.post(form))
        self.assertEqual(first, second)
        self.assertEqual(before, self.snapshot())

    def test_repeat_after_later_task_change_does_not_rewrite_current_task(self):
        form = self.creation_form()
        ident = self.assert_sent(self.post(form))
        with self.app.app_context():
            task = db.session.get(Task, ident)
            task.status = "in_progress"
            task.assignee_id = self.ids["receiver"]
            task.revision = 9
            task.due_at = datetime(2031, 1, 1, tzinfo=timezone.utc)
            db.session.get(TaskCategory, self.category_id).is_archived = True
            db.session.get(Department, self.other_department_id).is_archived = True
            db.session.commit()
        before = self.snapshot()
        self.assertEqual(ident, self.assert_sent(self.post(form)))
        self.assertEqual(before, self.snapshot())

    def test_changed_payload_same_key_is_conflict_and_key_is_retained(self):
        form = self.creation_form()
        self.assert_sent(self.post(form))
        before = self.snapshot()
        response = self.post(form | {"description": "Другое поручение"})
        self.assertEqual(409, response.status_code)
        self.assertIn(form["creation_key"].encode(), response.data)
        self.assertIn("Другое поручение".encode(), response.data)
        self.assertEqual(before, self.snapshot())

    def test_new_key_allows_a_separate_identical_task(self):
        self.assert_sent(self.post(self.creation_form()))
        self.assert_sent(self.post(self.creation_form()))
        with self.app.app_context():
            self.assertEqual(2, Task.query.count())

    def test_key_is_required_authentic_and_bound_to_creator(self):
        form = self.creation_form()
        for key in (None, "", "a" * 32, "0" * 64, form["creation_key"][:-1] + ("0" if form["creation_key"][-1] != "0" else "1")):
            payload = dict(form)
            if key is None:
                payload.pop("creation_key")
            else:
                payload["creation_key"] = key
            self.assertEqual(400, self.post(payload).status_code)
        self.assertEqual(400, self.post(form, client=self.client_for("colleague")).status_code)
        self.assert_sent(self.post(form))
        before = self.snapshot()
        self.assertEqual(400, self.post(form, client=self.client_for("colleague")).status_code)
        self.assertEqual(before, self.snapshot())

    def test_required_fields_and_unknown_values_do_not_create_partial_rows(self):
        form = self.creation_form()
        for changes in ({"title":" "}, {"description":" "}, {"title":"x"*201}, {"contact_person":"x"*161},
            {"priority":"critical"}, {"due_at":""}, {"due_at":"bad"}, {"category_id":"unknown"},
            {"category_id":"999999"}, {"recipient_department_id":str(self.department_id)},
            {"recipient_department_id":"999999"}, {"recipient_department_id":"9"*100}):
            before = self.snapshot()
            self.assertEqual(400, self.post(form | changes).status_code, changes)
            self.assertEqual(before, self.snapshot())

    def test_forged_extra_duplicate_fields_and_file_field_are_rejected(self):
        form = self.creation_form()
        for field in ("author_id", "sender_department_id", "assignee_id", "status", "revision", "category",
                      "sent_at", "legacy_missing_due", "is_legacy_dispatched", "is_rework", "rework_cycle"):
            self.assertEqual(400, self.post(form | {field:"1"}).status_code, field)
        data = MultiDict(form)
        data.add("creation_key", form["creation_key"])
        self.assertEqual(400, self.post(data).status_code)
        self.assertEqual(400, self.post(form | {"other_file":(BytesIO(b"bad"),"x.pdf")}).status_code)
        with self.app.app_context():
            self.assertEqual(0, Task.query.count())

    def test_archived_source_destination_and_category_are_rechecked(self):
        form = self.creation_form(category_id=str(self.category_id))
        for model, ident, field, expected in ((TaskCategory, self.category_id, "is_archived", 400),
            (Department, self.other_department_id, "is_archived", 400),
            (Department, self.department_id, "is_archived", 403)):
            with self.app.app_context():
                row = db.session.get(model, ident)
                setattr(row, field, True)
                db.session.commit()
            self.assertEqual(expected, self.post(form).status_code)
            with self.app.app_context():
                setattr(db.session.get(model, ident), field, False)
                db.session.commit()
        self.assert_sent(self.post(form))

    def test_admin_cannot_create_and_blocked_session_cannot_send(self):
        form = self.creation_form()
        admin = self.client_for("root")
        self.assertEqual(403, admin.get("/tasks/new").status_code)
        self.assertEqual(403, self.post(form, client=admin).status_code)
        with self.app.app_context():
            db.session.get(User, self.ids["employee"]).is_blocked = True
            db.session.commit()
        self.assertEqual(302, self.post(form).status_code)

    def test_service_reloads_actor_and_does_not_trust_forged_role_or_department(self):
        with self.app.app_context():
            fake = SimpleNamespace(id=self.ids["employee"], role="head", department_id=self.third_department_id)
            key = make_creation_key(fake)
            task = create_task(actor=fake, recipient_department_id=self.other_department_id,
                title="Direct", description="Required", due_at=datetime(2030,1,1,tzinfo=timezone.utc), creation_key=key)
            db.session.commit()
            self.assertEqual(self.department_id, task.sender_department_id)
            db.session.execute(update(User).where(User.id==fake.id).values(is_blocked=True))
            db.session.commit()
            with self.assertRaises(PermissionError):
                create_task(actor=fake, recipient_department_id=self.other_department_id, title="Direct",
                    description="Required", due_at=datetime(2030,1,1,tzinfo=timezone.utc), creation_key=key)
            db.session.rollback()

    def test_initial_files_share_first_comment_and_replay_does_not_install_again(self):
        form = self.creation_form()
        filedata = attachment_bytes(".pdf")
        ident = self.assert_sent(self.post(form | {"attachment":(BytesIO(filedata),"Материал.pdf")}))
        before, files = self.snapshot(), self.installed()
        self.assertEqual(ident, self.assert_sent(self.post(form | {"attachment":(BytesIO(filedata),"Материал.pdf")})))
        self.assertEqual(files, self.installed())
        self.assertEqual(before, self.snapshot())
        with self.app.app_context():
            attachment = TaskAttachment.query.one()
            self.assertEqual(TaskComment.query.one().id, attachment.comment_id)
            self.assertEqual(filedata, files[attachment.storage_key])
        # Omitting or changing attachment bytes/name changes the submission.
        self.assertEqual(409, self.post(form).status_code)
        self.assertEqual(409, self.post(form | {"attachment":(BytesIO(filedata),"Other.pdf")}).status_code)
        self.assertEqual(409, self.post(form | {"attachment":(BytesIO(filedata+b"\n"),"Материал.pdf")}).status_code)
        self.assertEqual(files, self.installed())

    def test_upload_and_commit_or_audit_failure_roll_back_every_new_resource(self):
        form = self.creation_form()
        original = Path(self.files.name, "original-existing-file")
        original.write_bytes(b"keep")
        self.assertEqual(400, self.post(form | {"attachment":(BytesIO(b"fake PDF"),"x.pdf")}).status_code)
        for target in ("app.tasks.db.session.commit", "app.audit.record_task_event"):
            with patch(target, side_effect=SQLAlchemyError("synthetic failure")):
                response = self.post(form | {"attachment":(BytesIO(attachment_bytes(".pdf")),"x.pdf")})
                self.assertEqual(500, response.status_code)
                self.assertIn(form["creation_key"].encode(), response.data)
            self.assertEqual({original.name:b"keep"}, self.installed())
            self.assertTrue(all(not rows for rows in self.snapshot().values()))
        self.assert_sent(self.post(form))

    def test_csrf_and_readable_form_contract(self):
        page = self.client.get("/tasks/new").get_data(as_text=True)
        self.assertIn('name="creation_key"', page)
        self.assertIn('name="csrf_token"', page)
        self.assertIn("Отправить задачу", page)
        self.assertNotIn("Сохранить черновик", page)
        form = self.creation_form()
        self.app.config["WTF_CSRF_ENABLED"] = True
        self.assertEqual(400, self.post(form).status_code)
        import re
        page = self.client.get("/tasks/new").get_data(as_text=True)
        form["creation_key"] = re.search(r'name="creation_key" value="([0-9a-f]{64})"', page).group(1)
        form["csrf_token"] = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        self.assert_sent(self.post(form))

    def test_fixed_creation_upload_policy_ignores_retired_settings(self):
        with self.app.app_context():
            db.session.add(SystemSettings(id=1, max_upload_mb=1, allowed_extensions=[]))
            db.session.commit()
        form = self.creation_form()
        self.assert_sent(self.post(form | {"attachment":(BytesIO(attachment_bytes(".png")),"x.png")}))
        with self.app.app_context():
            self.assertEqual("image/png", TaskAttachment.query.one().content_type)

    def test_normalized_replay_preserves_original_snapshot(self):
        form = self.creation_form(title="  Нормализация  ", description="  Описание  ", category_id=str(self.category_id))
        ident = self.assert_sent(self.post(form))
        with self.app.app_context():
            db.session.get(TaskCategory, self.category_id).name = "Другое название"
            db.session.get(User, self.ids["employee"]).full_name = "Другое имя"
            db.session.commit()
        before = self.snapshot()
        self.assertEqual(ident, self.assert_sent(self.post(form | {"title":"Нормализация", "description":"Описание"})))
        self.assertEqual(before, self.snapshot())


@unittest.skipUnless(os.environ.get("PORTAL_TEST_DATABASE_URL"), "Use isolated local_portal.py verify")
class PostgreSQLDirectSend(MigrationDatabase, unittest.TestCase):
    postgres = True

    def setUp(self):
        super().setUp()
        self.upgrade()
        departments = self.rows("departments")
        self.sender, self.receiver = departments[0]["id"], departments[1]["id"]
        actor = User(username="direct-author", full_name="Synthetic author", role="employee",
            department_id=self.sender, password_hash="synthetic")
        head = User(username="direct-head", full_name="Synthetic head", role="head",
            department_id=self.receiver, password_hash="synthetic")
        db.session.add_all((actor,head))
        db.session.commit()
        self.actor_id = actor.id
        self.key = make_creation_key(actor)
        self.fields = dict(recipient_department_id=self.receiver, title="Native send", description="Required",
            due_at=datetime(2030,1,1,tzinfo=timezone.utc), creation_key=self.key)
        db.session.remove()

    def test_native_send_replay_and_changed_payload(self):
        from app.workflow import WorkflowConflict
        actor = db.session.get(User,self.actor_id)
        task = create_task(actor=actor,**self.fields)
        db.session.commit()
        ident = task.id
        db.session.remove()
        actor = db.session.get(User,self.actor_id)
        self.assertEqual(ident,create_task(actor=actor,**self.fields).id)
        db.session.commit()
        with self.assertRaises(WorkflowConflict):
            create_task(actor=actor,**(self.fields | {"description":"Changed"}))
        db.session.rollback()
        self.assertEqual(1,Task.query.count())
        self.assertEqual(1,TaskEvent.query.count())
        self.assertEqual(1,TaskComment.query.count())
        self.assertEqual(2,TaskNotification.query.count())

    def test_two_simultaneous_sends_same_key_create_exactly_one_task(self):
        barrier = Barrier(2)
        def send():
            with self.app.app_context():
                try:
                    actor = db.session.get(User,self.actor_id)
                    barrier.wait(timeout=10)
                    task = create_task(actor=actor,**self.fields)
                    ident = task.id
                    db.session.commit()
                    return ident
                finally:
                    db.session.remove()
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(send) for _ in range(2)]
            ids = [job.result(timeout=25) for job in jobs]
        self.assertEqual(ids[0],ids[1])
        self.assertEqual(1,Task.query.count())
        self.assertEqual(1,TaskComment.query.count())
        self.assertEqual(1,TaskEvent.query.count())
        self.assertEqual(2,TaskNotification.query.count())

    def test_native_notification_failure_rolls_back_creation(self):
        with patch("app.workflow.deliver_event_notifications",side_effect=SQLAlchemyError("synthetic")):
            with self.assertRaises(SQLAlchemyError):
                create_task(actor=db.session.get(User,self.actor_id),**self.fields)
            db.session.rollback()
        for model in (Task,TaskEvent,TaskComment,TaskNotification):
            self.assertEqual(0,model.query.count())
        create_task(actor=db.session.get(User,self.actor_id),**self.fields)
        db.session.commit()
        self.assertEqual(1,Task.query.count())
