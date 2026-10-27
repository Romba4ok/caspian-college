"""Priority then deadline for active work; actual closing time for the archive."""

from datetime import datetime, timedelta, timezone

from flask import template_rendered

from support.portal_fixtures import PortalCase


class TaskOrderingTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.add_member("receiver-head", role="head", department_id=self.other_department_id)
        self.base = datetime(2030, 1, 1, tzinfo=timezone.utc)

    def render(self, url):
        contexts = []
        def capture(sender, template, context, **kwargs):
            contexts.append(context)
        with template_rendered.connected_to(capture, self.app):
            response = self.client.get(url)
        self.assertEqual(200, response.status_code)
        return contexts[-1]

    def as_user(self, name):
        self.client = self.app.test_client()
        self.login(name)

    def test_both_active_directions_rank_priority_before_due_date(self):
        common = dict(status="assigned", sent_at=self.base, assignee_id=self.ids["receiver-head"])
        low = self.task(**common, priority="low", due_at=self.base)
        normal = self.task(**common, priority="normal", due_at=self.base + timedelta(days=1))
        high = self.task(**common, priority="high", due_at=self.base + timedelta(days=2))
        urgent_late = self.task(**common, priority="urgent", due_at=self.base + timedelta(days=10))
        urgent_near = self.task(**common, priority="urgent", due_at=self.base + timedelta(days=5))
        urgent_unknown = self.task(**common, priority="urgent", due_at=None, legacy_missing_due=True, is_legacy_dispatched=True)
        expected = [urgent_near, urgent_late, urgent_unknown, high, normal, low]
        for username, direction in (("head", "sent"), ("receiver-head", "received")):
            self.as_user(username)
            for view in ("board", "list"):
                context = self.render(f"/tasks/{direction}?view={view}")
                self.assertEqual(expected, [t.id for t in context["tasks"]])
                if view == "board":
                    populated = [g for g in context["board_groups"] if g["tasks"]]
                    self.assertEqual(1, len(populated))
                    self.assertEqual(expected, [t.id for t in populated[0]["tasks"]])

    def test_priority_is_applied_before_list_pagination(self):
        self.task_batch(26, statuses=("in_progress",), priority="low", due_at=self.base)
        urgent = self.task(status="in_progress", sent_at=self.base, priority="urgent",
                           due_at=self.base + timedelta(days=100))
        self.as_user("head")
        first = self.render("/tasks/sent?view=list")
        second = self.render("/tasks/sent?view=list&page=2")
        self.assertEqual(urgent, first["tasks"][0].id)
        self.assertNotIn(urgent, [t.id for t in second["tasks"]])
        self.assertEqual(27, first["pagination"].total)

    def test_archive_uses_completion_time_and_not_priority_or_due_date(self):
        common = dict(status="completed", sent_at=self.base)
        old = self.task(**common, priority="urgent", completed_at=self.base,
                        closed_at=self.base + timedelta(days=10))
        recent = self.task(**common, priority="low", completed_at=self.base + timedelta(days=8),
                           closed_at=self.base + timedelta(days=8))
        fallback = self.task(**common, closed_at=self.base + timedelta(days=6))
        unknown = self.task(**common)
        cancelled = self.task(status="cancelled", sent_at=self.base,
                              closed_at=self.base + timedelta(days=9))
        self.as_user("head")
        listing = self.render("/tasks/completed?scope=sent&view=list")
        self.assertEqual([cancelled, recent, fallback, old, unknown], [t.id for t in listing["tasks"]])
        board = self.render("/tasks/completed?scope=sent")
        completed = next(g for g in board["board_groups"] if g["key"] == "completed")
        self.assertEqual([recent, fallback, old, unknown], [t.id for t in completed["tasks"]])
