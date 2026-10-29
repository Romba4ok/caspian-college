"""Role/side/status matrix, independent from unimplemented mutations."""
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import permissions as p, workflow as w
from app.extensions import db
from app.models import Department, Task, User, TaskEvent, TaskComment, TaskNotification
from support.portal_fixtures import PortalCase


class SimplePermissionTests(PortalCase):
    def setUp(self):
        super().setUp()
        for name, role, dept in (("sender-colleague", "employee", self.department_id),
            ("head2", "head", self.department_id), ("receiver", "employee", self.other_department_id),
            ("receiver-colleague", "employee", self.other_department_id),
            ("receiver-head", "head", self.other_department_id), ("receiver-head2", "head", self.other_department_id)):
            self.add_member(name, role=role, department_id=dept)
        self.new = self.task()
        self.work = self.task(status="in_progress", assignee_id=self.ids["receiver"])
        self.done = self.task(status="completed", assignee_id=self.ids["receiver"])
        self.cancelled = self.task(status="cancelled")
        self.rejected = self.task(status="rejected")

    def actor(self, name):
        return db.session.get(User, self.ids[name], populate_existing=True)

    def assert_matrix(self, task_id, expected):
        names = ("assign", "claim", "reassign", "release", "complete", "return", "cancel", "reject", "deadline", "comment")
        funcs = (p.can_assign_task, p.can_claim_task, p.can_reassign_task, p.can_release_task,
                 p.can_complete_task, p.can_return_result, p.can_cancel_task, p.can_reject_task,
                 p.can_change_deadline, p.can_contribute_task)
        with self.app.app_context():
            task = db.session.get(Task, task_id)
            for name in self.ids:
                user = self.actor(name)
                actual = {key for key, check in zip(names, funcs) if check(user, task)}
                self.assertEqual(set(expected.get(name, ())), actual, name)

    def test_new_task_author_colleague_both_heads_and_free_claim(self):
        self.assert_matrix(self.new, {
            "employee": {"cancel", "comment"}, "sender-colleague": {"comment"},
            "head": {"cancel", "deadline", "comment"}, "head2": {"cancel", "deadline", "comment"},
            "receiver": {"claim", "comment"}, "receiver-colleague": {"claim", "comment"},
            "receiver-head": {"assign", "claim", "reject", "comment"},
            "receiver-head2": {"assign", "claim", "reject", "comment"},
        })

    def test_work_task_only_assignee_and_receiver_heads_manage_execution(self):
        self.assert_matrix(self.work, {
            "employee": {"cancel", "comment"}, "sender-colleague": {"comment"},
            "head": {"cancel", "deadline", "comment"}, "head2": {"cancel", "deadline", "comment"},
            "receiver": {"release", "complete", "reject", "comment"}, "receiver-colleague": {"claim", "comment"},
            "receiver-head": {"claim", "reassign", "complete", "reject", "comment"},
            "receiver-head2": {"claim", "reassign", "complete", "reject", "comment"},
        })

    def test_only_author_or_sender_head_returns_completed_task(self):
        self.assert_matrix(self.done, {"employee": {"return"}, "head": {"return"}, "head2": {"return"}})

    def test_cancelled_and_rejected_are_read_only_for_all_roles(self):
        self.assert_matrix(self.cancelled, {})
        self.assert_matrix(self.rejected, {})

    def test_archival_blocks_creation_but_keeps_working_permissions(self):
        with self.app.app_context():
            for did in (self.department_id, self.other_department_id):
                db.session.get(Department, did).is_archived = True
            db.session.commit()
            for name in self.ids:
                self.assertEqual(name == "outsider", p.can_create_task(self.actor(name)), name)
            task = db.session.get(Task, self.work)
            self.assertTrue(p.can_complete_task(self.actor("receiver-head"), task))
            self.assertTrue(p.can_release_task(self.actor("receiver"), task))
            self.assertTrue(p.can_cancel_task(self.actor("head"), task))

    def test_create_only_department_members_not_admin(self):
        with self.app.app_context():
            for name in self.ids:
                self.assertEqual(name != "root", p.can_create_task(self.actor(name)), name)

    def test_blocked_or_moved_previous_assignee_cannot_work_but_return_is_allowed(self):
        with self.app.app_context():
            task = db.session.get(Task, self.work)
            done = db.session.get(Task, self.done)
            user = self.actor("receiver")
            user.is_blocked = True
            db.session.flush()
            self.assertFalse(p.has_available_assignee(task))
            self.assertFalse(p.can_complete_task(self.actor("receiver-head"), task))
            self.assertFalse(p.can_release_task(user, task))
            self.assertTrue(p.can_reassign_task(self.actor("receiver-head"), task))
            self.assertTrue(p.can_return_result(self.actor("employee"), done))
            user.is_blocked = False
            user.department_id = self.third_department_id
            db.session.flush()
            self.assertFalse(p.has_available_assignee(task))
            self.assertFalse(p.can_reject_task(user, task))

    def test_head_assignee_can_release_self_but_not_colleagues(self):
        with self.app.app_context():
            task = db.session.get(Task, self.work)
            task.assignee_id = self.ids["receiver-head"]
            db.session.commit()
            db.session.expire_all()
            task = db.session.get(Task, self.work)
            self.assertTrue(p.can_release_task(self.actor("receiver-head"), task))
            self.assertFalse(p.can_release_task(self.actor("receiver-head2"), task))

    def test_legacy_marker_does_not_replace_actual_dispatch(self):
        with self.app.app_context():
            task = SimpleNamespace(status="new", sent_at=None, is_legacy_dispatched=True,
                sender_department_id=self.department_id, recipient_department_id=self.other_department_id)
            self.assertFalse(p.is_task_dispatched(task))
            for name in ("employee", "receiver", "receiver-head", "outsider"):
                self.assertFalse(p.can_access_task(self.actor(name), task))
            self.assertFalse(p.can_access_task(None, task))
            self.assertFalse(p.is_platform_user(SimpleNamespace(is_active=True, role="teacher", department_id=1)))

    def test_all_removed_capabilities_always_false(self):
        names = ("can_edit_draft", "can_submit_for_approval", "can_withdraw_approval", "can_return_approval",
                 "can_approve_task", "can_send_task", "can_start_task", "can_request_clarification",
                 "can_answer_clarification", "can_review_recipient_result", "can_review_result", "can_change_working_status")
        with self.app.app_context():
            for name in self.ids:
                for method in names:
                    self.assertFalse(getattr(p, method)(self.actor(name), db.session.get(Task, self.work)), (name, method))

    def test_direct_legacy_service_calls_cannot_mutate_or_prepare_files(self):
        class ForbiddenFile:
            def install(self):
                raise AssertionError("must not install file")
        with self.app.app_context():
            task, actor = db.session.get(Task, self.work), self.actor("root")
            for method in (w.change_task_deadline, w.add_task_comment, w.add_task_attachment):
                with self.assertRaises(PermissionError):
                    extra = {"due_at": task.due_at} if method is w.change_task_deadline else {}
                    method(task=task, actor=actor, expected_revision=0, comment="Required",
                        prepared_files=(ForbiddenFile(),), **extra)
            for method in (w.transition_review, w.submit_task_result, w.update_draft, w.transition_approval, w.update_task_before_acceptance,
                           w.change_working_status, w.request_task_clarification,
                           w.forward_task_result, w.confirm_task_result, w.answer_task_clarification):
                with self.assertRaises(w.WorkflowRemoved):
                    method(task=task, actor=actor)
            self.assertEqual(0, task.revision)
            self.assertEqual(0, TaskEvent.query.count())
            self.assertEqual(0, TaskComment.query.count())
            self.assertEqual(0, TaskNotification.query.count())

    def test_legacy_department_role_does_not_override_actual_role(self):
        with self.app.app_context():
            employee = self.actor("receiver-colleague")
            employee.department_role = "head"
            head = self.actor("receiver-head")
            head.department_role = "employee"
            db.session.flush()
            task = db.session.get(Task, self.new)
            self.assertFalse(p.can_assign_task(employee, task))
            self.assertTrue(p.can_assign_task(head, task))
