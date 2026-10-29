"""Current workflow: native two-connection races, constraints and private HTTP reads."""
from datetime import datetime,timedelta,timezone
from html import unescape
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sys
from threading import Event
from uuid import uuid4
import unittest

from sqlalchemy import event,inspect,select,text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import undefer
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.accounts import AccountConflict,AccountError,create_account,set_account_blocked,update_account
from app.audit import record_task_event
from app.departments import archive_department
from app.extensions import db
from app.models import Department,Task,TaskAttachment,TaskComment,TaskEvent,TaskNotification,User
from app.notifications import deliver_event_notifications,notification_read_boundary,mark_all_notifications_read
from app.permissions import has_available_assignee
from app.reminders import run_task_reminders
from app import workflow as w
from support.migration_fixtures import MigrationDatabase
from support.postgres_races import NativeRaces

NOW=datetime.now(timezone.utc).replace(microsecond=0)


@unittest.skipUnless(os.environ.get("PORTAL_TEST_DATABASE_URL"),"Run isolated local_portal.py verify")
class PostgreSQLTests(NativeRaces,MigrationDatabase,unittest.TestCase):
    postgres=True

    def seed(self,status="new",**changes):
        self.upgrade()
        with self.app.app_context():
            root=User.query.filter_by(role="admin").one();depts=Department.query.order_by(Department.id).all()
            self.sender,self.receiver,self.third,self.fourth=[d.id for d in depts[:4]];self.ids={"admin":root.id}
            for name,role,dep in (("author","employee",self.sender),("sender-head","head",self.sender),("sender-two","head",self.sender),
                ("receiver-head","head",self.receiver),("receiver-two","head",self.receiver),("executor","employee",self.receiver),
                ("alternate","employee",self.receiver),("outsider","employee",self.third)):
                user=User(username=name,full_name=name,role=role,department_id=dep,password_hash=root.password_hash)
                db.session.add(user);db.session.flush();self.ids[name]=user.id
            values=dict(title="Native current task",description="Synthetic workflow",author_id=self.ids["author"],sender_department_id=self.sender,
                recipient_department_id=self.receiver,status=status,due_at=NOW+timedelta(days=1),created_at=NOW-timedelta(days=3),sent_at=NOW-timedelta(days=2),
                assignee_ids=[] if status=="new" else [self.ids["executor"]],revision=0,
                completed_at=NOW if status=="completed" else None,closed_at=NOW if status in ("completed","cancelled","rejected") else None)
            task=Task(**(values|changes));db.session.add(task);db.session.commit();self.task_id=task.id;db.session.remove()

    def actor(self,name):return db.session.get(User,self.ids[name])
    def task(self):return db.session.get(Task,self.task_id,options=(undefer("*"),))

    def action(self,who,action,candidate=None,revision=0):
        args=dict(task=self.task(),actor=self.actor(who),expected_revision=revision,comment="Native "+action)
        if action in ("assign","claim","reassign","release"):
            return w.transition_execution(**args,action=action,assignee_ids=[self.ids[candidate]] if candidate else None)
        return w.transition_completion(**args,action=action)

    def edit(self,who,**changes):
        user=self.actor(who)
        return update_account(actor=self.actor("admin"),user_id=user.id,full_name=user.full_name,username=user.username,
            role=changes.get("role",user.role),department_id=changes.get("department_id",user.department_id))

    def mutate_access(self,kind):
        if kind=="disable":return set_account_blocked(actor=self.actor("admin"),user_id=self.ids["executor"],blocked=True)
        return self.edit("executor",department_id=self.third)

    def assert_events(self,count):
        self.assertEqual(count,TaskEvent.query.count());self.assertEqual(count,TaskComment.query.count())
        for row in TaskEvent.query.order_by(TaskEvent.id):
            notes=TaskNotification.query.filter(TaskNotification.deduplication_key.like(f"event:{row.id}:user:%")).all()
            self.assertEqual(set(row.details.get("notification_recipient_ids",[])),{n.user_id for n in notes})
            self.assertEqual(len(notes),len({n.user_id for n in notes}))
            self.assertTrue(row.comment_id)
        keys=[n.deduplication_key for n in TaskNotification.query.all() if n.deduplication_key]
        self.assertEqual(len(keys),len(set(keys)))

    def pair(self,kind,first):
        setups={"claim_claim":("new",("executor","claim",None),("alternate","claim",None)),
            "claim_assign":("new",("executor","claim",None),("receiver-head","assign","alternate")),
            "assign_assign":("new",("receiver-head","assign","executor"),("receiver-two","assign","alternate")),
            "release_reassign":("in_progress",("executor","release",None),("receiver-head","reassign","alternate")),
            "complete_complete":("in_progress",("executor","complete",None),("receiver-head","complete",None)),
            "complete_reject":("in_progress",("executor","complete",None),("receiver-head","reject",None)),
            "complete_cancel":("in_progress",("executor","complete",None),("sender-head","cancel",None)),
            "return_return":("completed",("author","return",None),("sender-head","return",None))}
        status,left,right=setups[kind];self.seed(status)
        results=self.race(lambda:self.action(*left),lambda:self.action(*right),first=first)
        self.assertEqual([False,True],sorted(r["ok"] for r in results),results)
        self.assertTrue(results[first]["ok"],results);self.assertEqual("WorkflowConflict",results[1-first]["error"])
        with self.app.app_context():
            task=self.task();self.assertEqual(1,task.revision);self.assert_events(1)
            expected={"assign":"in_progress","claim":"in_progress","release":"new","reassign":"in_progress","complete":"completed","reject":"rejected","cancel":"cancelled","return":"in_progress"}[(left,right)[first][1]]
            self.assertEqual(expected,task.status)
            if task.status=="new":self.assertIsNone(task.assignee_id)
            if kind=="return_return":self.assertEqual((True,1,None,None),(task.is_rework,task.rework_cycle,task.completed_at,task.closed_at))
            if task.status=="completed":self.assertEqual(task.completed_at,task.closed_at);self.assertIsNotNone(task.completed_at)

    def access_race(self,kind,action,first):
        self.seed("completed" if action=="return" else "new")
        who="sender-head" if action=="return" else ("executor" if action=="claim" else "receiver-head")
        results=self.race(lambda:self.mutate_access(kind),lambda:self.action(who,action,"executor" if action=="assign" else None),first=first)
        with self.app.app_context():
            user=self.actor("executor");task=self.task()
            self.assertTrue(results[first]["ok"],results)
            if first==1:
                self.assertFalse(results[0]["ok"],results);self.assertEqual("AccountConflict",results[0]["error"])
                self.assertFalse(user.is_blocked);self.assertEqual(self.receiver,user.department_id)
                self.assertEqual(0,db.session.scalar(text("SELECT count(*) FROM admin_audit_events")))
            else:
                self.assertEqual(kind=="disable",user.is_blocked)
                self.assertEqual(self.third if kind=="transfer" else self.receiver,user.department_id)
                self.assertEqual(1,db.session.scalar(text("SELECT count(*) FROM admin_audit_events")))
                self.assertEqual(action=="return",results[1]["ok"],results)
            success=results[1]["ok"];self.assertEqual(1 if success else 0,task.revision);self.assert_events(1 if success else 0)
            if action=="return" and first==0:
                self.assertEqual(("in_progress",True,1),(task.status,task.is_rework,task.rework_cycle));self.assertFalse(has_available_assignee(task))
                with self.assertRaises((PermissionError,w.WorkflowError)):
                    self.action("receiver-head","complete",revision=1)
                db.session.rollback()
                self.action("receiver-head","reassign","alternate",revision=1);db.session.commit()
                self.assertTrue(has_available_assignee(self.task()));self.assert_events(2)
            elif success:
                self.assertEqual("in_progress",task.status);self.assertEqual(self.ids["executor"],task.assignee_id);self.assertTrue(has_available_assignee(task))
            else:self.assertEqual(("new",None),(task.status,task.assignee_id))

    def reminder_race(self,kind,first):
        self.seed("in_progress")
        def other():
            if kind=="deadline":return w.change_task_deadline(task=self.task(),actor=self.actor("sender-head"),expected_revision=0,comment="Move deadline",due_at=NOW+timedelta(days=10))
            return self.action("sender-head" if kind=="cancel" else "receiver-head",kind)
        results=self.race(lambda:run_task_reminders(now=NOW),other,first=first)
        self.assertTrue(all(r["ok"] for r in results),results)
        with self.app.app_context():
            self.assert_events(1);self.assertEqual(1,self.task().revision)
            reminders=TaskNotification.query.filter(TaskNotification.event_type.like("reminder_%")).count()
            self.assertEqual(4 if first==0 else 0,reminders)
            before=TaskNotification.query.count();self.assertEqual(0,run_task_reminders(now=NOW));db.session.commit()
            self.assertEqual(before,TaskNotification.query.count())
            if kind=="deadline":self.assertEqual(NOW+timedelta(days=10),self.task().due_at)
            else:self.assertIn(self.task().status,("completed","cancelled","rejected"))

    def role_race(self,action,first):
        self.seed()
        name="receiver-head" if action=="assign" else "sender-head"
        def work(revision=0):
            if action=="assign":return self.action(name,"assign","executor",revision=revision)
            return w.change_task_deadline(task=self.task(),actor=self.actor(name),expected_revision=revision,comment="Deadline",due_at=NOW+timedelta(days=3))
        results=self.race(lambda:self.edit(name,role="employee"),work,first=first)
        self.assertTrue(results[0]["ok"],results);self.assertEqual(first==1,results[1]["ok"],results)
        with self.app.app_context():
            self.assertEqual("employee",self.actor(name).role);self.assert_events(1 if first==1 else 0)
            if first==1:self.assertEqual("head",TaskEvent.query.one().actor_snapshot["role"])
            with self.assertRaises(PermissionError):work(self.task().revision)
            db.session.rollback()

    def test_complete_and_return_from_working_snapshot_cannot_skip_a_revision(self):
        self.seed("in_progress")
        results=self.race(lambda:self.action("executor","complete"),lambda:self.action("author","return"),synchronize_users=True)
        self.assertTrue(results[0]["ok"],results);self.assertFalse(results[1]["ok"],results)
        with self.app.app_context():self.assertEqual(("completed",1),(self.task().status,self.task().revision));self.assert_events(1)

    def test_return_and_stale_complete_from_closed_snapshot_cannot_complete_the_new_cycle(self):
        self.seed("completed")
        results=self.race(lambda:self.action("author","return"),lambda:self.action("executor","complete"),synchronize_users=True)
        self.assertTrue(results[0]["ok"],results);self.assertFalse(results[1]["ok"],results)
        with self.app.app_context():self.assertEqual(("in_progress",1,1),(self.task().status,self.task().revision,self.task().rework_cycle));self.assert_events(1)

    def test_two_reminder_workers_have_one_slot_per_user_and_no_workflow_change(self):
        self.seed("in_progress")
        results=self.race(lambda:run_task_reminders(now=NOW),lambda:run_task_reminders(now=NOW),first=0)
        self.assertTrue(all(r["ok"] for r in results),results)
        with self.app.app_context():
            self.assertEqual(4,sum(r["value"] for r in results));self.assertEqual(4,TaskNotification.query.count());self.assert_events(0)
            self.assertEqual(0,self.task().revision);self.assertEqual({self.ids[n] for n in ("author","executor","receiver-head","receiver-two")},{n.user_id for n in TaskNotification.query.all()})

    def test_multi_task_reminder_batch_and_assignment_do_not_invert_locks(self):
        self.seed("in_progress")
        with self.app.app_context():
            db.session.add(Task(title="Second task",description="Synthetic",author_id=self.ids["author"],sender_department_id=self.sender,
                recipient_department_id=self.receiver,status="new",due_at=NOW+timedelta(days=1),sent_at=NOW-timedelta(days=2),created_at=NOW-timedelta(days=3)))
            db.session.commit()
        results=self.race(lambda:run_task_reminders(now=NOW),lambda:self.action("receiver-head","reassign","alternate"),synchronize_users=True)
        self.assertTrue(all(r["ok"] for r in results),results)
        with self.app.app_context():self.assert_events(1);self.assertEqual(self.ids["alternate"],self.task().assignee_id)

    def test_repeated_delivery_freezes_recipients_and_deduplicates_atomically(self):
        self.seed()
        with self.app.app_context():
            item=TaskEvent(task_id=self.task_id,actor_id=self.ids["author"],event_type="sent",details={"after":{"title":"Native delivery"}})
            db.session.add(item);db.session.commit();ident=item.id
        def delivery():return deliver_event_notifications(self.task(),db.session.get(TaskEvent,ident))
        results=self.race(delivery,delivery,first=0)
        self.assertTrue(all(r["ok"] for r in results),results)
        with self.app.app_context():
            self.assertEqual(3,sum(r["value"] for r in results));self.assertEqual(3,TaskNotification.query.count())
            self.assertEqual({self.ids[n] for n in ("author","receiver-head","receiver-two")},{n.user_id for n in TaskNotification.query.all()})
            self.assertEqual(1,TaskEvent.query.count());self.assertEqual(0,TaskComment.query.count())

    def test_notification_boundary_does_not_mark_uncommitted_lower_id_read(self):
        self.seed("in_progress");inserted,boundary_taken=Event(),Event()
        with self.app.app_context():
            old=TaskNotification(task_id=self.task_id,user_id=self.ids["executor"],event_type="comment_added",message="Already visible")
            db.session.add(old);db.session.commit();old_id=old.id
        def writer():
            row=TaskNotification(task_id=self.task_id,user_id=self.ids["executor"],event_type="comment_added",message="Commit later")
            db.session.add(row);db.session.flush();inserted.set();self.assertTrue(boundary_taken.wait(8));return row.id
        def reader():
            self.assertTrue(inserted.wait(8))
            db.session.add(TaskNotification(task_id=self.task_id,user_id=self.ids["executor"],event_type="comment_added",message="Visible higher ID"));db.session.flush()
            boundary=notification_read_boundary(self.actor("executor"));boundary_taken.set()
            return mark_all_notifications_read(self.actor("executor"),boundary)
        results=self.race(writer,reader)
        self.assertTrue(all(r["ok"] for r in results),results)
        with self.app.app_context():
            self.assertTrue(db.session.get(TaskNotification,old_id).is_read);self.assertFalse(db.session.get(TaskNotification,results[0]["value"]).is_read)
            self.assertEqual(2,results[1]["value"])

    def test_concurrent_attempts_cannot_remove_the_only_administrator(self):
        self.seed()
        results=self.race(lambda:set_account_blocked(actor=self.actor("admin"),user_id=self.ids["admin"],blocked=True),
            lambda:self.edit("admin",role="employee",department_id=self.sender))
        self.assertTrue(all(not r["ok"] for r in results),results)
        with self.app.app_context():
            self.assertEqual(1,User.query.filter_by(role="admin").count());self.assertFalse(self.actor("admin").is_blocked)
            self.assertEqual(0,db.session.scalar(text("SELECT count(*) FROM admin_audit_events")))

    def test_concurrent_service_promotions_cannot_create_second_admin(self):
        self.seed()
        results=self.race(lambda:self.edit("executor",role="admin"),lambda:self.edit("alternate",role="admin"))
        self.assertTrue(all(not r["ok"] for r in results),results)
        with self.app.app_context():self.assertEqual(1,User.query.filter_by(role="admin").count())

    def test_database_unique_admin_constraint_rejects_two_direct_insert_attempts(self):
        self.seed()
        def insert(name):
            try:
                with db.session.begin_nested():
                    db.session.add(User(username=name,full_name=name,password_hash="synthetic",role="admin"));db.session.flush()
            except IntegrityError:return "database_rejected"
            raise AssertionError("Database allowed a second admin")
        results=self.race(lambda:insert("extra-admin-a"),lambda:insert("extra-admin-b"))
        self.assertTrue(all(r["ok"] and r["value"]=="database_rejected" for r in results),results)
        with self.app.app_context():self.assertEqual(1,User.query.filter_by(role="admin").count())

    def direct_send(self,key):
        return w.create_task(actor=self.actor("author"),recipient_department_id=self.receiver,title="Direct native send",description="Synthetic initial comment",
            due_at=NOW+timedelta(days=2),creation_key=key)

    def archive_send(self,first):
        self.seed()
        with self.app.app_context():key=w.make_creation_key(self.actor("author"))
        results=self.race(lambda:archive_department(actor=self.actor("admin"),department_id=self.receiver),lambda:self.direct_send(key),first=first)
        self.assertTrue(results[0]["ok"],results);self.assertEqual(first==1,results[1]["ok"],results)
        with self.app.app_context():
            self.assertTrue(db.session.get(Department,self.receiver).is_archived);self.assertEqual(2 if first==1 else 1,Task.query.count());self.assert_events(1 if first==1 else 0)
            with self.assertRaises(w.WorkflowError) as error:self.direct_send(w.make_creation_key(self.actor("author")))
            self.assertEqual("recipient_department_id",error.exception.field)
            db.session.rollback()

    def test_two_identical_sends_reuse_one_creation_key_and_one_event(self):
        self.seed()
        with self.app.app_context():key=w.make_creation_key(self.actor("author"))
        results=self.race(lambda:self.direct_send(key),lambda:self.direct_send(key),first=0)
        self.assertTrue(all(r["ok"] for r in results),results)
        with self.app.app_context():self.assertEqual(2,Task.query.count());self.assert_events(1)

    def test_fresh_migration_and_repeat_upgrade(self):
        self.upgrade();before=self.database_snapshot();self.upgrade();self.assertEqual(before,self.database_snapshot())
        with self.app.app_context():
            self.assertEqual("20261028_09",db.session.scalar(text("SELECT version_num FROM alembic_version")))
            self.assertEqual(6,Department.query.count());self.assertTrue(User.query.filter_by(role="admin").one().check_password("synthetic-test-password"))

    def test_legacy_initial_accounts_groups_and_hashes_survive_empty_workflow_upgrade(self):
        with self.app.app_context(),db.engine.begin() as conn:
            conn.execute(text("CREATE TABLE users (id SERIAL PRIMARY KEY,full_name VARCHAR(160) NOT NULL,username VARCHAR(80) UNIQUE NOT NULL,password_hash VARCHAR(255) NOT NULL,role VARCHAR(24) NOT NULL)"))
            conn.execute(text("CREATE TABLE groups (id SERIAL PRIMARY KEY,title VARCHAR(160) NOT NULL,teacher_id INTEGER NOT NULL REFERENCES users(id))"))
            conn.execute(text("CREATE TABLE group_students (group_id INTEGER REFERENCES groups(id),student_id INTEGER REFERENCES users(id),PRIMARY KEY(group_id,student_id))"))
            conn.execute(text("INSERT INTO users VALUES (1,'Admin','migration-admin','preserved-admin','admin'),(2,'Teacher','teacher','preserved-teacher','teacher'),(3,'Student','student','preserved-student','student')"))
            conn.execute(text("SELECT setval(pg_get_serial_sequence('users','id'),3)"))
            conn.execute(text("INSERT INTO groups(title,teacher_id) VALUES ('Preserved group',2)"));conn.execute(text("INSERT INTO group_students VALUES (1,3)"))
        self.upgrade();self.upgrade()
        self.assertEqual(["preserved-admin","preserved-teacher","preserved-student"],[u["password_hash"] for u in self.rows("users")])
        self.assertEqual("Preserved group",self.rows("groups")[0]["title"]);self.assertEqual(3,self.rows("group_students")[0]["student_id"])
        with self.app.app_context():
            self.assertTrue(all(db.session.get(User,i).is_blocked for i in (2,3)));self.assertEqual(0,Task.query.count())
            self.assertTrue(all(u.language=="ru" for u in User.query.all()))

    def http(self,client,path,method="GET",**kwargs):
        # MigrationDatabase keeps an outer app context; real HTTP requests need fresh g.
        with self.app.app_context():return client.open(path,method=method,**kwargs)

    def native_client(self,name):
        client=self.app.test_client();page=self.http(client,"/login")
        token=re.search(r'name="csrf_token" value="([^"]+)"',page.get_data(as_text=True))
        payload=dict(username="migration-admin" if name=="admin" else name,password="synthetic-test-password")
        if token:payload["csrf_token"]=unescape(token[1])
        self.assertEqual(302,self.http(client,"/login",method="POST",data=payload).status_code);return client

    def fragment(self,client,direction="received"):
        response=self.http(client,f"/tasks/{direction}?view=board&fragment=board&page=999")
        self.assertEqual(200,response.status_code);self.assertIn("no-store",response.headers["Cache-Control"])
        self.assertEqual(direction,response.headers["X-Task-Board"]);return response

    def cards(self,response):
        return {c["id"]:c for c in (json.loads(unescape(raw)) for raw in re.findall(r'data-board-task="([^"]+)"',response.get_data(as_text=True)))}

    def test_two_competing_claim_uploads_leave_one_event_and_no_orphan_files(self):
        from test_tasks import attachment_bytes
        self.seed();clients=[self.native_client(n) for n in ("executor","alternate")]
        def claim(client):
            response=client.post(f"/tasks/{self.task_id}/claim",data=dict(revision="0",comment="Claim with file",
                attachment=(BytesIO(attachment_bytes(".pdf")),"native.pdf")))
            try:return response.status_code
            finally:response.close()
        results=self.race(lambda:claim(clients[0]),lambda:claim(clients[1]),synchronize_users=True)
        self.assertEqual([302,409],sorted(r["value"] for r in results))
        with self.app.app_context():
            self.assert_events(1);self.assertEqual(1,TaskAttachment.query.count())
            attachment=TaskAttachment.query.one();self.assertEqual(TaskComment.query.one().id,attachment.comment_id)
            self.assertEqual(self.task().assignee_id,attachment.uploaded_by_id)
            files=[p for p in Path(self.app.config["TASK_UPLOAD_DIR"]).rglob("*") if p.is_file()]
            self.assertEqual([attachment.storage_key],[p.name for p in files])

    def test_native_csrf_conflict_current_actions_complete_and_return(self):
        self.seed();self.app.config["WTF_CSRF_ENABLED"]=True
        one,two=self.native_client("receiver-head"),self.native_client("receiver-two")
        def token(client,direction="received"):
            return unescape(re.search(r'data-csrf-token="([^"]+)"',self.fragment(client,direction).get_data(as_text=True))[1])
        t1,t2=token(one),token(two);payload=dict(revision="0",comment="Assignment",assignee_ids=[self.ids["executor"]])
        self.assertEqual(400,self.http(one,f"/tasks/{self.task_id}/assign",method="POST",data=payload).status_code)
        self.assertEqual(302,self.http(one,f"/tasks/{self.task_id}/assign",method="POST",data=payload|dict(csrf_token=t1)).status_code)
        stale=self.http(two,f"/tasks/{self.task_id}/assign",method="POST",data=payload|dict(csrf_token=t2,comment="Preserve stale comment"))
        self.assertEqual(409,stale.status_code);self.assertIn("Preserve stale comment",stale.get_data(as_text=True))
        card=self.cards(self.fragment(one))[self.task_id];self.assertEqual(("in_progress","in_progress",1),(card["status"],card["group"],card["revision"]))
        for endpoint in ("approval/approve","execution/start","review/complete","confirm"):
            self.assertEqual(410,self.http(one,f"/tasks/{self.task_id}/{endpoint}",method="POST",data=dict(csrf_token=t1,revision=1,comment="Retired")).status_code)
        self.assertEqual(302,self.http(one,f"/tasks/{self.task_id}/complete",method="POST",data=dict(csrf_token=t1,revision=1,comment="Ready")).status_code)
        self.assertNotIn(self.task_id,self.cards(self.fragment(one)))
        sender=self.native_client("sender-head");panel=self.http(sender,f"/tasks/{self.task_id}/panel").get_data(as_text=True)
        self.assertIn("Вернуть на доработку",panel);self.assertNotIn("data-panel-action",panel)
        self.assertEqual(302,self.http(sender,f"/tasks/{self.task_id}/return",method="POST",data=dict(csrf_token=token(sender,"sent"),revision=2,comment="Rework")).status_code)
        card=self.cards(self.fragment(one))[self.task_id];self.assertEqual((True,1,3),(card["is_rework"],card["rework_cycle"],card["revision"]))
        with self.app.app_context():self.assert_events(3)

    def add_board_rows(self,count):
        rows=[]
        for i in range(count):
            status="new" if i%2==0 else "in_progress"
            rows.append(Task(title=f"Native card {uuid4().hex[:10]}",description="Full private description",author_id=self.ids["author"],
                sender_department_id=self.sender,recipient_department_id=self.receiver,status=status,assignee_id=None if status=="new" else self.ids["executor"],
                due_at=NOW+timedelta(days=1),sent_at=NOW-timedelta(days=2),created_at=NOW-timedelta(days=3)))
        db.session.add_all(rows);db.session.commit()

    def test_native_complete_board_36_and_300_and_list_have_bounded_queries(self):
        self.seed()
        with self.app.app_context():self.add_board_rows(35);engine=db.engine
        client=self.native_client("receiver-head")
        def read(view,count):
            statements=[]
            def capture(*args):statements.append(args[2])
            event.listen(engine,"before_cursor_execute",capture)
            try:response=self.http(client,f"/tasks/received?view={view}&page=999")
            finally:event.remove(engine,"before_cursor_execute",capture)
            self.assertEqual(200,response.status_code)
            if view=="board":self.assertEqual(count,len(self.cards(response)))
            else:self.assertEqual(count%25 or 25,response.get_data(as_text=True).count('data-task-id="'))
            self.assertLessEqual(len(statements),20);return len(statements)
        small={v:read(v,36) for v in ("board","list")}
        with self.app.app_context():self.add_board_rows(264)
        for view in ("board","list"):self.assertLessEqual(read(view,300),small[view]+2)

    def test_native_private_reads_all_roles_and_account_switch_do_not_write(self):
        self.seed()
        with self.app.app_context():
            foreign=Task(title="FOREIGN PRIVATE",description="Never leak",author_id=self.ids["outsider"],sender_department_id=self.third,
                recipient_department_id=self.fourth,status="new",due_at=NOW,sent_at=NOW,created_at=NOW)
            db.session.add(foreign);db.session.commit();hidden=foreign.id;before=self.database_snapshot()
        for name,direction,count in (("executor","received",1),("receiver-head","received",1),("author","sent",1),("sender-head","sent",1),("outsider","received",0),("admin","received",2)):
            client=self.native_client(name);response=self.fragment(client,direction);cards=self.cards(response)
            self.assertEqual(count,len(cards));self.assertEqual(str(self.ids[name]),response.headers["X-Task-User"])
            if name!="admin":self.assertNotIn("FOREIGN PRIVATE",response.get_data(as_text=True))
            if name=="admin":self.assertTrue(all(not c["actions"] and not c["candidates"] for c in cards.values()))
            panel=self.http(client,f"/tasks/{self.task_id}/panel")
            self.assertEqual(404 if name=="outsider" else 200,panel.status_code)
            if name!="outsider":self.assertEqual(str(self.ids[name]),panel.headers["X-Task-User"])
        with self.app.app_context():self.assertEqual(before,self.database_snapshot())

    def test_native_panel_history_escaping_files_archive_and_session_revocation(self):
        from test_tasks import attachment_bytes
        self.seed("in_progress");payload='<script>alert("private")</script>'
        with self.app.app_context():
            Path(self.app.config["TASK_UPLOAD_DIR"]).mkdir(parents=True,exist_ok=True)
            task=self.task();task.title=payload;task.description=payload
            item=record_task_event(task=task,actor=self.actor("author"),event_type="comment_added",comment=payload)
            ids=[]
            for comment in (None,item.comment_id):
                key=uuid4().hex;content=attachment_bytes(".pdf");Path(self.app.config["TASK_UPLOAD_DIR"],key).write_bytes(content)
                row=TaskAttachment(task_id=task.id,comment_id=comment,uploaded_by_id=self.ids["author"],original_filename=payload+".pdf",storage_key=key,content_type="application/pdf",size_bytes=len(content))
                db.session.add(row);db.session.flush();ids.append(row.id)
            db.session.commit()
        for name in ("author","receiver-head","admin"):
            client=self.native_client(name);panel=self.http(client,f"/tasks/{self.task_id}/panel")
            self.assertEqual(200,panel.status_code);html=panel.get_data(as_text=True);self.assertIn("&lt;script&gt;",html);self.assertNotIn("<script>",html);self.assertNotIn("<form",html)
            self.assertIn("no-store",panel.headers["Cache-Control"])
            for aid in ids:
                with self.http(client,f"/tasks/attachments/{aid}/download") as response:self.assertEqual(200,response.status_code)
        stranger=self.native_client("outsider")
        for aid in ids:self.assertEqual(404,self.http(stranger,f"/tasks/attachments/{aid}/download").status_code)
        client=self.native_client("receiver-two")
        with self.app.app_context():archive_department(actor=self.actor("admin"),department_id=self.receiver);db.session.commit()
        self.assertEqual(200,self.http(client,f"/tasks/{self.task_id}/panel").status_code)
        with self.app.app_context():set_account_blocked(actor=self.actor("admin"),user_id=self.ids["receiver-two"],blocked=True);db.session.commit()
        for path in (f"/tasks/{self.task_id}/panel","/tasks/received?view=board&fragment=board"):
            response=self.http(client,path);self.assertEqual(302,response.status_code);self.assertIn("no-store",response.headers["Cache-Control"]);self.assertNotIn(payload,response.get_data(as_text=True))

    def test_native_soft_disable_preserves_references_and_rejects_active_executor(self):
        self.seed("in_progress");client=self.native_client("admin")
        self.assertEqual(409,self.http(client,f"/admin/users/{self.ids['executor']}/delete",method="POST").status_code)
        with self.app.app_context():self.action("executor","complete");db.session.commit()
        self.assertEqual(302,self.http(client,f"/admin/users/{self.ids['executor']}/delete",method="POST").status_code)
        with self.app.app_context():self.assertTrue(self.actor("executor").is_blocked);self.assertEqual(self.ids["executor"],self.task().assignee_id);self.assert_events(1)


for kind in ("claim_claim","claim_assign","assign_assign","release_reassign","complete_complete","complete_reject","complete_cancel","return_return"):
    for first in (0,1):
        def check(self,kind=kind,first=first):self.pair(kind,first)
        setattr(PostgreSQLTests,f"test_{kind}_first_{first}",check)
for kind in ("disable","transfer"):
    for action in ("claim","assign","return"):
        for first in (0,1):
            def check(self,kind=kind,action=action,first=first):self.access_race(kind,action,first)
            setattr(PostgreSQLTests,f"test_{kind}_{action}_first_{first}",check)
for kind in ("deadline","complete","cancel","reject"):
    for first in (0,1):
        def check(self,kind=kind,first=first):self.reminder_race(kind,first)
        setattr(PostgreSQLTests,f"test_reminder_{kind}_first_{first}",check)
for action in ("assign","deadline"):
    for first in (0,1):
        def check(self,action=action,first=first):self.role_race(action,first)
        setattr(PostgreSQLTests,f"test_role_{action}_first_{first}",check)
for first in (0,1):
    def check(self,first=first):self.archive_send(first)
    setattr(PostgreSQLTests,f"test_archive_send_first_{first}",check)
