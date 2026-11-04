"""Графики дашборда и отчётов: цифры считаются из реальных полей и не выходят за права роли."""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import true

from app import analytics
from app.extensions import db
from app.models import AdminAuditEvent, Department, TaskAssignee, TaskEvent, User
from app.reporting import department_report
from support.portal_fixtures import PortalCase

NOW = datetime.now(timezone.utc)
# Фиксированный момент для граничных случаев: 15 июня 17:00 по Кызылорде (UTC+5).
FIXED = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


class AnalyticsTests(PortalCase):
    def mine(self, **changes):
        base = dict(sender_department_id=self.other_department_id, recipient_department_id=self.department_id,
            sent_at=NOW - timedelta(days=3), due_at=NOW + timedelta(days=5))
        return self.task(**(base | changes))

    def assign(self, task_id, username):
        with self.app.app_context():
            db.session.add(TaskAssignee(task_id=task_id, user_id=self.ids[username]))
            db.session.commit()

    def delete_task(self, task_id):
        # Удаление задачи — событие-«надгробие», а не стирание строки.
        with self.app.app_context():
            db.session.add(TaskEvent(task_id=task_id, actor_id=self.ids["head"], event_type="deleted", details={}))
            db.session.commit()

    def account_event(self, username, action, deleted):
        with self.app.app_context():
            db.session.add(AdminAuditEvent(actor_id=self.ids["root"], actor_snapshot={}, action=action, object_type="user",
                object_id=self.ids[username], details={"after": {"is_deleted": deleted}}))
            db.session.commit()

    def test_trend_counts_days_and_ignores_older_tasks(self):
        self.mine(sent_at=NOW - timedelta(days=1))
        self.mine(sent_at=NOW - timedelta(days=1))
        self.mine(status="completed", sent_at=NOW - timedelta(days=2), completed_at=NOW - timedelta(hours=1))
        self.mine(sent_at=NOW - timedelta(days=45))
        with self.app.app_context():
            result = analytics.trend(true(), true(), NOW, 30)
        self.assertEqual(30, len(result["days"]))
        self.assertEqual(3, result["sent_total"])
        self.assertEqual(1, result["completed_total"])
        self.assertEqual(2, result["maximum"])

    def test_trend_window_starts_at_local_midnight_and_excludes_future(self):
        # Окно из 30 дней: с 17 мая 00:00 местного = 16 мая 19:00 UTC.
        start = datetime(2026, 5, 16, 19, 0, tzinfo=timezone.utc)
        self.mine(sent_at=start)
        self.mine(sent_at=start - timedelta(seconds=1))
        self.mine(sent_at=FIXED)
        self.mine(sent_at=FIXED + timedelta(seconds=1))
        self.mine(status="completed", sent_at=start, completed_at=FIXED + timedelta(hours=1))
        with self.app.app_context():
            result = analytics.trend(true(), true(), FIXED, 30)
        self.assertEqual((date(2026, 5, 17), date(2026, 6, 15)), (result["days"][0]["day"], result["days"][-1]["day"]))
        self.assertEqual(2, result["days"][0]["sent"])
        self.assertEqual(1, result["days"][-1]["sent"])
        self.assertEqual(3, result["sent_total"])
        self.assertEqual(0, result["completed_total"])

    def test_trend_assigns_tasks_to_local_calendar_day_near_midnight(self):
        now = datetime(2026, 6, 15, 20, 0, tzinfo=timezone.utc)  # 16 июня 01:00 местного
        self.mine(sent_at=datetime(2026, 6, 15, 18, 30, tzinfo=timezone.utc))  # 15 июня 23:30 местного
        self.mine(sent_at=datetime(2026, 6, 15, 19, 30, tzinfo=timezone.utc))  # 16 июня 00:30 местного
        with self.app.app_context():
            result = analytics.trend(true(), true(), now, 7)
        by_day = {d["day"]: d["sent"] for d in result["days"]}
        self.assertEqual((1, 1), (by_day[date(2026, 6, 15)], by_day[date(2026, 6, 16)]))
        self.assertEqual(date(2026, 6, 16), result["days"][-1]["day"])

    def test_trend_completed_series_needs_completed_status(self):
        self.mine(status="cancelled", sent_at=FIXED - timedelta(days=2), completed_at=FIXED - timedelta(days=1))
        self.mine(status="completed", sent_at=FIXED - timedelta(days=2), completed_at=FIXED - timedelta(days=1))
        with self.app.app_context():
            result = analytics.trend(true(), true(), FIXED, 30)
        self.assertEqual(1, result["completed_total"])

    def test_empty_database_gives_zero_series_without_errors(self):
        with self.app.app_context():
            trend = analytics.trend(true(), true(), FIXED, 30)
            self.assertEqual((30, 0, 0, 1), (len(trend["days"]), trend["sent_total"], trend["completed_total"], trend["maximum"]))
            self.assertEqual(dict(low=0, normal=0, high=0, urgent=0), analytics.priorities(true())["counts"])
            punctual = analytics.punctuality(true(), FIXED)
            self.assertEqual((0, 0, 0, None), (punctual["on_time"], punctual["late"], punctual["total"], punctual["percent"]))
            self.assertEqual([], analytics.counterparts(self.department_id)["rows"])
            self.assertEqual([], analytics.department_ranking(FIXED)["rows"])
            self.assertEqual(0, analytics.workload(self.department_id, FIXED)["free"])

    def test_priorities_count_only_active_tasks_and_keep_all_keys(self):
        self.mine(priority="urgent")
        self.mine(priority="urgent", status="in_progress")
        self.mine(priority="low", status="completed", completed_at=NOW)
        with self.app.app_context():
            result = analytics.priorities(true())
        self.assertEqual(dict(urgent=2, high=0, normal=0, low=0), result["counts"])
        self.assertEqual(2, result["total"])

    def test_punctuality_separates_on_time_late_and_empty(self):
        with self.app.app_context():
            self.assertIsNone(analytics.punctuality(true(), NOW)["percent"])
        self.mine(status="completed", due_at=NOW, completed_at=NOW - timedelta(hours=2))
        self.mine(status="completed", due_at=NOW - timedelta(days=2), completed_at=NOW - timedelta(days=1))
        with self.app.app_context():
            result = analytics.punctuality(true(), NOW)
        self.assertEqual((1, 1, 2, 50), (result["on_time"], result["late"], result["total"], result["percent"]))

    def test_punctuality_ignores_future_completion_and_non_completed_status(self):
        self.mine(status="completed", due_at=FIXED, completed_at=FIXED)  # ровно в срок
        self.mine(status="completed", due_at=FIXED - timedelta(days=1), completed_at=FIXED + timedelta(hours=1))
        self.mine(status="cancelled", due_at=FIXED - timedelta(days=1), completed_at=FIXED - timedelta(hours=1))
        with self.app.app_context():
            result = analytics.punctuality(true(), FIXED)
        self.assertEqual((1, 0, 1, 100), (result["on_time"], result["late"], result["total"], result["percent"]))

    def test_workload_lists_active_members_with_their_own_numbers(self):
        self.add_member("blocked-member", blocked=True)
        active_late = self.mine(status="in_progress", due_at=NOW - timedelta(days=1))
        done = self.mine(status="completed", completed_at=NOW)
        self.mine(status="new")
        self.mine(recipient_department_id=self.third_department_id, status="in_progress")
        self.assign(active_late, "employee")
        self.assign(done, "employee")
        with self.app.app_context():
            result = analytics.workload(self.department_id, NOW)
        rows = {r["name"]: r for r in result["rows"]}
        self.assertNotIn("blocked-member", rows)
        self.assertEqual((1, 1, 1), (rows["Сотрудник"]["active"], rows["Сотрудник"]["overdue"], rows["Сотрудник"]["completed"]))
        self.assertEqual((0, 0, 0), (rows["head"]["active"], rows["head"]["overdue"], rows["head"]["completed"]))
        self.assertEqual(1, result["free"])

    def test_workload_hides_blocked_and_deleted_accounts_but_shows_restored(self):
        for name, blocked in (("blocked-member", True), ("removed-member", False), ("returned-member", False)):
            self.add_member(name, blocked=blocked)
        self.account_event("removed-member", "account_deleted", True)
        self.account_event("returned-member", "account_deleted", True)
        self.account_event("returned-member", "account_restored", False)
        with self.app.app_context():
            names = {r["name"] for r in analytics.workload(self.department_id, FIXED)["rows"]}
        self.assertTrue({"Сотрудник", "head", "returned-member"} <= names)
        self.assertFalse({"blocked-member", "removed-member", "outsider"} & names)

    def test_workload_ignores_closed_tasks_and_other_department_assignments(self):
        self.assign(self.mine(status="cancelled"), "employee")
        self.assign(self.mine(recipient_department_id=self.third_department_id, status="in_progress"), "employee")
        with self.app.app_context():
            row = {r["name"]: r for r in analytics.workload(self.department_id, FIXED)["rows"]}["Сотрудник"]
        self.assertEqual((0, 0, 0), (row["active"], row["overdue"], row["completed"]))

    def test_multi_assignee_task_is_seen_by_each_member_but_counted_once(self):
        self.add_member("second")
        task = self.mine(status="in_progress", sent_at=FIXED - timedelta(days=1), due_at=FIXED - timedelta(hours=1))
        self.assign(task, "employee")
        self.assign(task, "second")
        with self.app.app_context():
            rows = {r["name"]: r for r in analytics.workload(self.department_id, FIXED)["rows"]}
            self.assertEqual((1, 1), (rows["Сотрудник"]["active"], rows["Сотрудник"]["overdue"]))
            self.assertEqual((1, 1), (rows["second"]["active"], rows["second"]["overdue"]))
            self.assertEqual(1, analytics.priorities(true())["total"])
            self.assertEqual(1, analytics.trend(true(), true(), FIXED, 30)["sent_total"])
            self.assertEqual(1, analytics.counterparts(self.department_id)["rows"][0]["incoming"])
            self.assertEqual(1, analytics.department_ranking(FIXED)["rows"][0]["active"])
            report = department_report(db.session.get(User, self.ids["head"]), now=FIXED)
            self.assertEqual((1, 1), (report["incoming"]["total"], report["overall"]["total"]))

    def test_deleted_tasks_are_not_counted_anywhere(self):
        kept = self.mine(sent_at=FIXED - timedelta(days=1), priority="high", status="in_progress", due_at=FIXED - timedelta(hours=1))
        gone = self.mine(sent_at=FIXED - timedelta(days=1), priority="high", status="in_progress", due_at=FIXED - timedelta(hours=1))
        gone_done = self.mine(status="completed", sent_at=FIXED - timedelta(days=2), completed_at=FIXED - timedelta(days=1))
        outgoing = dict(sender_department_id=self.department_id, recipient_department_id=self.other_department_id,
            sent_at=FIXED - timedelta(days=1))
        self.task(**outgoing)
        gone_out = self.task(**outgoing)
        self.assign(kept, "employee")
        self.assign(gone, "employee")
        for ident in (gone, gone_done, gone_out):
            self.delete_task(ident)
        with self.app.app_context():
            trend = analytics.trend(true(), true(), FIXED, 30)
            self.assertEqual((2, 0), (trend["sent_total"], trend["completed_total"]))
            self.assertEqual((1, 1), (analytics.priorities(true())["counts"]["high"], analytics.priorities(true())["counts"]["normal"]))
            self.assertEqual(0, analytics.punctuality(true(), FIXED)["total"])
            row = {r["name"]: r for r in analytics.workload(self.department_id, FIXED)["rows"]}["Сотрудник"]
            self.assertEqual((1, 1, 0), (row["active"], row["overdue"], row["completed"]))
            other = analytics.counterparts(self.department_id)["rows"][0]
            self.assertEqual((1, 1), (other["incoming"], other["outgoing"]))
            ranking = {r["id"]: r for r in analytics.department_ranking(FIXED)["rows"]}[self.department_id]
            self.assertEqual((1, 1, 0), (ranking["active"], ranking["overdue"], ranking["completed"]))

    def test_counterparts_split_received_and_sent(self):
        self.mine()
        self.mine()
        self.task(sender_department_id=self.department_id, recipient_department_id=self.other_department_id, sent_at=NOW - timedelta(days=1))
        with self.app.app_context():
            rows = analytics.counterparts(self.department_id)["rows"]
        self.assertEqual(1, len(rows))
        self.assertEqual((2, 1), (rows[0]["incoming"], rows[0]["outgoing"]))

    def test_department_ranking_puts_overdue_first(self):
        self.mine(status="in_progress", due_at=NOW - timedelta(days=1))
        self.mine(recipient_department_id=self.third_department_id)
        self.mine(recipient_department_id=self.third_department_id)
        with self.app.app_context():
            rows = analytics.department_ranking(NOW)["rows"]
        self.assertEqual(1, rows[0]["overdue"])
        self.assertEqual(self.department_id, rows[0]["id"])

    def test_department_ranking_keeps_archived_department_with_flag(self):
        with self.app.app_context():
            db.session.get(Department, self.third_department_id).is_archived = True
            db.session.commit()
        self.mine(recipient_department_id=self.third_department_id, status="in_progress", due_at=FIXED - timedelta(days=1))
        self.mine(recipient_department_id=self.third_department_id, status="new")
        for _ in range(3):
            self.mine(status="new")
        with self.app.app_context():
            rows = analytics.department_ranking(FIXED)["rows"]
        self.assertEqual([self.third_department_id, self.department_id], [r["id"] for r in rows])
        self.assertEqual((True, 1, 2), (rows[0]["archived"], rows[0]["overdue"], rows[0]["active"]))
        self.assertEqual((False, 0, 3), (rows[1]["archived"], rows[1]["overdue"], rows[1]["active"]))


