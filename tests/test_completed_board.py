"""Closed boards preserve direction, full membership and read-only capabilities."""

from html import unescape
import json
import re
from urllib.parse import parse_qs, urlsplit

from support.portal_fixtures import PortalCase


class CompletedBoardTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.login("head")

    def cards(self, html):
        return [json.loads(unescape(raw)) for raw in re.findall(r'data-board-task="([^"]+)"', html)]

    def test_all_closed_cards_grouped_once_and_never_offer_actions(self):
        ids = self.task_batch(36, statuses=("completed", "cancelled", "rejected"))
        page = self.client.get("/tasks/completed?scope=sent").get_data(as_text=True)
        cards = self.cards(page)
        self.assertEqual(set(ids), {c["id"] for c in cards})
        self.assertEqual(36, len(cards))
        self.assertEqual({"completed", "cancelled", "rejected"}, {c["group"] for c in cards})
        self.assertTrue(all(c["actions"] == [] and c["candidates"] == [] for c in cards))
        for label in ("Завершены", "Отменены", "Отклонены"):
            self.assertIn(label, page)
        self.assertNotIn('data-board-action href=', page)
        self.assertNotIn('aria-label="Страницы задач"', page)
        self.assertIn("Изменять статусы нельзя.", page)

    def test_direction_view_switch_and_refresh_keep_scope_and_total(self):
        outgoing = self.task_batch(3, statuses=("completed", "cancelled", "rejected"))
        incoming = self.task(status="completed", sender_department_id=self.other_department_id,
                             recipient_department_id=self.department_id, is_legacy_dispatched=True)
        for scope, expected in (("sent", set(outgoing)), ("received", {incoming})):
            response = self.client.get(f"/tasks/completed?scope={scope}&view=board&fragment=board")
            self.assertEqual(200, response.status_code)
            self.assertEqual("completed", response.headers["X-Task-Board"])
            self.assertIn("no-store", response.headers["Cache-Control"])
            fragment = response.get_data(as_text=True)
            self.assertEqual(expected, {c["id"] for c in self.cards(fragment)})
            self.assertIn(f'data-total="{len(expected)}"', fragment)
            counts = json.loads(unescape(re.search(r'data-counts="([^"]+)"', fragment)[1]))
            self.assertEqual(4, counts["completed"])
            self.assertIn(f"scope={scope}", fragment)
        listing = self.client.get("/tasks/completed?scope=sent&view=list").get_data(as_text=True)
        self.assertIn('scope=received&amp;view=list', listing)
        links = [urlsplit(unescape(href)) for href in re.findall(r'href="([^"]+)"', listing)]
        self.assertTrue(any(link.path == "/tasks/completed" and parse_qs(link.query) ==
                            {"scope": ["sent"], "view": ["board"]} for link in links))

    def test_other_departments_and_unsent_cancellations_stay_private(self):
        hidden = self.task(status="cancelled", sender_department_id=self.other_department_id,
                           recipient_department_id=self.department_id)
        visible = self.task(status="rejected", sender_department_id=self.other_department_id,
                            recipient_department_id=self.department_id, is_legacy_dispatched=True)
        for suffix in ("", "&fragment=board"):
            page = self.client.get("/tasks/completed?scope=received&view=board" + suffix).get_data(as_text=True)
            self.assertEqual({visible}, {c["id"] for c in self.cards(page)})
            self.assertNotIn(f'data-task-id="{hidden}"', page)
        self.assertEqual(404, self.client.get(f"/tasks/{hidden}/panel").status_code)

    def test_closed_panel_is_read_only_and_has_history(self):
        task_id = self.task(status="completed", is_legacy_dispatched=True)
        response = self.client.get(f"/tasks/{task_id}/panel")
        self.assertEqual(200, response.status_code)
        page = response.get_data(as_text=True)
        self.assertIn("История", page)
        self.assertIn("Задача закрыта", page)
        self.assertNotIn("Действия с задачей", page)

    def test_empty_board_keeps_all_three_columns(self):
        page = self.client.get("/tasks/completed").get_data(as_text=True)
        self.assertEqual(3, page.count('class="board-column"'))
        self.assertIn("Закрытых задач в выбранном направлении пока нет.", page)
