"""Native migrations and synchronized two-connection workflow checks in a disposable DB."""
import os
import json
import re
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO
from html import unescape
from pathlib import Path
from threading import Barrier, Event, local
from uuid import uuid4

from sqlalchemy import create_engine, event, inspect, select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from app.audit import account_snapshot, record_admin_event, record_task_event
from app.departments import archive_department, DepartmentError
from app.extensions import db
from app.models import Department, Group, Task, TaskAttachment, TaskComment, TaskEvent, TaskNotification, User
from app.notifications import deliver_event_notifications, notification_read_boundary, mark_all_notifications_read
from app.reminders import run_task_reminders
from app.workflow import WorkflowError, create_task, transition_approval, transition_execution, transition_review

NOW = datetime(2030, 1, 10, 10, tzinfo=timezone.utc)


@unittest.skipUnless(os.environ.get("PORTAL_TEST_DATABASE_URL"), "Run scripts/local_portal.py verify for isolated PostgreSQL checks")
class PostgreSQLTests(unittest.TestCase):
    def setUp(self):
        self.url=os.environ["PORTAL_TEST_DATABASE_URL"]; self.schema="portal_case_"+uuid4().hex
        self.control=create_engine(self.url)
        with self.control.begin() as conn: conn.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.directory=tempfile.TemporaryDirectory(prefix="portal_case_")
        self.app=create_app({"DATABASE":self.url,"SECRET_KEY":"isolated-pg-tests",
            "ADMIN_USERNAME":"pg-admin","ADMIN_PASSWORD":"pg-test-password","TASK_UPLOAD_DIR":self.directory.name,
            "SQLALCHEMY_ENGINE_OPTIONS":{"connect_args":{"options":f"-csearch_path={self.schema}"}},"WTF_CSRF_ENABLED":False})

    def tearDown(self):
        with self.app.app_context(): db.session.remove();db.engine.dispose()
        with self.control.begin() as conn:conn.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        self.control.dispose();self.directory.cleanup()

    def upgrade(self):
        result=self.app.test_cli_runner().invoke(args=["db","upgrade"],catch_exceptions=False)
        self.assertEqual(0,result.exit_code,result.output)

    def test_fresh_migration_and_repeat_upgrade(self):
        self.upgrade();self.upgrade()
        with self.app.app_context():
            self.assertEqual("postgresql",db.engine.dialect.name)
            self.assertEqual("20261024_07",db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one())
            self.assertEqual(6,Department.query.count())
            self.assertTrue(User.query.filter_by(username="pg-admin").one().check_password("pg-test-password"))
            self.assertTrue({"tasks","task_comments","task_attachments","task_events","task_notifications","admin_audit_events","system_settings","task_categories"}.issubset(inspect(db.engine).get_table_names()))

    def test_legacy_migration_preserves_accounts_groups_and_membership(self):
        with self.app.app_context(),db.engine.begin() as conn:
            conn.execute(text("CREATE TABLE users (id SERIAL PRIMARY KEY, full_name VARCHAR(160) NOT NULL, username VARCHAR(80) UNIQUE NOT NULL, password_hash VARCHAR(255) NOT NULL, role VARCHAR(24) NOT NULL)"))
            conn.execute(text("CREATE TABLE groups (id SERIAL PRIMARY KEY, title VARCHAR(160) NOT NULL, teacher_id INTEGER NOT NULL REFERENCES users(id))"))
            conn.execute(text("CREATE TABLE group_students (group_id INTEGER REFERENCES groups(id), student_id INTEGER REFERENCES users(id), PRIMARY KEY(group_id,student_id))"))
            conn.execute(text("INSERT INTO users(full_name,username,password_hash,role) VALUES ('Existing admin','pg-admin','preserved-hash','admin'),('Teacher','teacher','teacher-hash','teacher'),('Student','student','student-hash','student')"))
            conn.execute(text("INSERT INTO groups(title,teacher_id) VALUES ('Existing group',2)"));conn.execute(text("INSERT INTO group_students VALUES (1,3)"))
        self.upgrade();self.upgrade()
        with self.app.app_context():
            self.assertEqual(3,User.query.count());self.assertEqual("preserved-hash",db.session.get(User,1).password_hash)
            self.assertEqual("teacher-hash",db.session.get(User,2).password_hash);self.assertEqual("student-hash",db.session.get(User,3).password_hash)
            self.assertEqual("Existing group",db.session.get(Group,1).title);self.assertEqual(3,db.session.get(Group,1).students[0].id)
            for ident in (2,3):self.assertEqual("employee",db.session.get(User,ident).role);self.assertTrue(db.session.get(User,ident).is_blocked)
            self.assertTrue(all(u.language=="ru" for u in User.query.all()))

    def seed(self,status="pending_approval",**values):
        self.upgrade()
        with self.app.app_context():
            root=User.query.filter_by(role="admin").one();deps=Department.query.order_by(Department.id).all()
            self.sender,self.receiver,self.third=deps[0].id,deps[1].id,deps[2].id
            self.ids={"admin":root.id}
            for name,role,dep in (("author","employee",self.sender),("sender-head","head",self.sender),("sender-two","head",self.sender),
                ("receiver-head","head",self.receiver),("receiver-two","head",self.receiver),("executor","employee",self.receiver),("alternate","employee",self.receiver)):
                user=User(username=name,full_name=name,role=role,department_id=dep,password_hash=root.password_hash)
                db.session.add(user);db.session.flush();self.ids[name]=user.id
            task=Task(**(dict(title="Native race",description="Synthetic workflow",author_id=self.ids["author"],sender_department_id=self.sender,
                recipient_department_id=self.receiver,status=status,due_at=NOW+timedelta(days=1),revision=1,created_at=NOW-timedelta(days=3),
                sent_at=None if status in ("draft","pending_approval") else NOW-timedelta(days=2),
                assignee_id=self.ids["executor"] if status in ("assigned","in_progress","recipient_review","sender_acceptance") else None)|values))
            db.session.add(task);db.session.commit();self.task_id=task.id

    def actor(self,name):return db.session.get(User,self.ids[name])
    def task(self):return db.session.get(Task,self.task_id)

    def race(self,left,right,*,gate_users=False):
        barrier=Barrier(2)
        user_gate=Barrier(2);thread_state=local()
        def lock_gate(conn,cursor,statement,parameters,context,executemany):
            if gate_users and "FROM users" in statement and "FOR " in statement and not getattr(thread_state,"seen",False):
                thread_state.seen=True;user_gate.wait(timeout=15)
        with self.app.app_context():engine=db.engine
        event.listen(engine,"after_cursor_execute",lock_gate)
        def worker(fn):
            with self.app.app_context():
                db.session.execute(text("SET LOCAL lock_timeout='8s'"));db.session.execute(text("SET LOCAL statement_timeout='15s'"))
                pid=db.session.scalar(text("SELECT pg_backend_pid()"));barrier.wait(timeout=15)
                try:
                    value=fn();db.session.commit()
                    return {"ok":True,"value":value if type(value) in (int,str,bool) else None,"pid":pid}
                except (WorkflowError,PermissionError,DepartmentError) as error:
                    db.session.rollback();return {"ok":False,"error":type(error).__name__,"pid":pid}
                finally:db.session.remove()
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(worker,fn) for fn in (left,right)]
                results=[future.result(timeout=35) for future in futures]
        finally:event.remove(engine,"after_cursor_execute",lock_gate)
        self.assertEqual(2,len({r["pid"] for r in results}),"Two independent PostgreSQL backends are required")
        return results

    def approval(self,who,action):
        return transition_approval(task=self.task(),actor=self.actor(who),action=action,expected_revision=1,comment="Native approval")
    def execution(self,who,action="assign",candidate="executor"):
        return transition_execution(task=self.task(),actor=self.actor(who),action=action,expected_revision=1,comment="Native execution",
            **({"assignee_id":self.ids[candidate]} if action=="assign" else {}))
    def review(self,who,action="complete",**fields):
        return transition_review(task=self.task(),actor=self.actor(who),action=action,expected_revision=1,comment="Native review",**fields)

    def assert_single_change(self,results,statuses):
        self.assertEqual(1,sum(r["ok"] for r in results),results)
        with self.app.app_context():
            task=self.task();self.assertIn(task.status,statuses);self.assertEqual(2,task.revision)
            self.assertEqual(1,TaskEvent.query.count());self.assertEqual(1,TaskComment.query.count())
            keys=[row.deduplication_key for row in TaskNotification.query.all()];self.assertEqual(len(keys),len(set(keys)))

    def test_concurrent_approval_and_author_withdrawal(self):
        self.seed();results=self.race(lambda:self.approval("sender-head","approve"),lambda:self.approval("author","withdraw"),gate_users=True)
        self.assert_single_change(results,{"draft","awaiting_assignment"})
        with self.app.app_context():
            self.assertEqual(self.task().status=="awaiting_assignment",self.task().sent_at is not None)

    def test_two_heads_assign_exactly_one_executor(self):
        self.seed("awaiting_assignment")
        results=self.race(lambda:self.execution("receiver-head"),lambda:self.execution("receiver-two",candidate="alternate"))
        self.assert_single_change(results,{"assigned"})
        with self.app.app_context():self.assertIn(self.task().assignee_id,{self.ids["executor"],self.ids["alternate"]})

    def test_two_sender_heads_accept_result_once(self):
        self.seed("sender_acceptance")
        results=self.race(lambda:self.review("sender-head"),lambda:self.review("sender-two"))
        self.assert_single_change(results,{"completed"})
        with self.app.app_context():self.assertIsNotNone(self.task().completed_at);self.assertEqual(self.task().completed_at,self.task().closed_at)

    def test_reminder_races_deadline_change_without_stale_later_delivery(self):
        self.seed("in_progress")
        results=self.race(lambda:run_task_reminders(now=NOW),lambda:self.review("sender-head","deadline",due_at=NOW+timedelta(days=10)))
        self.assertTrue(all(r["ok"] for r in results),results)
        with self.app.app_context():
            self.assertEqual(NOW+timedelta(days=10),self.task().due_at);self.assertEqual(1,TaskEvent.query.count())
            before=TaskNotification.query.count();self.assertEqual(0,run_task_reminders(now=NOW));db.session.commit()
            self.assertEqual(before,TaskNotification.query.count())
            keys=[n.deduplication_key for n in TaskNotification.query.all()];self.assertEqual(len(keys),len(set(keys)))

    def test_reminder_races_cancellation_and_stops_after_close(self):
        self.seed("in_progress")
        results=self.race(lambda:run_task_reminders(now=NOW),lambda:self.review("sender-head","cancel"))
        self.assertTrue(all(r["ok"] for r in results),results)
        with self.app.app_context():
            self.assertEqual("cancelled",self.task().status);before=TaskNotification.query.count()
            self.assertEqual(0,run_task_reminders(now=NOW));db.session.commit();self.assertEqual(before,TaskNotification.query.count())

    def test_archive_races_sending_without_new_delivery_to_archived_department(self):
        self.seed()
        results=self.race(lambda:archive_department(actor=self.actor("admin"),department_id=self.receiver),lambda:self.approval("sender-head","approve"))
        self.assertTrue(results[0]["ok"],results)
        with self.app.app_context():
            self.assertTrue(db.session.get(Department,self.receiver).is_archived)
            self.assertEqual(1 if results[1]["ok"] else 0,TaskEvent.query.count())
            self.assertEqual("awaiting_assignment" if results[1]["ok"] else "pending_approval",self.task().status)
            with self.assertRaises((WorkflowError,PermissionError)):
                create_task(actor=self.actor("author"),recipient_department_id=self.receiver,title="No new task",description="Must fail",due_at=NOW)
            db.session.rollback()

    def _admin_change(self,name,**values):
        rows=db.session.execute(select(User).where(User.id.in_((self.ids["admin"],self.ids[name]))).order_by(User.id).with_for_update(key_share=True)).scalars().all()
        actor=next(u for u in rows if u.id==self.ids["admin"]);target=next(u for u in rows if u.id==self.ids[name]);before=account_snapshot(target)
        for key,value in values.items():setattr(target,key,value)
        record_admin_event("account_updated","user",target.id,actor=actor,before=before,after=account_snapshot(target))

    def test_role_change_races_assignment_and_fresh_role_is_enforced(self):
        self.seed("awaiting_assignment")
        results=self.race(lambda:self._admin_change("receiver-head",role="employee"),lambda:self.execution("receiver-head"))
        self.assertTrue(results[0]["ok"],results)
        with self.app.app_context():
            self.assertEqual("employee",self.actor("receiver-head").role)
            self.assertEqual(1 if results[1]["ok"] else 0,TaskEvent.query.count())
            self.assertEqual("assigned" if results[1]["ok"] else "awaiting_assignment",self.task().status)
            if results[1]["ok"]:self.assertEqual("head",TaskEvent.query.one().actor_snapshot["role"])
            with self.assertRaises((PermissionError,WorkflowError)):self.execution("receiver-head")
            db.session.rollback()

    def test_blocking_races_start_and_blocked_executor_cannot_act_later(self):
        self.seed("assigned")
        results=self.race(lambda:self._admin_change("executor",is_blocked=True),lambda:self.execution("executor","start"))
        self.assertTrue(results[0]["ok"],results)
        with self.app.app_context():
            self.assertTrue(self.actor("executor").is_blocked);self.assertEqual(1 if results[1]["ok"] else 0,TaskEvent.query.count())
            with self.assertRaises((PermissionError,WorkflowError)):self.execution("executor","start")
            db.session.rollback()

    def test_two_reminder_workers_do_not_duplicate_notifications(self):
        self.seed("in_progress")
        results=self.race(lambda:run_task_reminders(now=NOW),lambda:run_task_reminders(now=NOW))
        self.assertTrue(all(r["ok"] for r in results),results)
        with self.app.app_context():
            count=TaskNotification.query.count();self.assertGreater(count,0)
            self.assertEqual(count,sum(r["value"] for r in results));self.assertEqual(0,TaskEvent.query.count())
            self.assertEqual(1,self.task().revision)

    def test_repeated_event_delivery_is_idempotent_under_concurrency(self):
        self.seed("awaiting_assignment")
        with self.app.app_context():
            event=TaskEvent(task_id=self.task_id,actor_id=self.ids["sender-head"],event_type="approved_and_sent",
                details={"after":{"title":"Native race"},"notification_recipient_ids":[self.ids["author"],self.ids["receiver-head"],self.ids["receiver-two"]]})
            db.session.add(event);db.session.commit();event_id=event.id
        def deliver():return deliver_event_notifications(self.task(),db.session.get(TaskEvent,event_id))
        results=self.race(deliver,deliver)
        self.assertTrue(all(r["ok"] for r in results),results)
        with self.app.app_context():
            self.assertEqual(3,TaskNotification.query.count());self.assertEqual(3,sum(r["value"] for r in results))
            self.assertEqual(1,TaskEvent.query.count())

    def test_mark_all_does_not_read_an_older_id_committed_after_boundary(self):
        self.seed("in_progress");inserted=Event();boundary_taken=Event()
        with self.app.app_context():
            existing=TaskNotification(task_id=self.task_id,user_id=self.ids["executor"],event_type="comment_added",message="Visible before boundary")
            db.session.add(existing);db.session.commit();existing_id=existing.id
        def insert():
            row=TaskNotification(task_id=self.task_id,user_id=self.ids["executor"],event_type="comment_added",message="Uncommitted at boundary")
            db.session.add(row);db.session.flush();inserted.set()
            if not boundary_taken.wait(10):raise AssertionError("Reader did not capture boundary")
            return row.id
        def read():
            if not inserted.wait(10):raise AssertionError("Writer did not insert")
            # Allocate a visible larger ID after the other connection's uncommitted ID.
            # A max(sequence) boundary would incorrectly mark that older ID read later.
            db.session.add(TaskNotification(task_id=self.task_id,user_id=self.ids["executor"],
                event_type="comment_added",message="Visible larger ID at boundary"))
            db.session.flush()
            boundary=notification_read_boundary(self.actor("executor"));boundary_taken.set()
            return mark_all_notifications_read(self.actor("executor"),boundary)
        results=self.race(insert,read)
        self.assertTrue(all(r["ok"] for r in results),results)
        with self.app.app_context():
            self.assertTrue(db.session.get(TaskNotification,existing_id).is_read)
            self.assertFalse(db.session.get(TaskNotification,results[0]["value"]).is_read)
            self.assertEqual(2,results[1]["value"])
            self.assertLess(results[0]["value"],max(n.id for n in TaskNotification.query.filter_by(is_read=True).all()))

    def test_competing_approvals_with_files_leave_no_orphan_uploads(self):
        from test_tasks import attachment_bytes
        self.seed();clients=[]
        for name in ("sender-head","sender-two"):
            client=self.app.test_client();client.post("/login",data={"username":name,"password":"pg-test-password"});clients.append(client)
        def approve(client):
            response=client.post(f"/tasks/{self.task_id}/approval/approve",data={"revision":"1","comment":"Approve with file","attachment":(BytesIO(attachment_bytes(".pdf")),"synthetic.pdf")})
            code=response.status_code;response.close();return code
        results=self.race(lambda:approve(clients[0]),lambda:approve(clients[1]))
        self.assertEqual([302,409],sorted(r["value"] for r in results))
        with self.app.app_context():
            self.assertEqual(1,TaskAttachment.query.count());self.assertEqual(1,TaskEvent.query.count());self.assertEqual(1,TaskComment.query.count())
            self.assertEqual(1,len(list(Path(self.directory.name).iterdir())))

    def test_related_author_cannot_be_deleted_after_native_workflow(self):
        self.seed("in_progress");client=self.app.test_client()
        client.post("/login",data={"username":"pg-admin","password":"pg-test-password"})
        response=client.post(f'/admin/users/{self.ids["author"]}/delete',follow_redirects=True)
        self.assertEqual(200,response.status_code);self.assertIn("Вместо удаления заблокируйте её".encode(),response.data)
        with self.app.app_context():self.assertIsNotNone(self.actor("author"));self.assertIsNotNone(self.task())

    def board_seed(self,count=36):
        self.seed('awaiting_assignment')
        self.board_ids=[self.task_id]
        with self.app.app_context():
            outsider=User(username='outsider',full_name='Other department',role='employee',
                department_id=self.third,password_hash=self.actor('admin').password_hash)
            db.session.add(outsider);db.session.flush();self.ids['outsider']=outsider.id
            self._board_add(count-1)
            db.session.commit()

    def _board_add(self,count):
        """Caller owns the isolated native transaction, never a working database."""
        statuses=('awaiting_assignment','assigned','in_progress','awaiting_clarification','recipient_review','sender_acceptance')
        for index in range(count):
            status=statuses[(len(self.board_ids))%len(statuses)]
            row=Task(title=f'Native board {len(self.board_ids)+1:03}',description='Native complete board',
                author_id=self.ids['author'],sender_department_id=self.sender,recipient_department_id=self.receiver,
                assignee_id=None if status=='awaiting_assignment' else self.ids['executor'],
                status=status,revision=1,due_at=NOW+timedelta(days=1),sent_at=NOW-timedelta(days=2),created_at=NOW)
            db.session.add(row);db.session.flush();self.board_ids.append(row.id)

    def native_client(self,username):
        client=self.app.test_client();page=client.get('/login')
        match=re.search(r'name="csrf_token"\s+value="([^"]+)"',page.get_data(as_text=True))
        data={'username':username,'password':'pg-test-password'}
        if match:data['csrf_token']=unescape(match[1])
        self.assertEqual(302,client.post('/login',data=data).status_code)
        return client

    def native_fragment(self,client,direction='received'):
        response=client.get(f'/tasks/{direction}?view=board&fragment=board&page=999')
        self.assertEqual(200,response.status_code)
        self.assertIn('no-store',response.headers['Cache-Control'])
        self.assertEqual(direction,response.headers['X-Task-Board'])
        return response

    def native_cards(self,response):
        return {c['id']:c for c in (json.loads(unescape(value)) for value in
            re.findall(r'data-board-task="([^"]+)"',response.get_data(as_text=True)))}

    def native_snapshot(self):
        with self.app.app_context():
            return {table:db.session.execute(text(f'SELECT * FROM {table} ORDER BY id')).all()
                for table in ('users','departments','tasks','task_comments','task_attachments','task_events','task_notifications')}

    def test_native_shared_panel_tracks_current_account_without_exposing_presend(self):
        self.board_seed()
        with self.app.app_context():
            hidden = Task(title='Private sender draft', description='Presend', author_id=self.ids['author'],
                sender_department_id=self.sender, recipient_department_id=self.receiver,
                status='draft', due_at=NOW)
            db.session.add(hidden); db.session.flush(); hidden_id = hidden.id; db.session.commit()
        before = self.native_snapshot()
        sender = self.native_client('author')
        first = sender.get(f'/tasks/{self.task_id}/panel')
        self.assertEqual(200, first.status_code)
        self.assertEqual(self.native_fragment(sender, 'sent').headers['X-Task-User'], first.headers['X-Task-User'])
        recipient = self.native_client('receiver-head')
        current = recipient.get(f'/tasks/{self.task_id}/panel')
        self.assertEqual(200, current.status_code)
        self.assertEqual(str(self.task_id), current.headers['X-Task-Panel'])
        self.assertEqual(self.native_fragment(recipient).headers['X-Task-User'], current.headers['X-Task-User'])
        self.assertNotEqual(first.headers['X-Task-User'], current.headers['X-Task-User'])
        self.assertIn('no-store', current.headers['Cache-Control'])
        denied = recipient.get(f'/tasks/{hidden_id}/panel')
        self.assertEqual(404, denied.status_code)
        self.assertNotIn('Private sender draft', denied.get_data(as_text=True))
        self.assertNotIn('X-Task-User', denied.headers)
        self.assertEqual(before, self.native_snapshot())

    def test_native_complete_board_36_and_300_has_bounded_selects(self):
        from test_task_board import BoardMarkup
        self.board_seed();client=self.native_client('receiver-head')
        with self.app.app_context():engine=db.engine
        samples=[]
        for size in (36,300):
            if size==300:
                with self.app.app_context():self._board_add(264);db.session.commit()
            statements=[]
            def capture(conn,cursor,statement,parameters,context,executemany):
                if statement.lstrip().upper().startswith('SELECT'):statements.append(statement)
            event.listen(engine,'before_cursor_execute',capture)
            try:response=self.native_fragment(client)
            finally:event.remove(engine,'before_cursor_execute',capture)
            cards=self.native_cards(response);markup=BoardMarkup(response.get_data(as_text=True))
            self.assertEqual(set(self.board_ids),set(cards));self.assertEqual(size,markup.counts['received'])
            self.assertEqual(5,len(markup.columns));self.assertEqual(size,sum(len(g['ids']) for g in markup.columns.values()))
            self.assertFalse(any('task_comments.body' in q or 'task_attachments.storage_key' in q for q in statements))
            samples.append(len(statements))
        self.assertEqual(samples[0],samples[1],samples);self.assertLessEqual(samples[1],15,samples)
        print(f'NATIVE_BOARD_SELECTS 36={samples[0]} 300={samples[1]}')
        listing=client.get('/tasks/received?view=list&page=2').get_data(as_text=True)
        self.assertEqual(25,len(re.findall(r'data-task-id="\d+"',listing)))

    def test_native_fragment_privacy_all_roles_and_reads_are_immutable(self):
        self.board_seed()
        with self.app.app_context():
            for status in ('draft','pending_approval'):
                db.session.add(Task(title='Private presend '+status,description='Not for receiver',author_id=self.ids['author'],
                    sender_department_id=self.sender,recipient_department_id=self.receiver,status=status,due_at=NOW))
            fourth=Department.query.order_by(Department.id).all()[3].id
            db.session.add(Task(title='Unrelated private task',description='Other departments',author_id=self.ids['outsider'],
                sender_department_id=self.third,recipient_department_id=fourth,status='awaiting_assignment',sent_at=NOW,due_at=NOW))
            db.session.commit()
        before=self.native_snapshot()
        for username,direction,expected in (('receiver-head','received',36),('executor','received',36),('author','sent',38),
                ('sender-head','sent',38),('outsider','received',0),('pg-admin','received',37),('pg-admin','sent',39)):
            client=self.native_client(username);fragment=self.native_fragment(client,direction)
            self.assertEqual(expected,len(self.native_cards(fragment)))
            self.assertEqual(str(self.ids['admin' if username=='pg-admin' else username]),fragment.headers['X-Task-User'])
            if username in ('receiver-head','executor','outsider'):
                self.assertNotIn('Private presend',fragment.get_data(as_text=True))
            if username=='pg-admin':self.assertTrue(all(not card['actions'] for card in self.native_cards(fragment).values()))
        self.assertEqual(before,self.native_snapshot())

    def test_native_panel_shares_escaped_history_and_private_file_access(self):
        from test_tasks import attachment_bytes
        self.board_seed();payload='<script>alert("native")</script>'
        with self.app.app_context():
            task=self.task();task.title=payload;task.description=payload
            record_task_event(task=task,actor=self.actor('author'),event_type='created',comment=payload)
            db.session.flush();comment=TaskComment.query.filter_by(task_id=task.id).one()
            ids=[]
            for link in (None,comment.id):
                key=uuid4().hex;data=attachment_bytes('.pdf');Path(self.directory.name,key).write_bytes(data)
                row=TaskAttachment(task_id=task.id,comment_id=link,uploaded_by_id=self.ids['author'],original_filename=payload+'.pdf',
                    storage_key=key,content_type='application/pdf',size_bytes=len(data))
                db.session.add(row);db.session.flush();ids.append(row.id)
            db.session.commit()
        before=self.native_snapshot()
        for username in ('author','receiver-head','pg-admin'):
            client=self.native_client(username)
            full=client.get(f'/tasks/{self.task_id}').get_data(as_text=True)
            panel=client.get(f'/tasks/{self.task_id}/panel');html=panel.get_data(as_text=True)
            self.assertEqual(200,panel.status_code);self.assertIn('no-store',panel.headers['Cache-Control'])
            self.assertEqual(str(self.task_id),panel.headers['X-Task-Panel'])
            for source in (full,html):
                self.assertIn('&lt;script&gt;',source);self.assertNotIn('<script>alert',source)
                self.assertIn('Задача создана',source)
                for ident in ids:self.assertIn(f'/tasks/attachments/{ident}/download',source)
            self.assertNotIn('<form',html)
            for ident in ids:
                with client.get(f'/tasks/attachments/{ident}/download') as download:self.assertEqual(200,download.status_code)
        outsider=self.native_client('outsider')
        response=outsider.get(f'/tasks/{self.task_id}/panel');self.assertEqual(404,response.status_code)
        self.assertIn('no-store',response.headers['Cache-Control']);self.assertNotIn(payload,response.get_data(as_text=True))
        for ident in ids:self.assertEqual(404,outsider.get(f'/tasks/attachments/{ident}/download').status_code)
        self.assertEqual(before,self.native_snapshot())

    def test_native_panel_hides_presend_and_enforces_expired_access(self):
        self.board_seed()
        with self.app.app_context():
            row=Task(title='Hidden draft',description='Private draft',author_id=self.ids['author'],sender_department_id=self.sender,
                recipient_department_id=self.receiver,status='draft',due_at=NOW)
            db.session.add(row);db.session.flush();hidden=row.id;db.session.commit()
        client=self.native_client('receiver-head')
        response=client.get(f'/tasks/{hidden}/panel');self.assertEqual(404,response.status_code)
        self.assertIn('no-store',response.headers['Cache-Control'])
        with self.app.app_context():archive_department(actor=self.actor('admin'),department_id=self.receiver);db.session.commit()
        self.assertEqual(200,client.get(f'/tasks/{self.task_id}/panel').status_code)
        with self.app.app_context():self.actor('receiver-head').is_blocked=True;db.session.commit()
        for path in (f'/tasks/{self.task_id}/panel','/tasks/received?view=board&fragment=board'):
            response=client.get(path);self.assertEqual(302,response.status_code)
            self.assertIn('no-store',response.headers['Cache-Control'])
            self.assertNotIn('data-task-panel-content',response.get_data(as_text=True))

    def test_native_csrf_conflict_and_read_after_complete_use_current_revision(self):
        from test_task_board import BoardMarkup
        self.seed('awaiting_assignment');self.app.config['WTF_CSRF_ENABLED']=True
        first=self.native_client('receiver-head');second=self.native_client('receiver-two')
        def token(client,direction='received'):
            fragment=self.native_fragment(client,direction)
            return unescape(re.search(r'data-csrf-token="([^"]+)"',fragment.get_data(as_text=True))[1])
        first_token=token(first);second_token=token(second)
        path=f'/tasks/{self.task_id}/execution/assign'
        data={'revision':'1','comment':'Assign with real CSRF','assignee_id':str(self.ids['executor'])}
        self.assertEqual(400,first.post(path,data=data).status_code)
        self.assertEqual(302,first.post(path,data=data|{'csrf_token':first_token}).status_code)
        self.assertEqual(409,second.post(path,data=data|{'csrf_token':second_token,'comment':'Stale second request'}).status_code)
        card=self.native_cards(self.native_fragment(first))[self.task_id]
        self.assertEqual(('assigned','to_do',2),(card['status'],card['group'],card['revision']))
        self.assertNotIn('assign',{a['key'] for a in card['actions']})
        for route,action,revision in (('execution','start',2),('review','submit_result',3),('review','forward_result',4)):
            response=first.post(f'/tasks/{self.task_id}/{route}/{action}',data={'revision':str(revision),
                'csrf_token':first_token,'comment':f'Native {action}'})
            self.assertEqual(302,response.status_code)
        sender=self.native_client('sender-head');sender_token=token(sender,'sent')
        self.assertEqual(302,sender.post(f'/tasks/{self.task_id}/review/complete',data={'revision':'5',
            'csrf_token':sender_token,'comment':'Native final acceptance'}).status_code)
        for client,direction in ((first,'received'),(sender,'sent')):
            response=self.native_fragment(client,direction);self.assertNotIn(self.task_id,self.native_cards(response))
            self.assertEqual(1,BoardMarkup(response.get_data(as_text=True)).counts['completed'])
            panel=client.get(f'/tasks/{self.task_id}/panel').get_data(as_text=True)
            self.assertIn('Задача закрыта',panel);self.assertNotIn('data-panel-action',panel)
        with self.app.app_context():
            self.assertEqual(('completed',6),(self.task().status,self.task().revision))
            self.assertEqual(5,TaskEvent.query.count());self.assertEqual(5,TaskComment.query.count())
            keys=[n.deduplication_key for n in TaskNotification.query.all()];self.assertEqual(len(keys),len(set(keys)))
