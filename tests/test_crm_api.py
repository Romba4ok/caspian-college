"""Real Flask/SQL tests for the external service boundary and atomic CRM events."""
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from app.extensions import db
from app.models import (AdminAuditEvent, CrmEvent, Department, Task, TaskAttachment,
    TaskComment, TaskEvent, TaskNotification, User)
from app.notifications import visible_notifications_query
from app.reminders import run_task_reminders

UTC = timezone.utc
NOW = datetime(2026,11,2,7,tzinfo=UTC)
READ = "synthetic-read-" + "a" * 40
WRITE = "synthetic-write-" + "b" * 40


class CrmApiTests(unittest.TestCase):
    postgres = False

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="caspian_crm_test_")
        config = dict(TESTING=True, DATABASE=":memory:", SECRET_KEY="synthetic-only",
            ADMIN_PASSWORD="synthetic-admin", ADMIN_USERNAME="crm-api-admin", CRM_API_READ_TOKEN=READ, CRM_API_WRITE_TOKEN=WRITE,
            TASK_UPLOAD_DIR=str(Path(self.temp.name)/"uploads"))
        self.control = None
        if self.postgres:
            self.schema = "crm_api_test_"+uuid4().hex
            self.control = create_engine(os.environ["PORTAL_TEST_DATABASE_URL"])
            with self.control.begin() as connection:
                connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
            config.update(DATABASE=os.environ["PORTAL_TEST_DATABASE_URL"],
                SQLALCHEMY_ENGINE_OPTIONS={"connect_args":{"options":"-csearch_path="+self.schema}})
        self.app = create_app(config)
        self.context = self.app.app_context()
        self.context.push()
        self.client = self.app.test_client()
        self.sender = Department.query.filter_by(slug="marketing").one()
        self.receiver = Department.query.filter_by(slug="it").one()
        self.other = Department.query.filter_by(slug="practice").one()
        self.author = self.user("author",self.sender.id)
        self.worker = self.user("worker",self.receiver.id)
        self.head = self.user("head",self.receiver.id,"head")
        self.outsider = self.user("outsider",self.other.id,"head")
        self.task = Task(title="CRM: подготовить материалы",description="Рабочие данные",
            author_id=self.author.id,sender_department_id=self.sender.id,recipient_department_id=self.receiver.id,
            assignee_ids=[self.worker.id],status="in_progress",due_at=NOW+timedelta(days=1),
            created_at=NOW-timedelta(days=2),sent_at=NOW-timedelta(days=2),updated_at=NOW,
            revision=1,is_rework=False,rework_cycle=0,creation_key="e"*64)
        db.session.add(self.task)
        db.session.commit()
        self.patch_read = patch("app.crm_api._now",return_value=NOW)
        self.patch_events = patch("app.crm_events.utc_now",return_value=NOW)
        self.patch_read.start();self.patch_events.start()

    def tearDown(self):
        self.patch_read.stop();self.patch_events.stop()
        db.session.remove();db.engine.dispose();self.context.pop();self.temp.cleanup()
        if self.control is not None:
            with self.control.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
            self.control.dispose()

    def user(self,name,department,role="employee"):
        user = User(username=name,full_name=name,role=role,department_id=department,
            password_hash="never-export-password-hash")
        db.session.add(user);db.session.flush()
        return user

    def headers(self,write=False):
        return {"Authorization":"Bearer "+(WRITE if write else READ)}

    def get(self,path,**kwargs):
        return self.client.get("/api/v1"+path,headers=self.headers(),**kwargs)

    def payload(self,**changes):
        return dict(source="college-crm",event_id="task-deadline-1",event_type="deadline_reminder",
            task_id=self.task.id,due_at="2026-11-03T12:00:00+05:00",rework_cycle=0,**changes)

    def post(self,payload=None,write=True):
        return self.client.post("/api/v1/events",json=self.payload() if payload is None else payload,
            headers=self.headers(write))

    def counts(self):
        return CrmEvent.query.count(),TaskNotification.query.count()

    def creation_payload(self, **changes):
        payload = dict(source="college-crm", event_id="create-task-1", author_id=self.author.id,
            recipient_department_id=self.receiver.id, title="Задача из CRM", description="Подготовить материалы",
            due_at="2026-11-03T12:00:00+05:00")
        payload.update(changes)
        return payload

    def create_from_crm(self, payload=None, write=True):
        return self.client.post("/api/v1/tasks", json=self.creation_payload() if payload is None else payload,
            headers=self.headers(write))

    def test_creation_dispatches_using_workflow_history_and_notifications(self):
        response = self.create_from_crm()
        self.assertEqual(201, response.status_code, response.text)
        task = response.json["data"]
        self.assertEqual("new", task["status"])
        self.assertEqual("normal", task["priority"])
        self.assertEqual(self.sender.id, task["sender_department_id"])
        self.assertEqual([], task["assignee_ids"])
        self.assertEqual(1, task["revision"])
        self.assertEqual("2026-11-03T07:00:00Z", task["due_at"])
        self.assertIsNotNone(task["sent_at"])
        self.assertEqual(task, self.get("/tasks/"+str(task["id"])).json["data"])
        self.assertEqual("/api/v1/tasks/"+str(task["id"]), response.headers["Location"])
        event = TaskEvent.query.filter_by(task_id=task["id"]).one()
        self.assertEqual("sent", event.event_type)
        self.assertEqual(self.author.id, event.actor_id)
        self.assertEqual({"source":"college-crm","event_id":"create-task-1"}, event.details["crm"])
        self.assertEqual({self.author.id, self.head.id}, {n.user_id for n in TaskNotification.query.filter_by(task_id=task["id"])})
        receipt = CrmEvent.query.one()
        self.assertEqual("task_created", receipt.event_type)
        self.assertEqual(2, receipt.notification_count)
        self.assertEqual(False, response.json["meta"]["duplicate"])

    def test_creation_replay_normalizes_default_priority_whitespace_and_timezone(self):
        first = self.create_from_crm()
        replay = self.create_from_crm(self.creation_payload(title=" Задача из CRM ",
            description=" Подготовить материалы ", priority="normal", due_at="2026-11-03T07:00:00Z"))
        self.assertEqual(200, replay.status_code, replay.text)
        self.assertTrue(replay.json["meta"]["duplicate"])
        self.assertEqual(first.json["data"]["id"], replay.json["data"]["id"])
        self.assertEqual((2,1,2), (Task.query.count(),CrmEvent.query.count(),TaskNotification.query.count()))

    def test_creation_conflicts_do_not_create_second_task_even_for_different_author(self):
        self.assertEqual(201, self.create_from_crm().status_code)
        for changes in ({"title":"Другой текст"}, {"author_id":self.outsider.id},
                        {"priority":"urgent"}, {"recipient_department_id":self.other.id}):
            response = self.create_from_crm(self.creation_payload(**changes))
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual("event_conflict", response.json["error"]["code"])
        self.assertEqual((2,1,2), (Task.query.count(),CrmEvent.query.count(),TaskNotification.query.count()))

    def test_creation_retry_returns_current_task_after_close_and_author_block(self):
        response = self.create_from_crm()
        task = db.session.get(Task, response.json["data"]["id"])
        task.status="cancelled";task.closed_at=NOW;self.author.is_blocked=True;db.session.commit()
        retry = self.create_from_crm()
        self.assertEqual(200, retry.status_code, retry.text)
        self.assertEqual("cancelled", retry.json["data"]["status"])
        self.assertEqual((2,1,2), (Task.query.count(),CrmEvent.query.count(),TaskNotification.query.count()))

    def test_creation_requires_write_key_and_preserves_html_csrf(self):
        self.app.config["WTF_CSRF_ENABLED"]=True
        self.assertEqual(401, self.client.post("/api/v1/tasks",json=self.creation_payload()).status_code)
        self.assertEqual(403, self.create_from_crm(write=False).status_code)
        self.assertEqual(201, self.create_from_crm().status_code)
        self.assertEqual(400, self.client.post("/tasks/new",data={}).status_code)

    def test_creation_rejects_malformed_and_privileged_fields_without_writes(self):
        for changes in ({"title":" "},{"title":"x"*201},{"description":[]},{"description":""},
                        {"title":"bad\x00text"},{"description":"bad\ud800text"},
                        {"due_at":"2026-11-03T12:00"},{"due_at":"bad"},{"author_id":True},
                        {"author_id":"1"},{"recipient_department_id":0},{"recipient_department_id":2**40},
                        {"priority":[]},{"priority":"unknown"},{"source":"bad source"},
                        {"event_id":""},{"status":"completed"},{"sender_department_id":self.other.id},
                        {"assignee_ids":[self.worker.id]},{"creation_key":"a"*64},{"files":[]}):
            response = self.create_from_crm(self.creation_payload(**changes))
            self.assertEqual(400,response.status_code,(changes,response.text))
        missing=self.creation_payload();del missing["due_at"]
        self.assertEqual(400,self.create_from_crm(missing).status_code)
        self.assertEqual(400,self.create_from_crm([]).status_code)
        for raw,status in (('{"source":"a","source":"b"}',400),('NaN',400),(' '*17000,413)):
            self.assertEqual(status,self.client.post("/api/v1/tasks",data=raw,content_type="application/json",headers=self.headers(True)).status_code)
        self.assertEqual(415,self.client.post("/api/v1/tasks",data="x",headers=self.headers(True)).status_code)
        self.assertEqual(400,self.client.post("/api/v1/tasks?x=1",json=self.creation_payload(),headers=self.headers(True)).status_code)
        self.assertEqual((1,0,0), (Task.query.count(),CrmEvent.query.count(),TaskNotification.query.count()))

    def test_creation_enforces_real_author_and_active_distinct_departments(self):
        admin=User.query.filter_by(role="admin").one()
        for author,status in ((admin.id,403),(999999,404)):
            self.assertEqual(status,self.create_from_crm(self.creation_payload(author_id=author)).status_code)
        self.author.is_blocked=True;db.session.commit()
        self.assertEqual(403,self.create_from_crm().status_code)
        self.author.is_blocked=False;self.sender.is_archived=True;db.session.commit()
        self.assertEqual(403,self.create_from_crm().status_code)
        self.sender.is_archived=False;self.receiver.is_archived=True;db.session.commit()
        self.assertEqual(400,self.create_from_crm().status_code)
        self.receiver.is_archived=False;db.session.commit()
        for recipient in (self.sender.id,999999):
            self.assertEqual(400,self.create_from_crm(self.creation_payload(recipient_department_id=recipient)).status_code)
        self.assertEqual((1,0,0), (Task.query.count(),CrmEvent.query.count(),TaskNotification.query.count()))

    def test_creation_receipt_namespace_is_shared_with_reminders(self):
        creation=self.creation_payload(event_id="task-deadline-1")
        self.assertEqual(201,self.post().status_code)
        self.assertEqual(409,self.create_from_crm(creation).status_code)
        self.assertEqual(1,Task.query.count())
        creation["event_id"]="create-separate"
        created=self.create_from_crm(creation)
        self.assertEqual(201,created.status_code)
        reminder=self.payload();reminder["event_id"]="create-separate"
        self.assertEqual(409,self.post(reminder).status_code)

    def test_creation_failure_rolls_back_task_history_receipt_and_notifications(self):
        from app.notifications import deliver_event_notifications
        def fail(task,event):
            deliver_event_notifications(task,event)
            raise SQLAlchemyError("synthetic-after-notification")
        with patch("app.workflow.deliver_event_notifications",side_effect=fail):
            response=self.create_from_crm()
        self.assertEqual(503,response.status_code,response.text)
        self.assertNotIn("synthetic-after",response.text)
        self.assertEqual((1,0,0,0), (Task.query.count(),TaskEvent.query.count(),CrmEvent.query.count(),TaskNotification.query.count()))
        self.assertEqual(201,self.create_from_crm().status_code)

    def test_disabled_missing_wrong_and_misconfigured_tokens_fail_closed(self):
        self.assertEqual(401,self.client.get("/api/v1/tasks").status_code)
        self.assertEqual(401,self.client.get("/api/v1/tasks",headers={"Authorization":"Bearer wrong"}).status_code)
        self.assertEqual(401,self.client.get("/api/v1/tasks?token="+READ).status_code)
        for values in ((None,None),("short",WRITE),(READ,READ)):
            self.app.config.update(CRM_API_READ_TOKEN=values[0],CRM_API_WRITE_TOKEN=values[1])
            response=self.get("/tasks")
            self.assertEqual(503,response.status_code)
            self.assertNotIn(WRITE,response.text)

    def test_read_key_cannot_write_and_cookie_does_not_authorize_api(self):
        self.assertEqual(403,self.post(write=False).status_code)
        self.assertEqual((0,0),self.counts())
        login=self.client.post("/login",data={"username":"crm-api-admin","password":"synthetic-admin"})
        self.assertEqual(302,login.status_code)
        with self.client.session_transaction() as session:
            self.assertIn("_user_id",session)
        self.assertEqual(401,self.client.get("/api/v1/tasks").status_code)
        self.assertEqual(200,self.client.get("/api/v1/tasks",headers=self.headers(True)).status_code)

    def test_errors_are_json_and_responses_are_not_cacheable(self):
        for path,status in (("/unknown",404),("/tasks/99999",404),("/tasks?limit=0",400)):
            response=self.get(path)
            self.assertEqual(status,response.status_code)
            self.assertIsInstance(response.json["error"]["code"],str)
            self.assertIn("no-store",response.headers["Cache-Control"])
            self.assertEqual("nosniff",response.headers["X-Content-Type-Options"])
            self.assertIn("Authorization",response.headers["Vary"])
        self.assertEqual(405,self.client.post("/api/v1/users",json={},headers=self.headers(True)).status_code)
        self.assertEqual('Bearer realm="crm-api"',self.client.get("/api/v1").headers["WWW-Authenticate"])

    def test_path_ids_are_validated_before_database_lookup(self):
        for path in ("/tasks/0","/users/2147483648","/tasks/99999999999999999999","/attachments/2147483648/download"):
            response=self.get(path)
            self.assertEqual(400,response.status_code,path)
            self.assertEqual("invalid_parameter",response.json["error"]["code"])

    def test_full_data_projection_omits_passwords_keys_and_storage_paths(self):
        task=self.get("/tasks/"+str(self.task.id)).json["data"]
        self.assertEqual([self.worker.id],task["assignee_ids"])
        self.assertEqual("2026-11-03T07:00:00Z",task["due_at"])
        self.assertEqual("2026-11-03",task["due_date"])
        self.assertEqual("12:00",task["due_time"])
        self.assertEqual("Asia/Qyzylorda",task["timezone"])
        for path in ("/tasks","/users","/departments"):
            response=self.get(path)
            self.assertEqual(200,response.status_code)
            for secret in ("password_hash","creation_key","never-export","storage_key","auth_stamp"):
                self.assertNotIn(secret,response.text)

    def test_pagination_is_bounded_and_keyset_preserves_filters(self):
        for i in range(4):
            db.session.add(Task(title="Extra "+str(i),description="x",author_id=self.author.id,
                sender_department_id=self.sender.id,recipient_department_id=self.receiver.id,status="new",
                due_at=NOW+timedelta(days=2),created_at=NOW,sent_at=NOW,updated_at=NOW))
        db.session.commit()
        seen=[];path="/api/v1/tasks?limit=2&recipient_department_id="+str(self.receiver.id)
        while path:
            response=self.client.get(path,headers=self.headers())
            self.assertEqual(200,response.status_code)
            self.assertLessEqual(len(response.json["data"]),2)
            seen += [row["id"] for row in response.json["data"]]
            path=response.json["pagination"]["next"]
            if path:self.assertIn("recipient_department_id=",path)
        self.assertEqual(5,len(seen));self.assertEqual(len(seen),len(set(seen)))
        for query in ("limit=201","after_id=-1","limit=1&limit=2","x=1","status=unknown",
                      "updated_since=2026-11-02T00:00:00","due_from=2027-01-01T00:00:00Z&due_to=2026-01-01T00:00:00Z"):
            self.assertEqual(400,self.get("/tasks?"+query).status_code,query)

    def test_task_filters_and_deadline_feed_are_authoritative(self):
        response=self.get("/tasks",query_string={"assignee_id":self.worker.id,"updated_since":"2026-11-02T07:00:00Z"})
        self.assertEqual([self.task.id],[row["id"] for row in response.json["data"]])
        self.assertFalse(response.json["data"][0]["is_overdue"])
        self.assertEqual([],self.get("/tasks?status=completed").json["data"])
        self.assertEqual(1,len(self.get("/deadlines").json["data"]))
        self.task.status="completed";self.task.completed_at=NOW;self.task.closed_at=NOW;db.session.commit()
        self.assertEqual([],self.get("/deadlines").json["data"])
        self.assertEqual(404,self.get("/deadlines/"+str(self.task.id)).status_code)

    def test_future_dispatch_is_not_a_current_deadline(self):
        self.task.sent_at=NOW+timedelta(hours=1);db.session.commit()
        self.assertEqual([],self.get("/deadlines").json["data"])
        self.assertEqual(404,self.get("/deadlines/"+str(self.task.id)).status_code)

    def test_http_level_size_error_is_also_json(self):
        self.app.config["MAX_CONTENT_LENGTH"]=32
        response=self.client.post("/api/v1/events",data=" "*100,
            content_type="application/json",headers=self.headers(True))
        self.assertEqual(413,response.status_code)
        self.assertEqual("request_too_large",response.json["error"]["code"])

    def test_related_resources_and_reports_preserve_real_counts(self):
        comment=TaskComment(task_id=self.task.id,author_id=self.author.id,body="Комментарий")
        db.session.add(comment);db.session.flush()
        event=TaskEvent(task_id=self.task.id,actor_id=self.author.id,event_type="comment_added",
            comment_id=comment.id,actor_snapshot={"name":"Author","password":"never-export"},
            details={"comment":"Комментарий","secret":"never-export","nested":{"token":"never-export","kept":1}})
        db.session.add(event)
        db.session.add(AdminAuditEvent(actor_snapshot={"name":"Admin","password_hash":"never-export"},
            action="account_updated",object_type="user",object_id=self.author.id,details={"api_key":"never-export"}))
        db.session.commit()
        for path in ("/comments","/task-events","/audit-events"):
            result=self.get(path)
            self.assertEqual(1,len(result.json["data"]))
            self.assertNotIn("never-export",result.text)
        report=self.get("/reports").json["data"]
        self.assertEqual(1,report["overall"]["total"])
        self.assertEqual(1,report["overall"]["statuses"]["in_progress"])
        self.assertIsNone(report["overall"]["average_hours"])

    def test_private_file_download_requires_bearer_and_never_exports_storage_key(self):
        root=Path(self.app.config["TASK_UPLOAD_DIR"]);root.mkdir()
        key="a"*32;(root/key).write_bytes(b"synthetic-pdf")
        file=TaskAttachment(task_id=self.task.id,uploaded_by_id=self.author.id,original_filename="Test.pdf",
            storage_key=key,content_type="application/pdf",size_bytes=13)
        db.session.add(file);db.session.commit()
        meta=self.get("/attachments").json["data"][0]
        self.assertNotIn("storage_key",meta)
        self.assertEqual(401,self.client.get(meta["download_url"]).status_code)
        response=self.client.get(meta["download_url"],headers=self.headers())
        self.assertEqual(200,response.status_code);self.assertEqual(b"synthetic-pdf",response.data)
        response.close()
        (root/key).unlink()
        self.assertEqual(404,self.client.get(meta["download_url"],headers=self.headers()).status_code)

    def test_crm_event_queues_current_owners_and_retry_is_a_receipt(self):
        before=(self.task.revision,self.task.updated_at,TaskEvent.query.count(),TaskComment.query.count())
        first=self.post();second=self.post()
        self.assertEqual(201,first.status_code,first.text);self.assertEqual(200,second.status_code,second.text)
        self.assertFalse(first.json["data"]["duplicate"]);self.assertTrue(second.json["data"]["duplicate"])
        self.assertEqual(3,first.json["data"]["notification_count"])
        self.assertEqual((1,3),self.counts())
        self.assertEqual({self.author.id,self.worker.id,self.head.id},{n.user_id for n in TaskNotification.query.all()})
        self.assertTrue(all(n.event_type=="reminder_one_day" and not n.is_read for n in TaskNotification.query.all()))
        self.assertEqual(1,visible_notifications_query(self.worker).count())
        self.assertEqual(0,visible_notifications_query(self.outsider).count())
        self.assertEqual(before,(self.task.revision,self.task.updated_at,TaskEvent.query.count(),TaskComment.query.count()))

    def test_second_event_id_and_scheduler_do_not_duplicate_daily_notifications(self):
        self.assertEqual(201,self.post().status_code)
        payload=self.payload();payload["event_id"]="another-id"
        self.assertEqual(0,self.post(payload).json["data"]["notification_count"])
        self.assertEqual(0,run_task_reminders(now=NOW));db.session.commit()
        self.assertEqual((2,3),self.counts())

    def test_scheduler_then_crm_also_deduplicates(self):
        self.assertEqual(3,run_task_reminders(now=NOW));db.session.commit()
        response=self.post()
        self.assertEqual(201,response.status_code)
        self.assertEqual(0,response.json["data"]["notification_count"])
        self.assertEqual((1,3),self.counts())

    def test_event_id_conflict_cannot_change_task_or_delivery(self):
        self.post()
        payload=self.payload();payload["rework_cycle"]=1
        response=self.post(payload)
        self.assertEqual(409,response.status_code);self.assertEqual("event_conflict",response.json["error"]["code"])
        self.assertEqual((1,3),self.counts())

    def test_old_receipt_stays_idempotent_after_task_closes(self):
        self.post();self.task.status="cancelled";self.task.closed_at=NOW;db.session.commit()
        self.assertEqual(200,self.post().status_code)
        self.assertEqual((1,3),self.counts())

    def test_stale_deadline_and_cycle_closed_task_and_future_window_fail_without_receipts(self):
        for change in ({"due_at":"2026-11-04T07:00:00Z"},{"rework_cycle":1},{"task_id":999999}):
            payload=self.payload();payload.update(change)
            self.assertIn(self.post(payload).status_code,(404,409))
            self.assertEqual((0,0),self.counts())
        self.task.due_at=NOW+timedelta(days=4);db.session.commit()
        payload=self.payload();payload["due_at"]="2026-11-06T07:00:00Z"
        self.assertEqual("reminder_not_due",self.post(payload).json["error"]["code"])
        self.task.status="cancelled";self.task.closed_at=NOW;db.session.commit()
        self.assertEqual("task_not_active",self.post(payload).json["error"]["code"])
        self.assertEqual((0,0),self.counts())

    def test_invalid_payload_types_media_duplicates_and_size_are_rejected(self):
        for change in ({"task_id":True},{"task_id":"1"},{"rework_cycle":-1},{"due_at":"2026-11-03T12:00"},
                       {"event_type":"anything"},{"source":"space source"},{"event_id":""},{"message":"Injected"}):
            payload=self.payload();payload.update(change)
            self.assertIn(self.post(payload).status_code,(400,422))
        self.assertEqual(400,self.post([]).status_code)
        self.assertEqual(400,self.client.post("/api/v1/events",data='{"source":"a","source":"b"}',
            content_type="application/json",headers=self.headers(True)).status_code)
        self.assertEqual(415,self.client.post("/api/v1/events",data="text",headers=self.headers(True)).status_code)
        self.assertEqual(413,self.client.post("/api/v1/events",data=" "*17000,
            content_type="application/json",headers=self.headers(True)).status_code)
        self.assertEqual((0,0),self.counts())

    def test_api_uses_token_without_disabling_html_csrf(self):
        self.app.config["WTF_CSRF_ENABLED"]=True
        self.assertEqual(201,self.post().status_code)
        self.assertEqual(400,self.client.post("/tasks/new",data={}).status_code)

    def test_database_failure_is_json_and_does_not_expose_sql(self):
        with patch("app.crm_api.ingest_deadline_event",side_effect=SQLAlchemyError("NEVER-EXPORT-DB-URL")):
            response=self.post()
        self.assertEqual(503,response.status_code)
        self.assertNotIn("NEVER",response.text)
        self.assertEqual((0,0),self.counts())

    def test_failure_after_receipt_flush_rolls_back_receipt_and_notifications(self):
        def fail_after_write(*args,**kwargs):
            db.session.add(TaskNotification(task_id=self.task.id,user_id=self.worker.id,event_type="reminder_one_day",message="x"))
            db.session.flush()
            raise SQLAlchemyError("synthetic failure")
        with patch("app.crm_events._queue_reminders",side_effect=fail_after_write):
            self.assertEqual(503,self.post().status_code)
        self.assertEqual((0,0),self.counts())
        self.assertEqual(201,self.post().status_code)
        self.assertEqual((1,3),self.counts())

    def test_blocked_owner_is_not_notified(self):
        self.worker.is_blocked=True;db.session.commit()
        response=self.post()
        self.assertEqual(201,response.status_code)
        self.assertEqual(2,response.json["data"]["notification_count"])
        self.assertNotIn(self.worker.id,{n.user_id for n in TaskNotification.query.all()})


