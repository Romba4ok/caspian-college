"""Read-only clean-install gate. Requires an explicitly named URL environment variable.

No Flask factory, ORM, dotenv, local credentials, bootstrap or migration is loaded.
The report contains fixed labels, known revisions and aggregate counts only.
"""
import argparse
import ast
import json
import os
from pathlib import Path
import re
import sqlite3

import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[1]
BASE = "20261024_07"
INTERMEDIATE = "20261028_08"
HEAD = "20261028_09"
WORKING = ("tasks", "task_comments", "task_events", "task_attachments",
           "task_notifications", "task_translations", "task_assignees")
REFERENCE = ("users", "departments", "task_categories", "system_settings", "admin_audit_events")
NEW_COLUMNS = {"creation_key", "is_rework", "rework_cycle"}
REQUIRED_CHECKS = {"ck_tasks_status", "ck_tasks_due_required", "ck_tasks_rework_cycle",
    "ck_tasks_creation_key", "ck_tasks_legacy_assignee_unused", "ck_tasks_priority",
    "ck_tasks_revision", "ck_tasks_distinct_departments"}
STATUSES = {"new", "in_progress", "completed", "cancelled", "rejected"}


def report(issue=None):
    return dict(result="blocked", revision="unknown", expected_head=HEAD,
        needs_migration=None, counts={name: None for name in WORKING}, accounts=None,
        issues=[issue] if issue else [])


def valid_migration_graph(directory=None):
    """Inspect literal revision metadata without importing/executing migrations."""
    directory = Path(directory) if directory is not None else ROOT / "migrations" / "versions"
    revisions = {}
    try:
        for path in directory.glob("*.py"):
            values = {}
            for node in ast.parse(path.read_text(encoding="utf-8-sig")).body:
                if isinstance(node, ast.Assign):
                    names = {t.id for t in node.targets if isinstance(t, ast.Name)}
                    for name in names & {"revision", "down_revision", "branch_labels", "depends_on"}:
                        values[name] = ast.literal_eval(node.value)
            if "revision" not in values:
                continue
            revision, parent = values["revision"], values.get("down_revision")
            if not isinstance(revision, str) or revision in revisions or parent is not None and not isinstance(parent, str):
                return False
            if values.get("branch_labels") is not None or values.get("depends_on") is not None:
                return False
            revisions[revision] = parent
        if revisions.get(HEAD) != INTERMEDIATE or revisions.get(INTERMEDIATE) != BASE:
            return False
        parents = {parent for parent in revisions.values() if parent is not None}
        if set(revisions) - parents != {HEAD}:
            return False
        visited, revision = set(), HEAD
        while revision is not None:
            if revision in visited or revision not in revisions:
                return False
            visited.add(revision)
            revision = revisions[revision]
        return visited == set(revisions)
    except (OSError, SyntaxError, ValueError, TypeError):
        return False


def schema_matches(inspector, revision):
    """Structural gate; business constraint semantics are tested by real upgrades."""
    columns = {c["name"]: c for c in inspector.get_columns("tasks")}
    if not {"status", "due_at", "sent_at", "revision"} <= set(columns):
        return False
    if revision != HEAD and "task_assignees" in inspector.get_table_names():
        return False
    if revision == BASE:
        return not NEW_COLUMNS & set(columns)
    if not NEW_COLUMNS <= set(columns):
        return False
    for name in ("status", "due_at", "sent_at", "revision", *NEW_COLUMNS):
        if columns[name]["nullable"]:
            return False
    default = re.sub(r"::.*", "", str(columns["status"]["default"])).strip("()'\" ").lower()
    if default != "new":
        return False
    checks = {c["name"]: c["sqltext"] for c in inspector.get_check_constraints("tasks")}
    required = REQUIRED_CHECKS if revision == HEAD else REQUIRED_CHECKS - {"ck_tasks_legacy_assignee_unused"} | {"ck_tasks_new_unassigned"}
    if not required <= set(checks):
        return False
    # PostgreSQL reflects IN as ANY/ARRAY, with casts; compare its literal set.
    if set(re.findall(r"'([^']*)'", checks["ck_tasks_status"])) != STATUSES:
        return False
    if set(re.findall(r"'([^']*)'", checks["ck_tasks_priority"])) != {"low", "normal", "high", "urgent"}:
        return False
    unique = inspector.get_unique_constraints("tasks") + [i for i in inspector.get_indexes("tasks") if i.get("unique")]
    if not any(c.get("column_names") == ["creation_key"] for c in unique):
        return False
    if revision == HEAD:
        if "task_assignees" not in inspector.get_table_names():
            return False
        members = {c["name"]: c for c in inspector.get_columns("task_assignees")}
        if set(members) != {"task_id", "user_id"} or any(c["nullable"] for c in members.values()):
            return False
        if set(inspector.get_pk_constraint("task_assignees")["constrained_columns"]) != {"task_id", "user_id"}:
            return False
        fks = inspector.get_foreign_keys("task_assignees")
        if not all(any(f["constrained_columns"] == [col] and f["referred_table"] == table and f["referred_columns"] == ["id"]
            for f in fks) for col, table in (("task_id", "tasks"), ("user_id", "users"))):
            return False
        connection = inspector.bind
        if connection.dialect.name == "sqlite":
            triggers = set(connection.execute(sa.text("SELECT name FROM sqlite_master WHERE type='trigger'")).scalars())
            expected = {"task_assignees_not_new_insert", "task_assignees_not_new_update", "tasks_new_without_assignees"}
        else:
            triggers = set(connection.execute(sa.text("SELECT tgname FROM pg_trigger JOIN pg_class ON tgrelid=pg_class.oid WHERE NOT tgisinternal AND pg_table_is_visible(pg_class.oid)")).scalars())
            expected = {"task_assignees_not_new", "tasks_new_without_assignees"}
        if not expected <= triggers:
            return False
    foreign_keys = inspector.get_foreign_keys("task_attachments")
    return any(c["constrained_columns"] == ["comment_id", "task_id"] and
        c["referred_table"] == "task_comments" and c["referred_columns"] == ["id", "task_id"]
        for c in foreign_keys)