class AnalyticsPagesTests(PortalCase):
    def test_reports_render_new_sections_even_without_tasks(self):
        self.login("head")
        page = self.client.get("/reports/department").get_data(as_text=True)
        for text in ("Динамика за 30 дней", "Нагрузка сотрудников", "С какими отделами работаем", "Нет данных: ещё нет выполненных задач"):
            self.assertIn(text, page)
        admin = self.app.test_client()
        self.login("root", client=admin)
        page = admin.get("/admin/statistics").get_data(as_text=True)
        for text in ("Динамика за 30 дней", "Где больше всего нагрузки", "Активные по приоритетам"):
            self.assertIn(text, page)

    def test_employee_still_cannot_open_department_report(self):
        self.login("employee")
        self.assertEqual(403, self.client.get("/reports/department").status_code)

    def test_department_report_hides_other_department_people_and_partners(self):
        with self.app.app_context():
            names = {d.id: d.name for d in Department.query.all()}
        # Обмен только между вторым и третьим отделами: первый отдел его не видит.
        self.task(title="Чужой обмен", sender_department_id=self.other_department_id, recipient_department_id=self.third_department_id,
            sent_at=NOW - timedelta(days=1), status="in_progress", due_at=NOW + timedelta(days=2))
        self.login("head")
        page = self.client.get("/reports/department").get_data(as_text=True)
        self.assertNotIn(names[self.third_department_id], page)
        self.assertNotIn(names[self.other_department_id], page)
        self.assertNotIn("outsider", page)
        self.assertNotIn("Чужой обмен", page)
        self.assertEqual(403, self.client.get(f"/reports/department?department_id={self.other_department_id}").status_code)

    def test_department_report_counts_only_own_department_workload(self):
        self.add_member("head-two", role="head", department_id=self.other_department_id)
        self.task(sender_department_id=self.department_id, recipient_department_id=self.other_department_id, sent_at=NOW - timedelta(days=1),
            status="in_progress", due_at=NOW + timedelta(days=2))
        self.login("head-two")
        page = self.client.get("/reports/department").get_data(as_text=True)
        block = page.split('id="report-workload"')[1].split('id="report-partners"')[0]
        self.assertIn("head-two", block)
        self.assertNotIn("Сотрудник", block)  # сотрудник первого отдела
        self.assertNotIn("head<", block)

    def test_dashboard_shows_my_tasks_free_tasks_and_hides_other_departments(self):
        incoming = dict(sender_department_id=self.other_department_id, recipient_department_id=self.department_id,
            sent_at=NOW - timedelta(days=1), due_at=NOW + timedelta(days=3))
        assigned = self.task(title="Назначенная мне", status="in_progress", **incoming)
        self.task(title="Свободная новая", status="new", **incoming)
        self.task(title="Чужая новая", status="new", sender_department_id=self.other_department_id,
            recipient_department_id=self.third_department_id, sent_at=NOW, due_at=NOW + timedelta(days=1))
        with self.app.app_context():
            db.session.add(TaskAssignee(task_id=assigned, user_id=self.ids["employee"]))
            db.session.commit()
        self.login("employee")
        page = self.client.get("/dashboard").get_data(as_text=True)
        mine_block = page.split('id="dash-mine"')[1].split('id="dash-free"')[0]
        free_block = page.split('id="dash-free"')[1].split('id="dash-trend"')[0]
        self.assertIn("Назначенная мне", mine_block)
        self.assertNotIn("Свободная новая", mine_block)
        self.assertIn("Свободная новая", free_block)
        self.assertNotIn("Чужая новая", page)
        self.assertIn("Динамика за 14 дней", page)

    def test_dashboard_hides_colleague_and_foreign_assigned_tasks_from_employee(self):
        colleague = self.add_member("colleague")
        common = dict(sender_department_id=self.other_department_id, sent_at=NOW - timedelta(days=1), due_at=NOW + timedelta(days=3),
            status="in_progress")
        own_department = self.task(title="Задача коллеги", recipient_department_id=self.department_id, **common)
        foreign = self.task(title="Задача чужого отдела", recipient_department_id=self.third_department_id, **common)
        with self.app.app_context():
            db.session.add_all([TaskAssignee(task_id=own_department, user_id=colleague),
                TaskAssignee(task_id=foreign, user_id=self.ids["outsider"])])
            db.session.commit()
        self.login("employee")
        page = self.client.get("/dashboard").get_data(as_text=True)
        mine_block = page.split('id="dash-mine"')[1].split('id="dash-free"')[0]
        self.assertNotIn("Задача коллеги", mine_block)
        self.assertNotIn("Задача чужого отдела", page)
        self.assertNotIn("Нагрузка отделов", page)

    def test_admin_dashboard_has_department_load_and_no_personal_blocks(self):
        self.task(sender_department_id=self.other_department_id, recipient_department_id=self.department_id, sent_at=NOW, due_at=NOW + timedelta(days=1))
        self.login("root")
        page = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn("Нагрузка отделов", page)
        self.assertNotIn('id="dash-mine"', page)
        self.assertNotIn('id="dash-free"', page)
        self.assertIn("Журнал действий", page)
