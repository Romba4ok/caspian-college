"""Deletion hides accounts without destroying history or allowing stale access."""
from concurrent.futures import ThreadPoolExecutor
import os
from threading import Barrier
from uuid import uuid4
from unittest.mock import patch
import unittest

from sqlalchemy import create_engine,text
from sqlalchemy.exc import SQLAlchemyError

from app import create_app
from app.accounts import authentication_stamp,is_account_deleted
from app.extensions import db
from app.models import AdminAuditEvent,Department,Task,User
from support.portal_fixtures import PortalCase


class AccountDeletionTests(PortalCase):
    postgres=False

    def setUp(self):
        config=dict(TESTING=True,DATABASE=":memory:",SECRET_KEY="synthetic-deletion",
                    ADMIN_USERNAME="root",ADMIN_PASSWORD="root-password")
        self.control=None
        if self.postgres:
            self.schema="deletion_"+uuid4().hex
            self.control=create_engine(os.environ["PORTAL_TEST_DATABASE_URL"])
            with self.control.begin() as connection:connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
            config.update(DATABASE=os.environ["PORTAL_TEST_DATABASE_URL"],
                SQLALCHEMY_ENGINE_OPTIONS={"connect_args":{"options":"-csearch_path="+self.schema}})
        self.app=create_app(config);self.client=self.app.test_client()
        with self.app.app_context():
            departments=Department.query.order_by(Department.id).all()
            self.department_id,self.other_department_id,self.third_department_id=[d.id for d in departments[:3]]
            worker=User(username="employee",full_name="Удаляемый сотрудник",role="employee",department_id=self.department_id)
            worker.set_password("member-password");db.session.add(worker)
            for name,role,dept in (("head","head",self.department_id),("outsider","employee",self.other_department_id)):
                db.session.add(User(username=name,full_name=name,role=role,department_id=dept,password_hash=worker.password_hash))
            db.session.commit();self.ids={u.username:u.id for u in User.query.all()}

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            if "memory_db" in self.app.extensions:self.app.extensions["memory_db"].close()
            db.engine.dispose()
        if self.control:
            with self.control.begin() as connection:connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
            self.control.dispose()

    def client_as(self,name):
        client=self.app.test_client();self.login(name,client=client);return client

    def test_head_deletes_own_employee_and_admin_can_restore_without_data_loss(self):
        ident=self.task();target=self.ids["employee"];head=self.client_as("head")
        response=head.post(f"/staff/{target}/delete")
        self.assertEqual(302,response.status_code)
        self.assertNotIn(f'data-account-id="{target}"',head.get("/staff").text)
        with self.app.app_context():
            user=db.session.get(User,target)
            self.assertTrue(user.is_blocked);self.assertTrue(is_account_deleted(user))
            self.assertEqual(target,db.session.get(Task,ident).author_id)
            self.assertEqual("account_deleted",AdminAuditEvent.query.one().action)
        admin=self.client_as("root")
        self.assertNotIn(f'data-account-id="{target}"',admin.get("/admin/users").text)
        self.assertIn(f'data-account-id="{target}"',admin.get("/admin/users?scope=deleted").text)
        self.assertEqual(404,admin.post(f"/admin/users/{target}/unblock").status_code)
        self.assertEqual(404,admin.get(f"/admin/users/{target}/edit").status_code)
        self.assertEqual(302,admin.post(f"/admin/users/{target}/restore").status_code)
        self.assertIn(f'data-account-id="{target}"',admin.get("/admin/users").text)
        with self.app.app_context():
            self.assertFalse(is_account_deleted(db.session.get(User,target)))
            self.assertTrue(db.session.get(User,target).is_blocked)
        self.assertEqual(302,admin.post(f"/admin/users/{target}/unblock").status_code)

    def test_delete_is_idempotent_and_does_not_reuse_identity(self):
        admin=self.client_as("root");target=self.ids["employee"]
        for _ in range(2):self.assertEqual(302,admin.post(f"/admin/users/{target}/delete").status_code)
        with self.app.app_context():
            self.assertEqual(1,AdminAuditEvent.query.filter_by(action="account_deleted").count())
            self.assertIsNotNone(db.session.get(User,target))

    def test_api_preserves_deleted_identity_and_exposes_state_for_external_sync(self):
        token="synthetic-deletion-read-token-000000000000"
        self.app.config.update(CRM_API_READ_TOKEN=token,CRM_API_WRITE_TOKEN="synthetic-deletion-write-token-000000000000")
        admin=self.client_as("root");target=self.ids["employee"]
        admin.post(f"/admin/users/{target}/delete")
        def state():
            response=self.client.get("/api/v1/users",headers={"Authorization":"Bearer "+token})
            self.assertEqual(200,response.status_code)
            self.assertNotIn("password_hash",response.text)
            return next(user for user in response.json["data"] if user["id"]==target)
        deleted=state();self.assertTrue(deleted["is_deleted"]);self.assertFalse(deleted["is_active"])
        admin.post(f"/admin/users/{target}/restore")
        restored=state();self.assertFalse(restored["is_deleted"]);self.assertFalse(restored["is_active"])

    def test_permissions_csrf_and_tampered_forms_cannot_delete_other_accounts(self):
        head=self.client_as("head")
        for name in ("outsider","root","head"):
            self.assertEqual(404,head.post(f'/staff/{self.ids[name]}/delete').status_code)
        employee=self.client_as("employee")
        self.assertEqual(403,employee.post(f'/staff/{self.ids["outsider"]}/delete').status_code)
        self.assertEqual(400,head.post(f'/staff/{self.ids["employee"]}/delete',data={"role":"admin"}).status_code)
        self.app.config["WTF_CSRF_ENABLED"]=True
        self.assertEqual(400,head.post(f'/staff/{self.ids["employee"]}/delete').status_code)
        with self.app.app_context():self.assertFalse(is_account_deleted(db.session.get(User,self.ids["employee"])))

    def test_active_assignments_require_reassignment_and_failed_delete_is_atomic(self):
        target=self.ids["employee"]
        self.task(author_id=self.ids["outsider"],sender_department_id=self.other_department_id,
                  recipient_department_id=self.department_id,status="in_progress",assignee_ids=[target])
        for client,path in ((self.client_as("head"),f"/staff/{target}/delete"),(self.client_as("root"),f"/admin/users/{target}/delete")):
            response=client.post(path)
            self.assertEqual(409,response.status_code)
            self.assertIn("переназначьте",response.text)
        with self.app.app_context():
            self.assertFalse(db.session.get(User,target).is_blocked)
            self.assertFalse(is_account_deleted(db.session.get(User,target)))
            self.assertEqual(0,AdminAuditEvent.query.count())

    def test_old_sessions_cannot_revive_after_delete_restore_and_unblock(self):
        employee=self.client_as("employee");head=self.client_as("head");admin=self.client_as("root")
        target=self.ids["employee"]
        with self.app.app_context():old_stamp=authentication_stamp(db.session.get(User,target))
        head.post(f"/staff/{target}/delete")
        admin.post(f"/admin/users/{target}/restore");admin.post(f"/admin/users/{target}/unblock")
        self.assertEqual(302,employee.get("/tasks/received").status_code)
        with self.app.app_context():self.assertNotEqual(old_stamp,authentication_stamp(db.session.get(User,target)))

    def test_database_failure_rolls_back_marker_and_block(self):
        head=self.client_as("head");target=self.ids["employee"]
        with patch("app.staff.db.session.commit",side_effect=SQLAlchemyError("PRIVATE")):
            response=head.post(f"/staff/{target}/delete")
        self.assertEqual(500,response.status_code);self.assertNotIn("PRIVATE",response.text)
        with self.app.app_context():
            self.assertFalse(db.session.get(User,target).is_blocked)
            self.assertFalse(is_account_deleted(db.session.get(User,target)))
            self.assertEqual(0,AdminAuditEvent.query.count())


@unittest.skipUnless(os.environ.get("PORTAL_TEST_DATABASE_URL"),"Requires isolated PostgreSQL")
class PostgreSQLAccountDeletionTests(AccountDeletionTests):
    postgres=True

    def test_deletion_races_with_claim_without_orphaning_active_assignments(self):
        ident=self.task(author_id=self.ids["outsider"],sender_department_id=self.other_department_id,
                        recipient_department_id=self.department_id)
        head=self.client_as("head");employee=self.client_as("employee");barrier=Barrier(2)
        def action(item):
            client,path,data=item;barrier.wait(timeout=10)
            return client.post(path,data=data).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses=list(pool.map(action,[(head,f'/staff/{self.ids["employee"]}/delete',{}),
                (employee,f"/tasks/{ident}/claim",{"revision":"0","comment":"Беру в работу"})]))
        self.assertIn(statuses[0],(302,409),statuses)
        self.assertIn(statuses[1],(302,403,409),statuses)
        with self.app.app_context():
            user=db.session.get(User,self.ids["employee"]);task=db.session.get(Task,ident)
            if is_account_deleted(user):self.assertEqual([],task.assignee_ids)
            else:self.assertIn(user.id,task.assignee_ids)
