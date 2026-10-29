"""Priority and deadline precede pagination; closing dates use the current cycle."""
import os
from datetime import datetime,timedelta
from unittest import TestCase,skipUnless
from unittest.mock import patch
from uuid import uuid4
from sqlalchemy import select
from app.extensions import db
from app.models import Task
from app.tasks import _task_ordering
from support.task_view_fixtures import ViewCase,NOW
from support.migration_fixtures import MigrationDatabase


class TaskOrderingTests(ViewCase):
    def test_both_active_directions_rank_priority_before_nearest_deadline(self):
        low=self.card(priority="low",due_at=NOW)
        normal=self.card(priority="normal",due_at=NOW+timedelta(days=1))
        high=self.card(priority="high",due_at=NOW+timedelta(days=2))
        urgent_late=self.card(priority="urgent",due_at=NOW+timedelta(days=10))
        urgent_near=self.card(priority="urgent",due_at=NOW+timedelta(days=5))
        expected=[urgent_near,urgent_late,high,normal,low]
        for direction,actor in (("sent","head"),("received","receiver-head")):
            for view in ("board","list"):
                _,ctx=self.render(direction,actor,view=view);self.assertEqual(expected,[t.id for t in ctx["tasks"]])
                if view=="board":self.assertEqual(expected,[t.id for t in ctx["board_groups"][0]["tasks"]])

    def test_sort_applies_before_list_pagination_and_in_each_column(self):
        self.many(26,priority="low",due_at=NOW);urgent=self.card("in_progress",priority="urgent",due_at=NOW+timedelta(days=100))
        for direction,actor in (("sent","head"),("received","receiver-head")):
            _,one=self.render(direction,actor,view="list");_,two=self.render(direction,actor,view="list",page=2)
            self.assertEqual(urgent,one["tasks"][0].id);self.assertNotIn(urgent,[t.id for t in two["tasks"]]);self.assertEqual(27,one["pagination"].total)
            _,board=self.render(direction,actor)
            all_list=one["tasks"]+two["tasks"]
            for group in board["board_groups"]:self.assertEqual([t.id for t in all_list if t.status in group["statuses"]],[t.id for t in group["tasks"]])

    def test_deadline_ties_use_newest_creation_then_highest_id(self):
        old=self.card(created_at=NOW-timedelta(days=10));newer=self.card(created_at=NOW-timedelta(days=1));tie=self.card(created_at=NOW-timedelta(days=1))
        _,ctx=self.render();self.assertEqual([tie,newer,old],[t.id for t in ctx["tasks"]])

    def test_closed_order_uses_completed_at_or_other_closed_at_with_unknown_last(self):
        old=self.card("completed",priority="urgent",completed_at=NOW-timedelta(days=8),closed_at=NOW+timedelta(days=10))
        recent=self.card("completed",priority="low",completed_at=NOW-timedelta(days=1))
        unknown=self.card("completed",completed_at=None,closed_at=NOW+timedelta(days=20))
        cancel=self.card("cancelled",closed_at=NOW);reject=self.card("rejected",closed_at=NOW-timedelta(days=2))
        for view in ("board","list"):
            _,ctx=self.render("completed",view=view,scope="sent");self.assertEqual([cancel,recent,reject,old,unknown],[t.id for t in ctx["tasks"]])
            if view=="board":self.assertEqual([recent,old,unknown],[t.id for t in ctx["board_groups"][0]["tasks"]])

    def test_repeated_completion_sorts_using_latest_cycle(self):
        ident=self.card("in_progress");worker=self.client_for("worker");sender=self.client_for()
        for revision,stamp in ((0,NOW),(2,NOW+timedelta(days=3))):
            with patch("app.workflow.datetime",wraps=datetime) as clock:
                clock.now.return_value=stamp
                self.assertEqual(302,worker.post(f"/tasks/{ident}/complete",data=dict(revision=revision,comment="Результат готов")).status_code)
            if revision==0:
                other=self.card("completed",completed_at=NOW+timedelta(days=1))
                self.assertEqual(302,sender.post(f"/tasks/{ident}/return",data=dict(revision=1,comment="Исправить замечания")).status_code)
                _,ctx=self.render("completed",scope="sent");self.assertEqual([other],[t.id for t in ctx["tasks"]])
        _,ctx=self.render("completed",scope="sent");self.assertEqual([ident,other],[t.id for t in ctx["tasks"]])


