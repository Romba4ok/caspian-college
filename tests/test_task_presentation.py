"""Make issuing a task distinct from handing back its finished result."""

from datetime import datetime, timezone

from support.portal_fixtures import PortalCase


class TaskPresentationTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.add_member("receiver-head", role="head", department_id=self.other_department_id)
        self.add_member("worker", department_id=self.other_department_id)
        self.sent = datetime(2029, 1, 1, tzinfo=timezone.utc)

    def as_user(self, name):
        self.client = self.app.test_client()
        self.login(name)

    def test_internal_and_customer_review_explained_in_list_board_and_details(self):
        internal = self.task(status="recipient_review", sent_at=self.sent)
        acceptance = self.task(status="sender_acceptance", sent_at=self.sent)
        for user, direction in (("head", "sent"), ("receiver-head", "received")):
            self.as_user(user)
            for view in ("list", "board"):
                html = self.client.get(f"/tasks/{direction}?view={view}").get_data(as_text=True)
                self.assertIn("Задачи нашему отделу", html)
                self.assertIn("Задачи другим отделам", html)
                self.assertIn("Заказчику результат ещё не передан.", html)
                self.assertIn("на окончательную приёмку.", html)
            for task_id, text in ((internal, "Заказчику результат ещё не передан."),
                                  (acceptance, "на окончательную приёмку.")):
                for suffix in ("", "/panel"):
                    html = self.client.get(f"/tasks/{task_id}{suffix}").get_data(as_text=True)
                    self.assertIn(text, html)
                    self.assertIn("Готовый результат", html)

    def test_completed_cards_identify_both_department_roles(self):
        closed = dict(status="completed", sent_at=self.sent, completed_at=self.sent, closed_at=self.sent)
        self.task(**closed)
        self.task(**closed, author_id=self.ids["receiver-head"],
                  sender_department_id=self.other_department_id, recipient_department_id=self.department_id)
        self.as_user("head")
        html = self.client.get("/tasks/completed?scope=sent").get_data(as_text=True)
        self.assertIn("Наш отдел поручал", html)
        incoming = self.client.get("/tasks/completed?scope=received").get_data(as_text=True)
        self.assertIn("Наш отдел выполнял", incoming)
        self.assertEqual(1, html.count("принял результат. Поручение завершено."))
        self.assertEqual(1, incoming.count("принял результат. Поручение завершено."))

    def test_cancelled_draft_is_not_presented_as_dispatched(self):
        self.task(status="cancelled", closed_at=self.sent)
        self.as_user("head")
        html = self.client.get("/tasks/completed?scope=sent").get_data(as_text=True)
        self.assertIn("Заявка нашего отдела", html)
        self.assertIn("Заявка отменена до отправки другому отделу.", html)

    def test_legacy_cancelled_task_uses_legacy_dispatch_marker(self):
        self.task(status="cancelled", closed_at=self.sent, is_legacy_dispatched=True)
        self.as_user("head")
        html = self.client.get("/tasks/completed?scope=sent").get_data(as_text=True)
        self.assertIn("отменил поручение.", html)
        self.assertNotIn("Заявка отменена до отправки другому отделу.", html)

    def test_rejection_does_not_claim_the_recipient_performed_the_task(self):
        self.task(status="rejected", sent_at=self.sent, closed_at=self.sent)
        self.as_user("receiver-head")
        html = self.client.get("/tasks/completed?scope=received").get_data(as_text=True)
        self.assertIn("Поручение нашему отделу", html)
        self.assertIn("отклонил поручение.", html)
        self.assertNotIn("Наш отдел выполнял", html)

    def test_executor_action_explicitly_hands_result_to_own_head(self):
        task_id = self.task(status="in_progress", sent_at=self.sent, assignee_id=self.ids["worker"])
        self.as_user("worker")
        html = self.client.get(f"/tasks/{task_id}").get_data(as_text=True)
        self.assertIn("Передать результат начальнику своего отдела", html)
        self.assertNotIn("Отправить поручение в другой отдел", html)

    def test_admin_copy_does_not_claim_a_department(self):
        self.task(status="sender_acceptance", sent_at=self.sent)
        self.as_user("root")
        html = self.client.get("/tasks/sent?view=list").get_data(as_text=True)
        self.assertIn("Заказчик:", html)
        self.assertIn("Исполняющий отдел:", html)
        self.assertNotIn("Наш отдел — заказчик", html)
