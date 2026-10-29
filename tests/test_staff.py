"""Head-owned employees, shared access guards and immutable account history."""
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.datastructures import MultiDict

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.extensions import db
from app.models import AdminAuditEvent, Department, Task, TaskComment, TaskEvent, TaskNotification, User
from app.accounts import create_account, update_account, set_account_blocked, authentication_stamp, AccountError
from support.portal_fixtures import PortalCase


class StaffTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.add_member("head2",role="head")
        self.add_member("replacement")
        self.add_member("receiver",department_id=self.other_department_id)
        self.add_member("other-head",role="head",department_id=self.other_department_id)

    def client_for(self,name="head"):
        client=self.app.test_client()
        with self.app.app_context():stamp=authentication_stamp(db.session.get(User,self.ids[name]))
        with client.session_transaction() as session:
            session["_user_id"]=str(self.ids[name]);session["_fresh"]=True;session["_auth_stamp"]=stamp
        return client

    def form(self,**changes):
        return dict(full_name="Новый сотрудник",username="new-staff",password="new-secret")|changes

    def snapshot(self):
        with self.app.app_context():
            return {m.__tablename__:deepcopy(db.session.execute(select(m.__table__).order_by(m.id)).all())
                for m in (User,Task,TaskComment,TaskEvent,TaskNotification,AdminAuditEvent)}

    def assigned_task(self):
        return self.task(status="in_progress",author_id=self.ids["receiver"],sender_department_id=self.other_department_id,
            recipient_department_id=self.department_id,assignee_id=self.ids["employee"])

    def test_list_is_scoped_to_own_employees_and_navigation_is_head_only(self):
        from flask import template_rendered
        contexts=[]
        def capture(sender,template,context,**kw):contexts.append(context)
        with template_rendered.connected_to(capture,self.app):response=self.client_for().get("/staff")
        self.assertEqual(200,response.status_code)
        self.assertEqual({self.ids["employee"],self.ids["replacement"]},{u.id for u in contexts[-1]["employees"]})
        self.assertIn(b'href="/staff"',response.data)
        for role in ("employee","root"):
            self.assertEqual(403,self.client_for(role).get("/staff").status_code)

    def test_head_creates_employee_with_fixed_role_department_and_safe_audit(self):
        self.assertEqual(302,self.client_for("head2").post("/staff/new",data=self.form()).status_code)
        with self.app.app_context():
            user=User.query.filter_by(username="new-staff").one()
            self.assertEqual(("employee",self.department_id,False),(user.role,user.department_id,user.is_blocked))
            self.assertTrue(user.check_password("new-secret"))
            event=AdminAuditEvent.query.one()
            self.assertEqual(("account_created",self.ids["head2"]),(event.action,event.actor_id))
            self.assertEqual("head",event.actor_snapshot["role"])
            self.assertNotIn("new-secret",json.dumps(event.details));self.assertNotIn("password_hash",json.dumps(event.details))
        self.assertEqual(200,self.login("new-staff",password="new-secret",client=self.app.test_client()).status_code)

    def test_employee_admin_and_anonymous_cannot_use_staff_routes(self):
        for name in ("employee","root",None):
            client=self.client_for(name) if name else self.app.test_client()
            expected=403 if name else 302
            for path in ("/staff","/staff/new",f'/staff/{self.ids["employee"]}/edit'):
                self.assertEqual(expected,client.get(path).status_code)
            for path in ("/staff/new",f'/staff/{self.ids["employee"]}/edit',f'/staff/{self.ids["employee"]}/disable'):
                self.assertEqual(expected,client.post(path,data=self.form()).status_code)

    def test_head_cannot_manage_self_heads_admin_or_other_department(self):
        client=self.client_for();before=self.snapshot()
        for name in ("head","head2","root","receiver","other-head","outsider"):
            for suffix in ("edit","disable"):
                self.assertEqual(404,client.post(f'/staff/{self.ids[name]}/{suffix}',data=self.form()).status_code)
            self.assertEqual(404,client.get(f'/staff/{self.ids[name]}/edit').status_code)
        self.assertEqual(before,self.snapshot())

    def test_untrusted_fields_duplicates_and_uploads_cannot_set_role_department_or_access(self):
        client=self.client_for();before=self.snapshot()
        for path in ("/staff/new",f'/staff/{self.ids["employee"]}/edit'):
            for fields in ({"role":"head"},{"role":"admin"},{"department_id":str(self.other_department_id)},
                           {"department_id":str(self.department_id)},{"is_blocked":"false"},{"password_hash":"forged"},
                           {"user_id":str(self.ids["root"])},{"department_role":"head"}):
                self.assertEqual(400,client.post(path,data=self.form(**fields)).status_code)
            form=MultiDict(self.form());form.add("username","override")
            self.assertEqual(400,client.post(path,data=form).status_code)
            self.assertEqual(400,client.post(path,data=self.form(attachment=(io.BytesIO(b"data"),"x.txt"))).status_code)
        self.assertEqual(before,self.snapshot())

    def test_blank_edit_password_preserves_hash_and_blocked_state(self):
        with self.app.app_context():before=db.session.get(User,self.ids["employee"]).password_hash
        response=self.client_for().post(f'/staff/{self.ids["employee"]}/edit',data=self.form(username="employee",password="",full_name="Новое имя"))
        self.assertEqual(302,response.status_code)
        with self.app.app_context():
            user=db.session.get(User,self.ids["employee"])
            self.assertEqual((before,False),(user.password_hash,user.is_blocked))
            audit=AdminAuditEvent.query.one();self.assertFalse(audit.details["after"]["password_changed"])
            db.session.get(User,self.ids["head"]).full_name="Имя изменено позже";db.session.commit()
            self.assertNotEqual("Имя изменено позже",audit.actor_snapshot["name"])

    def test_disable_preserves_identity_authorship_history_and_password(self):
        ident=self.task()
        with self.app.app_context():
            user=db.session.get(User,self.ids["employee"]);original=user.password_hash
            comment=TaskComment(task_id=ident,author_id=user.id,body="Прежняя запись");db.session.add(comment);db.session.commit()
        client=self.client_for()
        self.assertEqual(302,client.post(f'/staff/{self.ids["employee"]}/disable').status_code)
        before=self.snapshot()
        self.assertEqual(302,client.post(f'/staff/{self.ids["employee"]}/disable').status_code)
        self.assertEqual(before,self.snapshot())
        with self.app.app_context():
            user=db.session.get(User,self.ids["employee"])
            self.assertEqual((True,original),(user.is_blocked,user.password_hash))
            self.assertEqual(user.id,db.session.get(Task,ident).author_id)
            self.assertEqual(user.id,TaskComment.query.one().author_id)
            self.assertEqual("account_blocked",AdminAuditEvent.query.one().action)
        self.assertEqual(404,client.post(f'/staff/{self.ids["employee"]}/unblock').status_code)

    def test_active_assignments_block_head_admin_delete_alias_and_department_transfer(self):
        ident=self.assigned_task();before=self.snapshot();target=self.ids["employee"]
        self.assertEqual(409,self.client_for().post(f"/staff/{target}/disable").status_code)
        admin=self.client_for("root")
        for action in ("block","delete"):
            self.assertEqual(409,admin.post(f"/admin/users/{target}/{action}").status_code)
        values=dict(full_name="employee",username="employee",password="",role="employee",department_id=str(self.other_department_id))
        self.assertEqual(409,admin.post(f"/admin/users/{target}/edit",data=values).status_code)
        self.assertEqual(before,self.snapshot())
        self.assertEqual(302,self.client_for().post(f"/tasks/{ident}/reassign",data={
            "revision":"0","comment":"Переназначить перед отключением","assignee_ids":str(self.ids["replacement"])}).status_code)
        self.assertEqual(302,self.client_for().post(f"/staff/{target}/disable").status_code)
        with self.app.app_context():self.assertEqual(self.ids["replacement"],db.session.get(Task,ident).assignee_id)

    def test_archived_department_blocks_creation_but_keeps_existing_management(self):
        with self.app.app_context():db.session.get(Department,self.department_id).is_archived=True;db.session.commit()
        client=self.client_for();self.assertEqual(400,client.get("/staff/new").status_code)
        self.assertEqual(400,client.post("/staff/new",data=self.form()).status_code)
        self.assertEqual(302,client.post(f'/staff/{self.ids["employee"]}/edit',data=self.form(username="employee",password="")).status_code)
        self.assertEqual(302,client.post(f'/staff/{self.ids["employee"]}/disable').status_code)

    def test_invalid_fields_duplicate_username_and_noop_keep_audit_clean(self):
        client=self.client_for();before=self.snapshot()
        for change in ({"username":"employee"},{"username":""},{"username":"x"*81},{"full_name":" "},{"full_name":"x"*161},{"password":""}):
            self.assertEqual(400,client.post("/staff/new",data=self.form(**change)).status_code)
        self.assertEqual(before,self.snapshot())
        with self.app.app_context():user=db.session.get(User,self.ids["employee"]);values=dict(full_name=user.full_name,username=user.username,password="")
        self.assertEqual(302,client.post(f'/staff/{self.ids["employee"]}/edit',data=values).status_code)
        self.assertEqual(before,self.snapshot())

    def test_csrf_methods_and_escaping(self):
        client=self.client_for();before=self.snapshot()
        self.assertEqual(405,client.get(f'/staff/{self.ids["employee"]}/disable').status_code)
        self.app.config["WTF_CSRF_ENABLED"]=True
        for path in ("/staff/new",f'/staff/{self.ids["employee"]}/edit',f'/staff/{self.ids["employee"]}/disable'):
            self.assertEqual(400,client.post(path,data=self.form()).status_code)
        self.app.config["WTF_CSRF_ENABLED"]=False
        self.assertEqual(before,self.snapshot())
        self.assertEqual(302,client.post("/staff/new",data=self.form(full_name="<script>staff</script>")).status_code)
        page=client.get("/staff").get_data(as_text=True)
        self.assertIn("&lt;script&gt;staff&lt;/script&gt;",page);self.assertNotIn("<script>staff</script>",page)

    def test_stale_role_is_rechecked_in_direct_service_and_old_session(self):
        client=self.client_for()
        with self.app.app_context():
            actor=db.session.get(User,self.ids["head"])
            db.session.execute(update(User).where(User.id==actor.id).values(role="employee").execution_options(synchronize_session=False))
            with self.assertRaises(PermissionError):create_account(actor=actor,**self.form())
            db.session.rollback()
            actor=db.session.get(User,self.ids["head"]);actor.role="employee";db.session.commit()
        self.assertEqual(403,client.get("/staff").status_code)
        self.assertEqual(403,client.post("/staff/new",data=self.form()).status_code)

    def test_direct_service_rejects_foreign_scope_and_head_role_escalation(self):
        before=self.snapshot()
        with self.app.app_context():
            actor=db.session.get(User,self.ids["head"])
            with self.assertRaises(LookupError):update_account(actor=actor,user_id=self.ids["receiver"],**self.form())
            with self.assertRaises(AccountError):create_account(actor=actor,role="head",**self.form())
            with self.assertRaises(AccountError):create_account(actor=actor,department_id=self.other_department_id,**self.form())
            with self.assertRaises(PermissionError):set_account_blocked(actor=actor,user_id=self.ids["employee"],blocked=False)
            db.session.rollback()
        self.assertEqual(before,self.snapshot())

    def test_invalid_ids_and_tampered_disable_forms_have_no_effect(self):
        before=self.snapshot();head=self.client_for();admin=self.client_for("root")
        for ident in ("0","9"*70):
            self.assertEqual(404,head.get(f"/staff/{ident}/edit").status_code)
            self.assertEqual(404,head.post(f"/staff/{ident}/disable").status_code)
            self.assertEqual(404,admin.get(f"/admin/users/{ident}/edit").status_code)
            self.assertEqual(404,admin.post(f"/admin/users/{ident}/block").status_code)
        self.assertEqual(400,head.post(f'/staff/{self.ids["employee"]}/disable',data={"is_blocked":"false"}).status_code)
        self.assertEqual(400,admin.post(f'/admin/users/{self.ids["employee"]}/delete',data={"role":"admin"}).status_code)
        with self.app.app_context():
            actor=db.session.get(User,self.ids["head"])
            for ident in (None,True,"-1","9"*70):
                with self.assertRaises(LookupError):set_account_blocked(actor=actor,user_id=ident,blocked=True)
            db.session.rollback()
        self.assertEqual(before,self.snapshot())

    def test_failures_roll_back_account_password_and_audit_without_leaking_errors(self):
        client=self.client_for();target=self.ids["employee"]
        for where in ("app.accounts.record_admin_event","app.staff.db.session.commit"):
            for path,data in (("/staff/new",self.form()),(f"/staff/{target}/edit",self.form(username="employee")),(f"/staff/{target}/disable",{})):
                before=self.snapshot()
                with patch(where,side_effect=SQLAlchemyError("synthetic-secret")):
                    response=client.post(path,data=data)
                self.assertEqual(500,response.status_code)
                self.assertNotIn(b"synthetic-secret",response.data);self.assertEqual(before,self.snapshot())

    def test_disabled_completed_executor_is_not_restored_by_return(self):
        ident=self.assigned_task()
        self.assertEqual(302,self.client_for("employee").post(f"/tasks/{ident}/complete",data={"revision":"0","comment":"Результат"}).status_code)
        self.assertEqual(302,self.client_for().post(f'/staff/{self.ids["employee"]}/disable').status_code)
        self.assertEqual(302,self.client_for("other-head").post(f"/tasks/{ident}/return",data={"revision":"1","comment":"Доработать"}).status_code)
        with self.app.app_context():
            self.assertTrue(db.session.get(User,self.ids["employee"]).is_blocked)
            self.assertEqual([],db.session.get(Task,ident).assignee_ids)
        self.assertEqual(409,self.client_for().post(f"/tasks/{ident}/complete",data={"revision":"2","comment":"Ещё результат"}).status_code)


