import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from unittest.mock import patch

from sqlalchemy import create_engine, text, update
from sqlalchemy.exc import SQLAlchemyError
from uuid import uuid4

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app import create_app
from app.extensions import db
from app.models import Department, Task, TaskComment, TaskEvent, TaskNotification, User
from app.reminders import run_task_reminders
from app.workflow import (WorkflowError, cancel_task, change_task_deadline, confirm_task_result,
    reject_task, reassign_task, forward_task_result, return_task_for_revision)


UTC = timezone.utc


class TaskReminderTests(unittest.TestCase):
    """File-backed synthetic SQLite: frozen calendar and real workflow actions."""
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True, "DATABASE": os.path.join(self.directory.name, "reminders.sqlite3"),
            "SECRET_KEY": "reminders-test", "ADMIN_PASSWORD": "root-password",
        })
        self.context = self.app.app_context()
        self.context.push()
        self.sender_id = Department.query.filter_by(slug="marketing").one().id
        self.receiver_id = Department.query.filter_by(slug="it").one().id
        self.other_id = Department.query.filter_by(slug="practice").one().id
        self.author = self.user("author", self.sender_id)
        self.worker = self.user("worker", self.receiver_id)
        self.replacement = self.user("replacement", self.receiver_id)
        self.sender_head = self.user("sender-head", self.sender_id, role="head")
        self.receiver_head = self.user("receiver-head", self.receiver_id, role="head")
        self.receiver_head_two = self.user("receiver-head-two", self.receiver_id, role="head")
        self.outsider = self.user("outsider", self.other_id, role="head")
        db.session.commit()
        self.due_at = datetime(2026, 11, 10, 9, tzinfo=UTC)
        self.now = datetime(2026, 11, 8, 7, tzinfo=UTC)

    def user(self, username, department_id, *, role="employee"):
        user = User(full_name=username, username=username, role=role, department_id=department_id)
        if hasattr(self, "author"):
            user.password_hash = self.author.password_hash
        else:
            user.set_password("secure-pass")
        db.session.add(user)
        db.session.flush()
        return user

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()
        self.directory.cleanup()

    def task(self, *, due_at="default", status="in_progress", **changes):
        values = dict(title="Задача со сроком", description="Контроль напоминаний",
            author_id=self.author.id, sender_department_id=self.sender_id, recipient_department_id=self.receiver_id,
            assignee_id=self.worker.id, status=status, due_at=self.due_at if due_at == "default" else due_at,
            sent_at=datetime(2026, 11, 1, tzinfo=UTC), created_at=datetime(2026, 11, 1, tzinfo=UTC), revision=1)
        task = Task(**(values | changes))
        db.session.add(task)
        db.session.commit()
        return task

    def reminders(self):
        return TaskNotification.query.filter(TaskNotification.event_type.like("reminder_%")).order_by(TaskNotification.id)

    def owners(self, task=None):
        query = self.reminders()
        if task is not None:
            query = query.filter_by(task_id=task.id)
        return {n.user_id for n in query.all()}

    def run_at(self, moment):
        count = run_task_reminders(now=moment)
        db.session.commit()
        return count

    def work_owners(self):
        return {self.author.id, self.worker.id, self.receiver_head.id, self.receiver_head_two.id}

    def test_two_days_one_day_due_day_and_daily_overdue_are_idempotent(self):
        self.task()
        expected = ["reminder_two_days", "reminder_one_day", "reminder_due_today", "reminder_overdue", "reminder_overdue"]
        for day, kind in enumerate(expected):
            moment = self.now + timedelta(days=day)
            self.assertEqual(4, self.run_at(moment))
            self.assertEqual(0, self.run_at(moment + timedelta(hours=1)))
            latest = self.reminders().all()[-4:]
            self.assertEqual({kind}, {n.event_type for n in latest})
            self.assertEqual(self.work_owners(), {n.user_id for n in latest})
        self.assertEqual(20, self.reminders().count())
        self.assertEqual(20, len({n.deduplication_key for n in self.reminders().all()}))

    def test_exact_recipients_for_every_active_stage(self):
        a = {self.author.id}; r = {self.receiver_head.id, self.receiver_head_two.id}
        for status, expected in (
            ("awaiting_assignment", a | r), ("assigned", a | r | {self.worker.id}),
            ("in_progress", a | r | {self.worker.id}), ("recipient_review", a | r | {self.worker.id}),
            ("awaiting_clarification", a | r | {self.worker.id, self.sender_head.id}),
            ("sender_acceptance", a | {self.sender_head.id}),
        ):
            with self.subTest(status=status):
                task = self.task(status=status)
                self.assertEqual(len(expected), self.run_at(self.now))
                self.assertEqual(expected, self.owners(task))
                self.assertEqual(0, self.run_at(self.now))

    def test_late_run_never_backfills_missed_intervals(self):
        self.task()
        self.assertEqual(4, self.run_at(self.now + timedelta(days=3)))
        self.assertEqual(4, self.run_at(self.now + timedelta(days=5)))
        self.assertEqual({"reminder_overdue"}, {n.event_type for n in self.reminders().all()})
        self.assertEqual({datetime(2026, 11, 11).date(), datetime(2026, 11, 13).date()},
                         {n.created_at.date() for n in self.reminders().all()})

    def test_only_dispatched_active_tasks_with_a_real_deadline_are_selected(self):
        self.task(due_at=None, legacy_missing_due=True, is_legacy_dispatched=True)
        for status in ("draft", "pending_approval", "completed", "cancelled", "rejected"):
            self.task(status=status)
        self.task(sent_at=None, is_legacy_dispatched=False)
        self.task(sent_at=self.now + timedelta(hours=1))
        self.task(created_at=self.now + timedelta(hours=1))
        self.task(due_at=self.due_at + timedelta(days=1))
        self.assertEqual(0, self.run_at(self.now))
        self.assertEqual(0, self.reminders().count())

    def test_presend_legacy_flag_does_not_leak_reminders(self):
        for status in ("draft", "pending_approval"):
            self.task(status=status, is_legacy_dispatched=True)
        self.assertEqual(0, self.run_at(self.now))

    def test_historical_dispatched_task_without_known_send_date_is_included(self):
        self.task(sent_at=None, is_legacy_dispatched=True)
        self.assertEqual(4, self.run_at(self.now))
        self.assertEqual(self.work_owners(), self.owners())

    def test_archived_departments_keep_reminders_for_old_dispatched_tasks(self):
        self.task()
        for ident in (self.sender_id, self.receiver_id):
            db.session.get(Department, ident).is_archived = True
        self.task(status="draft", sent_at=None)
        db.session.commit()
        self.assertEqual(4, self.run_at(self.now))
        self.assertEqual(self.work_owners(), self.owners())

    def test_completion_cancellation_and_rejection_stop_future_reminders(self):
        for action in ("complete", "cancel", "reject"):
            task = self.task(status="sender_acceptance" if action == "complete" else "assigned")
            self.assertEqual(2 if action == "complete" else 4, self.run_at(self.now))
            kwargs = dict(task=task, expected_revision=task.revision, comment="Проверено ответственным")
            if action == "complete":
                confirm_task_result(actor=self.sender_head, **kwargs)
            elif action == "cancel":
                cancel_task(actor=self.sender_head, **kwargs)
            else:
                reject_task(actor=self.receiver_head, **kwargs)
            db.session.commit()
            self.assertEqual(0, self.run_at(self.now + timedelta(days=1)))

    def test_deadline_rescheduling_recalculates_only_current_slot_and_cannot_remove(self):
        task = self.task()
        self.assertEqual(4, self.run_at(self.now))
        original = task.due_at
        change_task_deadline(task=task, actor=self.sender_head, due_at=self.due_at + timedelta(days=2),
            expected_revision=task.revision, comment="Перенести по согласованию")
        db.session.commit()
        self.assertEqual(0, self.run_at(self.now))
        self.assertEqual(4, self.run_at(self.now + timedelta(days=2)))
        with self.assertRaises(WorkflowError):
            change_task_deadline(task=task, actor=self.sender_head, due_at=None,
                expected_revision=task.revision, comment="Проверка запрета")
        db.session.rollback()
        self.assertEqual(0, self.run_at(self.now + timedelta(days=2)))
        change_task_deadline(task=task, actor=self.sender_head, due_at=original,
            expected_revision=task.revision, comment="Вернуть первоначальный срок")
        db.session.commit()
        # Returning to an already delivered deadline/day must not redeliver it.
        self.assertEqual(0, self.run_at(self.now))
        self.assertEqual(8, self.reminders().count())

    def test_fill_historical_missing_deadline_starts_current_slot_only(self):
        task = self.task(due_at=None, sent_at=None, is_legacy_dispatched=True, legacy_missing_due=True)
        self.assertEqual(0, self.run_at(self.now + timedelta(days=3)))
        change_task_deadline(task=task, actor=self.sender_head, due_at=self.due_at,
            expected_revision=task.revision, comment="Восстановлен достоверный срок")
        db.session.commit()
        self.assertEqual(4, self.run_at(self.now + timedelta(days=3)))
        self.assertEqual({"reminder_overdue"}, {n.event_type for n in self.reminders().all()})

    def test_same_day_deadline_change_allows_new_slot_but_not_duplicates(self):
        task = self.task()
        self.assertEqual(4, self.run_at(self.now))
        change_task_deadline(task=task, actor=self.sender_head, due_at=self.due_at + timedelta(hours=1),
            expected_revision=task.revision, comment="Уточнено время")
        db.session.commit()
        self.assertEqual(4, self.run_at(self.now))
        self.assertEqual(0, self.run_at(self.now))
        self.assertEqual(8, self.reminders().count())

    def test_reminders_follow_college_date_at_local_midnight(self):
        self.task(due_at=datetime(2026, 11, 9, 20, tzinfo=UTC))  # 10 November 01:00 local
        self.assertEqual(4, self.run_at(datetime(2026, 11, 9, 18, 59, 59, tzinfo=UTC)))
        self.assertEqual({"reminder_one_day"}, {n.event_type for n in self.reminders().all()})
        self.assertEqual(4, self.run_at(datetime(2026, 11, 9, 19, tzinfo=UTC)))
        self.assertEqual(0, self.run_at(datetime(2026, 11, 10, 18, 59, 59, tzinfo=UTC)))
        self.assertEqual(4, self.run_at(datetime(2026, 11, 10, 19, tzinfo=UTC)))
        self.assertEqual("reminder_overdue", self.reminders().all()[-1].event_type)

    def test_due_day_remains_due_today_even_after_deadline_time(self):
        self.task()
        self.assertEqual(4, self.run_at(self.due_at + timedelta(hours=1)))
        self.assertEqual({"reminder_due_today"}, {n.event_type for n in self.reminders().all()})

    def test_naive_utc_and_aware_utc_use_the_same_daily_key(self):
        self.task(due_at=self.due_at.replace(tzinfo=None))
        self.assertEqual(4, self.run_at(self.now.replace(tzinfo=None)))
        self.assertEqual(0, self.run_at(self.now))

    def test_new_assignee_receives_today_slot_without_duplicating_others(self):
        task = self.task()
        self.assertEqual(4, self.run_at(self.now))
        reassign_task(task=task, actor=self.receiver_head, assignee_id=self.replacement.id,
            expected_revision=task.revision, comment="Замена исполнителя")
        db.session.commit()
        self.assertEqual(1, self.run_at(self.now))
        self.assertEqual(self.replacement.id, self.reminders().all()[-1].user_id)
        self.assertEqual(0, self.run_at(self.now))

    def test_stage_change_only_notifies_new_owners_without_resending_old_ones(self):
        task = self.task(status="recipient_review")
        self.assertEqual(4, self.run_at(self.now))
        forward_task_result(task=task, actor=self.receiver_head,
            expected_revision=task.revision, comment="Проверено, передать отправителю")
        db.session.commit()
        self.assertEqual(1, self.run_at(self.now))
        self.assertEqual(self.sender_head.id, self.reminders().all()[-1].user_id)
        return_task_for_revision(task=task, actor=self.sender_head,
            expected_revision=task.revision, comment="Дополнить результат")
        db.session.commit()
        self.assertEqual(0, self.run_at(self.now))
        self.assertEqual(4, self.run_at(self.now + timedelta(days=1)))
        self.assertEqual(self.work_owners(), {n.user_id for n in self.reminders().all()[-4:]})

    def test_overlapping_author_executor_head_get_one_record_each(self):
        task = self.task(author_id=self.receiver_head.id, assignee_id=self.receiver_head.id)
        self.assertEqual(2, self.run_at(self.now))
        self.assertEqual({self.receiver_head.id, self.receiver_head_two.id}, self.owners(task))

    def test_blocked_moved_and_demoted_owners_are_not_notified(self):
        self.task()
        self.author.department_id = self.other_id
        self.worker.is_blocked = True
        self.receiver_head.role = "employee"
        self.receiver_head_two.department_id = self.other_id
        db.session.commit()
        self.assertEqual(0, self.run_at(self.now))
        self.assertEqual(0, self.reminders().count())

    def test_moved_assignee_still_reading_as_sender_is_not_an_executor(self):
        self.task()
        self.worker.department_id = self.sender_id
        db.session.commit()
        self.assertEqual(3, self.run_at(self.now))
        self.assertNotIn(self.worker.id, self.owners())

    def test_new_current_head_receives_today_slot_but_no_old_calendar_slots(self):
        self.task()
        self.assertEqual(4, self.run_at(self.now))
        new_head = self.user("new-head", self.receiver_id, role="head")
        db.session.commit()
        self.assertEqual(1, self.run_at(self.now))
        self.assertEqual(new_head.id, self.reminders().all()[-1].user_id)
        self.assertEqual(5, self.run_at(self.now + timedelta(days=4)))
        self.assertEqual(1, self.reminders().filter_by(user_id=new_head.id, event_type="reminder_two_days").count())
        self.assertEqual(0, self.reminders().filter_by(user_id=new_head.id, event_type="reminder_one_day").count())

    def test_cli_commits_and_repeat_invocation_is_safe(self):
        self.task()
        with patch("app.reminders.utc_now", return_value=self.now):
            first = self.app.test_cli_runner().invoke(args=["send-task-reminders"], catch_exceptions=False)
            second = self.app.test_cli_runner().invoke(args=["send-task-reminders"], catch_exceptions=False)
        self.assertEqual(0, first.exit_code, first.output)
        self.assertIn("Создано уведомлений: 4", first.output)
        self.assertIn("Создано уведомлений: 0", second.output)
        self.assertEqual(4, self.reminders().count())

    def test_cli_rollback_on_failure_leaves_no_partial_deliveries(self):
        self.task()
        from app import reminders
        original = reminders._queue_reminders
        def failing(*args):
            original(*args)
            raise SQLAlchemyError("synthetic delivery failure")
        with patch("app.reminders.utc_now", return_value=self.now), patch("app.reminders._queue_reminders", side_effect=failing):
            result = self.app.test_cli_runner().invoke(args=["send-task-reminders"])
        self.assertNotEqual(0, result.exit_code)
        self.assertNotIn("Создано уведомлений:", result.output)
        self.assertEqual(0, self.reminders().count())
        self.assertEqual(4, self.run_at(self.now))

    def test_task_changed_after_selection_is_skipped_until_next_run(self):
        task = self.task()
        from app import reminders
        original = reminders._lock_reminder_task
        def changed(candidate):
            db.session.execute(update(Task).where(Task.id == candidate.id).values(
                status="sender_acceptance", revision=candidate.revision + 1).execution_options(synchronize_session=False))
            return original(candidate)
        with patch("app.reminders._lock_reminder_task", side_effect=changed):
            self.assertEqual(0, self.run_at(self.now))
        db.session.expire_all()
        self.assertEqual("sender_acceptance", task.status)
        self.assertEqual(2, self.run_at(self.now))
        self.assertEqual({self.author.id, self.sender_head.id}, self.owners(task))

    def test_reminders_do_not_change_revision_dates_comments_or_history(self):
        task = self.task()
        before = (task.revision, task.updated_at, task.created_at, task.sent_at, task.status, task.due_at)
        self.assertEqual(4, self.run_at(self.now))
        db.session.refresh(task)
        self.assertEqual(before, (task.revision, task.updated_at, task.created_at, task.sent_at, task.status, task.due_at))
        self.assertEqual(0, TaskComment.query.count())
        self.assertEqual(0, TaskEvent.query.count())

    def test_preupgrade_deduplication_key_is_preserved(self):
        from hashlib import sha256
        task = self.task()
        raw = f"{task.id}|{self.author.id}|{self.due_at.isoformat()}|2026-11-08|reminder_two_days"
        db.session.add(TaskNotification(task_id=task.id, user_id=self.author.id, event_type="reminder_two_days",
            message="Сохранённое прежнее напоминание", deduplication_key=sha256(raw.encode()).hexdigest()))
        db.session.commit()
        self.assertEqual(3, self.run_at(self.now))
        self.assertEqual(4, self.reminders().count())

    def test_concurrent_sqlite_jobs_do_not_duplicate_daily_delivery(self):
        self.task()
        self.task(status="sender_acceptance")
        barrier = Barrier(2)
        def run_job(_):
            with self.app.app_context():
                barrier.wait(timeout=10)
                try:
                    count = run_task_reminders(now=self.now)
                    db.session.commit()
                    return count
                finally:
                    db.session.remove()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run_job, range(2)))
        db.session.expire_all()
        self.assertEqual(6, sum(results))
        self.assertEqual(6, self.reminders().count())


