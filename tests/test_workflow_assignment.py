"""One executor, explicit assignment actions, revision conflicts and rollback."""
from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.datastructures import MultiDict

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.extensions import db
from app.models import Department, Task, TaskComment, TaskEvent, TaskAttachment, TaskNotification, User
from app import workflow as w
from support.portal_fixtures import PortalCase
from support.migration_fixtures import MigrationDatabase
from test_tasks import attachment_bytes


class AssignmentTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.files=tempfile.TemporaryDirectory(prefix="portal_case_")
        self.app.config["TASK_UPLOAD_DIR"]=self.files.name
        for name,role,blocked in (("receiver","employee",False),("peer","employee",False),
            ("receiver-head","head",False),("receiver-head2","head",False),("blocked","employee",True)):
            self.add_member(name,role=role,department_id=self.other_department_id,blocked=blocked)
        self.sent=self.task()

    def tearDown(self):
        super().tearDown()
        self.files.cleanup()

    def client_for(self,name):
        c=self.app.test_client()
        with c.session_transaction() as s:s["_user_id"]=str(self.ids[name]);s["_fresh"]=True
        return c

    def saved(self):
        with self.app.app_context():
            t=db.session.get(Task,self.sent)
            return {**{col.name:deepcopy(getattr(t, "legacy_assignee_id" if col.name=="assignee_id" else col.name)) for col in Task.__table__.columns}, "assignee_ids":t.assignee_ids}

    def snapshot(self):
        with self.app.app_context():
            return {m.__tablename__:deepcopy(db.session.execute(select(m.__table__).order_by(m.id)).all())
                    for m in (Task,TaskComment,TaskEvent,TaskAttachment,TaskNotification)}

    def act(self,key,*,actor="receiver-head",assignee=None,comment="Пояснение",revision=None,extra=None,alias=None):
        data={"revision":str(self.saved()["revision"] if revision is None else revision),"comment":comment}
        if assignee is not None:data["assignee_ids"]=str(self.ids.get(assignee,assignee))
        if extra:data.update(extra)
        return self.client_for(actor).post(f"/tasks/{self.sent}/{alias or key}",data=data)

    def assert_ok(self,r):
        self.assertEqual(302,r.status_code,r.get_data(as_text=True)[:300])

    def test_real_direct_send_claim_release_and_assignment_share_one_history(self):
        sender=self.client_for("employee")
        response=sender.post("/tasks/new",data=self.creation_form(client=sender))
        self.assertEqual(302,response.status_code)
        self.sent=int(response.location.rsplit("/",1)[1])
        self.assertEqual(1,self.saved()["revision"])
        self.assert_ok(self.act("claim",actor="receiver"))
        self.assert_ok(self.act("release",actor="receiver"))
        self.assert_ok(self.act("assign",actor="receiver-head2",assignee="peer"))
        self.assertEqual(("in_progress",4,[self.ids["peer"]]),tuple(self.saved()[k] for k in ("status","revision","assignee_ids")))
        with self.app.app_context():
            events=TaskEvent.query.filter_by(task_id=self.sent).order_by(TaskEvent.id).all()
            self.assertEqual(["sent","claimed","released","assigned"],[e.event_type for e in events])
            self.assertEqual(4,TaskComment.query.filter_by(task_id=self.sent).count())

    def test_any_receiver_head_assigns_one_member_and_immediately_starts_work(self):
        original=self.saved()
        self.assert_ok(self.act("assign",actor="receiver-head2",assignee="receiver"))
        current=self.saved()
        self.assertEqual(("in_progress",1,[self.ids["receiver"]]),(current["status"],current["revision"],current["assignee_ids"]))
        for key in original.keys()-{"status","revision","assignee_ids","updated_at"}:self.assertEqual(original[key],current[key],key)
        with self.app.app_context():
            event=TaskEvent.query.one()
            self.assertEqual("assigned",event.event_type)
            self.assertEqual(self.ids["receiver-head2"],event.actor_id)
            self.assertEqual([],event.details["before"]["assignee_ids"])
            self.assertEqual([self.ids["receiver"]],event.details["after"]["assignee_ids"])
            self.assertEqual(event.comment_id,TaskComment.query.one().id)

    def test_claim_release_and_another_employee_claim_same_task(self):
        self.assert_ok(self.act("claim",actor="receiver"))
        self.assertEqual([self.ids["receiver"]],self.saved()["assignee_ids"])
        self.assert_ok(self.act("release",actor="receiver",comment="Не могу продолжать"))
        self.assertEqual(("new",[],2),tuple(self.saved()[k] for k in ("status","assignee_ids","revision")))
        self.assert_ok(self.act("claim",actor="peer"))
        self.assertEqual(("in_progress",[self.ids["peer"]],3),tuple(self.saved()[k] for k in ("status","assignee_ids","revision")))
        with self.app.app_context():self.assertEqual(["claimed","released","claimed"],[e.event_type for e in TaskEvent.query.order_by(TaskEvent.id)])

    def test_head_can_take_and_release_self_but_not_release_another_executor(self):
        self.assert_ok(self.act("claim",actor="receiver-head"))
        self.assertEqual(403,self.act("release",actor="receiver-head2").status_code)
        self.assert_ok(self.act("release",actor="receiver-head"))

    def test_reassign_preserves_work_and_rework_cycle_and_allows_repair(self):
        self.assert_ok(self.act("claim",actor="receiver"))
        with self.app.app_context():
            task=db.session.get(Task,self.sent);task.is_rework=True;task.rework_cycle=3
            db.session.get(User,self.ids["receiver"]).is_blocked=True
            db.session.commit()
        self.assert_ok(self.act("reassign",actor="receiver-head2",assignee="peer"))
        task=self.saved()
        self.assertEqual(("in_progress",True,3,[self.ids["peer"]]),tuple(task[k] for k in ("status","is_rework","rework_cycle","assignee_ids")))
        self.assert_ok(self.act("release",actor="peer"))
        self.assertTrue(self.saved()["is_rework"])
        self.assert_ok(self.act("claim",actor="receiver-head"))
        self.assertEqual(3,self.saved()["rework_cycle"])

    def test_wrong_roles_departments_and_unavailable_candidates_do_not_mutate(self):
        before=self.snapshot()
        for name,expected in (("employee",403),("head",403),("receiver",403),("root",403),("outsider",404)):
            self.assertEqual(expected,self.act("assign",actor=name,assignee="peer").status_code,name)
        for target in ("employee","head","outsider","blocked","999999","-1","1.5"):
            self.assertEqual(400,self.act("assign",assignee=target).status_code,target)
        self.assertEqual(before,self.snapshot())

    def test_work_task_can_be_joined_but_only_head_changes_the_whole_roster(self):
        self.assert_ok(self.act("claim",actor="receiver"))
        before=self.snapshot()
        self.assertEqual(403,self.act("release",actor="peer").status_code)
        self.assertEqual(403,self.act("reassign",actor="receiver",assignee="peer").status_code)
        self.assertEqual(400,self.act("reassign",assignee="receiver").status_code)
        self.assertEqual(before,self.snapshot())
        self.assert_ok(self.act("claim",actor="peer"))
        self.assertEqual([self.ids["receiver"],self.ids["peer"]],self.saved()["assignee_ids"])

    def test_version_comment_and_forged_assignment_fields_are_strict(self):
        for comment in (""," ","\n  "):
            self.assertEqual(400,self.act("claim",actor="receiver",comment=comment).status_code)
        c=self.client_for("receiver")
        for version in (None,"","-1","1.5","true","١","9"*80):
            data={"comment":"Required"}
            if version is not None:data["revision"]=version
            self.assertEqual(400,c.post(f"/tasks/{self.sent}/claim",data=data).status_code)
        for extra in ({"assignee_ids":str(self.ids["peer"])},{"status":"completed"},{"is_rework":"0"},{"author_id":"1"}):
            self.assertEqual(400,self.act("claim",actor="receiver",extra=extra).status_code)
        form=MultiDict({"revision":"0","comment":"Required"});form.add("comment","Override")
        self.assertEqual(400,c.post(f"/tasks/{self.sent}/claim",data=form).status_code)
        self.assertEqual(0,self.saved()["revision"])

    def test_stale_and_repeated_actions_preserve_the_unsaved_comment(self):
        self.assert_ok(self.act("claim",actor="receiver",revision=0))
        before=self.snapshot()
        response=self.act("claim",actor="peer",revision=0,comment="UNSAVED-CLAIM")
        self.assertEqual(409,response.status_code)
        self.assertIn(b"UNSAVED-CLAIM",response.data)
        self.assertEqual(before,self.snapshot())
        self.assert_ok(self.act("release",actor="receiver",revision=1))
        before=self.snapshot()
        self.assertEqual(409,self.act("release",actor="receiver",revision=1).status_code)
        self.assertEqual(before,self.snapshot())

    def test_archived_departments_finish_existing_tasks_but_closed_tasks_cannot_assign(self):
        with self.app.app_context():
            for did in (self.department_id,self.other_department_id):db.session.get(Department,did).is_archived=True
            db.session.commit()
        self.assert_ok(self.act("assign",assignee="receiver"))
        self.assert_ok(self.act("reassign",assignee="peer"))
        self.assert_ok(self.act("release",actor="peer"))
        for status in ("completed","cancelled","rejected"):
            with self.app.app_context():
                db.session.get(Task,self.sent).status=status;db.session.commit()
            self.assertEqual(409,self.act("claim",actor="receiver").status_code)

    def test_action_lists_and_candidate_lists_are_authoritative(self):
        from flask import template_rendered
        contexts=[]
        def capture(sender,template,context,**kw):contexts.append(context)
        with template_rendered.connected_to(capture,self.app):
            self.client_for("receiver-head").get(f"/tasks/{self.sent}")
        self.assertEqual({"assign","claim","reject"},{a["key"] for a in contexts[-1]["task_actions"]})
        self.assertTrue(all(a["url"].endswith('/'+a["key"]) for a in contexts[-1]["task_actions"]))
        self.assertNotIn(self.ids["blocked"],{u.id for u in contexts[-1]["assignee_candidates"]})
        self.assert_ok(self.act("claim",actor="receiver"))
        with template_rendered.connected_to(capture,self.app):
            self.client_for("receiver-head").get(f"/tasks/{self.sent}")
        self.assertEqual({"claim","reassign","complete","reject"},{a["key"] for a in contexts[-1]["task_actions"]})
        self.assertIn(self.ids["receiver"],{u.id for u in contexts[-1]["assignee_candidates"]})
        with template_rendered.connected_to(capture,self.app):self.client_for("employee").get(f"/tasks/{self.sent}")
        self.assertEqual({"cancel"},{a["key"] for a in contexts[-1]["task_actions"]})
        self.assertEqual([],contexts[-1]["assignee_candidates"])

    def test_files_and_database_failure_rollback_action_and_keep_old_materials(self):
        Path(self.files.name,"existing").write_bytes(b"keep")
        before=self.snapshot()
        pdf=attachment_bytes('.pdf')
        for where in ("app.tasks.db.session.commit","app.audit.record_task_event"):
            with patch(where,side_effect=SQLAlchemyError("synthetic")):
                self.assertEqual(500,self.act("assign",assignee="receiver",extra={"attachment":(BytesIO(pdf),"x.pdf")}).status_code)
            self.assertEqual(before,self.snapshot())
            self.assertEqual(["existing"],sorted(p.name for p in Path(self.files.name).iterdir()))
        self.assert_ok(self.act("assign",assignee="receiver",extra={"attachment":(BytesIO(pdf),"x.pdf")}))
        with self.app.app_context():
            attachment=TaskAttachment.query.one();event=TaskEvent.query.one()
            self.assertEqual(event.comment_id,attachment.comment_id)
            self.assertEqual(pdf,Path(self.files.name,attachment.storage_key).read_bytes())

    def test_notification_recipients_and_history_are_specific_not_department_wide(self):
        self.assert_ok(self.act("claim",actor="receiver"))
        with self.app.app_context():
            self.assertEqual({self.ids[x] for x in ("employee","receiver-head","receiver-head2")},
                {n.user_id for n in TaskNotification.query.all()})
        self.assert_ok(self.act("reassign",assignee="peer"))
        with self.app.app_context():
            event=TaskEvent.query.order_by(TaskEvent.id.desc()).first()
            self.assertEqual([self.ids["receiver"]],event.details["before"]["assignee_ids"])
            self.assertEqual([self.ids["peer"]],event.details["after"]["assignee_ids"])
            recips={n.user_id for n in TaskNotification.query.filter(TaskNotification.deduplication_key.like(f"event:{event.id}:user:%"))}
            self.assertEqual({self.ids[x] for x in ("employee","receiver","peer","receiver-head2")},recips)

    def test_service_reloads_actor_and_old_apis_require_revision_and_comment(self):
        with self.app.app_context():
            fake=SimpleNamespace(id=self.ids["receiver"],role="head",department_id=self.other_department_id)
            task=db.session.get(Task,self.sent)
            with self.assertRaises(PermissionError):w.assign_task(task=task,actor=fake,assignee_ids=[self.ids["peer"]],expected_revision=0,comment="Try")
            db.session.rollback()
            for kwargs in ({},{"expected_revision":0},{"comment":"Try"}):
                with self.assertRaises(w.WorkflowError):w.claim_task(task=task,actor=fake,**kwargs)
                db.session.rollback()

    def test_post_only_and_csrf_protect_all_assignment_actions(self):
        c=self.client_for("receiver")
        for suffix in ("assign","claim","reassign","release"):
            self.assertEqual(405,c.get(f"/tasks/{self.sent}/{suffix}").status_code)
        self.app.config['WTF_CSRF_ENABLED']=True
        self.assertEqual(400,c.post(f"/tasks/{self.sent}/claim",data={"revision":"0","comment":"Try"}).status_code)


