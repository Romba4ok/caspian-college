"""Account access, session revocation and server-side role boundaries."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.extensions import db
from app.models import AdminAuditEvent, Department, User
from support.portal_fixtures import PortalCase


class AccountAccessTests(PortalCase):
    def test_three_roles_login_and_initial_destinations(self):
        for name, location in (("root", "/tasks/received"), ("head", "/tasks/received"), ("employee", "/tasks/received")):
            client = self.app.test_client()
            self.assertEqual(302, self.login(name, client=client, follow=False).status_code)
            self.assertTrue(client.get("/").location.endswith(location))
            self.assertEqual(200, client.get(location).status_code)

    def test_employee_and_head_cannot_use_any_account_action_directly(self):
        urls = ("/admin/users", "/admin/users/new", f'/admin/users/{self.ids["employee"]}/edit')
        for name in ("employee", "head"):
            client = self.app.test_client()
            self.login(name, client=client)
            for url in urls:
                self.assertEqual(403, client.get(url).status_code)
                self.assertEqual(403, client.post(url, data=self.form(role="admin")).status_code if url != "/admin/users" else client.get(url).status_code)
            for action in ("block", "unblock", "delete"):
                self.assertEqual(403, client.post(f'/admin/users/{self.ids["root"]}/{action}').status_code)
        with self.app.app_context():
            self.assertEqual(0, AdminAuditEvent.query.count())

    def test_block_revokes_old_session_and_does_not_revive_it_on_unblock(self):
        self.login()
        active = self.app.test_client()
        self.login("employee", client=active)
        self.assertEqual(200, active.get("/tasks/received").status_code)
        target = self.ids["employee"]
        self.client.post(f"/admin/users/{target}/block")
        self.assertEqual(302, active.get("/tasks/received").status_code)
        failed = self.login("employee", client=self.app.test_client())
        self.assertIn("Неверный логин или пароль".encode(), failed.data)
        self.client.post(f"/admin/users/{target}/unblock")
        self.assertEqual(302, active.get("/tasks/received").status_code)
        self.assertEqual(200, self.login("employee", client=active).status_code)

    def test_ambiguous_user_requires_department_before_unblock(self):
        with self.app.app_context():
            u = User(full_name="Не настроен", username="unclear", role="employee", is_blocked=True, password_hash="historical")
            db.session.add(u)
            db.session.commit()
            user_id = u.id
        self.login()
        response = self.client.post(f"/admin/users/{user_id}/unblock", follow_redirects=True)
        self.assertIn("Перед разблокировкой".encode(), response.data)
        self.assertTrue(self.account("unclear")["is_blocked"])

    def test_role_and_department_are_loaded_from_database_for_existing_session(self):
        active = self.app.test_client()
        self.login("head", client=active)
        self.login()
        self.client.post(f'/admin/users/{self.ids["head"]}/edit', data=self.form("head", role="employee", department_id=str(self.other_department_id), password=""))
        self.assertEqual(200, active.get("/tasks/received").status_code)
        self.assertEqual(403, active.get("/admin/users").status_code)
        page = active.get("/tasks/received").get_data(as_text=True)
        self.assertIn("Сотрудник отдела", page)
        self.assertNotIn("Начальник отдела", page)

    def test_untrusted_next_redirects_and_bad_password(self):
        for next_url in ("https://example.invalid", "//example.invalid", "/\\example.invalid"):
            client = self.app.test_client()
            response = client.post("/login", query_string={"next": next_url}, data={"username": "root", "password": "root-password"})
            self.assertEqual("/", response.location)
        self.assertIn("Неверный логин или пароль".encode(), self.login(password="wrong").data)

    def test_csrf_protects_login_and_account_changes(self):
        self.app.config["WTF_CSRF_ENABLED"] = True
        self.assertEqual(400, self.client.post("/login", data={"username": "root", "password": "root-password"}).status_code)
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.login()
        self.app.config["WTF_CSRF_ENABLED"] = True
        for url in ("/admin/users/new", f'/admin/users/{self.ids["employee"]}/edit', f'/admin/users/{self.ids["employee"]}/delete'):
            self.assertEqual(400, self.client.post(url, data=self.form()).status_code)

    def test_archived_department_cannot_receive_new_user_but_keeps_existing(self):
        with self.app.app_context():
            db.session.get(Department, self.department_id).is_archived = True
            db.session.commit()
        self.login()
        self.assertEqual(400, self.client.post("/admin/users/new", data=self.form()).status_code)
        self.assertEqual(302, self.client.post(f'/admin/users/{self.ids["employee"]}/edit', data=self.form("employee", password="")).status_code)
        self.assertIsNone(self.account("created"))


    def test_disable_and_restore_without_old_request_never_revives_old_session(self):
        old=self.app.test_client();self.login("employee",client=old)
        self.login();target=self.ids["employee"]
        self.assertEqual(302,self.client.post(f"/admin/users/{target}/block").status_code)
        self.assertEqual(302,self.client.post(f"/admin/users/{target}/unblock").status_code)
        self.assertEqual(302,old.get("/tasks/received").status_code)
        self.assertEqual(200,self.login("employee",client=old).status_code)

    def test_legacy_id_only_cookie_cannot_survive_disable_restore_cycle(self):
        old=self.app.test_client()
        with old.session_transaction() as state:state["_user_id"]=str(self.ids["employee"]);state["_fresh"]=True
        self.assertEqual(200,old.get("/tasks/received").status_code)
        self.login();target=self.ids["employee"]
        self.client.post(f"/admin/users/{target}/block");self.client.post(f"/admin/users/{target}/unblock")
        self.assertEqual(302,old.get("/tasks/received").status_code)

    def test_head_password_reset_revokes_old_session_without_exposing_hash(self):
        old=self.app.test_client();self.login("employee",client=old)
        with old.session_transaction() as state:
            self.assertEqual(64,len(state["_auth_stamp"]))
            self.assertNotIn("password_hash",state)
        manager=self.app.test_client();self.login("head",client=manager)
        self.assertEqual(302,manager.post(f'/staff/{self.ids["employee"]}/edit',data={
            "full_name":"employee","username":"employee","password":"reset-secret"}).status_code)
        self.assertEqual(302,old.get("/tasks/received").status_code)
        self.assertEqual(200,self.login("employee",password="reset-secret",client=old).status_code)

    def test_malformed_auth_stamp_and_user_id_do_not_raise_server_errors(self):
        for key,value in (("_auth_stamp","ю"*64),("_auth_stamp",{}),("_user_id","9"*100),("_user_id","-1")):
            client=self.app.test_client()
            with client.session_transaction() as state:
                state["_user_id"]=str(self.ids["employee"]);state[key]=value
            self.assertEqual(302,client.get("/tasks/received").status_code)


    def test_failed_login_transaction_never_leaves_authenticated_cookie(self):
        from unittest.mock import patch
        from sqlalchemy.exc import SQLAlchemyError
        with patch("app.auth.db.session.commit",side_effect=SQLAlchemyError("synthetic-secret")):
            response=self.client.post("/login",data={"username":"employee","password":"member-password"})
        self.assertEqual(500,response.status_code);self.assertNotIn(b"synthetic-secret",response.data)
        with self.client.session_transaction() as state:
            self.assertNotIn("_user_id",state);self.assertNotIn("_auth_stamp",state)
        self.assertEqual(302,self.client.get("/tasks/received").status_code)
        self.assertEqual(200,self.login("employee").status_code)