@unittest.skipUnless(os.environ.get("PORTAL_TEST_DATABASE_URL"),"Requires an isolated PostgreSQL test service")
class CrmApiPostgresTests(CrmApiTests):
    postgres = True

    def parallel_creations(self,payloads):
        barrier=Barrier(len(payloads))
        def request_creation(payload):
            with self.app.test_client() as client:
                barrier.wait(timeout=10)
                response=client.post("/api/v1/tasks",json=payload,headers=self.headers(True))
                return response.status_code,response.json
        with ThreadPoolExecutor(max_workers=len(payloads)) as pool:
            return list(pool.map(request_creation,payloads))

    def test_native_same_creation_race_creates_one_task_and_delivery(self):
        results=self.parallel_creations([self.creation_payload(),self.creation_payload()])
        self.assertEqual([200,201],sorted(status for status,_ in results),results)
        self.assertEqual(1,len({body["data"]["id"] for _,body in results}))
        self.assertEqual((2,1,1,2), (Task.query.count(),TaskEvent.query.count(),CrmEvent.query.count(),TaskNotification.query.count()))

    def test_native_conflicting_creation_authors_leave_only_winner(self):
        results=self.parallel_creations([self.creation_payload(),self.creation_payload(author_id=self.outsider.id)])
        self.assertEqual([201,409],sorted(status for status,_ in results),results)
        self.assertEqual((2,1,1,2), (Task.query.count(),TaskEvent.query.count(),CrmEvent.query.count(),TaskNotification.query.count()))

    def parallel_events(self,payloads):
        barrier=Barrier(len(payloads))
        def request_event(payload):
            with self.app.test_client() as client:
                barrier.wait(timeout=10)
                response=client.post("/api/v1/events",json=payload,headers=self.headers(True))
                return response.status_code,response.json
        with ThreadPoolExecutor(max_workers=len(payloads)) as pool:
            return list(pool.map(request_event,payloads))

    def test_native_same_event_race_delivers_once(self):
        results=self.parallel_events([self.payload(),self.payload()])
        self.assertEqual([200,201],sorted(status for status,_ in results),results)
        self.assertEqual((1,3),self.counts())

    def test_native_different_event_ids_same_day_deliver_once(self):
        other=self.payload();other["event_id"]="concurrent-other"
        results=self.parallel_events([self.payload(),other])
        self.assertEqual([201,201],[status for status,_ in results],results)
        self.assertEqual([0,3],sorted(body["data"]["notification_count"] for _,body in results))
        self.assertEqual((2,3),self.counts())


