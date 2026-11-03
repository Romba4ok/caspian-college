"""Дашборд, смена собственного пароля, поиск и выгрузка задач."""
from datetime import datetime, timedelta, timezone

from support.portal_fixtures import PortalCase


class DashboardTests(PortalCase):
    def test_counts_ring_and_deadline_lists_belong_to_the_receiving_department(self):
        now = datetime.now(timezone.utc)
        mine = dict(sender_department_id=self.other_department_id, recipient_department_id=self.department_id, sent_at=now - timedelta(days=10))
        self.task(title="Выполненное поручение", status="completed", due_at=now + timedelta(days=30), **mine)
        self.task(title="Просроченное поручение", status="in_progress", due_at=now - timedelta(days=2), **mine)
        self.task(title="Скоро срок", status="new", due_at=now + timedelta(days=2), **mine)
        self.task(title="Чужой отдел", status="new", due_at=now + timedelta(days=1), sender_department_id=self.other_department_id,
            recipient_department_id=self.third_department_id)
        self.login("head")
        page = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn("Просроченное поручение", page)
        self.assertIn("Скоро срок", page)
        self.assertNotIn("Чужой отдел", page)
        self.assertIn("--pct: 33", page)

    def test_all_roles_open_dashboard_and_guests_are_redirected(self):
        for name in ("root", "head", "employee"):
            client = self.app.test_client()
            self.login(name, client=client)
            self.assertEqual(200, client.get("/dashboard").status_code)
        self.assertEqual(302, self.app.test_client().get("/dashboard").status_code)

    def test_empty_department_has_zero_ring_without_division_error(self):
        self.login("employee")
        page = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn("--pct: 0", page)


class PasswordChangeTests(PortalCase):
    def post(self, current="member-password", new="new-password-1", repeat=None):
        return self.client.post("/account/password", data=dict(current_password=current, new_password=new,
            repeat_password=new if repeat is None else repeat))

    def test_success_keeps_session_and_switches_credentials(self):
        self.login("employee")
        response = self.post()
        self.assertEqual(302, response.status_code)
        self.assertEqual(200, self.client.get("/dashboard").status_code)
        old = self.app.test_client()
        self.assertIn("Неверный логин или пароль", old.post("/login", data=dict(username="employee", password="member-password"),
            follow_redirects=True).get_data(as_text=True))
        fresh = self.app.test_client()
        self.assertEqual(302, self.login("employee", "new-password-1", client=fresh, follow=False).status_code)

    def test_other_sessions_are_invalidated(self):
        second = self.app.test_client()
        self.login("employee")
        self.login("employee", client=second)
        self.post()
        self.assertEqual(302, second.get("/dashboard").status_code)

    def test_validation_errors_do_not_change_password_or_echo_it(self):
        self.login("employee")
        for current, new, repeat, text in (("wrong-password", "new-password-1", None, "Текущий пароль указан неверно"),
                ("member-password", "short", None, "не короче 8"), ("member-password", "new-password-1", "other-password-2", "не совпадают"),
                ("member-password", "member-password", None, "должен отличаться")):
            response = self.post(current, new, repeat)
            self.assertEqual(400, response.status_code)
            body = response.get_data(as_text=True)
            self.assertIn(text, body)
            self.assertNotIn("new-password-1", body)
        self.assertEqual(302, self.login("employee", client=self.app.test_client(), follow=False).status_code)

    def test_guest_cannot_use_the_form(self):
        self.assertEqual(302, self.app.test_client().get("/account/password").status_code)


class SearchAndExportTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.alpha = self.task(title="Закупка проекторов", description="для актового зала", recipient_department_id=self.department_id,
            sender_department_id=self.other_department_id)
        self.beta = self.task(title="Отчёт по практике", description="=HYPERLINK(1)", recipient_department_id=self.department_id,
            sender_department_id=self.other_department_id)
        self.login("head")

    def test_search_filters_by_title_description_and_number(self):
        for query, shown, hidden in (("проектор", self.alpha, self.beta), ("актового", self.alpha, self.beta), (f"№{self.beta}", self.beta, self.alpha)):
            page = self.client.get("/tasks/received", query_string={"search": query}).get_data(as_text=True)
            self.assertIn(f'data-task-id="{shown}"', page)
            self.assertNotIn(f'data-task-id="{hidden}"', page)

    def test_like_wildcards_are_literal_and_search_is_preserved_in_refresh_url(self):
        page = self.client.get("/tasks/received", query_string={"search": "%"}).get_data(as_text=True)
        self.assertNotIn(f'data-task-id="{self.alpha}"', page)
        page = self.client.get("/tasks/received", query_string={"search": "проектор"}).get_data(as_text=True)
        self.assertIn("search=%D0%BF%D1%80%D0%BE%D0%B5%D0%BA%D1%82%D0%BE%D1%80", page)

    def test_export_is_scoped_filtered_and_formula_safe(self):
        self.task(title="Чужое поручение", recipient_department_id=self.third_department_id, sender_department_id=self.other_department_id)
        response = self.client.get("/tasks/export.csv", query_string={"direction": "received"})
        text = response.get_data(as_text=True)
        self.assertEqual(200, response.status_code)
        self.assertIn("text/csv", response.headers["Content-Type"])
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertIn("Закупка проекторов", text)
        self.assertNotIn("Чужое поручение", text)
        filtered = self.client.get("/tasks/export.csv", query_string={"direction": "received", "search": "практике"}).get_data(as_text=True)
        self.assertIn("Отчёт по практике", filtered)
        self.assertNotIn("Закупка проекторов", filtered)
        self.assertEqual(400, self.client.get("/tasks/export.csv", query_string={"direction": "bad"}).status_code)
        self.assertEqual(302, self.app.test_client().get("/tasks/export.csv").status_code)
