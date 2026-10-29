"""Read-only clean-install gate on real disposable SQLite/PostgreSQL schemas."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
import re
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.exc import OperationalError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.extensions import db
from scripts import workflow_preflight as gate
from support.migration_fixtures import MigrationDatabase


class PreflightCases(MigrationDatabase):
    def check_readonly(self):
        before = self.database_snapshot()
        statements = []
        def capture(*args): statements.append(args[2])
        sa.event.listen(db.engine, "before_cursor_execute", capture)
        try: result = gate.inspect_database(db.engine)
        finally: sa.event.remove(db.engine, "before_cursor_execute", capture)
        self.assertEqual(before, self.database_snapshot())
        for sql in statements:
            self.assertIsNone(re.match(r"\s*(INSERT|UPDATE|DELETE|TRUNCATE|CREATE|DROP|ALTER)\b", sql, re.I), sql)
        return result

    def task_values(self):
        departments = self.rows("departments")
        users = self.table("users")
        with db.engine.begin() as conn:
            uid = conn.execute(users.insert().values(full_name="PRIVATE PERSON MARKER", username="synthetic-worker",
                password_hash="PRIVATE HASH MARKER", role="employee", department_id=departments[0]["id"])).inserted_primary_key[0]
        return dict(title="PRIVATE TITLE MARKER", description="PRIVATE DESCRIPTION MARKER", author_id=uid,
            sender_department_id=departments[0]["id"], recipient_department_id=departments[1]["id"],
            due_at=datetime(2030, 1, 10, tzinfo=timezone.utc), sent_at=datetime(2030, 1, 1, tzinfo=timezone.utc))

    def test_empty_database_is_install_candidate_without_creating_schema_or_admin(self):
        result = self.check_readonly()
        self.assertEqual(("ready_install", "none", True), (result["result"], result["revision"], result["needs_migration"]))
        self.assertIsNone(result["accounts"])
        self.assertEqual([], sa.inspect(db.engine).get_table_names())

    def test_empty_07_and_08_are_distinguished_and_neither_is_changed(self):
        self.upgrade(gate.BASE)
        result = self.check_readonly()
        self.assertEqual(("ready_upgrade", gate.BASE, True), (result["result"], result["revision"], result["needs_migration"]))
        self.assertEqual({**dict.fromkeys(gate.WORKING, 0), "task_assignees":None}, result["counts"])
        self.assertEqual(1, result["accounts"]["active_administrators"])
        self.upgrade()
        result = self.check_readonly()
        self.assertEqual(("ready_current", gate.HEAD, False), (result["result"], result["revision"], result["needs_migration"]))

    def test_actual_command_checks_only_explicit_owned_target_and_reports_aggregates(self):
        self.upgrade(gate.BASE)
        url=db.engine.url
        if self.postgres:url=url.update_query_dict({"options":f"-csearch_path={self.schema}"})
        raw=url.render_as_string(hide_password=False)
        before=self.database_snapshot();out=io.StringIO()
        with patch.dict(os.environ,{"PREFLIGHT_OWNED_TARGET":raw}),redirect_stdout(out):
            code=gate.main(["--database-url-env","PREFLIGHT_OWNED_TARGET"])
        result=json.loads(out.getvalue());self.assertEqual(0,code);self.assertEqual("ready_upgrade",result["result"])
        self.assertFalse(raw in out.getvalue(),"Target connection details leaked")
        self.assertEqual(before,self.database_snapshot())

    def test_stamping_07_as_head_is_not_an_actual_upgrade(self):
        self.upgrade(gate.BASE)
        with db.engine.begin() as conn: conn.execute(sa.text("UPDATE alembic_version SET version_num=:revision"), {"revision": gate.HEAD})
        result = self.check_readonly()
        self.assertEqual("blocked", result["result"])
        self.assertTrue({"schema_revision_mismatch","incomplete_schema"} & set(result["issues"]))

    def test_unknown_revision_is_blocked_without_echoing_its_contents(self):
        self.upgrade(gate.BASE)
        with db.engine.begin() as conn: conn.execute(sa.text("UPDATE alembic_version SET version_num='PRIVATE_REVISION_MARKER'"))
        result = self.check_readonly()
        self.assertEqual("unexpected", result["revision"])
        self.assertIn("unexpected_revision", result["issues"])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_multiple_database_heads_are_blocked(self):
        self.upgrade(gate.BASE)
        with db.engine.begin() as conn: conn.execute(sa.text("INSERT INTO alembic_version(version_num) VALUES (:revision)"), {"revision": gate.HEAD})
        self.assertIn("unexpected_revision", self.check_readonly()["issues"])

    def test_unversioned_existing_tables_are_not_a_fresh_install(self):
        sa.Table("synthetic_unversioned", sa.MetaData(), sa.Column("id", sa.Integer, primary_key=True)).create(db.engine)
        result = self.check_readonly()
        self.assertEqual("blocked", result["result"])
        self.assertIn("unversioned_schema", result["issues"])

    def test_missing_working_table_blocks_even_if_other_counts_are_zero(self):
        self.upgrade(gate.BASE)
        # Deliberately malformed owned fixture, never a real portal table.
        self.table("task_translations").drop(db.engine)
        result = self.check_readonly()
        self.assertIsNone(result["counts"]["task_translations"])
        self.assertIn("incomplete_schema", result["issues"])

    def test_missing_admin_does_not_trigger_bootstrap_or_reset_accounts(self):
        self.upgrade(gate.BASE)
        table = self.table("users")
        with db.engine.begin() as conn:
            conn.execute(table.update().values(role="employee", department_id=self.rows("departments")[0]["id"]))
        result = self.check_readonly()
        self.assertIn("administrator_not_ready", result["issues"])
        self.assertEqual(0, result["accounts"]["administrators"])

    def test_current_08_records_block_clean_profile_without_suggesting_deletion(self):
        self.upgrade()
        with db.engine.begin() as conn:
            conn.execute(self.table("tasks").insert().values(**self.task_values(), status="new", creation_key=uuid4().hex))
        result = self.check_readonly()
        self.assertEqual("blocked", result["result"])
        self.assertEqual(1, result["counts"]["tasks"])
        self.assertIn("working_records_present", result["issues"])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_connection_readonly_guard_rejects_accidental_mutation_and_restores_pool_state(self):
        self.upgrade(gate.BASE)
        before = self.database_snapshot()
        attempts = []
        def forbidden(connection):
            attempts.append(True)
            connection.execute(sa.text("INSERT INTO task_categories(name,normalized_name) VALUES ('PRIVATE ERROR','private error')"))
        with patch.object(gate, "_inspect_connection", side_effect=forbidden): result = gate.inspect_database(db.engine)
        self.assertEqual([True], attempts)
        self.assertEqual(["inspection_failed"], result["issues"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(before, self.database_snapshot())
        # The gate must not leave a pooled connection unusable for its owner.
        with db.engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO task_categories(name,normalized_name) VALUES ('After gate','after gate')"))

    def assert_nonempty(self, name):
        self.upgrade() if name=="task_assignees" else self.upgrade(gate.BASE)
        values = self.task_values()
        if name == "task_assignees":
            with db.engine.begin() as conn: conn.execute(self.table("tasks").insert().values(**values,id=777,status="in_progress",creation_key="t018-read-only-"+"x"*32))
        if name == "tasks" or self.postgres and name in ("task_comments", "task_events", "task_attachments"):
            with db.engine.begin() as conn: conn.execute(self.table("tasks").insert().values(**values, id=777, status="draft"))
        child = {
            "task_assignees": dict(task_id=777,user_id=values["author_id"]),
            "task_comments": dict(task_id=777, author_id=values["author_id"], body="PRIVATE COMMENT MARKER"),
            "task_events": dict(task_id=777, actor_id=values["author_id"], event_type="private", details={"PRIVATE": True}),
            "task_attachments": dict(task_id=777, uploaded_by_id=values["author_id"], original_filename="PRIVATE FILE.pdf", storage_key="PRIVATE STORAGE KEY", content_type="application/pdf", size_bytes=10),
            "task_notifications": dict(task_id=None, user_id=values["author_id"], event_type="private", message="PRIVATE NOTIFICATION"),
            "task_translations": dict(source_type="task", source_id=777, source_hash="PRIVATE HASH", target_language="en", translated_text="PRIVATE TRANSLATION"),
        }
        if name != "tasks":
            with db.engine.begin() as conn: conn.execute(self.table(name).insert().values(**child[name]))
        result = self.check_readonly()
        self.assertEqual("blocked", result["result"])
        self.assertEqual(1, result["counts"][name])
        self.assertIn("working_records_present", result["issues"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("synthetic-worker", json.dumps(result))


for table_name in gate.WORKING:
    def check(self, name=table_name): self.assert_nonempty(name)
    setattr(PreflightCases, "test_blocks_nonempty_" + table_name, check)


class SQLitePreflightTests(PreflightCases, unittest.TestCase): pass


@unittest.skipUnless(os.environ.get("PORTAL_TEST_DATABASE_URL"), "Use isolated local_portal.py verify")
class PostgreSQLPreflightTests(PreflightCases, unittest.TestCase):
    postgres = True


class PreflightCommandTests(unittest.TestCase):
    def invoke(self, arguments, env):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env, clear=True), redirect_stdout(out), redirect_stderr(err):
            code = gate.main(arguments)
        self.assertEqual("", err.getvalue())
        return code, json.loads(out.getvalue()), out.getvalue()

    def test_requires_explicit_environment_and_never_falls_back_or_echoes_secrets(self):
        secret = "postgresql://PRIVATE_USER:PRIVATE_PASSWORD@private.invalid/private_database"
        for args, env in (([], {"DATABASE_URL": secret}), ([secret], {}),
                          (["--database-url-env", "TARGET"], {}), (["--database-url-env", "BAD=NAME"], {"SAFE_NAME": secret})):
            code, result, text = self.invoke(args, env)
            self.assertEqual(2, code)
            self.assertEqual("blocked", result["result"])
            self.assertNotIn("PRIVATE", text)
            self.assertNotIn("private.invalid", text)

    def test_sqlite_cli_reads_existing_file_without_mutation_or_secret_default(self):
        with TemporaryDirectory(prefix="portal_preflight_cli_") as folder:
            path = Path(folder) / "owned.sqlite3"
            sqlite3.connect(path).close()
            before = path.read_bytes()
            code, result, text = self.invoke(["--database-url-env", "TARGET"],
                {"TARGET": "sqlite:///" + path.as_posix(), "DATABASE_URL": "PRIVATE_DEFAULT_MUST_NOT_BE_USED"})
            self.assertEqual((0, "ready_install"), (code, result["result"]))
            self.assertNotIn(str(path), text)
            self.assertEqual(before, path.read_bytes())
            self.assertEqual([path.name], [p.name for p in Path(folder).iterdir()])

    def test_nonexistent_sqlite_is_not_created(self):
        with TemporaryDirectory(prefix="portal_preflight_missing_") as folder:
            path = Path(folder) / "missing.sqlite3"
            code, result, text = self.invoke(["--database-url-env", "TARGET"], {"TARGET": "sqlite:///" + path.as_posix()})
            self.assertEqual(2, code)
            self.assertFalse(path.exists())
            self.assertNotIn(str(path), text)

    def test_sqlite_engine_itself_opens_in_filesystem_readonly_mode(self):
        with TemporaryDirectory(prefix="portal_preflight_readonly_") as folder:
            path = Path(folder) / "owned.sqlite3"
            conn=sqlite3.connect(path)
            try:conn.execute("CREATE TABLE sentinel(id INTEGER)");conn.commit()
            finally:conn.close()
            before = path.read_bytes()
            engine = gate.read_only_engine("sqlite:///" + path.as_posix())
            try:
                with self.assertRaises(OperationalError), engine.begin() as conn: conn.exec_driver_sql("INSERT INTO sentinel VALUES (1)")
            finally: engine.dispose()
            self.assertEqual(before, path.read_bytes())

    def test_bad_driver_and_driver_exception_are_sanitized(self):
        for url in ("sqlite:///:memory:", "sqlite:///relative.sqlite", "mysql://PRIVATE:SECRET@nowhere/db", "not a PRIVATE URL"):
            code, result, text = self.invoke(["--database-url-env", "TARGET"], {"TARGET": url})
            self.assertEqual(2, code)
            self.assertNotIn(url, text)
        with patch.object(gate, "read_only_engine", side_effect=RuntimeError("PRIVATE PASSWORD URL TRACEBACK")):
            code, result, text = self.invoke(["--database-url-env", "TARGET"], {"TARGET": "anything"})
        self.assertEqual(2, code)
        self.assertNotIn("PRIVATE", text)

    def test_graph_is_checked_without_executing_migration_files(self):
        self.assertTrue(gate.valid_migration_graph())
        with TemporaryDirectory(prefix="portal_preflight_graph_") as folder:
            root = Path(folder)
            (root / "base.py").write_text(f"revision={gate.BASE!r}\ndown_revision=None\nraise RuntimeError('DO NOT EXECUTE')", encoding="utf-8")
            head = root / "head.py"
            (root / "middle.py").write_text(f"revision={gate.INTERMEDIATE!r}\ndown_revision={gate.BASE!r}\n", encoding="utf-8")
            head.write_text(f"revision={gate.HEAD!r}\ndown_revision={gate.INTERMEDIATE!r}\n", encoding="utf-8")
            self.assertTrue(gate.valid_migration_graph(root))
            (root / "fork.py").write_text(f"revision='unexpected'\ndown_revision={gate.BASE!r}\n", encoding="utf-8")
            self.assertFalse(gate.valid_migration_graph(root))
            # The database is not even opened if code has multiple heads.
            engine = Mock()
            self.assertEqual(["unexpected_migration_graph"], gate.inspect_database(engine, migration_directory=root)["issues"])
            engine.connect.assert_not_called()

    def test_missing_parent_duplicate_or_cyclic_graph_is_blocked(self):
        cases = ({gate.HEAD: "missing"}, {gate.BASE: gate.HEAD, gate.HEAD: gate.BASE},
                 {gate.BASE: None, gate.HEAD: gate.BASE, "disconnected": "disconnected"})
        for revisions in cases:
            with TemporaryDirectory(prefix="portal_preflight_graph_") as folder:
                root = Path(folder)
                for index, (revision, parent) in enumerate(revisions.items()):
                    (root / f"v{index}.py").write_text(f"revision={revision!r}\ndown_revision={parent!r}\n", encoding="utf-8")
                self.assertFalse(gate.valid_migration_graph(root))
        with TemporaryDirectory(prefix="portal_preflight_graph_") as folder:
            root = Path(folder)
            for name in ("a", "b"): (root / f"{name}.py").write_text(f"revision={gate.BASE!r}\ndown_revision=None", encoding="utf-8")
            self.assertFalse(gate.valid_migration_graph(root))