import os
from support.migration_fixtures import MigrationDatabase
from app.accounts import AccountConflict


@unittest.skipUnless(os.environ.get("PORTAL_TEST_DATABASE_URL"),"Use isolated local_portal.py verify")
class PostgreSQLStaffTests(MigrationDatabase,unittest.TestCase):
    postgres=True

    def setUp(self):
        super().setUp();self.upgrade();departments=self.rows("departments");self.ids={}
        for name,role,dept in (("head","head",departments[0]["id"]),("employee","employee",departments[0]["id"]),
                ("replacement","employee",departments[0]["id"]),("sender","employee",departments[1]["id"])):
            user=User(username=name,full_name=name,role=role,department_id=dept,password_hash="synthetic")
            db.session.add(user);db.session.flush();self.ids[name]=user.id
        db.session.commit();self.own=departments[0]["id"];self.other=departments[1]["id"]

    def test_native_guard_then_reassign_disable_and_audit(self):
        task=Task(title="Synthetic",description="Required",author_id=self.ids["sender"],sender_department_id=self.other,
            recipient_department_id=self.own,status="in_progress",assignee_id=self.ids["employee"],
            sent_at=datetime.now(timezone.utc),due_at=datetime(2030,1,1,tzinfo=timezone.utc))
        db.session.add(task);db.session.commit();ident=task.id
        with self.assertRaises(AccountConflict):
            set_account_blocked(actor=db.session.get(User,self.ids["head"]),user_id=self.ids["employee"],blocked=True)
        db.session.rollback()
        self.assertEqual(0,AdminAuditEvent.query.count())
        from app.workflow import reassign_task
        reassign_task(task=db.session.get(Task,ident),actor=db.session.get(User,self.ids["head"]),
            assignee_ids=[self.ids["replacement"]],expected_revision=0,comment="Перед отключением")
        db.session.commit()
        set_account_blocked(actor=db.session.get(User,self.ids["head"]),user_id=self.ids["employee"],blocked=True)
        db.session.commit()
        self.assertTrue(db.session.get(User,self.ids["employee"]).is_blocked)
        self.assertEqual("account_blocked",AdminAuditEvent.query.one().action)
        self.assertEqual(self.ids["replacement"],db.session.get(Task,ident).assignee_id)

    def test_native_create_rollback_and_security_stamp_after_disable_restore(self):
        actor=db.session.get(User,self.ids["head"])
        created=create_account(actor=actor,full_name="Created",username="new-staff",password="synthetic-secret")
        self.assertEqual(("employee",self.own),(created.role,created.department_id));db.session.rollback()
        self.assertIsNone(User.query.filter_by(username="new-staff").first());self.assertEqual(0,AdminAuditEvent.query.count())
        user=db.session.get(User,self.ids["employee"]);before=authentication_stamp(user);password=user.password_hash
        set_account_blocked(actor=db.session.get(User,self.ids["head"]),user_id=user.id,blocked=True);db.session.commit()
        set_account_blocked(actor=User.query.filter_by(role="admin").one(),user_id=user.id,blocked=False);db.session.commit()
        self.assertNotEqual(before,authentication_stamp(user));self.assertEqual(password,user.password_hash)
        self.assertEqual(1,User.query.filter_by(role="admin").count())
