"""Дашборд: уведомления и «Мои задачи» подчиняются тем же правилам доступа, что и остальной портал."""
import re

from support.portal_fixtures import PortalCase

from app.extensions import db
from app.models import TaskAssignee, TaskEvent, TaskNotification, User


class DashboardNotificationTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.visible = self.task(title="Видимая задача отдела", recipient_department_id=self.department_id,
            sender_department_id=self.other_department_id)

    def notify(self, task_id=None, text="Синтетическое уведомление", user="employee"):
        with self.app.app_context():
            db.session.add(TaskNotification(user_id=self.ids[user], task_id=task_id, event_type="edited", message=text))
            db.session.commit()

    def assign(self, task_id, user="employee"):
        with self.app.app_context():
            db.session.add(TaskAssignee(task_id=task_id, user_id=self.ids[user]))
            db.session.commit()

    def dashboard(self, user="employee"):
        self.login(user)
        return self.client.get("/dashboard").get_data(as_text=True)

    def header_count(self, page):
        match = re.search(r"непрочитанных: (\d+)", page)
        return int(match[1]) if match else 0

    def badge_count(self, page):
        match = re.search(r"(\d+) новых", page)
        return int(match[1]) if match else 0

    def test_transferred_employee_does_not_see_titles_of_the_old_department(self):
        old = self.task(title="Задача прежнего отдела", recipient_department_id=self.department_id,
            sender_department_id=self.other_department_id)
        self.notify(old)
        self.notify(None, "Общее сообщение")
        with self.app.app_context():
            db.session.get(User, self.ids["employee"]).department_id = self.third_department_id
            db.session.commit()
        page = self.dashboard()
        self.assertNotIn("Задача прежнего отдела", page)
        self.assertEqual(1, self.header_count(page))
        self.assertEqual(self.header_count(page), self.badge_count(page))
        self.assertIn("Общее сообщение", page)

    def test_soft_deleted_task_title_is_not_shown_and_not_counted(self):
        gone = self.task(title="Удалённая задача", recipient_department_id=self.department_id,
            sender_department_id=self.other_department_id)
        self.notify(gone)
        self.notify(None, "Общее сообщение")
        with self.app.app_context():
            db.session.add(TaskEvent(task_id=gone, actor_id=self.ids["head"], event_type="deleted", details={}))
            db.session.commit()
        page = self.dashboard()
        self.assertNotIn("Удалённая задача", page)
        self.assertEqual(self.header_count(page), self.badge_count(page))
        self.assertEqual(1, self.badge_count(page))

    def test_notifications_without_task_and_for_visible_tasks_are_shown_newest_first(self):
        self.notify(None, "Сообщение без задачи")
        self.notify(self.visible)
        page = self.dashboard()
        self.assertIn("Сообщение без задачи", page)
        self.assertIn("Видимая задача отдела", page)
        self.assertLess(page.index("Видимая задача отдела"), page.index("Сообщение без задачи"))
        self.assertEqual(2, self.badge_count(page))
        self.assertEqual(2, self.header_count(page))

    def test_admin_dashboard_still_lists_notifications(self):
        self.notify(self.visible, user="root")
        page = self.dashboard("root")
        self.assertIn("Видимая задача отдела", page)
        self.assertEqual(1, self.badge_count(page))

    def test_my_tasks_hide_assigned_tasks_the_user_can_no_longer_see(self):
        working = self.task(title="Задача в работе", status="in_progress", recipient_department_id=self.department_id,
            sender_department_id=self.other_department_id)
        self.assign(working)
        self.assertIn("Задача в работе", self.dashboard())
        with self.app.app_context():  # the DB refuses to assign an outsider, so the assignment goes stale by a transfer
            db.session.get(User, self.ids["employee"]).department_id = self.third_department_id
            db.session.commit()
        self.assertNotIn("Задача в работе", self.dashboard())
