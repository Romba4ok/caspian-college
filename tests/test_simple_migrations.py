"""Revision 08: empty-only real upgrades and database-enforced task invariants."""
import os
import re
from pathlib import Path
import sys
import unittest
from datetime import datetime, timezone
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.exc import DataError, IntegrityError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.extensions import db
from app.models import Task, TASK_STATUSES
from support.migration_fixtures import MigrationDatabase

HEAD = "20261028_09"
WORKING = ("tasks", "task_comments", "task_events", "task_attachments",
           "task_notifications", "task_translations")


class SimpleMigrationCases(MigrationDatabase):
    def revision(self):
        with db.engine.connect() as conn:
            return conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()

    def task_values(self):
        users = self.table("users")
        departments = self.rows("departments")
        with db.engine.begin() as conn:
            worker = conn.execute(sa.select(users.c.id).where(users.c.username == "simple-worker")).scalar()
            if worker is None:
                worker = conn.execute(users.insert().values(username="simple-worker", full_name="Synthetic",
                    password_hash="preserved-synthetic-hash", role="employee",
                    department_id=departments[0]["id"])).inserted_primary_key[0]
        return dict(title="Synthetic task", description="Required description", author_id=worker,
            sender_department_id=departments[0]["id"], recipient_department_id=departments[1]["id"],
            due_at=datetime(2030, 1, 10, tzinfo=timezone.utc),
            sent_at=datetime(2030, 1, 1, tzinfo=timezone.utc), creation_key=uuid4().hex)

    def test_fresh_install_and_repeat_do_not_change_initial_accounts(self):
        self.upgrade("20261024_07")
        names = ("users", "departments", "system_settings", "task_categories", "admin_audit_events")
        before = {n: self.rows(n) for n in names}
        self.upgrade()
        self.assertEqual(HEAD, self.revision())
        self.assertEqual(before, {n: self.rows(n) for n in names})
        self.upgrade()
        self.assertEqual(before, {n: self.rows(n) for n in names})
        self.assertTrue(all(not self.rows(n) for n in WORKING))

    def test_full_chain_and_orm_match_required_schema(self):
        self.upgrade()
        self.assertEqual(HEAD, self.revision())
        columns = {c["name"]: c for c in sa.inspect(db.engine).get_columns("tasks")}
        for name in ("status", "due_at", "sent_at", "creation_key", "is_rework", "rework_cycle"):
            self.assertFalse(columns[name]["nullable"], name)
        self.assertIn("new", columns["status"]["default"])
        data = self.task_values()
        # Model create_all is not used here: this writes the migrated schema.
        task = Task(**{k: v for k, v in data.items() if k != "creation_key"})
        db.session.add(task)
        db.session.commit()
        self.assertEqual("new", task.status)
        self.assertEqual(0, task.rework_cycle)
        self.assertFalse(task.is_rework)
        self.assertEqual(32, len(task.creation_key))
        self.assertEqual(0, task.revision)
        self.assertEqual(0, len(task.events))
        self.assertIsNone(task.completed_at)

    def test_bootstrap_only_once_with_no_new_secret_after_installation(self):
        from werkzeug.security import check_password_hash
        self.upgrade()
        before=self.rows("users");self.assertEqual(1,len(before));self.assertEqual("admin",before[0]["role"])
        self.assertEqual("migration-admin",before[0]["username"])
        self.assertTrue(check_password_hash(before[0]["password_hash"],"synthetic-test-password"))
        self.app.config.update(ADMIN_PASSWORD=None,ADMIN_USERNAME="must-not-replace-admin")
        self.upgrade();self.assertEqual(before,self.rows("users"))

    def test_empty_07_preserves_custom_reference_data_passwords_and_upload_bytes(self):
        from werkzeug.security import generate_password_hash,check_password_hash
        self.upgrade("20261024_07")
        data=self.task_values();uid=data["author_id"];users=self.table("users");depts=self.table("departments")
        with db.engine.begin() as conn:
            conn.execute(users.update().where(users.c.role=="admin").values(password_hash=generate_password_hash("existing-private-password"),full_name="Existing administrator",language="kk"))
            conn.execute(users.update().where(users.c.id==uid).values(role="head",language="en"))
            conn.execute(depts.update().where(depts.c.id==data["sender_department_id"]).values(name="Свой отдел",normalized_name="свой отдел",is_archived=True))
            cid=conn.execute(self.table("task_categories").insert().values(name="Сохранить справочник",normalized_name="сохранить справочник",is_archived=True)).inserted_primary_key[0]
            settings=self.table("system_settings");conn.execute(settings.update().values(max_upload_mb=3,allowed_extensions=["pdf"]))
            conn.execute(self.table("admin_audit_events").insert().values(actor_id=uid,actor_snapshot={"role":"head","name":"Historical"},action="synthetic_history",object_type="category",object_id=cid,details={"preserve":True}))
        names=("users","departments","task_categories","system_settings","admin_audit_events")
        before={name:self.rows(name) for name in names}
        upload=Path(self.app.config["TASK_UPLOAD_DIR"])/"existing-private-material.bin";upload.parent.mkdir();upload.write_bytes(b"preserve synthetic upload bytes")
        self.app.config.update(ADMIN_USERNAME="must-not-replace",ADMIN_PASSWORD=None)
        self.upgrade();self.upgrade()
        self.assertEqual(before,{name:self.rows(name) for name in names})
        self.assertEqual(b"preserve synthetic upload bytes",upload.read_bytes())
        admin=next(u for u in self.rows("users") if u["role"]=="admin")
        self.assertTrue(check_password_hash(admin["password_hash"],"existing-private-password"))

    def test_database_defaults_and_all_five_statuses(self):
        self.upgrade()
        table = self.table("tasks")
        data = self.task_values()
        with db.engine.begin() as conn:
            conn.execute(table.insert().values(**data))
            row = conn.execute(sa.select(table)).mappings().one()
            self.assertEqual("new", row["status"])
            self.assertFalse(row["is_rework"])
            self.assertEqual(0, row["rework_cycle"])
            for status in TASK_STATUSES:
                conn.execute(table.insert().values(**(data | {"status": status, "creation_key": uuid4().hex})))
        self.assertEqual(6, len(self.rows("tasks")))

    def test_database_rejects_old_statuses_nulls_and_legacy_deadline_bypass(self):
        self.upgrade()
        table = self.table("tasks")
        data = self.task_values()
        changes = [{"status": s} for s in ("draft", "pending_approval", "awaiting_assignment", "assigned",
                   "awaiting_clarification", "recipient_review", "sender_acceptance", "unknown", None)]
        changes += [{"due_at": None}, {"sent_at": None}, {"creation_key": None}, {"is_rework": None},
                    {"rework_cycle": None}, {"rework_cycle": -1}, {"revision": -1}, {"priority": "critical"},
                    {"due_at": None, "legacy_missing_due": True, "is_legacy_dispatched": True},
                    {"creation_key": " " * 32}, {"creation_key": "x" * 65}, {"creation_key": "short"},
                    {"status": "new", "assignee_id": data["author_id"]}]
        with db.engine.begin() as conn:
            for change in changes:
                with self.subTest(change=change), self.assertRaises((IntegrityError, DataError)):
                    with conn.begin_nested():
                        conn.execute(table.insert().values(**(data | change)))
        self.assertEqual([], self.rows("tasks"))

    def test_creation_key_unique_and_existing_relational_constraints_survive(self):
        self.upgrade()
        table = self.table("tasks")
        data = self.task_values()
        with db.engine.begin() as conn:
            conn.execute(table.insert().values(**data))
            for change in ({}, {"creation_key": uuid4().hex, "recipient_department_id": data["sender_department_id"]}):
                with self.assertRaises(IntegrityError):
                    with conn.begin_nested():
                        conn.execute(table.insert().values(**(data | change)))
        constraints = sa.inspect(db.engine).get_foreign_keys("task_attachments")
        self.assertTrue(any(c["constrained_columns"] == ["comment_id", "task_id"] for c in constraints))
        self.assertEqual(1, len(self.rows("tasks")))
        if not self.postgres:
            with db.engine.connect() as conn:
                self.assertEqual([], conn.exec_driver_sql("PRAGMA foreign_key_check").all())

    def assert_nonempty_guard(self, name):
        self.upgrade("20261024_07")
        data = self.task_values()
        worker = data["author_id"]
        if name == "tasks" or self.postgres and name in ("task_comments", "task_events", "task_attachments"):
            with db.engine.begin() as conn:
                conn.execute(self.table("tasks").insert().values(**{
                    k: v for k, v in (data | {"id": 777, "status": "draft"}).items() if k != "creation_key"}))
        values = {
            "task_comments": dict(task_id=777, author_id=worker, body="Unsent synthetic comment"),
            "task_events": dict(task_id=777, actor_id=worker, event_type="synthetic", details={}),
            "task_attachments": dict(task_id=777, uploaded_by_id=worker, original_filename="synthetic.pdf",
                storage_key="synthetic-private-key", content_type="application/pdf", size_bytes=1),
            "task_notifications": dict(task_id=None, user_id=worker, event_type="synthetic", message="Synthetic"),
            "task_translations": dict(source_type="task", source_id=777, source_hash="synthetic",
                target_language="en", translated_text="Synthetic"),
        }
        if name != "tasks":
            with db.engine.begin() as conn:
                conn.execute(self.table(name).insert().values(**values[name]))
        names = WORKING + ("users", "departments", "system_settings", "task_categories", "admin_audit_events")
        before = {n: self.rows(n) for n in names}
        columns = [c["name"] for c in sa.inspect(db.engine).get_columns("tasks")]
        statements=[]
        def capture(*args):statements.append(args[2])
        sa.event.listen(db.engine,"before_cursor_execute",capture)
        try:result = self.app.test_cli_runner().invoke(args=["db", "upgrade"], catch_exceptions=False)
        finally:sa.event.remove(db.engine,"before_cursor_execute",capture)
        for sql in statements:
            self.assertIsNone(re.match(r"\s*(INSERT|DELETE|TRUNCATE|DROP|ALTER|CREATE)\b",sql,re.I),sql)
            if re.match(r"\s*UPDATE\b",sql,re.I):self.assertIn("WHERE 0=1",sql.upper())
        self.assertNotEqual(0, result.exit_code)
        self.assertIn("пустая рабочая БД", result.output)
        self.assertIn(name, result.output)
        self.assertEqual("20261024_07", self.revision())
        self.assertEqual(before, {n: self.rows(n) for n in names})
        self.assertEqual(columns, [c["name"] for c in sa.inspect(db.engine).get_columns("tasks")])

    def test_schema_failure_rolls_back_revision_and_ddl(self):
        self.upgrade("20261024_07")
        before = [c["name"] for c in sa.inspect(db.engine).get_columns("tasks")]
        def fail_schema(conn, cursor, statement, parameters, context, many):
            sql = statement.lower()
            if ("alter table _alembic_tmp_tasks rename to tasks" in sql or "add column rework_cycle" in sql):
                raise RuntimeError("synthetic schema failure")
        sa.event.listen(db.engine, "before_cursor_execute", fail_schema)
        try:
            result = self.app.test_cli_runner().invoke(args=["db", "upgrade"], catch_exceptions=False)
            self.assertNotEqual(0, result.exit_code)
            self.assertIn("synthetic schema failure", result.output)
        finally:
            sa.event.remove(db.engine, "before_cursor_execute", fail_schema)
        self.assertEqual("20261024_07", self.revision())
        self.assertEqual(before, [c["name"] for c in sa.inspect(db.engine).get_columns("tasks")])
        self.assertNotIn("_alembic_tmp_tasks", sa.inspect(db.engine).get_table_names())

    def test_downgrade_refuses_without_destroying_new_data(self):
        self.upgrade()
        data = self.task_values()
        with db.engine.begin() as conn:
            conn.execute(self.table("tasks").insert().values(**data))
        before = self.rows("tasks")
        result = self.app.test_cli_runner().invoke(args=["db", "downgrade", "20261024_07"], catch_exceptions=False)
        self.assertNotEqual(0, result.exit_code)
        self.assertEqual(HEAD, self.revision())
        self.assertEqual(before, self.rows("tasks"))


for table_name in WORKING:
    def check(self, name=table_name):
        self.assert_nonempty_guard(name)
    setattr(SimpleMigrationCases, "test_refuses_nonempty_" + table_name, check)


class SQLiteSimpleMigrations(SimpleMigrationCases, unittest.TestCase):
    def test_enabled_foreign_keys_guard_preserves_schema(self):
        self.upgrade("20261024_07")
        def enable(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")
        sa.event.listen(db.engine, "connect", enable)
        db.engine.dispose()
        result = self.app.test_cli_runner().invoke(args=["db", "upgrade"], catch_exceptions=False)
        self.assertNotEqual(0, result.exit_code)
        self.assertIn("foreign_keys=ON", result.output)
        self.assertEqual("20261024_07", self.revision())
        self.assertNotIn("creation_key", [c["name"] for c in sa.inspect(db.engine).get_columns("tasks")])


@unittest.skipUnless(os.environ.get("PORTAL_TEST_DATABASE_URL"), "Use isolated local_portal.py verify")
class PostgreSQLSimpleMigrations(SimpleMigrationCases, unittest.TestCase):
    postgres = True
