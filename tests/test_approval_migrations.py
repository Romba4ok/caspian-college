"""Actual schema/data upgrades on SQLite and disposable PostgreSQL schemas."""
import importlib.util
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import create_app
from app.extensions import db
from app.models import Department, Task, TaskCategory, User
from app.reminders import run_task_reminders
from support.migration_fixtures import fill_legacy, OLD_STATUSES, MigrationDatabase


class MigrationCases(MigrationDatabase):
    def upgrade(self, revision="20261024_07"):
        # These tests preserve 04→07 evidence; 08 deliberately refuses old data.
        return super().upgrade(revision)

    def legacy(self):
        self.upgrade("20261024_04")
        with db.engine.begin() as conn:
            self.ids = fill_legacy(conn)

    def table(self, name):
        return sa.Table(name, sa.MetaData(), autoload_with=db.engine)

    def rows(self, name):
        table = self.table(name)
        with db.engine.connect() as conn:
            return [dict(row) for row in conn.execute(sa.select(table).order_by(*table.primary_key.columns)).mappings()]

    def foreign_keys(self, conn):
        if not self.postgres:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            self.assertEqual(1, conn.exec_driver_sql("PRAGMA foreign_keys").scalar())

    def rejected(self, conn, statement, expected=None):
        with self.assertRaises(IntegrityError) as error:
            with conn.begin_nested():
                conn.execute(statement)
        if expected:
            self.assertIn(expected.upper(), str(error.exception.orig).upper())

    def test_fresh_and_repeat_upgrade(self):
        self.upgrade()
        before = {name: self.rows(name) for name in ("departments", "users", "system_settings", "admin_audit_events")}
        self.upgrade()
        self.assertEqual(before, {name: self.rows(name) for name in before})
        self.assertEqual(6, len(before["departments"]))
        self.assertTrue(all(d["normalized_name"] == d["name"].strip().casefold() for d in before["departments"]))
        self.assertEqual(["admin"], [u["role"] for u in before["users"]])
        self.assertTrue(User.query.one().check_password("synthetic-test-password"))
        self.assertEqual(20, before["system_settings"][0]["max_upload_mb"])
        self.assertEqual(["pdf", "docx", "xlsx", "pptx", "jpg", "png"], before["system_settings"][0]["allowed_extensions"])
        with db.engine.connect() as conn:
            self.assertEqual("20261024_07", conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar())

    def test_legacy_evidence_and_unknown_dates_preserved(self):
        self.legacy()
        upload = Path(self.app.config["TASK_UPLOAD_DIR"]) / "preserved-private-key"
        upload.parent.mkdir()
        upload.write_bytes(b"original attachment bytes, not a new generated file")
        original_bytes = upload.read_bytes()
        before = {n: self.rows(n) for n in ("groups", "group_students", "task_comments", "task_attachments",
                  "task_events", "task_notifications", "task_translations", "tasks", "users")}
        self.upgrade()
        self.assertEqual(original_bytes, upload.read_bytes())
        for name in ("groups", "group_students", "task_comments", "task_attachments", "task_notifications", "task_translations"):
            old = before[name]
            current = self.rows(name)
            self.assertEqual(old, [{key: row[key] for key in old[0]} for row in current], name)
        events = self.rows("task_events")
        original = [row for row in events if row["event_type"] != "workflow_migrated"]
        self.assertEqual(before["task_events"], [{key: row[key] for key in before["task_events"][0]} for row in original])
        self.assertTrue(all(row["actor_snapshot"] is None for row in original))
        self.assertEqual(7, sum(row["event_type"] == "workflow_migrated" for row in events))
        users = {u["username"]: u for u in self.rows("users")}
        self.assertEqual("employee", users["author"]["role"])
        self.assertEqual("head", users["boss"]["role"])
        self.assertTrue(users["blocked"]["is_blocked"])
        for name in ("student", "unclear"):
            self.assertEqual("employee", users[name]["role"])
            self.assertTrue(users[name]["is_blocked"])
        for old in before["users"]:
            for key in ("id", "username", "full_name", "password_hash", "language", "department_id", "department_role"):
                self.assertEqual(old[key], users[old["username"]][key], key)
        tasks = sorted(self.rows("tasks"), key=lambda t: t["id"])
        self.assertEqual(["awaiting_assignment", "assigned", "in_progress", "awaiting_clarification", "recipient_review", "completed", "cancelled"], [t["status"] for t in tasks])
        for old, task in zip(before["tasks"], tasks):
            for key in old:
                if key != "status":
                    self.assertEqual(old[key], task[key], key)
            self.assertTrue(task["is_legacy_dispatched"])
            self.assertEqual(old["due_at"] is None, task["legacy_missing_due"])
        self.assertIsNone(tasks[3]["sent_at"])
        self.assertIsNotNone(tasks[5]["completed_at"])
        self.assertEqual(tasks[5]["completed_at"], tasks[5]["closed_at"])
        self.assertIsNone(tasks[6]["closed_at"])
        self.assertEqual(1, len(self.rows("task_categories")))
        self.assertEqual(1, len({t["category_id"] for t in tasks}))
        snapshot = self.rows("task_events")
        self.upgrade()
        self.assertEqual(snapshot, self.rows("task_events"))
        if not self.postgres:
            with db.engine.connect() as conn:
                self.assertEqual([], conn.exec_driver_sql("PRAGMA foreign_key_check").all())

    def test_database_enforces_roles_deadline_priorities_and_settings(self):
        self.legacy()
        self.upgrade()
        users, tasks, settings = (self.table(n) for n in ("users", "tasks", "system_settings"))
        depts = self.ids["departments"]
        data = dict(id=100, title="New", description="Required", author_id=self.ids["users"]["author"],
                    sender_department_id=depts[0], recipient_department_id=depts[1], status="draft")
        with db.engine.begin() as conn:
            self.foreign_keys(conn)
            self.rejected(conn, tasks.insert().values(**data), "ck_tasks_due_required")
            self.rejected(conn, tasks.insert().values(**data, legacy_missing_due=True), "ck_tasks_due_required")
            conn.execute(tasks.insert().values(**data, due_at=datetime.now(timezone.utc), priority="urgent"))
            self.rejected(conn, tasks.update().where(tasks.c.id == 100).values(priority="invalid"), "ck_tasks_priority")
            self.rejected(conn, tasks.update().where(tasks.c.id == 100).values(status="new"), "ck_tasks_status")
            self.rejected(conn, tasks.update().where(tasks.c.id == 100).values(revision=-1), "ck_tasks_revision")
            self.rejected(conn, users.insert().values(full_name="Other admin", username="other-admin", password_hash="x", role="admin"), "UNIQUE")
            self.rejected(conn, users.update().where(users.c.role == "admin").values(is_blocked=True), "ck_users_admin_active")
            self.rejected(conn, users.insert().values(full_name="Invalid", username="invalid-role", password_hash="x",
                role="teacher", department_id=depts[0]), "ck_users_role")
            self.rejected(conn, users.insert().values(full_name="Missing department", username="no-dept", password_hash="x", role="employee"), "ck_users_department")
            for limit in (0, 21, -1):
                self.rejected(conn, settings.update().values(max_upload_mb=limit), "ck_system_settings_upload_limit")

    def test_comment_foreign_key_rejects_other_task(self):
        self.legacy()
        self.upgrade()
        attachments, events = self.table("task_attachments"), self.table("task_events")
        with db.engine.begin() as conn:
            self.foreign_keys(conn)
            data = dict(task_id=1, comment_id=self.ids["comment"], uploaded_by_id=self.ids["users"]["author"],
                original_filename="sample.pdf", storage_key="new-valid-key", content_type="application/pdf", size_bytes=3)
            conn.execute(attachments.insert().values(**data))
            self.rejected(conn, attachments.insert().values(**dict(data, task_id=2, storage_key="new-invalid-key")))
            self.rejected(conn, events.insert().values(task_id=2, comment_id=self.ids["comment"],
                actor_id=self.ids["users"]["author"], event_type="commented", details={}))
            conn.execute(events.insert().values(task_id=1, comment_id=self.ids["comment"],
                actor_id=self.ids["users"]["author"], event_type="commented", details={}))

    def test_multiple_admins_fail_before_expansion(self):
        self.upgrade("20261024_04")
        with db.engine.begin() as conn:
            conn.execute(self.table("users").insert().values(full_name="Second", username="second-admin", password_hash="keep", role="admin"))
        result = self.app.test_cli_runner().invoke(args=["db", "upgrade"], catch_exceptions=False)
        self.assertNotEqual(0, result.exit_code)
        self.assertIn("ровно один", result.output)
        self.assertNotIn("normalized_name", {c["name"] for c in sa.inspect(db.engine).get_columns("departments")})
        self.assertEqual(2, len(self.rows("users")))

    def test_duplicate_normalized_names_fail_before_expansion(self):
        self.upgrade("20261024_04")
        original = self.rows("departments")[0]
        with db.engine.begin() as conn:
            conn.execute(self.table("departments").insert().values(slug="conflicting", name=" " + original["name"].upper() + " "))
        result = self.app.test_cli_runner().invoke(args=["db", "upgrade"], catch_exceptions=False)
        self.assertNotEqual(0, result.exit_code)
        self.assertIn("конфликтующие", result.output)
        self.assertNotIn("task_categories", sa.inspect(db.engine).get_table_names())
        self.assertEqual(7, len(self.rows("departments")))

    def test_old_schema_reminder_reader_still_works_before_switch(self):
        self.legacy()
        count = run_task_reminders(now=datetime(2026, 1, 19, 9, tzinfo=timezone.utc))
        db.session.commit()
        self.assertGreater(count, 0)
        with db.engine.connect() as conn:
            self.assertEqual("20261024_04", conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar())

    def test_revision07_reminders_do_not_select_revision08_fields(self):
        self.legacy()
        self.upgrade()
        before = self.rows("tasks")
        count = run_task_reminders(now=datetime(2026, 1, 19, 9, tzinfo=timezone.utc))
        db.session.commit()
        self.assertGreater(count, 0)
        self.assertEqual(before, self.rows("tasks"))
        self.assertNotIn("creation_key", {c["name"] for c in sa.inspect(db.engine).get_columns("tasks")})

    def test_current_models_can_create_and_read_new_schema(self):
        db.create_all()
        dept1 = Department(slug="first", name="Первый")
        dept2 = Department(slug="second", name="Второй")
        db.session.add_all([dept1, dept2])
        db.session.flush()
        admin = User(full_name="Admin", username="admin", role="admin", password_hash="synthetic")
        worker = User(full_name="Worker", username="worker", role="employee", department_id=dept1.id, password_hash="synthetic")
        category = TaskCategory(name="Категория")
        db.session.add_all([admin, worker, category])
        db.session.flush()
        task = Task(title="New", description="New description", author_id=worker.id, sender_department_id=dept1.id,
                    recipient_department_id=dept2.id, due_at=datetime.now(timezone.utc), priority="urgent", category_id=category.id, sent_at=datetime.now(timezone.utc))
        db.session.add(task)
        db.session.commit()
        db.session.expire_all()
        self.assertEqual("new", task.status)
        self.assertEqual(0, task.revision)
        self.assertFalse(task.is_legacy_dispatched)
        self.assertEqual("первый", dept1.normalized_name)
        self.assertEqual("категория", category.normalized_name)


