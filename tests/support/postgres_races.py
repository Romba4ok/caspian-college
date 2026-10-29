"""Bounded native transactions, stale initial reads and observable lock contention."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, local
from time import monotonic
import re

from sqlalchemy import event, select, text
from sqlalchemy.orm import undefer

from app.accounts import AccountError
from app.departments import DepartmentError
from app.extensions import db
from app.models import Task, User
from app.workflow import WorkflowError


class NativeRaces:
    def race(self, left, right, *, first=None, synchronize_users=False):
        """first=0/1 holds a successful operation until pg_blocking_pids proves contention.

        No barrier is placed AFTER taking mutually exclusive locks. Both workers
        preload old ORM objects before the start barrier. Expected domain refusals
        roll back; SQL errors/deadlocks/timeouts propagate and fail the test.
        """
        start, users = Barrier(2), Barrier(2)
        holder_ready, release_holder = Event(), Event()
        pids, thread_state, locks = {}, local(), {0:[],1:[]}
        with self.app.app_context():
            db.session.remove()
            engine = db.engine

        def before_lock(conn, cursor, statement, parameters, context, many):
            sql=" ".join(statement.lower().split())
            index=getattr(thread_state,"index",None)
            if index is not None:
                if " for " in sql:
                    for table,level in (("users",1),("departments",2),("tasks",3),("task_events",4)):
                        if f"from {table} " in sql:
                            locks[index].append((table,level));break
                elif re.match(r"update tasks\s",sql):locks[index].append(("tasks",3))
            if synchronize_users and "FROM users" in statement and "FOR NO KEY UPDATE" in statement and not getattr(thread_state, "seen", False):
                thread_state.seen = True
                users.wait(timeout=10)

        def run(index, operation):
            with self.app.app_context():
                try:
                    thread_state.index=index
                    db.session.execute(text("SET LOCAL lock_timeout='8s'"))
                    db.session.execute(text("SET LOCAL statement_timeout='12s'"))
                    pids[index] = db.session.scalar(text("SELECT pg_backend_pid()"))
                    # Keep strong references so services really receive stale identities.
                    old_users = db.session.execute(select(User).order_by(User.id)).scalars().all()
                    old_tasks = db.session.execute(select(Task).options(undefer("*")).order_by(Task.id)).scalars().all()
                    start.wait(timeout=10)
                    if first is not None and index != first:
                        if not holder_ready.wait(10): raise AssertionError("Holder never reached its commit boundary")
                    value = operation()
                    if index == first:
                        holder_ready.set()
                        if not release_holder.wait(10): raise AssertionError("Contender never demonstrated a PostgreSQL lock wait")
                    db.session.commit()
                    return dict(ok=True, pid=pids[index], value=value if type(value) in (int, str, bool) else None)
                except (WorkflowError, AccountError, DepartmentError, PermissionError, LookupError) as error:
                    db.session.rollback()
                    return dict(ok=False, pid=pids.get(index), error=type(error).__name__)
                finally:
                    db.session.remove()

        event.listen(engine, "before_cursor_execute", before_lock)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(run, i, op) for i, op in enumerate((left, right))]
                if first is not None:
                    try:
                        self.assertTrue(holder_ready.wait(10), "First operation did not complete before commit")
                        deadline, waiting = monotonic() + 6, False
                        with self.control.connect() as observer:
                            while monotonic() < deadline:
                                if len(pids) == 2:
                                    blockers = observer.execute(text("SELECT pg_blocking_pids(:pid)"), {"pid": pids[1-first]}).scalar_one()
                                    if pids[first] in blockers:
                                        waiting = True
                                        break
                                if futures[1-first].done(): break
                                Event().wait(0.01)
                        self.assertTrue(waiting, "Two transactions did not actually contend for a native row lock")
                    finally:
                        release_holder.set()
                results = [future.result(timeout=20) for future in futures]
        finally:
            release_holder.set()
            event.remove(engine, "before_cursor_execute", before_lock)
        self.assertEqual(2, len({r["pid"] for r in results}), "Independent PostgreSQL backends required")
        for index,trace in locks.items():
            levels=[level for table,level in trace]
            self.assertEqual(sorted(levels),levels,f"Lock inversion in transaction {index}: {trace}")
            results[index]["locks"]=[table for table,level in trace]
        with self.app.app_context(): db.session.remove()
        return results
