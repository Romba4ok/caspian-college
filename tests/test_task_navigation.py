"""Task landing, navigation order and private closed-task direction selection."""

from datetime import datetime, timezone
from html.parser import HTMLParser

from support.portal_fixtures import PortalCase


class NavigationLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and "nav-link" in attrs.get("class", "").split():
            self.links.append(attrs["href"])


class TaskNavigationTests(PortalCase):
    def as_user(self, name):
        self.client = self.app.test_client()
        self.login(name)

    def test_default_is_board_but_explicit_list_remains_available(self):
        self.as_user("head")
        for direction in ("received", "sent", "completed"):
            default = self.client.get(f"/tasks/{direction}").get_data(as_text=True)
            listed = self.client.get(f"/tasks/{direction}?view=list").get_data(as_text=True)
            self.assertIn('data-task-view="board"', default)
            self.assertIn('aria-label="Доска задач"', default)
            self.assertIn('data-task-view="list"', listed)
            self.assertNotIn('aria-label="Доска задач"', listed)

    def test_menu_order_and_tasks_landing_for_all_roles(self):
        for name in ("root", "head", "employee"):
            self.as_user(name)
            self.assertEqual("/tasks/received", self.client.get("/").location)
            page = self.client.get("/tasks/received").get_data(as_text=True)
            parser = NavigationLinks()
            parser.feed(page)
            self.assertEqual("/tasks/received", parser.links[0])
            self.assertEqual("/tasks/notifications", parser.links[-1])
            if name in ("root", "head"):
                self.assertEqual("/admin/statistics" if name == "root" else "/reports/department", parser.links[-2])
            else:
                self.assertEqual(2, len(parser.links))

    def test_closed_direction_switch_is_private_and_filters_both_ways(self):
        sent = datetime(2029, 1, 1, tzinfo=timezone.utc)
        out = self.task(title="Наше исходящее поручение", status="completed", sent_at=sent)
        incoming = self.task(title="Входящее поручение нашему отделу", status="rejected", sent_at=sent,
                             sender_department_id=self.other_department_id, recipient_department_id=self.department_id)
        unrelated = self.task(title="Чужая закрытая задача", status="cancelled", sent_at=sent,
                              sender_department_id=self.other_department_id, recipient_department_id=self.third_department_id)
        self.as_user("head")
        for scope, visible, hidden in (("received", incoming, out), ("sent", out, incoming)):
            page = self.client.get(f"/tasks/completed?scope={scope}").get_data(as_text=True)
            self.assertIn(f'data-task-id="{visible}"', page)
            self.assertNotIn(f'data-task-id="{hidden}"', page)
            self.assertNotIn(f'data-task-id="{unrelated}"', page)
        default = self.client.get("/tasks/completed?scope=invalid").get_data(as_text=True)
        self.assertIn(f'data-task-id="{incoming}"', default)
        self.assertNotIn(f'data-task-id="{out}"', default)

    def test_closed_pagination_and_back_link_keep_direction(self):
        ids = self.task_batch(30, statuses=("completed",))
        self.as_user("head")
        first = self.client.get("/tasks/completed?scope=sent").get_data(as_text=True)
        self.assertIn("scope=sent", first)
        last = self.client.get("/tasks/completed?scope=sent&view=list&page=999").get_data(as_text=True)
        self.assertIn("Страница 2 из 2", last)
        self.assertEqual(5, last.count('data-task-id="'))
        detail = self.client.get(f"/tasks/{ids[0]}").get_data(as_text=True)
        self.assertIn('href="/tasks/completed?scope=sent"', detail)

    def test_admin_keeps_all_departments_in_closed_archive(self):
        task_id = self.task(status="cancelled")
        self.as_user("root")
        html = self.client.get("/tasks/completed?scope=received").get_data(as_text=True)
        self.assertIn(f'data-task-id="{task_id}"', html)
        self.assertNotIn('aria-label="Направление завершённых задач"', html)
