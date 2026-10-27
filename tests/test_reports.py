"""Known arithmetic, direction privacy and native PostgreSQL report aggregates."""
import os
from datetime import datetime, timedelta, timezone
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, event, select, text, update
from app import create_app
from app.extensions import db
from app.models import Department, Task, TaskComment, TaskEvent, TaskNotification, User, AdminAuditEvent, TASK_STATUSES
from app.reporting import global_statistics, department_report, report_hours
from support.portal_fixtures import PortalCase

UTC=timezone.utc
NOW=datetime(2030,1,10,10,tzinfo=UTC)


class ReportTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.add_member("other-head",role="head",department_id=self.other_department_id)
        self.now=NOW
        self.clock=patch("app.reporting.utc_now",return_value=self.now);self.clock.start();self.addCleanup(self.clock.stop)

    def add_task(self, status="in_progress", **changes):
        values=dict(status=status, due_at=self.now-timedelta(hours=1),sent_at=self.now-timedelta(days=3),
            created_at=self.now-timedelta(days=4),revision=1,
            assignee_id=self.ids["other-head"],closed_at=self.now if status in ("completed","cancelled","rejected") else None)
        if status in ("draft","pending_approval"):values.update(sent_at=None,assignee_id=None)
        return self.task(**(values|changes))

    def sample(self):
        # Outgoing: 10 non-completed states + 6 completions = 16.
        for status in TASK_STATUSES:
            if status!="completed":self.add_task(status)
        for hours in (2,6,0):self.add_task("completed",sent_at=self.now-timedelta(hours=hours),completed_at=self.now)
        self.add_task("completed",sent_at=None,is_legacy_dispatched=True,completed_at=self.now)
        self.add_task("completed",completed_at=None)
        self.add_task("completed",sent_at=self.now,completed_at=self.now-timedelta(hours=1))
        incoming=dict(author_id=self.ids["other-head"],sender_department_id=self.other_department_id,
            recipient_department_id=self.department_id,assignee_id=self.ids["employee"])
        self.add_task("completed",**incoming,sent_at=self.now-timedelta(hours=10),completed_at=self.now)
        self.add_task(**incoming)
        self.add_task("cancelled",**incoming,sent_at=None)
        self.add_task("draft",**incoming)
        self.add_task(author_id=self.ids["outsider"],sender_department_id=self.third_department_id)
        with self.app.app_context():db.session.get(Department,self.other_department_id).is_archived=True;db.session.commit()

    def global_report(self, **kwargs):
        with self.app.app_context():return global_statistics(db.session.get(User,self.ids["root"]),**kwargs)

    def own_report(self,who="head",**kwargs):
        with self.app.app_context():return department_report(db.session.get(User,self.ids[who]),**kwargs)

    def as_user(self,who):
        self.client=self.app.test_client();self.assertEqual(200,self.login(who).status_code)

    def test_global_total_is_21_not_two_departments_per_task(self):
        self.sample();report=self.global_report()
        self.assertEqual(21,report["overall"]["total"])
        self.assertEqual(21,sum(report["overall"]["statuses"].values()))
        self.assertEqual(21,sum(d["outgoing"]["total"] for d in report["departments"]))
        self.assertEqual(17,sum(d["incoming"]["total"] for d in report["departments"]))
        self.assertEqual(8,report["overall"]["overdue"])
        self.assertEqual(7,report["overall"]["statuses"]["completed"])
        self.assertEqual(2,report["overall"]["statuses"]["draft"])
        self.assertEqual(2,report["overall"]["statuses"]["cancelled"])

    def test_own_department_directions_exclude_private_incoming_and_unrelated(self):
        self.sample();report=self.own_report()
        self.assertEqual(18,report["overall"]["total"])
        self.assertEqual(16,report["outgoing"]["total"]);self.assertEqual(2,report["incoming"]["total"])
        self.assertEqual(7,report["overall"]["overdue"])
        self.assertEqual(6,report["outgoing"]["overdue"]);self.assertEqual(1,report["incoming"]["overdue"])
        self.assertEqual(0,report["incoming"]["statuses"]["draft"])
        self.assertEqual(0,report["incoming"]["statuses"]["cancelled"])

    def test_average_is_calendar_hours_and_bad_dates_are_explicitly_excluded(self):
        self.sample();report=self.own_report()
        outgoing=report["outgoing"];incoming=report["incoming"]
        self.assertEqual(3,outgoing["included"]);self.assertEqual(3,outgoing["excluded"])
        self.assertAlmostEqual(8/3,outgoing["average_hours"],places=5)
        self.assertEqual(1,incoming["included"]);self.assertEqual(0,incoming["excluded"])
        self.assertAlmostEqual(10,incoming["average_hours"],places=5)

    def test_archive_and_empty_departments_stay_in_global_statistics(self):
        self.sample();report=self.global_report()
        archive=next(d for d in report["departments"] if d["id"]==self.other_department_id)
        self.assertTrue(archive["archived"]);self.assertEqual(15,archive["incoming"]["total"])
        self.assertEqual(4,archive["outgoing"]["total"])
        self.assertEqual(6,len(report["departments"]))
        self.assertTrue(any(d["incoming"]["total"]==d["outgoing"]["total"]==0 for d in report["departments"]))
        own=self.own_report("other-head");self.assertTrue(own["department"]["archived"])
        self.assertEqual(19,own["overall"]["total"])

    def test_no_data_is_none_and_not_a_false_zero(self):
        report=self.own_report()
        self.assertIsNone(report["incoming"]["average_hours"]);self.assertIsNone(report["outgoing"]["average_hours"])
        self.assertEqual(0,report["overall"]["total"]);self.assertEqual("Нет данных",report_hours(None))
        self.as_user("head");page=self.client.get("/reports/department").get_data(as_text=True)
        self.assertIn("Нет данных",page);self.assertNotIn("0,00 ч",page)

    def test_real_zero_duration_is_included_and_distinguished_from_no_data(self):
        self.add_task("completed",sent_at=self.now,completed_at=self.now)
        data=self.own_report()["outgoing"]
        self.assertEqual(1,data["included"]);self.assertEqual(0,data["excluded"])
        self.assertEqual(0,data["average_hours"]);self.assertEqual("0,00 ч",report_hours(data["average_hours"]))

    def test_only_sender_confirmed_completion_contributes_to_mean(self):
        for status in ("draft","pending_approval","assigned","in_progress","recipient_review","sender_acceptance","cancelled","rejected"):
            self.add_task(status,completed_at=self.now)
        data=self.own_report()["outgoing"]
        self.assertIsNone(data["average_hours"]);self.assertEqual(0,data["included"]);self.assertEqual(0,data["excluded"])

    def test_unknown_send_completion_and_unproven_dispatch_are_counted_as_exclusions(self):
        self.add_task("completed",sent_at=None,completed_at=None,is_legacy_dispatched=True)
        self.add_task("completed",sent_at=None,completed_at=self.now)
        data=self.own_report()["outgoing"]
        self.assertEqual(2,data["excluded"]);self.assertIsNone(data["average_hours"])

    def test_missing_due_does_not_invent_overdue_or_prevent_valid_elapsed_time(self):
        self.add_task("completed",due_at=None,legacy_missing_due=True,is_legacy_dispatched=True,
            sent_at=self.now-timedelta(hours=4),completed_at=self.now)
        data=self.own_report()["outgoing"]
        self.assertEqual(0,data["overdue"]);self.assertAlmostEqual(4,data["average_hours"],places=5)

    def test_overdue_requires_actual_send_nonterminal_and_strictly_elapsed_deadline(self):
        self.add_task(due_at=self.now)
        self.add_task(due_at=None,legacy_missing_due=True,is_legacy_dispatched=True)
        self.add_task(sent_at=None)
        for status in ("draft","pending_approval","completed","cancelled","rejected"):
            self.add_task(status,is_legacy_dispatched=True)
        self.assertEqual(0,self.global_report()["overall"]["overdue"])
        self.add_task(due_at=self.now-timedelta(seconds=1))
        self.assertEqual(1,self.global_report()["overall"]["overdue"])

    def test_utc_naive_and_local_aware_now_give_same_counts(self):
        self.add_task(due_at=self.now-timedelta(minutes=1))
        for now in (self.now,self.now.replace(tzinfo=None),self.now.astimezone(timezone(timedelta(hours=5)))):
            self.assertEqual(1,self.global_report(now=now)["overall"]["overdue"])

    def test_service_denies_wrong_roles_and_foreign_department_before_aggregates(self):
        for who in ("employee","outsider","root"):
            with self.assertRaises(PermissionError):self.own_report(who)
        for ident in (self.other_department_id,True,"1"):
            with self.assertRaises(PermissionError):self.own_report(department_id=ident)
        with self.app.app_context():
            for who in ("employee","head","outsider"):
                with self.assertRaises(PermissionError):global_statistics(db.session.get(User,self.ids[who]))

    def test_head_query_cannot_select_another_department_or_ambiguous_id(self):
        self.as_user("head")
        self.assertEqual(200,self.client.get(f"/reports/department?department_id={self.department_id}").status_code)
        self.assertEqual(403,self.client.get(f"/reports/department?department_id={self.other_department_id}").status_code)
        for query in ("department_id=bad","department_id=1&department_id=2","department_id=-1"):
            self.assertEqual(400,self.client.get("/reports/department?"+query).status_code)

    def test_direct_routes_require_correct_role_and_anonymous_login(self):
        for path in ("/reports/department","/admin/statistics"):
            self.assertEqual(302,self.client.get(path).status_code)
        for who in ("employee","outsider","head"):
            self.as_user(who);self.assertEqual(403,self.client.get("/admin/statistics").status_code)
        for who in ("employee","outsider","root"):
            self.as_user(who);self.assertEqual(403,self.client.get("/reports/department").status_code)
        self.as_user("root");self.assertEqual(200,self.client.get("/admin/statistics").status_code)

    def test_old_session_role_department_and_blocking_are_rechecked(self):
        self.sample();self.as_user("head")
        with self.app.app_context():
            user=db.session.get(User,self.ids["head"]);user.department_id=self.third_department_id;db.session.commit()
        with self.app.app_context():
            report=department_report(db.session.get(User,self.ids["head"]));self.assertEqual(1,report["overall"]["total"])
        self.assertEqual(403,self.client.get(f"/reports/department?department_id={self.department_id}").status_code)
        with self.app.app_context():
            user=db.session.get(User,self.ids["head"]);user.role="employee";db.session.commit()
        self.assertEqual(403,self.client.get("/reports/department").status_code)
        with self.app.app_context():
            user=db.session.get(User,self.ids["head"]);user.role="head";user.is_blocked=True;db.session.commit()
        self.assertEqual(302,self.client.get("/reports/department").status_code)

    def test_reports_are_read_only_for_task_dates_history_and_notifications(self):
        self.sample()
        def snapshot():
            with self.app.app_context():
                return (db.session.execute(select(Task.id,Task.status,Task.revision,Task.updated_at,Task.sent_at,Task.completed_at).order_by(Task.id)).all(),
                    [model.query.count() for model in (TaskEvent,TaskComment,TaskNotification,AdminAuditEvent)])
        before=snapshot();self.global_report();self.own_report()
        self.as_user("root");self.client.get("/admin/statistics")
        self.as_user("head");self.client.get("/reports/department")
        self.assertEqual(before,snapshot())

    def test_queries_are_bounded_when_number_of_departments_grows(self):
        with self.app.app_context():
            for i in range(20):db.session.add(Department(name=f"Дополнительный {i}",slug=f"extra-{i}"))
            db.session.commit();root=db.session.get(User,self.ids["root"])
            statements=[]
            def capture(*args):statements.append(args[2])
            event.listen(db.engine,"before_cursor_execute",capture)
            try:
                report=global_statistics(root,now=self.now)
                self.assertEqual(26,len(report["departments"]));self.assertLessEqual(len(statements),8)
            finally:event.remove(db.engine,"before_cursor_execute",capture)

    def test_russian_templates_escape_names_and_show_distinct_direction_labels(self):
        self.sample()
        with self.app.app_context():db.session.get(Department,self.other_department_id).name='<script>alert(1)</script>';db.session.commit()
        self.as_user("root");source=self.client.get("/admin/statistics").get_data(as_text=True)
        self.assertNotIn('<script>alert(1)</script>',source);self.assertIn('&lt;script&gt;',source)
        for text_value in ("Архивный отдел","Входящие","Исходящие","Активные просроченные"):
            self.assertIn(text_value,source)
        self.as_user("head");source=self.client.get("/reports/department").get_data(as_text=True)
        self.assertIn("2,67 ч",source);self.assertIn("10,00 ч",source)
        self.assertIn("Исключено завершённых",source)
        self.assertNotIn("Проверка доски",source)

    def test_navigation_shows_only_the_role_appropriate_report_link(self):
        for who,admin_link,head_link in (("root",True,False),("head",False,True),("employee",False,False)):
            self.as_user(who);source=self.client.get("/tasks/received").get_data(as_text=True)
            self.assertEqual(admin_link,'href="/admin/statistics"' in source)
            self.assertEqual(head_link,'href="/reports/department"' in source)


