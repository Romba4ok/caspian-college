"""Board completeness, private aggregates and bounded SQL for large boards."""
from datetime import datetime, timedelta, timezone
from html import unescape
import json
import re
from uuid import uuid4

from flask import template_rendered
from sqlalchemy import event, inspect

from app.extensions import db
from app.models import Department, Task, TaskAttachment, TaskComment, TaskEvent, TaskNotification, User
from support.portal_fixtures import PortalCase


ACTIVE = ("draft", "pending_approval", "awaiting_assignment", "assigned", "in_progress",
          "awaiting_clarification", "recipient_review", "sender_acceptance")


class BoardProjectionTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.add_member("worker", department_id=self.other_department_id)
        self.add_member("receiver-head", role="head", department_id=self.other_department_id)
        self.add_member("blocked-worker", department_id=self.other_department_id, blocked=True)

    def as_user(self, username="employee"):
        self.client = self.app.test_client()
        self.assertEqual(200, self.login(username).status_code)

    def board(self, direction="sent", query="view=board"):
        contexts = []
        def capture(sender, template, context, **extra):
            contexts.append(context)
        with template_rendered.connected_to(capture, self.app):
            response = self.client.get(f"/tasks/{direction}?{query}")
        self.assertEqual(200, response.status_code)
        return response, contexts[-1]

    def cards(self, response):
        return {card["id"]: card for card in (
            json.loads(unescape(raw)) for raw in re.findall(
                r'data-board-task="([^"]+)"', response.get_data(as_text=True)))}

    def assert_groups(self, context, expected, ids):
        groups = context["board_groups"]
        self.assertEqual(list(expected), [group["label"] for group in groups])
        self.assertEqual(list(expected.values()), [group["statuses"] for group in groups])
        grouped = [task.id for group in groups for task in group["tasks"]]
        self.assertEqual(set(ids), set(grouped))
        self.assertEqual(len(ids), len(grouped))
        self.assertEqual(len(ids), sum(group["count"] for group in groups))
        self.assertEqual(len(ids), context["counts"][context["direction"]])
        for group in groups:
            self.assertEqual(group["count"], len(group["tasks"]))
            self.assertTrue(all(task.status in group["statuses"] for task in group["tasks"]))

    def test_sent_groups_cover_every_active_status_exactly_once(self):
        ids = self.task_batch(36, statuses=ACTIVE)
        self.as_user()
        response, context = self.board()
        self.assertEqual(set(ids), set(self.cards(response)))
        self.assert_groups(context, {
            "Черновики": ("draft",), "На согласовании": ("pending_approval",),
            "Переданы отделу": ("awaiting_assignment", "assigned"),
            "Выполняются": ("in_progress", "awaiting_clarification"),
            "Проверка результата": ("recipient_review", "sender_acceptance"),
        }, ids)
        self.assertEqual(set(ACTIVE), {task.status for task in context["tasks"]})

    def test_received_groups_exclude_presend_and_closed_tasks(self):
        hidden = self.task_batch(4, statuses=("draft", "pending_approval"))
        closed = self.task_batch(3, statuses=("completed", "cancelled", "rejected"))
        ids = self.task_batch(36, statuses=ACTIVE[2:])
        self.as_user("receiver-head")
        response, context = self.board("received")
        self.assertEqual(set(ids), set(self.cards(response)))
        self.assertTrue(set(hidden + closed).isdisjoint(self.cards(response)))
        self.assert_groups(context, {
            "К выполнению": ("awaiting_assignment", "assigned"),
            "В работе": ("in_progress",), "Уточнение": ("awaiting_clarification",),
            "Проверка своего отдела": ("recipient_review",),
            "Приёмка отправителя": ("sender_acceptance",),
        }, ids)

    def test_300_tasks_in_both_directions_ignore_page_but_lists_keep_25(self):
        ids = self.task_batch(300, statuses=("awaiting_assignment",))
        for username, direction in (("employee", "sent"), ("receiver-head", "received")):
            self.as_user(username)
            for page in ("1", "2", "999", "invalid"):
                response, context = self.board(direction, f"view=board&page={page}")
                self.assertEqual(set(ids), set(self.cards(response)))
                self.assertEqual(300, context["pagination"].total)
                self.assertFalse(context["pagination"].has_next)
                self.assertNotIn('aria-label="Страницы задач"', response.get_data(as_text=True))
            pages = []
            for page in (1, 2):
                response, context = self.board(direction, f"view=list&page={page}")
                self.assertEqual(25, len(context["tasks"]))
                self.assertEqual(300, context["pagination"].total)
                self.assertEqual(12, context["pagination"].pages)
                self.assertEqual([], context["board_groups"])
                pages.append({task.id for task in context["tasks"]})
            self.assertTrue(pages[0].isdisjoint(pages[1]))

    def test_empty_board_has_five_zero_groups_and_no_candidates(self):
        self.as_user()
        for direction in ("received", "sent"):
            response, context = self.board(direction)
            self.assertEqual({}, self.cards(response))
            self.assertEqual(5, len(context["board_groups"]))
            self.assertTrue(all(group["count"] == 0 for group in context["board_groups"]))
            self.assertEqual(0, context["pagination"].total)

    def test_counts_are_independent_and_include_legacy_and_comment_files(self):
        ids = self.task_batch(2, statuses=("awaiting_assignment",))
        hidden = self.task()
        with self.app.app_context():
            comments = [TaskComment(task_id=ids[0], author_id=self.ids["employee"], body="Пример") for _ in range(2)]
            private = TaskComment(task_id=hidden, author_id=self.ids["employee"], body="Приватный комментарий")
            db.session.add_all(comments + [private]); db.session.flush()
            for task_id, comment_id in [(ids[0], None), (ids[0], comments[0].id),
                                         (ids[0], comments[1].id), (hidden, private.id)]:
                db.session.add(TaskAttachment(task_id=task_id, uploaded_by_id=self.ids["employee"],
                    original_filename="Пример.pdf", storage_key=uuid4().hex, content_type="application/pdf",
                    size_bytes=20, comment_id=comment_id))
            db.session.commit()
        self.as_user("receiver-head")
        response, context = self.board("received")
        cards = self.cards(response)
        self.assertEqual(2, cards[ids[0]]["comment_count"])
        self.assertEqual(3, cards[ids[0]]["attachment_count"])
        self.assertEqual(0, cards[ids[1]]["comment_count"])
        self.assertEqual(0, cards[ids[1]]["attachment_count"])
        self.assertNotIn(hidden, cards)
        for task in context["tasks"]:
            self.assertTrue({"comments", "attachments", "events"} <= inspect(task).unloaded)

    def test_candidates_only_on_permitted_cards_and_only_active_local_members(self):
        ids = self.task_batch(2, statuses=("awaiting_assignment", "sender_acceptance"), assignee_id=None)
        self.as_user("receiver-head")
        response, _ = self.board("received")
        cards = self.cards(response)
        self.assertEqual({self.ids["worker"], self.ids["receiver-head"]}, {u["id"] for u in cards[ids[0]]["candidates"]})
        self.assertEqual([], cards[ids[1]]["candidates"])
        self.as_user("worker")
        self.assertTrue(all(not card["candidates"] for card in self.cards(self.board("received")[0]).values()))
        self.as_user("root")
        self.assertTrue(all(not card["actions"] and not card["candidates"] for card in self.cards(self.board("received")[0]).values()))

    def test_department_boundary_applies_to_cards_groups_and_totals(self):
        own = self.task_batch(36, statuses=ACTIVE)
        foreign = self.task_batch(36, statuses=ACTIVE, author_id=self.ids["outsider"],
            sender_department_id=self.third_department_id, recipient_department_id=self.other_department_id)
        self.as_user()
        response, context = self.board()
        self.assertEqual(set(own), set(self.cards(response)))
        self.assertEqual(36, sum(group["count"] for group in context["board_groups"]))
        self.as_user("outsider")
        response, context = self.board()
        self.assertEqual(set(foreign), set(self.cards(response)))
        self.assertEqual(36, sum(group["count"] for group in context["board_groups"]))
        self.assertTrue(set(own).isdisjoint(self.cards(response)))

    def test_admin_sees_all_active_tasks_but_received_still_excludes_presend(self):
        own = self.task_batch(36, statuses=ACTIVE)
        other = self.task_batch(8, statuses=ACTIVE, author_id=self.ids["outsider"],
            sender_department_id=self.third_department_id, recipient_department_id=self.other_department_id)
        self.task_batch(3, statuses=("completed", "cancelled", "rejected"))
        self.as_user("root")
        response, context = self.board()
        self.assertEqual(set(own + other), set(self.cards(response)))
        self.assertEqual(44, sum(group["count"] for group in context["board_groups"]))
        response, context = self.board("received")
        self.assertTrue(all(task.status not in ACTIVE[:2] for task in context["tasks"]))
        self.assertEqual(len(self.cards(response)), sum(group["count"] for group in context["board_groups"]))

    def test_missing_sent_marker_does_not_make_task_visible_to_recipient(self):
        hidden = self.task_batch(2, statuses=("assigned",), sent_at=None)
        self.as_user("receiver-head")
        response, context = self.board("received")
        self.assertEqual({}, self.cards(response))
        self.assertEqual(0, sum(group["count"] for group in context["board_groups"]))
        self.assertEqual(404, self.client.get(f"/tasks/{hidden[0]}").status_code)

    def test_archived_department_keeps_old_board_but_no_new_send_action(self):
        ids = self.task_batch(2, statuses=("draft", "awaiting_assignment"), author_id=self.ids["head"])
        with self.app.app_context():
            db.session.get(Department, self.department_id).is_archived = True; db.session.commit()
        self.as_user("head")
        response, context = self.board()
        self.assertEqual(set(ids), set(self.cards(response)))
        self.assertEqual({"cancel"}, {a["key"] for a in self.cards(response)[ids[0]]["actions"]})
        self.assertFalse(context["may_create"])

    def test_anonymous_and_blocked_account_cannot_read_projection(self):
        self.task_batch(36, statuses=("awaiting_assignment",))
        response = self.client.get("/tasks/received?view=board")
        self.assertEqual(302, response.status_code)
        self.as_user("blocked-worker")
        response = self.client.get("/tasks/received?view=board")
        self.assertEqual(302, response.status_code)
        self.assertNotIn("Поручение", response.get_data(as_text=True))

    def test_completed_and_invalid_views_keep_paginated_list(self):
        self.task_batch(36, statuses=("completed",))
        self.as_user()
        response, context = self.board("completed")
        self.assertEqual("list", context["view"])
        self.assertEqual(25, len(context["tasks"]))
        self.assertEqual([], context["board_groups"])
        self.task_batch(36, statuses=("draft",))
        response, context = self.board(query="view=invalid")
        self.assertEqual("list", context["view"])
        self.assertEqual(25, len(context["tasks"]))

    def test_board_preserves_list_deadline_then_creation_then_id_order(self):
        base = datetime(2030, 1, 1, tzinfo=timezone.utc)
        ids = [self.task(title=f"Порядок {index}", due_at=base + timedelta(days=day),
                         created_at=base + timedelta(hours=hour))
               for index, (day, hour) in enumerate([(2, 1), (1, 3), (1, 1), (1, 1)])]
        self.as_user()
        _, context = self.board()
        ordered = [task.id for task in context["tasks"]]
        self.assertEqual([ids[1], ids[3], ids[2], ids[0]], ordered)
        _, listing = self.board(query="view=list")
        self.assertEqual(ordered, [task.id for task in listing["tasks"]])

    def test_reading_board_does_not_mutate_workflow_or_notifications(self):
        ids = self.task_batch(36, statuses=ACTIVE)
        self.as_user("head")
        self.board(); self.board(query="view=board&page=2")
        with self.app.app_context():
            tasks = Task.query.order_by(Task.id).all()
            self.assertEqual(ids, [task.id for task in tasks])
            self.assertEqual([ACTIVE[i % len(ACTIVE)] for i in range(36)], [task.status for task in tasks])
            self.assertTrue(all(task.revision == 0 for task in tasks))
            for model in (TaskEvent, TaskComment, TaskNotification): self.assertEqual(0, model.query.count())

    def test_sql_queries_stay_bounded_from_36_to_300_cards(self):
        self.task_batch(36, statuses=("assigned",), assignee_id=self.ids["worker"])
        self.as_user("receiver-head")
        with self.app.app_context(): engine = db.engine
        def measure():
            statements = []
            def capture(conn, cursor, statement, parameters, context, executemany):
                statements.append(statement)
            event.listen(engine, "before_cursor_execute", capture)
            try: response, context = self.board("received")
            finally: event.remove(engine, "before_cursor_execute", capture)
            self.assertTrue(all(s.lstrip().upper().startswith("SELECT") for s in statements))
            return len(statements), len(self.cards(response))
        small_queries, small_cards = measure()
        self.task_batch(264, statuses=("assigned",), assignee_id=self.ids["worker"])
        large_queries, large_cards = measure()
        self.assertEqual((36, 300), (small_cards, large_cards))
        self.assertLessEqual(large_queries, small_queries + 1)
        self.assertLess(large_queries, 20)
        print(f"Board SQL: {small_cards} cards / {small_queries} SELECT; {large_cards} cards / {large_queries} SELECT")