def _inspect_connection(connection):
    result = report()
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    for name in WORKING:
        if name in tables:
            result["counts"][name] = connection.execute(sa.select(sa.func.count()).select_from(sa.table(name))).scalar_one()
    versions = []
    if "alembic_version" in tables:
        versions = connection.execute(sa.select(sa.column("version_num")).select_from(sa.table("alembic_version"))).scalars().all()
    if not versions and not tables - {"alembic_version"}:
        result.update(result="ready_install", revision="none", needs_migration=True)
        return result
    if len(versions) != 1 or versions[0] not in (BASE, INTERMEDIATE, HEAD):
        # Do not print arbitrary database strings as purported revision IDs.
        result["revision"] = "unexpected" if versions else "none"
        result["issues"].append("unexpected_revision" if versions else "unversioned_schema")
        return result
    revision = versions[0]
    result.update(revision=revision, needs_migration=revision != HEAD)
    if any(result["counts"].values()):
        result["issues"].append("working_records_present")
    required_tables = set(WORKING + REFERENCE) - ({"task_assignees"} if revision != HEAD else set())
    if not required_tables <= tables:
        result["issues"].append("incomplete_schema")
        return result
    users = sa.table("users", sa.column("role"), sa.column("is_blocked"), sa.column("department_id"))
    user_count, admin_count, active_admin = connection.execute(sa.select(sa.func.count(),
        sa.func.sum(sa.case((users.c.role == "admin", 1), else_=0)),
        sa.func.sum(sa.case((sa.and_(users.c.role == "admin", users.c.is_blocked.is_(False),
                                   users.c.department_id.is_(None)), 1), else_=0))).select_from(users)).one()
    result["accounts"] = dict(users=user_count, administrators=int(admin_count or 0),
        active_administrators=int(active_admin or 0),
        departments=connection.execute(sa.select(sa.func.count()).select_from(sa.table("departments"))).scalar_one())
    if admin_count != 1 or active_admin != 1:
        result["issues"].append("administrator_not_ready")
    if not schema_matches(inspector, revision):
        result["issues"].append("schema_revision_mismatch")
    if not result["issues"]:
        result["result"] = "ready_upgrade" if revision != HEAD else "ready_current"
    return result


def inspect_database(engine, *, migration_directory=None):
    if not valid_migration_graph(migration_directory):
        return report("unexpected_migration_graph")
    if engine.dialect.name not in ("sqlite", "postgresql"):
        return report("unsupported_database")
    try:
        with engine.connect() as connection:
            if engine.dialect.name == "postgresql":
                with connection.begin():
                    connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    connection.exec_driver_sql("SET LOCAL statement_timeout = '5000ms'")
                    connection.exec_driver_sql("SET LOCAL lock_timeout = '2000ms'")
                    return _inspect_connection(connection)
            previous = connection.exec_driver_sql("PRAGMA query_only").scalar_one()
            connection.commit()
            try:
                connection.exec_driver_sql("PRAGMA query_only=ON")
                connection.exec_driver_sql("BEGIN")
                return _inspect_connection(connection)
            finally:
                connection.rollback()
                connection.exec_driver_sql("PRAGMA query_only=" + ("ON" if previous else "OFF"))
                connection.rollback()
    except Exception:
        # Driver errors may contain passwords, URLs, SQL or database values.
        return report("inspection_failed")


def read_only_engine(raw_url):
    url = make_url(raw_url)
    if url.get_backend_name() == "sqlite":
        if url.drivername not in ("sqlite", "sqlite+pysqlite") or url.query or not url.database or url.database == ":memory:":
            raise ValueError("configuration")
        path = Path(url.database)
        if not path.is_absolute() or not path.is_file():
            raise ValueError("configuration")
        uri = path.resolve().as_uri() + "?mode=ro"
        return sa.create_engine("sqlite+pysqlite://", creator=lambda: sqlite3.connect(uri, uri=True), poolclass=NullPool)
    if url.get_backend_name() == "postgresql" and url.drivername in ("postgresql", "postgresql+psycopg"):
        return sa.create_engine(url.set(drivername="postgresql+psycopg"), poolclass=NullPool,
            connect_args={"connect_timeout": 5, "application_name": "portal_workflow_preflight"}, hide_parameters=True)
    raise ValueError("configuration")


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("configuration")


def main(argv=None):
    engine = None
    try:
        parser = SafeParser(description="Read-only clean-install gate; no migration, bootstrap or cleanup.", allow_abbrev=False)
        parser.add_argument("--database-url-env", required=True, metavar="ENV_NAME",
            help="Explicit environment variable containing the target SQLAlchemy URL; its value is never printed.")
        args = parser.parse_args(argv)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.database_url_env):
            raise ValueError("configuration")
        raw_url = os.environ.get(args.database_url_env)
        if not raw_url:
            raise ValueError("configuration")
        engine = read_only_engine(raw_url)
        result = inspect_database(engine)
    except Exception:
        result = report("configuration_or_connection_failed")
    finally:
        if engine is not None:
            engine.dispose()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 2 if result["result"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