class CrmMigrationTests(unittest.TestCase):
    def test_upgrade_from_populated_09_preserves_data_and_is_repeatable(self):
        with tempfile.TemporaryDirectory(prefix="caspian_crm_migration_") as directory:
            app=create_app(dict(TESTING=False,DATABASE=str(Path(directory)/"migration.sqlite"),
                SECRET_KEY="synthetic-only",ADMIN_PASSWORD="synthetic-admin"))
            runner=app.test_cli_runner()
            initial=runner.invoke(args=["db","upgrade","20261028_09"])
            self.assertEqual(0,initial.exit_code,initial.output+repr(initial.exception))
            with app.app_context():
                departments=Department.query.order_by(Department.id).all()
                user=User(username="migration-worker",full_name="Synthetic",role="employee",
                    department_id=departments[0].id,password_hash="preserve-this-test-hash")
                db.session.add(user);db.session.flush()
                task=Task(title="Preserve task",description="Preserve body",author_id=user.id,
                    sender_department_id=departments[0].id,recipient_department_id=departments[1].id,
                    due_at=NOW+timedelta(days=1),created_at=NOW,sent_at=NOW,status="new")
                db.session.add(task);db.session.commit()
                before=(User.query.count(),Task.query.count(),task.title,user.password_hash)
            result=runner.invoke(args=["db","upgrade"])
            self.assertEqual(0,result.exit_code,result.output)
            self.assertEqual(0,runner.invoke(args=["db","upgrade"]).exit_code)
            with app.app_context():
                self.assertIn("crm_events",inspect(db.engine).get_table_names())
                self.assertEqual("20261102_10",db.session.scalar(text("SELECT version_num FROM alembic_version")))
                user=User.query.filter_by(username="migration-worker").one();task=Task.query.one()
                self.assertEqual(before,(User.query.count(),Task.query.count(),task.title,user.password_hash))
                db.session.remove();db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