@unittest.skipUnless(os.environ.get("PORTAL_TEST_DATABASE_URL"),"Use isolated local_portal.py verify")
class PostgreSQLAssignment(MigrationDatabase,unittest.TestCase):
    postgres=True

    def setUp(self):
        super().setUp();self.upgrade()
        depts=self.rows('departments');self.ids={}
        for name,role,dept in (("author","employee",depts[0]['id']),("receiver","employee",depts[1]['id']),
            ("peer","employee",depts[1]['id']),("head","head",depts[1]['id']),("head2","head",depts[1]['id'])):
            u=User(username=name,full_name=name,role=role,department_id=dept,password_hash='synthetic');db.session.add(u);db.session.flush();self.ids[name]=u.id
        t=Task(title='Synthetic',description='Required',author_id=self.ids['author'],sender_department_id=depts[0]['id'],
            recipient_department_id=depts[1]['id'],sent_at=datetime.now(timezone.utc),due_at=datetime(2030,1,1,tzinfo=timezone.utc))
        db.session.add(t);db.session.commit();self.ident=t.id;db.session.remove()

    def race(self,operations):
        barrier=Barrier(2)
        def run(actor,action,candidate):
            with self.app.app_context():
                try:
                    user=db.session.get(User,self.ids[actor]);task=db.session.get(Task,self.ident)
                    barrier.wait(timeout=10)
                    w.transition_execution(task=task,actor=user,action=action,expected_revision=0,comment='Required',
                        assignee_ids=[self.ids[candidate]] if candidate else None)
                    db.session.commit();return 'ok'
                except w.WorkflowConflict:
                    db.session.rollback();return 'conflict'
                finally:db.session.remove()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(run,*op) for op in operations]
            results=[f.result(timeout=25) for f in futures]
        self.assertEqual(['conflict','ok'],sorted(results))
        self.assertEqual(1,db.session.get(Task,self.ident).revision)
        self.assertEqual(1,TaskEvent.query.count());self.assertEqual(1,TaskComment.query.count())

    def test_two_employees_claim_only_one_wins(self):
        self.race((('receiver','claim',None),('peer','claim',None)))
        self.assertIn(db.session.get(Task,self.ident).assignee_id,(self.ids['receiver'],self.ids['peer']))

    def test_claim_and_head_assignment_compete_on_one_revision(self):
        self.race((('receiver','claim',None),('head','assign','peer')))
        self.assertEqual('in_progress',db.session.get(Task,self.ident).status)

    def test_release_and_reassignment_cannot_overwrite_each_other(self):
        task=db.session.get(Task,self.ident);task.status='in_progress';task.assignee_id=self.ids['receiver'];db.session.commit();db.session.remove()
        self.race((('receiver','release',None),('head','reassign','peer')))
        task=db.session.get(Task,self.ident)
        self.assertIn((task.status,task.assignee_id),(('new',None),('in_progress',self.ids['peer'])))