@unittest.skipUnless(os.environ.get("PORTAL_TEST_DATABASE_URL"), "Требуется безопасный runner PostgreSQL")
class PostgreSQLReminderTests(unittest.TestCase):
    """Native timezone, locks and rollback on a schema in the disposable runner DB.

    These are sequential integration checks; synchronized workflow races belong
    to T016 and are not claimed by this class.
    """
    def setUp(self):
        self.schema = "portal_case_" + uuid4().hex
        self.control = create_engine(os.environ["PORTAL_TEST_DATABASE_URL"])
        with self.control.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.app = create_app({"TESTING": True, "DATABASE": os.environ["PORTAL_TEST_DATABASE_URL"],
            "SQLALCHEMY_ENGINE_OPTIONS": {"connect_args": {"options": f"-csearch_path={self.schema}"}},
            "SECRET_KEY": "synthetic-reminders", "ADMIN_PASSWORD": "synthetic-password"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        departments = Department.query.order_by(Department.id).all()
        self.sender, self.recipient = departments[0].id, departments[1].id
        self.people = {}
        template = User.query.filter_by(role="admin").one().password_hash
        for name, role, dept in (("author", "employee", self.sender), ("sender-head", "head", self.sender),
            ("worker", "employee", self.recipient), ("receiver-head", "head", self.recipient),
            ("receiver-head-two", "head", self.recipient)):
            user = User(username=name, full_name=name, role=role, department_id=dept, password_hash=template)
            db.session.add(user)
            db.session.flush()
            self.people[name] = user
        db.session.commit()
        self.now = datetime(2030, 1, 9, 19, tzinfo=UTC)  # 10 January 00:00 local

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        with self.control.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        self.control.dispose()

    def task(self, due_at, *, status="in_progress"):
        task = Task(title="Проверка PostgreSQL", description="Синтетическая задача", status=status,
            author_id=self.people["author"].id, assignee_id=self.people["worker"].id,
            sender_department_id=self.sender, recipient_department_id=self.recipient, revision=1,
            due_at=due_at, sent_at=self.now - timedelta(days=1), created_at=self.now - timedelta(days=1))
        db.session.add(task)
        db.session.commit()
        return task

    def test_new_schema_batch_calendar_stage_and_idempotent_delivery(self):
        first = self.task(datetime(2030, 1, 10, 20, tzinfo=UTC), status="recipient_review")  # 11 Jan 01:00
        second = self.task(datetime(2030, 1, 10, 18, 59, tzinfo=UTC))  # 10 Jan 23:59
        self.assertEqual(8, run_task_reminders(now=self.now))
        db.session.commit()
        self.assertEqual(0, run_task_reminders(now=self.now))
        notes = TaskNotification.query.filter_by(task_id=first.id).all()
        self.assertEqual({"reminder_one_day"}, {n.event_type for n in notes})
        self.assertEqual({self.people[n].id for n in ("author", "worker", "receiver-head", "receiver-head-two")},
                         {n.user_id for n in notes})
        self.assertTrue(all("11.01.2030 01:00" in n.message for n in notes))
        self.assertEqual({"reminder_due_today"}, {n.event_type for n in TaskNotification.query.filter_by(task_id=second.id)})
        forward_task_result(task=first, actor=self.people["receiver-head"], expected_revision=first.revision,
            comment="Результат проверен")
        db.session.commit()
        self.assertEqual(1, run_task_reminders(now=self.now))
        db.session.commit()
        self.assertEqual(6, run_task_reminders(now=self.now + timedelta(days=1)))
        db.session.commit()
        self.assertEqual(0, run_task_reminders(now=self.now + timedelta(days=1)))
        new = TaskNotification.query.filter(TaskNotification.created_at == self.now + timedelta(days=1)).all()
        self.assertEqual({self.people["author"].id, self.people["sender-head"].id},
                         {n.user_id for n in new if n.task_id == first.id})
        self.assertEqual({"reminder_due_today"}, {n.event_type for n in new if n.task_id == first.id})
        self.assertEqual({"reminder_overdue"}, {n.event_type for n in new if n.task_id == second.id})

    def test_new_schema_reminders_roll_back_with_caller_transaction(self):
        task = self.task(self.now + timedelta(days=1))
        before = (task.revision, task.updated_at, task.status)
        self.assertEqual(4, run_task_reminders(now=self.now))
        db.session.rollback()
        self.assertEqual(0, TaskNotification.query.count())
        self.assertEqual(before, (task.revision, task.updated_at, task.status))
        self.assertEqual(4, run_task_reminders(now=self.now))
        db.session.commit()
        self.assertEqual(0, run_task_reminders(now=self.now))
        self.assertEqual(0, TaskEvent.query.count())
        self.assertEqual(0, TaskComment.query.count())


class NotificationMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_old_notifications_and_adds_unique_key(self):
        app = create_app({"DATABASE": ":memory:", "ADMIN_PASSWORD": "migration-password", "SECRET_KEY": "migration"})
        runner = app.test_cli_runner()
        self.assertEqual(0, runner.invoke(args=["db", "upgrade", "20261024_02"], catch_exceptions=False).exit_code)
        with app.app_context():
            db.session.execute(text("INSERT INTO task_notifications (user_id, event_type, message) VALUES (1, 'system', 'Existing notification')"))
            db.session.commit()
        self.assertEqual(0, runner.invoke(args=["db", "upgrade"], catch_exceptions=False).exit_code)
        self.assertEqual(0, runner.invoke(args=["db", "upgrade"], catch_exceptions=False).exit_code)
        with app.app_context():
            note = TaskNotification.query.one()
            self.assertEqual("Existing notification", note.message)
            self.assertIsNone(note.deduplication_key)
            db.session.remove()
            db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
