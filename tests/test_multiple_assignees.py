"""T018 team workflow on synthetic data; native races follow below."""
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from werkzeug.datastructures import MultiDict
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Task, TaskAssignee, TaskEvent, TaskComment, TaskNotification, User
from app import workflow as w
from app.accounts import AccountConflict, set_account_blocked
from app.permissions import can_complete_task
from support.portal_fixtures import PortalCase


class MultipleAssigneeTests(PortalCase):
    def setUp(self):
        super().setUp()
        for name,role in (("one","employee"),("two","employee"),("three","employee"),("receiver-head","head")):
            self.add_member(name,role=role,department_id=self.other_department_id)
        self.ident=self.task()
        self.files=tempfile.TemporaryDirectory(prefix="portal_t018_")
        self.app.config["TASK_UPLOAD_DIR"]=self.files.name
        self.addCleanup(self.files.cleanup)

    def state(self):
        with self.app.app_context():
            task=db.session.get(Task,self.ident)
            return (task.status,task.assignee_ids,task.revision,task.rework_cycle,
                TaskEvent.query.count(),TaskComment.query.count(),TaskNotification.query.count())

    def action(self,action,actor="receiver-head",members=None,revision=None,comment="Пояснение команды"):
        with self.app.app_context():
            task=db.session.get(Task,self.ident);user=db.session.get(User,self.ids[actor])
            args=dict(task=task,actor=user,expected_revision=task.revision if revision is None else revision,comment=comment)
            if action in ("assign","claim","release","reassign"):
                result=w.transition_execution(**args,action=action,assignee_ids=members)
            else:result=w.transition_completion(**args,action=action)
            db.session.commit();return result.id

    def client_for(self,name):
        client=self.app.test_client();self.login(name,client=client);return client

    def test_assign_two_join_third_release_one_and_last(self):
        self.action("assign",members=[self.ids["one"],self.ids["two"]])
        self.action("claim","three")
        self.assertEqual(sorted(self.ids[n] for n in ("one","two","three")),self.state()[1])
        self.action("release","two");self.assertEqual("in_progress",self.state()[0])
        self.action("release","one");self.assertEqual([self.ids["three"]],self.state()[1])
        self.action("release","three");self.assertEqual(("new",[]),self.state()[:2])
        with self.app.app_context():
            event=TaskEvent.query.order_by(TaskEvent.id).first()
            self.assertEqual([self.ids["one"],self.ids["two"]],event.details["after"]["assignee_ids"])

    def test_any_member_completes_and_two_reworks_keep_the_team(self):
        team=[self.ids["one"],self.ids["two"]];self.action("assign",members=team)
        for cycle in (1,2):
            self.action("complete","two");self.assertEqual(("completed",team),self.state()[:2])
            self.action("return","employee");self.assertEqual(("in_progress",team),self.state()[:2]);self.assertEqual(cycle,self.state()[3])
        self.action("complete","one");self.assertEqual("completed",self.state()[0])

    def test_http_repeated_ids_render_all_members_and_legacy_scalar_is_rejected(self):
        client=self.client_for("receiver-head");url=f"/tasks/{self.ident}/assign"
        before=self.state();response=client.post(url,data=dict(revision=0,comment="Старая форма",assignee_id=self.ids["one"]))
        self.assertEqual(400,response.status_code);self.assertEqual(before,self.state())
        payload=MultiDict([("revision","0"),("comment","Работаем вместе"),("assignee_ids",str(self.ids["one"])),("assignee_ids",str(self.ids["two"]))])
        self.assertEqual(302,client.post(url,data=payload).status_code)
        for path in (f"/tasks/{self.ident}","/tasks/received",f"/tasks/{self.ident}/panel"):
            response=client.get(path);self.assertEqual(200,response.status_code,path)
            self.assertIn(b"one",response.data);self.assertIn(b"two",response.data)


    def test_head_replaces_only_one_member_and_noop_or_invalid_sets_are_atomic(self):
        self.action("assign",members=[self.ids["one"],self.ids["two"]])
        self.action("reassign",members=[self.ids["two"],self.ids["three"]])
        self.assertEqual(sorted([self.ids["two"],self.ids["three"]]),self.state()[1])
        for ids in ([],[self.ids["two"],self.ids["two"]],[self.ids["outsider"]],[self.ids["employee"]],[self.ids["two"],self.ids["three"]],[True],["bad"]):
            before=self.state()
            with self.assertRaises(w.WorkflowError):self.action("reassign",members=ids)
            self.assertEqual(before,self.state())

    def test_join_stale_revision_and_repeat_do_not_drop_existing_members(self):
        self.action("claim","one",revision=0);before=self.state()
        for actor in ("one","two"):
            with self.assertRaises(w.WorkflowConflict):self.action("claim",actor,revision=0)
            self.assertEqual(before,self.state())
        self.action("claim","two",revision=1)
        with self.assertRaises(w.WorkflowConflict):self.action("claim","two")
        self.assertEqual([self.ids["one"],self.ids["two"]],self.state()[1])

    def test_only_self_releases_and_nonmember_cannot_complete_or_reject(self):
        self.action("assign",members=[self.ids["one"],self.ids["two"]]);before=self.state()
        for action in ("release","complete","reject"):
            with self.assertRaises(PermissionError):self.action(action,"three")
            self.assertEqual(before,self.state())
        for actor in ("root","outsider","employee"):
            with self.assertRaises((PermissionError,LookupError)):self.action("claim",actor)
            self.assertEqual(before,self.state())

    def test_any_member_rejects_whole_task_and_notifies_author_and_other_member(self):
        self.action("assign",members=[self.ids["one"],self.ids["two"]]);self.action("reject","two")
        self.assertEqual("rejected",self.state()[0])
        with self.app.app_context():
            users={n.user_id for n in TaskNotification.query.filter_by(event_type="rejected")}
            self.assertIn(self.ids["employee"],users);self.assertIn(self.ids["one"],users)

    def test_completion_notifies_other_member_and_frozen_event_never_gains_late_joiners(self):
        from app.notifications import deliver_event_notifications
        self.action("assign",members=[self.ids["one"],self.ids["two"]])
        self.action("claim","three")
        with self.app.app_context():
            event=TaskEvent.query.filter_by(event_type="assigned").one();before=TaskNotification.query.count()
            deliver_event_notifications(db.session.get(Task,self.ident),event);db.session.commit()
            self.assertEqual(before,TaskNotification.query.count())
            self.assertNotIn(self.ids["three"],event.details["notification_recipient_ids"])
        self.action("complete","two")
        with self.app.app_context():
            ids={n.user_id for n in TaskNotification.query.filter_by(event_type="completed")}
            self.assertTrue({self.ids["one"],self.ids["three"],self.ids["employee"]}<=ids)
            self.assertNotIn(self.ids["two"],ids)

    def test_disabled_former_member_is_history_only_and_other_member_can_continue(self):
        self.action("assign",members=[self.ids["one"],self.ids["two"]]);self.action("complete","two")
        with self.app.app_context():
            set_account_blocked(actor=db.session.get(User,self.ids["root"]),user_id=self.ids["one"],blocked=True);db.session.commit()
        page=self.client_for("receiver-head").get(f"/tasks/{self.ident}").get_data(as_text=True)
        self.assertIn("one (недоступен)",page)
        self.action("return","employee")
        self.assertEqual([self.ids["two"]],self.state()[1]);self.action("complete","two")
        with self.app.app_context():
            event=TaskEvent.query.filter_by(event_type="returned_for_revision").one()
            self.assertEqual([self.ids["one"],self.ids["two"]],event.details["before"]["assignee_ids"])
            self.assertNotIn(self.ids["one"],event.details["notification_recipient_ids"])

    def test_return_with_no_available_members_allows_join_but_not_completion_yet(self):
        self.action("claim","one");self.action("complete","one")
        with self.app.app_context():
            set_account_blocked(actor=db.session.get(User,self.ids["root"]),user_id=self.ids["one"],blocked=True);db.session.commit()
        self.action("return","employee");self.assertEqual(("in_progress",[]),self.state()[:2])
        with self.assertRaises(w.WorkflowConflict):self.action("complete")
        self.action("claim","two");self.action("complete","two")

    def test_removing_one_before_disabling_preserves_colleague_and_other_active_tasks_block(self):
        self.action("assign",members=[self.ids["one"],self.ids["two"]])
        with self.app.app_context():
            with self.assertRaises(AccountConflict):set_account_blocked(actor=db.session.get(User,self.ids["root"]),user_id=self.ids["one"],blocked=True)
            db.session.rollback()
        self.action("reassign",members=[self.ids["two"]])
        with self.app.app_context():
            set_account_blocked(actor=db.session.get(User,self.ids["root"]),user_id=self.ids["one"],blocked=True);db.session.commit()
        self.assertEqual([self.ids["two"]],self.state()[1]);self.action("complete","two")

    def test_three_assignees_do_not_multiply_board_list_staff_or_report_counts(self):
        self.action("assign",members=[self.ids[n] for n in ("one","two","three")])
        client=self.client_for("receiver-head")
        for path in ("/tasks/received","/tasks/received?view=list"):
            response=client.get(path);self.assertEqual(200,response.status_code)
            self.assertEqual(1,response.data.count(f'data-task-id="{self.ident}"'.encode()))
        for path in ("/staff","/reports/department"):
            response=client.get(path);self.assertEqual(200,response.status_code,path)

    def test_database_unique_pair_legacy_scalar_and_new_status_guards(self):
        self.action("assign",members=[self.ids["one"],self.ids["two"]])
        with self.app.app_context():
            for sql,params in (("INSERT INTO task_assignees(task_id,user_id) VALUES (:task,:user)",dict(task=self.ident,user=self.ids["one"])),
                ("UPDATE tasks SET status='new' WHERE id=:task",dict(task=self.ident)),
                ("UPDATE tasks SET assignee_id=:user WHERE id=:task",dict(task=self.ident,user=self.ids["one"]))):
                with self.assertRaises(IntegrityError),db.session.begin_nested():db.session.execute(text(sql),params)
        self.assertEqual([self.ids["one"],self.ids["two"]],self.state()[1])

    def test_bad_file_commit_failure_and_empty_comment_do_not_change_team(self):
        from sqlalchemy.exc import SQLAlchemyError
        from unittest.mock import patch
        from test_tasks import attachment_bytes
        self.action("assign",members=[self.ids["one"],self.ids["two"]]);before=self.state()
        client=self.client_for("receiver-head");url=f"/tasks/{self.ident}/reassign"
        base=dict(revision="1",comment="Заменяем одного",assignee_ids=[str(self.ids["two"]),str(self.ids["three"])])
        self.assertEqual(400,client.post(url,data=base|dict(comment=" ")).status_code)
        self.assertEqual(400,client.post(url,data=base|dict(attachment=(BytesIO(b"forged"),"bad.pdf"))).status_code)
        with patch("app.tasks.db.session.commit",side_effect=SQLAlchemyError("Synthetic failure")):
            response=client.post(url,data=base|dict(attachment=(BytesIO(attachment_bytes(".pdf")),"file.pdf")))
        self.assertEqual(500,response.status_code);self.assertEqual(before,self.state())
        self.assertEqual([], [p for p in Path(self.files.name).rglob("*") if p.is_file()])

    def test_csrf_and_foreign_user_ids_fail_without_partial_team_and_keep_selection(self):
        client=self.client_for("receiver-head");self.app.config["WTF_CSRF_ENABLED"]=True
        import re
        page=client.get(f"/tasks/{self.ident}").data;token=re.search(rb'name="csrf_token" value="([^"]+)"',page)[1].decode()
        base=dict(revision="0",comment="Коллективное назначение",assignee_ids=[str(self.ids["one"]),str(self.ids["two"])])
        self.assertEqual(400,client.post(f"/tasks/{self.ident}/assign",data=base).status_code)
        before=self.state();self.assertEqual(302,client.post(f"/tasks/{self.ident}/assign",data=base|dict(csrf_token=token)).status_code)
        self.assertEqual("new",before[0]);self.assertEqual(2,len(self.state()[1]))