class OrderingDatabaseChecks:
    def seed(self):
        users=self.table("users");departments=self.rows("departments")
        with db.engine.begin() as conn:
            uid=conn.execute(users.insert().values(full_name="Синтетический автор",username="ordering-author",password_hash="synthetic",role="employee",department_id=departments[0]["id"])).inserted_primary_key[0]
        return dict(author_id=uid,sender_department_id=departments[0]["id"],recipient_department_id=departments[1]["id"],description="Синтетическая сортировка",created_at=NOW-timedelta(days=10),sent_at=NOW-timedelta(days=9))

    def test_07_historical_nulls_sort_inside_priority_without_loosening_08(self):
        self.upgrade("20261024_07");common=self.seed();table=self.table("tasks")
        with db.engine.begin() as conn:
            ids=[]
            for title,priority,due in (("low","low",NOW),("urgent unknown","urgent",None),("urgent later","urgent",NOW+timedelta(days=3)),("urgent nearer","urgent",NOW+timedelta(days=1))):
                ids.append(conn.execute(table.insert().values(**common,title=title,status="in_progress",priority=priority,due_at=due,legacy_missing_due=due is None,is_legacy_dispatched=True)).inserted_primary_key[0])
            actual=conn.execute(select(Task.id).order_by(*_task_ordering("received"))).scalars().all()
        self.assertEqual([ids[3],ids[2],ids[1],ids[0]],actual)
        self.assertEqual("20261024_07",self.rows("alembic_version")[0]["version_num"])

    def test_08_native_current_priority_and_completion_date(self):
        self.upgrade();common=self.seed();table=self.table("tasks")
        with db.engine.begin() as conn:
            low=conn.execute(table.insert().values(**common,creation_key=uuid4().hex,title="Low",status="new",priority="low",due_at=NOW)).inserted_primary_key[0]
            urgent=conn.execute(table.insert().values(**common,creation_key=uuid4().hex,title="Urgent",status="new",priority="urgent",due_at=NOW+timedelta(days=5))).inserted_primary_key[0]
            old=conn.execute(table.insert().values(**common,creation_key=uuid4().hex,title="Done old",status="completed",due_at=NOW,completed_at=NOW-timedelta(days=1),closed_at=NOW+timedelta(days=5))).inserted_primary_key[0]
            latest=conn.execute(table.insert().values(**common,creation_key=uuid4().hex,title="Done latest",status="completed",due_at=NOW,completed_at=NOW,closed_at=NOW)).inserted_primary_key[0]
            unknown=conn.execute(table.insert().values(**common,creation_key=uuid4().hex,title="Done unknown",status="completed",due_at=NOW,completed_at=None,closed_at=NOW+timedelta(days=9))).inserted_primary_key[0]
            self.assertEqual([urgent,low],conn.execute(select(Task.id).where(Task.status=="new").order_by(*_task_ordering("sent"))).scalars().all())
            self.assertEqual([latest,old,unknown],conn.execute(select(Task.id).where(Task.status=="completed").order_by(*_task_ordering("completed"))).scalars().all())


class SQLiteOrderingDatabaseTests(OrderingDatabaseChecks,MigrationDatabase,TestCase):pass


@skipUnless(os.environ.get("PORTAL_TEST_DATABASE_URL"),"Требуется безопасный runner PostgreSQL")
class PostgreSQLOrderingDatabaseTests(OrderingDatabaseChecks,MigrationDatabase,TestCase):
    postgres=True
