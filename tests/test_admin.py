"""Administrative mutations: accounts, audit, attribution and failure rollback."""
import json
import sys
from pathlib import Path
from unittest.mock import patch
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from app.extensions import db
from app.models import AdminAuditEvent, Group, Task, TaskAttachment, TaskComment, TaskEvent, TaskNotification, User
from support.portal_fixtures import PortalCase


class AdminPanelTests(PortalCase):
    def dispose_extra_app(self, app):
        with app.app_context():
            db.session.remove()
            db.engine.dispose()

    def audits(self):
        with self.app.app_context():
            return [dict(action=e.action, object_id=e.object_id, actor=e.actor_snapshot, details=e.details) for e in AdminAuditEvent.query.order_by(AdminAuditEvent.id)]

    def test_create_edit_password_and_delete_are_audited(self):
        self.login()
        self.assertEqual(302, self.client.post("/admin/users/new", data=self.form()).status_code)
        user = self.account("created")
        self.assertEqual("employee", user["role"])
        self.assertIsNone(user["department_role"])
        self.assertEqual(302, self.client.post(f'/admin/users/{user["id"]}/edit', data=self.form("renamed", full_name="Начальник", role="head", department_id=str(self.other_department_id), password="new-password")).status_code)
        updated = self.account("renamed")
        self.assertEqual("head", updated["role"])
        self.assertNotEqual(user["password_hash"], updated["password_hash"])
        with self.app.app_context():
            self.assertTrue(db.session.get(User, user["id"]).check_password("new-password"))
        self.assertEqual(302, self.client.post(f'/admin/users/{user["id"]}/delete').status_code)
        self.assertTrue(self.account("renamed")["is_blocked"])
        entries = self.audits()
        self.assertEqual(["account_created", "account_updated", "account_blocked"], [e["action"] for e in entries])
        serialized = json.dumps(entries)
        for secret in ("new-password", "member-password", updated["password_hash"], "password_hash"):
            self.assertNotIn(secret, serialized)
        self.assertTrue(entries[1]["details"]["after"]["password_changed"])
        self.assertEqual("employee", entries[1]["details"]["before"]["role"])

    def test_blank_password_is_preserved_and_untrusted_fields_are_rejected(self):
        self.login();before=self.account("employee")
        response=self.client.post(f'/admin/users/{before["id"]}/edit',data=self.form("employee",password="",department_role="head",password_hash="forged",is_blocked="true"))
        self.assertEqual(400,response.status_code);self.assertEqual(before,self.account("employee"))
        self.assertEqual(302,self.client.post(f'/admin/users/{before["id"]}/edit',data=self.form("employee",password="")).status_code)
        after=self.account("employee")
        self.assertEqual(before["password_hash"],after["password_hash"])
        self.assertEqual(("employee",None,False),(after["role"],after["department_role"],after["is_blocked"]))

    def test_only_admin_cannot_be_demoted_blocked_deleted_or_duplicated(self):
        self.login()
        before = self.account("root")
        self.assertEqual(400, self.client.post("/admin/users/new", data=self.form("second", role="admin", department_id="")).status_code)
        self.assertEqual(400, self.client.post(f'/admin/users/{self.ids["employee"]}/edit', data=self.form("employee", role="admin", department_id="")).status_code)
        self.assertEqual(400, self.client.post(f'/admin/users/{before["id"]}/edit', data=self.form("root", role="employee")).status_code)
        for action in ("block", "delete"):
            response = self.client.post(f'/admin/users/{before["id"]}/{action}', follow_redirects=True)
            self.assertIn("единственного администратора".encode(), response.data)
        self.assertEqual(before, self.account("root"))
        self.assertEqual([], self.audits())

    def test_admin_own_name_edit_records_original_actor_snapshot(self):
        self.login()
        original = self.account("root")
        self.client.post(f'/admin/users/{original["id"]}/edit', data=self.form("root", full_name="Новое имя", role="admin", department_id="", password=""))
        self.assertEqual("Новое имя", self.account("root")["full_name"])
        self.assertEqual(original["full_name"], self.audits()[0]["actor"]["name"])

    def test_block_unblock_are_audited_and_repeat_is_noop(self):
        self.login()
        target = self.ids["employee"]
        for action in ("block", "block", "unblock", "unblock"):
            self.assertEqual(302, self.client.post(f"/admin/users/{target}/{action}").status_code)
        self.assertFalse(self.account("employee")["is_blocked"])
        self.assertEqual(["account_blocked", "account_unblocked"], [e["action"] for e in self.audits()])

    def test_bad_fields_duplicate_login_and_failure_do_not_add_audit(self):
        self.login()
        for data in (self.form(full_name=" "), self.form(username="x"*81), self.form(role="teacher"),
                     self.form(role="student"), self.form(department_id=""), self.form(department_id="999999"), self.form(password=""), self.form("employee")):
            self.assertEqual(400, self.client.post("/admin/users/new", data=data).status_code)
        self.assertEqual([], self.audits())
        before = self.account("employee")
        self.assertEqual(400, self.client.post(f'/admin/users/{before["id"]}/edit', data=self.form("head")).status_code)
        self.assertEqual(before, self.account("employee"))
        self.assertEqual([], self.audits())
        self.assertEqual(200, self.client.get("/admin/users").status_code)

    def snapshot(self):
        with self.app.app_context():
            return {model.__tablename__: db.session.execute(text(f'SELECT * FROM "{model.__tablename__}" ORDER BY id')).all()
                    for model in (User, Group, Task, TaskComment, TaskAttachment, TaskEvent, TaskNotification)}

    def linked(self, kind):
        target, owner = self.ids["outsider"], self.ids["employee"]
        task_id = self.task(author_id=target if kind == "author" else owner,
            assignee_id=target if kind == "assignee" else None, status="in_progress" if kind == "assignee" else "new")
        with self.app.app_context():
            values = {
                "comment": lambda: TaskComment(task_id=task_id, author_id=target, body="Keep"),
                "attachment": lambda: TaskAttachment(task_id=task_id, uploaded_by_id=target, original_filename="keep.pdf", storage_key="b"*32, content_type="application/pdf", size_bytes=10),
                "event": lambda: TaskEvent(task_id=task_id, actor_id=target, event_type="assigned", details={"keep": True}),
                "notification": lambda: TaskNotification(task_id=task_id, user_id=target, event_type="task_created", message="Keep"),
            }
            if kind in values:
                db.session.add(values[kind]())
                db.session.commit()
        return target

    def test_delete_alias_preserves_all_references_and_guards_active_assignment(self):
        self.login()
        for kind in ("author","assignee","comment","attachment","event","notification"):
            with self.app.app_context():
                user=User(full_name=kind,username="linked-"+kind,role="employee",department_id=self.other_department_id,password_hash="synthetic")
                db.session.add(user);db.session.commit();self.ids["outsider"]=user.id
            target=self.linked(kind)
            with self.app.app_context():
                before={m.__tablename__:db.session.execute(text("SELECT * FROM "+m.__tablename__+" ORDER BY id")).all()
                    for m in (Task,TaskComment,TaskAttachment,TaskEvent,TaskNotification)}
            with patch("app.admin.db.session.delete",side_effect=AssertionError("No physical deletion")):
                response=self.client.post(f"/admin/users/{target}/delete")
            self.assertEqual(409 if kind=="assignee" else 302,response.status_code)
            with self.app.app_context():
                self.assertEqual(kind!="assignee",db.session.get(User,target).is_blocked)
                for model in (Task,TaskComment,TaskAttachment,TaskEvent,TaskNotification):
                    self.assertEqual(before[model.__tablename__],db.session.execute(text("SELECT * FROM "+model.__tablename__+" ORDER BY id")).all())

    def test_disable_commit_failure_rolls_back_audit_and_keeps_session_usable(self):
        self.login();target=self.linked("author");before=self.snapshot()
        with patch("app.admin.db.session.commit",side_effect=SQLAlchemyError("synthetic-secret")):
            response=self.client.post(f"/admin/users/{target}/delete")
        self.assertEqual(500,response.status_code);self.assertNotIn(b"synthetic-secret",response.data)
        self.assertEqual(before,self.snapshot());self.assertEqual([],self.audits())
        self.assertEqual(200,self.client.get("/admin/users").status_code)

    def test_linked_delete_requires_csrf_and_administrator(self):
        self.login()
        target = self.linked("author")
        self.app.config["WTF_CSRF_ENABLED"] = True
        self.assertEqual(400, self.client.post(f"/admin/users/{target}/delete").status_code)
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.client.post("/logout")
        self.login("employee")
        self.assertEqual(403, self.client.post(f"/admin/users/{target}/delete").status_code)

    def test_migration_preserves_existing_accounts_and_groups(self):
        migration_app = create_app(
            {
                "DATABASE": ":memory:",
                "SECRET_KEY": "migration-test-secret",
                "ADMIN_USERNAME": "root",
                "ADMIN_PASSWORD": "root-password",
            }
        )
        self.addCleanup(self.dispose_extra_app, migration_app)
        with migration_app.app_context():
            with db.engine.begin() as connection:
                connection.exec_driver_sql(
                    "CREATE TABLE users ("
                    "id INTEGER PRIMARY KEY, full_name VARCHAR(160) NOT NULL, "
                    "username VARCHAR(80) NOT NULL UNIQUE, password_hash VARCHAR(255) NOT NULL, "
                    "role VARCHAR(24) NOT NULL)"
                )
                connection.exec_driver_sql(
                    "CREATE TABLE groups (id INTEGER PRIMARY KEY, title VARCHAR(160) NOT NULL, teacher_id INTEGER NOT NULL)"
                )
                connection.exec_driver_sql(
                    "CREATE TABLE group_students (group_id INTEGER NOT NULL, student_id INTEGER NOT NULL, "
                    "PRIMARY KEY (group_id, student_id))"
                )
                connection.exec_driver_sql(
                    "INSERT INTO users (id, full_name, username, password_hash, role) "
                    "VALUES (1, 'Existing Admin', 'root', 'existing-hash', 'admin'), "
                    "(2, 'Existing Teacher', 'teacher-one', 'teacher-hash', 'teacher'), "
                    "(3, 'Existing Student', 'student-one', 'student-hash', 'student')"
                )
                connection.exec_driver_sql("INSERT INTO groups (id, title, teacher_id) VALUES (7, '9A Algebra', 2)")
                connection.exec_driver_sql("INSERT INTO group_students (group_id, student_id) VALUES (7, 3)")

        result = migration_app.test_cli_runner().invoke(args=["db", "upgrade"], catch_exceptions=False)
        self.assertEqual(0, result.exit_code, f"{result.output}\n{result.exception!r}")
        repeated_result = migration_app.test_cli_runner().invoke(args=["db", "upgrade"], catch_exceptions=False)
        self.assertEqual(0, repeated_result.exit_code, repeated_result.output)
        with migration_app.app_context():
            with db.engine.connect() as connection:
                users = connection.execute(
                    text("SELECT id, username, role, department_id, is_blocked FROM users ORDER BY id")
                ).all()
                group_title = connection.execute(text("SELECT title FROM groups WHERE id = 7")).scalar_one()
                member_id = connection.execute(
                    text("SELECT student_id FROM group_students WHERE group_id = 7")
                ).scalar_one()
                department_count = connection.execute(text("SELECT COUNT(*) FROM departments")).scalar_one()
        self.assertEqual(3, len(users))
        self.assertEqual((1, "root", "admin", None, False), tuple(users[0]))
        self.assertEqual((2, "teacher-one", "employee", None, True), tuple(users[1]))
        self.assertEqual((3, "student-one", "employee", None, True), tuple(users[2]))
        self.assertEqual("9A Algebra", group_title)
        self.assertEqual(3, member_id)
        self.assertEqual(6, department_count)

        fresh_app = create_app(
            {
                "DATABASE": ":memory:",
                "SECRET_KEY": "fresh-migration-test-secret",
                "ADMIN_USERNAME": "initial-admin",
                "ADMIN_PASSWORD": "fresh-password",
            }
        )
        self.addCleanup(self.dispose_extra_app, fresh_app)
        result = fresh_app.test_cli_runner().invoke(args=["db", "upgrade"], catch_exceptions=False)
        self.assertEqual(0, result.exit_code, result.output)
        with fresh_app.app_context():
            with db.engine.connect() as connection:
                initial_admin = connection.execute(text("SELECT username, role FROM users")).one()
                fresh_department_count = connection.execute(text("SELECT COUNT(*) FROM departments")).scalar_one()
                groups_table_exists = connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'groups'")
                ).first()
        self.assertEqual(("initial-admin", "admin"), tuple(initial_admin))
        self.assertEqual(6, fresh_department_count)
        self.assertIsNotNone(groups_table_exists)