@skipUnless(os.environ.get("PORTAL_TEST_DATABASE_URL"),"Требуется безопасный runner PostgreSQL")
class PostgreSQLReportTests(TestCase):
    def setUp(self):
        self.schema="portal_case_"+uuid4().hex;self.control=create_engine(os.environ["PORTAL_TEST_DATABASE_URL"])
        with self.control.begin() as conn:conn.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.app=create_app({"TESTING":True,"DATABASE":os.environ["PORTAL_TEST_DATABASE_URL"],
            "SQLALCHEMY_ENGINE_OPTIONS":{"connect_args":{"options":f"-csearch_path={self.schema}"}},
            "SECRET_KEY":"synthetic-report","ADMIN_PASSWORD":"synthetic-password"})
        self.ctx=self.app.app_context();self.ctx.push()
        self.root=User.query.filter_by(role="admin").one()
        departments=Department.query.order_by(Department.id).all();self.sender,self.receiver=departments[0].id,departments[1].id
        self.head=User(username="head",full_name="Начальник",role="head",department_id=self.sender,password_hash=self.root.password_hash)
        db.session.add(self.head);db.session.commit()

    def tearDown(self):
        db.session.remove();db.engine.dispose();self.ctx.pop()
        with self.control.begin() as conn:conn.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        self.control.dispose()

    def task(self,status="completed",**changes):
        db.session.add(Task(**(dict(title="Синтетическая задача PostgreSQL",description="Агрегаты",author_id=self.head.id,
            sender_department_id=self.sender,recipient_department_id=self.receiver,status=status,
            due_at=NOW-timedelta(hours=1),sent_at=NOW-timedelta(hours=2),completed_at=NOW if status=="completed" else None)|changes)))
        db.session.commit()

    def test_native_interval_aggregation_grouping_and_no_double_count(self):
        self.task();self.task(sent_at=NOW-timedelta(hours=6));self.task(sent_at=None,is_legacy_dispatched=True)
        self.task("in_progress");self.task("pending_approval",sent_at=None)
        global_data=global_statistics(self.root,now=NOW);own=department_report(self.head,now=NOW)
        self.assertEqual(5,global_data["overall"]["total"]);self.assertEqual(1,global_data["overall"]["overdue"])
        self.assertEqual(2,own["outgoing"]["included"]);self.assertEqual(1,own["outgoing"]["excluded"])
        self.assertAlmostEqual(4,own["outgoing"]["average_hours"],places=8)
        receiver=next(d for d in global_data["departments"] if d["id"]==self.receiver)
        self.assertEqual(4,receiver["incoming"]["total"])

    def test_empty_negative_interval_and_zero_are_distinct_on_postgresql(self):
        self.assertIsNone(department_report(self.head,now=NOW)["outgoing"]["average_hours"])
        self.task(sent_at=NOW,completed_at=NOW-timedelta(hours=1))
        data=department_report(self.head,now=NOW)["outgoing"]
        self.assertIsNone(data["average_hours"]);self.assertEqual(1,data["excluded"])
        self.task(sent_at=NOW,completed_at=NOW)
        data=department_report(self.head,now=NOW)["outgoing"]
        self.assertEqual(0,data["average_hours"]);self.assertEqual(1,data["included"])