from support.migration_fixtures import MigrationDatabase
from support.postgres_races import NativeRaces
import os


@unittest.skipUnless(os.environ.get("PORTAL_TEST_DATABASE_URL"),"Use isolated PostgreSQL verify")
class NativeMultipleAssigneeTests(NativeRaces,MigrationDatabase,unittest.TestCase):
    postgres=True

    def seed(self, members=("one",), status="in_progress"):
        self.upgrade();root=User.query.filter_by(role="admin").one()
        from app.models import Department
        depts=Department.query.order_by(Department.id).all();self.sender,self.receiver=[d.id for d in depts[:2]];self.third=depts[2].id
        self.ids={"root":root.id}
        for name,role,dep in (("author","employee",self.sender),("head","head",self.receiver),("one","employee",self.receiver),("two","employee",self.receiver),("three","employee",self.receiver)):
            user=User(username=name,full_name=name,role=role,department_id=dep,password_hash=root.password_hash)
            db.session.add(user);db.session.flush();self.ids[name]=user.id
        self.now=datetime.now(timezone.utc).replace(microsecond=0)
        task=Task(title="Native team",description="Synthetic only",author_id=self.ids["author"],sender_department_id=self.sender,
            recipient_department_id=self.receiver,status=status,assignee_ids=[self.ids[n] for n in members],
            sent_at=self.now-timedelta(days=2),created_at=self.now-timedelta(days=3),due_at=self.now+timedelta(days=1),revision=0,
            completed_at=self.now if status=="completed" else None,closed_at=self.now if status=="completed" else None)
        db.session.add(task);db.session.commit();self.ident=task.id;db.session.remove()

    def perform(self,action,actor,roster=None,revision=0):
        task=db.session.get(Task,self.ident);user=db.session.get(User,self.ids[actor])
        args=dict(task=task,actor=user,expected_revision=revision,comment="Native team "+action)
        if action in ("claim","release","assign","reassign"):
            return w.transition_execution(**args,action=action,assignee_ids=[self.ids[n] for n in roster] if roster is not None else None)
        return w.transition_completion(**args,action=action)

    def run_case(self,kind,first):
        from app.reminders import run_task_reminders
        if kind=="join_join":
            self.seed((),"new");left=lambda:self.perform("claim","one");right=lambda:self.perform("claim","two")
        elif kind=="join_head":
            self.seed();left=lambda:self.perform("claim","two");right=lambda:self.perform("reassign","head",["three"])
        elif kind=="release_join":
            self.seed(("one","two"));left=lambda:self.perform("release","one");right=lambda:self.perform("claim","three")
        elif kind=="release_release":
            self.seed(("one","two"));left=lambda:self.perform("release","one");right=lambda:self.perform("release","two")
        elif kind=="complete_join":
            self.seed();left=lambda:self.perform("complete","one");right=lambda:self.perform("claim","two")
        elif kind=="complete_complete":
            self.seed(("one","two"));left=lambda:self.perform("complete","one");right=lambda:self.perform("complete","two")
        elif kind=="complete_release":
            self.seed(("one","two"));left=lambda:self.perform("complete","one");right=lambda:self.perform("release","two")
        elif kind=="disable_join":
            self.seed();left=lambda:set_account_blocked(actor=db.session.get(User,self.ids["root"]),user_id=self.ids["two"],blocked=True)
            right=lambda:self.perform("claim","two")
        elif kind=="transfer_join":
            self.seed()
            from app.accounts import update_account
            def left():
                user=db.session.get(User,self.ids["two"])
                return update_account(actor=db.session.get(User,self.ids["root"]),user_id=user.id,
                    username=user.username,full_name=user.full_name,role=user.role,department_id=self.third)
            right=lambda:self.perform("claim","two")
        elif kind=="reminders_join":
            self.seed();left=lambda:run_task_reminders(now=self.now);right=lambda:self.perform("claim","two")
        else:raise AssertionError(kind)
        results=self.race(left,right,first=first)
        self.assertTrue(results[first]["ok"],results)
        task=db.session.get(Task,self.ident)
        self.assertEqual(len(task.assignee_ids),len(set(task.assignee_ids)))
        if kind=="reminders_join":
            self.assertTrue(results[1]["ok"])
            run_task_reminders(now=self.now);db.session.commit()
            notes=TaskNotification.query.filter(TaskNotification.event_type.like("reminder_%")).all()
            self.assertEqual({self.ids[n] for n in ("author","head","one","two")},{n.user_id for n in notes})
            self.assertEqual(4,len(notes));self.assertEqual(0,run_task_reminders(now=self.now));db.session.commit()
        else:
            self.assertFalse(results[1-first]["ok"],results)
            self.assertEqual(0 if kind in ("disable_join","transfer_join") and first==0 else 1,task.revision)
            self.assertEqual(task.revision,TaskEvent.query.count())
        if kind=="join_join":
            self.perform("claim","two" if first==0 else "one",revision=1);db.session.commit()
            self.assertEqual([self.ids["one"],self.ids["two"]],task.assignee_ids)
        elif kind=="release_release":
            self.perform("release","two" if first==0 else "one",revision=1);db.session.commit()
            self.assertEqual(("new",[]),(task.status,task.assignee_ids))
        elif kind=="transfer_join":
            self.assertEqual(self.third if first==0 else self.receiver,db.session.get(User,self.ids["two"]).department_id)
        elif kind=="disable_join":
            blocked=db.session.get(User,self.ids["two"]).is_blocked
            self.assertEqual(first==0,blocked);self.assertEqual(first==1,self.ids["two"] in task.assignee_ids)

    def test_native_return_then_waiting_head_change_conflicts_until_refresh(self):
        self.seed(("one","two"),"completed")
        results=self.race(lambda:self.perform("return","author"),lambda:self.perform("reassign","head",["one","three"]),first=0)
        self.assertTrue(results[0]["ok"]);self.assertFalse(results[1]["ok"])
        task=db.session.get(Task,self.ident);self.assertEqual([self.ids["one"],self.ids["two"]],task.assignee_ids)
        self.perform("reassign","head",["one","three"],revision=1);db.session.commit()
        self.assertEqual([self.ids["one"],self.ids["three"]],task.assignee_ids)
        self.assertEqual(("in_progress",1,2),(task.status,task.rework_cycle,task.revision))

    def test_native_pair_foreign_keys_and_new_status_guards(self):
        self.seed(("one","two"))
        for sql,params in (("INSERT INTO task_assignees(task_id,user_id) VALUES (:task,:user)",dict(task=self.ident,user=self.ids["one"])),
            ("INSERT INTO task_assignees(task_id,user_id) VALUES (:task,:user)",dict(task=self.ident,user=2147483646)),
            ("UPDATE tasks SET status='new' WHERE id=:task",dict(task=self.ident)),
            ("UPDATE tasks SET assignee_id=:user WHERE id=:task",dict(task=self.ident,user=self.ids["one"]))):
            with self.assertRaises(IntegrityError),db.session.begin_nested():db.session.execute(text(sql),params)
        self.assertEqual([self.ids["one"],self.ids["two"]],db.session.get(Task,self.ident).assignee_ids)