class SQLiteApprovalMigrations(MigrationCases, unittest.TestCase):
    def test_foreign_keys_guard_also_protects_resuming_at_final_constraints(self):
        self.legacy()
        self.upgrade("20261024_06")
        before = {name: self.rows(name) for name in ("tasks", "task_comments", "task_attachments", "task_events", "users")}
        def enable_foreign_keys(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")
        sa.event.listen(db.engine, "connect", enable_foreign_keys)
        db.engine.dispose()
        result = self.app.test_cli_runner().invoke(args=["db", "upgrade"], catch_exceptions=False)
        self.assertNotEqual(0, result.exit_code)
        self.assertIn("foreign_keys=ON", result.output)
        self.assertEqual(before, {name: self.rows(name) for name in before})

    def test_enabled_foreign_keys_refuses_before_cascade_can_destroy_children(self):
        self.legacy()
        before = {name: self.rows(name) for name in ("tasks", "task_comments", "task_attachments", "task_events", "users")}
        def enable_foreign_keys(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")
        sa.event.listen(db.engine, "connect", enable_foreign_keys)
        db.engine.dispose()
        result = self.app.test_cli_runner().invoke(args=["db", "upgrade"], catch_exceptions=False)
        self.assertNotEqual(0, result.exit_code)
        self.assertIn("foreign_keys=ON", result.output)
        self.assertEqual(before, {name: self.rows(name) for name in before})
        self.assertNotIn("task_categories", sa.inspect(db.engine).get_table_names())


@unittest.skipUnless(os.environ.get("PORTAL_TEST_DATABASE_URL"), "Use local_portal.py verify --pattern test_approval_migrations.py")
class PostgreSQLApprovalMigrations(MigrationCases, unittest.TestCase):
    postgres = True


class VerificationRunnerTests(unittest.TestCase):
    def test_pattern_and_failed_test_cleanup_are_scoped_to_created_database(self):
        spec = importlib.util.spec_from_file_location("approval_runner_test", ROOT / "scripts/local_portal.py")
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        queries = []
        class Connection:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def execute(self, query): queries.append(query.as_string())
        with patch.object(runner, "start_database"), patch.object(runner, "connection", return_value=Connection()), \
             patch.object(runner, "database_url", return_value="isolated-test-url"), \
             patch.object(runner.subprocess, "run", return_value=SimpleNamespace(returncode=7)) as execute:
            self.assertEqual(7, runner.verify({}, "test_approval_migrations.py"))
        args = execute.call_args.args[0]
        self.assertEqual("test_approval_migrations.py", args[args.index("-p") + 1])
        self.assertEqual("isolated-test-url", execute.call_args.kwargs["env"]["PORTAL_TEST_DATABASE_URL"])
        created = re.search(r'portal_verify_[a-f0-9]+', queries[0]).group()
        self.assertIn(created, queries[1])
        self.assertEqual(2, len(queries))
        self.assertIn("CREATE DATABASE", queries[0])
        self.assertIn("DROP DATABASE", queries[1])
        self.assertNotIn('"caspian_portal"', queries[1])

    def test_cleanup_also_runs_when_process_cannot_start(self):
        spec = importlib.util.spec_from_file_location("approval_runner_failure_test", ROOT / "scripts/local_portal.py")
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        from contextlib import contextmanager
        queries = []
        @contextmanager
        def connection(*args, **kwargs):
            yield SimpleNamespace(execute=lambda query: queries.append(query.as_string()))
        with patch.object(runner, "start_database"), patch.object(runner, "connection", connection), \
             patch.object(runner, "database_url", return_value="isolated"), \
             patch.object(runner.subprocess, "run", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                runner.verify({})
        self.assertEqual(2, len(queries))
        self.assertIn("DROP DATABASE", queries[1])


if __name__ == "__main__":
    unittest.main()