for _kind in ("join_join","join_head","release_join","release_release","complete_join","complete_release","complete_complete","disable_join","transfer_join","reminders_join"):
    for _first in (0,1):
        def _check(self,kind=_kind,first=_first):self.run_case(kind,first)
        setattr(NativeMultipleAssigneeTests,f"test_native_{_kind}_first_{_first}",_check)


class MultipleMigrationCases(MigrationDatabase):
    def test_empty_08_upgrades_to_team_schema_preserving_references_and_repeats(self):
        self.upgrade("20261028_08")
        names=("users","departments","task_categories","system_settings","admin_audit_events","groups","group_students")
        before={name:self.rows(name) for name in names}
        self.upgrade()
        self.assertEqual("20261028_09",db.session.scalar(text("SELECT version_num FROM alembic_version")))
        self.assertEqual(before,{name:self.rows(name) for name in names})
        self.assertEqual([],self.rows("task_assignees"));snapshot=self.database_snapshot()
        self.upgrade();self.assertEqual(snapshot,self.database_snapshot())

    def test_nonempty_08_refuses_before_ddl_and_preserves_everything(self):
        self.upgrade("20261028_08")
        departments=self.rows("departments");user=self.rows("users")[0]
        with db.engine.begin() as conn:
            conn.execute(self.table("tasks").insert().values(title="Private task",description="Preserve",
                author_id=user["id"],sender_department_id=departments[0]["id"],recipient_department_id=departments[1]["id"],
                due_at=datetime(2030,1,2,tzinfo=timezone.utc),sent_at=datetime(2030,1,1,tzinfo=timezone.utc),
                status="in_progress",creation_key="t018-preserve-"+"x"*32))
        before=self.database_snapshot()
        result=self.app.test_cli_runner().invoke(args=["db","upgrade"])
        self.assertNotEqual(0,result.exit_code);db.session.remove()
        self.assertEqual(before,self.database_snapshot())

    def test_partial_membership_table_on_08_is_not_overwritten(self):
        self.upgrade("20261028_08")
        with db.engine.begin() as conn:conn.execute(text("CREATE TABLE task_assignees (id integer primary key)"))
        before=self.database_snapshot()
        result=self.app.test_cli_runner().invoke(args=["db","upgrade"])
        self.assertNotEqual(0,result.exit_code);db.session.remove();self.assertEqual(before,self.database_snapshot())


class SQLiteMultipleMigrationTests(MultipleMigrationCases,unittest.TestCase):pass


@unittest.skipUnless(os.environ.get("PORTAL_TEST_DATABASE_URL"),"Use isolated PostgreSQL verify")
class PostgreSQLMultipleMigrationTests(MultipleMigrationCases,unittest.TestCase):
    postgres=True
