"""Query/resource parity and capabilities for the new department workflow."""
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import re
import sys
import tempfile
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import select
from werkzeug.exceptions import NotFound
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.extensions import db
from app.models import Department, Task, TaskAttachment, TaskComment, TaskEvent, TaskNotification, User
from app import permissions as p
from app.notifications import visible_notifications_query
from support.portal_fixtures import PortalCase


class TaskPermissionTests(PortalCase):
    def setUp(self):
        super().setUp()
        for name, role, dept in (("sender-colleague", "employee", self.department_id),
            ("sender-head2", "head", self.department_id), ("recipient", "employee", self.other_department_id),
            ("recipient-colleague", "employee", self.other_department_id), ("recipient-head", "head", self.other_department_id),
            ("recipient-head2", "head", self.other_department_id), ("outsider-head", "head", self.third_department_id)):
            self.add_member(name, role=role, department_id=dept)
        self.files = tempfile.TemporaryDirectory(prefix="portal_case_")
        self.app.config["TASK_UPLOAD_DIR"] = self.files.name
        self.draft = self.make()
        self.sent = self.make("awaiting_assignment", sent=True)
        self.draft_resources = self.resources(self.draft)
        self.sent_resources = self.resources(self.sent)

    def tearDown(self):
        super().tearDown()
        self.files.cleanup()

    def make(self, status="draft", *, sent=False, **changes):
        return self.task(status=status, title="PRIVATE-" + uuid4().hex, sent_at=datetime.now(timezone.utc) if sent else None, **changes)

    def resources(self, task_id):
        key = uuid4().hex
        Path(self.files.name, key).write_bytes(b"synthetic private PDF bytes")
        with self.app.app_context():
            comment = TaskComment(task_id=task_id, author_id=self.ids["employee"], body="PRIVATE-COMMENT")
            db.session.add(comment)
            db.session.flush()
            attachment = TaskAttachment(task_id=task_id, comment_id=comment.id, uploaded_by_id=self.ids["employee"],
                original_filename="PRIVATE-FILE.pdf", storage_key=key, content_type="application/pdf", size_bytes=27)
            event = TaskEvent(task_id=task_id, comment_id=comment.id, actor_id=self.ids["employee"], event_type="created", details={"comment":"PRIVATE-HISTORY"})
            notification = TaskNotification(task_id=task_id, user_id=self.ids["recipient"], event_type="private_test", message=f"PRIVATE-NOTIFICATION-{task_id}")
            db.session.add_all([attachment, event, notification])
            db.session.commit()
            return dict(comment=comment.id, attachment=attachment.id, event=event.id, notification=notification.id)

    def member(self, name):
        return db.session.get(User, self.ids[name], populate_existing=True)

    def client_for(self, name):
        client = self.app.test_client()
        self.assertEqual(200, self.login(name, client=client).status_code)
        return client

    def unread(self, client):
        page = client.get("/tasks/received").get_data(as_text=True)
        return int(re.search(r'class="notification-count"[^>]*>(\d+)<', page).group(1))

    def test_python_and_sql_visibility_match_for_all_roles_and_markers(self):
        self.make("pending_approval")
        self.make("cancelled")
        self.make("completed", sent=True)
        self.make("rejected", sent=True)
        self.make("awaiting_assignment", is_legacy_dispatched=True)
        self.make("draft", sent=True, is_legacy_dispatched=True)
        self.make("pending_approval", sent=True)
        with self.app.app_context():
            tasks = Task.query.order_by(Task.id).all()
            for name in self.ids:
                user = self.member(name)
                expected = {t.id for t in tasks if p.can_access_task(user,t)}
                actual = set(db.session.execute(select(Task.id).where(p.task_visibility_clause(user))).scalars())
                self.assertEqual(expected, actual, name)
            blocked = self.member("recipient")
            blocked.is_blocked = True
            self.assertFalse(any(p.can_access_task(blocked,t) for t in tasks))
            self.assertEqual([], db.session.execute(select(Task.id).where(p.task_visibility_clause(blocked))).all())
            self.assertFalse(p.can_access_task(None, tasks[0]))

    def test_before_dispatch_every_related_resource_is_hidden_from_recipient_and_third_department(self):
        for name in ("employee", "sender-colleague", "head", "sender-head2", "root"):
            with self.app.app_context():
                user = self.member(name)
                self.assertEqual(self.draft, p.get_visible_task_or_404(self.draft,user).id)
                self.assertEqual(self.draft_resources["comment"],p.get_visible_comment_or_404(self.draft_resources["comment"],user).id)
                self.assertEqual(self.draft_resources["event"],p.get_visible_event_or_404(self.draft_resources["event"],user).id)
                self.assertEqual(self.draft_resources["attachment"],p.get_visible_attachment_or_404(self.draft_resources["attachment"],user).id)
        for name in ("recipient", "recipient-head", "recipient-head2", "outsider", "outsider-head"):
            with self.app.app_context():
                user = self.member(name)
                for getter,key in ((p.get_visible_comment_or_404,"comment"),(p.get_visible_event_or_404,"event"),
                    (p.get_visible_attachment_or_404,"attachment"),(p.get_visible_notification_or_404,"notification")):
                    with self.assertRaises(NotFound): getter(self.draft_resources[key],user)
            client = self.client_for(name)
            missing = client.get("/tasks/999999").data
            response = client.get(f"/tasks/{self.draft}")
            self.assertEqual(404,response.status_code)
            self.assertEqual(missing,response.data)
            download = client.get(f'/tasks/attachments/{self.draft_resources["attachment"]}/download')
            self.assertEqual(404,download.status_code)
            self.assertNotIn("Content-Disposition",download.headers)

    def test_sent_task_resources_are_visible_to_both_departments_and_admin(self):
        for name in ("employee","sender-colleague","head","recipient","recipient-colleague","recipient-head","recipient-head2","root"):
            client = self.client_for(name)
            self.assertEqual(200,client.get(f"/tasks/{self.sent}").status_code)
            response = client.get(f'/tasks/attachments/{self.sent_resources["attachment"]}/download')
            self.assertEqual(200,response.status_code)
            self.assertEqual(b"synthetic private PDF bytes",response.data)
            self.assertIn("private, no-store",response.headers["Cache-Control"])
            self.assertEqual("nosniff",response.headers["X-Content-Type-Options"])
            response.close()
        self.assertEqual(404,self.client_for("outsider").get(f"/tasks/{self.sent}").status_code)

    def test_pending_and_cancelled_unsent_do_not_leak_through_lists_or_filters(self):
        ids = [self.draft,self.make("pending_approval"),self.make("cancelled")]
        with self.app.app_context(): titles=[db.session.get(Task,tid).title for tid in ids]
        for name in ("recipient","recipient-head","outsider"):
            client=self.client_for(name)
            for url in ("/tasks/received","/tasks/sent","/tasks/completed","/tasks/completed?status=cancelled",
                "/tasks/received?status=draft","/tasks/received?assignee_id=unassigned",
                "/tasks/completed?scope=sent","/tasks/completed?scope=received"):
                page=client.get(url).get_data(as_text=True)
                for title in titles:self.assertNotIn(title,page)
            self.assertNotIn(f"PRIVATE-NOTIFICATION-{self.draft}",client.get("/tasks/notifications").get_data(as_text=True))
        sender=self.client_for("sender-colleague")
        page=sender.get("/tasks/sent").get_data(as_text=True)
        for title in titles[:2]:self.assertIn(title,page)
        self.assertNotIn(titles[2],page)
        completed=sender.get("/tasks/completed?scope=sent").get_data(as_text=True)
        self.assertIn(titles[2],completed)
        for title in titles[:2]:self.assertNotIn(title,completed)

    def test_historical_send_without_timestamp_counts_but_status_alone_does_not(self):
        historical=self.make("awaiting_assignment",is_legacy_dispatched=True)
        no_marker=self.make("awaiting_assignment")
        forged_draft=self.make("draft",sent=True,is_legacy_dispatched=True)
        client=self.client_for("recipient")
        self.assertEqual(200,client.get(f"/tasks/{historical}").status_code)
        for tid in (no_marker,forged_draft):self.assertEqual(404,client.get(f"/tasks/{tid}").status_code)

    def test_admin_has_no_task_business_capabilities(self):
        work=self.make("in_progress",sent=True,assignee_id=self.ids["recipient"])
        with self.app.app_context():
            admin=self.member("root")
            methods=[p.can_claim_task,p.can_assign_task,p.can_reassign_task,p.can_edit_draft,p.can_submit_for_approval,
                p.can_withdraw_approval,p.can_approve_task,p.can_send_task,p.can_change_working_status,p.can_submit_result,
                p.can_review_recipient_result,p.can_review_result,p.can_return_result,p.can_cancel_task,p.can_reject_task,
                p.can_change_deadline,p.can_contribute_task,p.can_answer_clarification]
            self.assertFalse(p.can_create_task(admin))
            for tid in (self.draft,self.sent,work):
                task=db.session.get(Task,tid)
                for method in methods:self.assertFalse(method(admin,task),method.__name__)

    def test_draft_approval_rights_and_archived_source_are_role_specific(self):
        pending=self.make("pending_approval")
        own_head=self.make(author_id=self.ids["head"])
        with self.app.app_context():
            draft=db.session.get(Task,self.draft); waiting=db.session.get(Task,pending)
            author=self.member("employee"); chief=self.member("head"); chief2=self.member("sender-head2")
            self.assertTrue(p.can_edit_draft(author,draft));self.assertTrue(p.can_submit_for_approval(author,draft))
            self.assertFalse(p.can_edit_draft(chief,draft));self.assertFalse(p.can_send_task(chief,draft))
            self.assertTrue(p.can_send_task(chief,db.session.get(Task,own_head)))
            self.assertTrue(p.can_withdraw_approval(author,waiting))
            for head in (chief,chief2):self.assertTrue(p.can_approve_task(head,waiting))
            db.session.get(Department,self.other_department_id).is_archived=True
            self.assertFalse(p.can_submit_for_approval(author,draft))
            self.assertTrue(p.can_return_approval(chief,waiting))
            db.session.get(Department,self.department_id).is_archived=True
            for member in (author,chief):
                self.assertFalse(p.can_create_task(member));self.assertFalse(p.can_contribute_task(member,draft))
                self.assertTrue(p.can_access_task(member,draft));self.assertTrue(p.can_cancel_task(member,draft))
            self.assertFalse(p.can_withdraw_approval(author,waiting))

    def test_assign_work_and_reassign_require_current_role_not_legacy_department_role(self):
        work=self.make("in_progress",sent=True,assignee_id=self.ids["recipient"])
        final_review=self.make("sender_acceptance",sent=True,assignee_id=self.ids["recipient"])
        with self.app.app_context():
            incoming=db.session.get(Task,self.sent);working=db.session.get(Task,work)
            for name in ("recipient-head","recipient-head2"):
                head=self.member(name)
                self.assertTrue(p.can_assign_task(head,incoming));self.assertTrue(p.can_reassign_task(head,working))
                self.assertTrue(p.can_change_working_status(head,working))
                self.assertFalse(p.can_reassign_task(head,db.session.get(Task,final_review)))
            employee=self.member("recipient-colleague");employee.department_role="head"
            self.assertFalse(p.can_assign_task(employee,incoming));self.assertFalse(p.can_reassign_task(employee,working))
            self.assertFalse(p.can_change_working_status(employee,working))
            self.assertTrue(p.can_change_working_status(self.member("recipient"),working))
            for name in self.ids:self.assertFalse(p.can_claim_task(self.member(name),incoming))

    def test_two_review_stages_cannot_be_confused_or_replaced_by_working_status(self):
        internal=self.make("recipient_review",sent=True,assignee_id=self.ids["recipient"])
        external=self.make("sender_acceptance",sent=True,assignee_id=self.ids["recipient"])
        with self.app.app_context():
            inside,outside=db.session.get(Task,internal),db.session.get(Task,external)
            for name in ("recipient-head","recipient-head2"):
                self.assertTrue(p.can_review_recipient_result(self.member(name),inside))
                self.assertFalse(p.can_review_result(self.member(name),outside))
                self.assertFalse(p.can_change_working_status(self.member(name),inside))
            for name in ("head","sender-head2"):
                self.assertFalse(p.can_review_recipient_result(self.member(name),inside))
                self.assertTrue(p.can_review_result(self.member(name),outside))
            self.assertFalse(p.can_review_result(self.member("employee"),outside))

    def test_deadline_cancel_and_rejection_rules(self):
        work=self.make("in_progress",sent=True,assignee_id=self.ids["recipient"])
        with self.app.app_context():
            draft,incoming,working=(db.session.get(Task,i) for i in (self.draft,self.sent,work))
            author,head,receiver=self.member("employee"),self.member("head"),self.member("recipient-head")
            self.assertTrue(p.can_change_deadline(author,draft));self.assertFalse(p.can_change_deadline(head,draft))
            self.assertFalse(p.can_cancel_task(author,incoming));self.assertTrue(p.can_cancel_task(head,incoming))
            self.assertFalse(p.can_change_deadline(author,incoming));self.assertTrue(p.can_change_deadline(head,incoming))
            self.assertTrue(p.can_reject_task(receiver,incoming));self.assertFalse(p.can_reject_task(receiver,working))
            self.assertFalse(p.can_cancel_task(receiver,incoming))

    def test_clarification_and_contribution_rules_include_archived_sent_departments(self):
        task_id=self.make("awaiting_clarification",sent=True,assignee_id=self.ids["recipient"])
        with self.app.app_context():
            task=db.session.get(Task,task_id)
            for did in (self.department_id,self.other_department_id):db.session.get(Department,did).is_archived=True
            for name in ("employee","sender-colleague","head"):
                self.assertTrue(p.can_answer_clarification(self.member(name),task))
            for name in ("employee","head","recipient","recipient-head"):
                self.assertTrue(p.can_contribute_task(self.member(name),task))
            self.assertFalse(p.can_answer_clarification(self.member("recipient-head"),task))
            self.assertFalse(p.can_change_working_status(self.member("recipient-head"),task))

    def test_legacy_missing_deadline_allows_read_comment_cancel_and_deadline_repair_only(self):
        tid=self.make("awaiting_assignment",is_legacy_dispatched=True,legacy_missing_due=True,due_at=None)
        with self.app.app_context():
            task=db.session.get(Task,tid);recipient=self.member("recipient-head");sender=self.member("head")
            self.assertTrue(p.can_access_task(recipient,task));self.assertTrue(p.can_contribute_task(recipient,task))
            self.assertFalse(p.can_assign_task(recipient,task));self.assertFalse(p.can_reject_task(recipient,task))
            self.assertTrue(p.can_change_deadline(sender,task));self.assertTrue(p.can_cancel_task(sender,task))

    def test_unavailable_assignee_blocks_work_but_head_can_reassign(self):
        tid=self.make("in_progress",sent=True,assignee_id=self.ids["recipient"])
        with self.app.app_context():
            task=db.session.get(Task,tid);head=self.member("recipient-head");assignee=self.member("recipient")
            assignee.is_blocked=True
            self.assertFalse(p.can_change_working_status(head,task));self.assertTrue(p.can_reassign_task(head,task))
            assignee.is_blocked=False;assignee.department_id=self.third_department_id
            self.assertFalse(p.can_change_working_status(head,task))
            task.assignee_id=None;task.assignee=None
            self.assertFalse(p.can_change_working_status(head,task));self.assertTrue(p.can_reassign_task(head,task))

    def test_terminal_states_are_read_only_for_every_participant(self):
        for status in p.TERMINAL:
            tid=self.make(status,sent=True,assignee_id=self.ids["recipient"])
            with self.app.app_context():
                task=db.session.get(Task,tid)
                for name in ("employee","head","recipient","recipient-head","root"):
                    user=self.member(name)
                    self.assertTrue(p.can_access_task(user,task))
                    for method in (p.can_cancel_task,p.can_reassign_task,p.can_change_deadline,p.can_change_working_status,
                        p.can_review_result,p.can_review_recipient_result,p.can_contribute_task,p.can_reject_task):
                        self.assertFalse(method(user,task),(status,name,method.__name__))

    def test_notifications_and_unread_count_are_owner_only_even_for_admin(self):
        with self.app.app_context():
            db.session.add(TaskNotification(task_id=self.draft,user_id=self.ids["root"],event_type="private_test",message="ADMIN-OWN"))
            db.session.add(TaskNotification(task_id=self.draft,user_id=self.ids["employee"],event_type="private_test",message="SENDER-OWN"))
            db.session.commit()
            for name in ("root","head","employee"):
                with self.assertRaises(NotFound):p.get_visible_notification_or_404(self.sent_resources["notification"],self.member(name))
        recipient=self.client_for("recipient")
        self.assertEqual(1,self.unread(recipient))
        self.assertEqual(404,recipient.post(f'/tasks/notifications/{self.draft_resources["notification"]}/read').status_code)
        admin=self.client_for("root")
        self.assertEqual(1,self.unread(admin))
        page=admin.get("/tasks/notifications").get_data(as_text=True)
        self.assertIn("ADMIN-OWN",page);self.assertNotIn("SENDER-OWN",page);self.assertNotIn("PRIVATE-NOTIFICATION",page)
        self.assertEqual(404,admin.post(f'/tasks/notifications/{self.sent_resources["notification"]}/read').status_code)
        self.assertEqual(302,recipient.post(f'/tasks/notifications/{self.sent_resources["notification"]}/read').status_code)
        self.assertEqual(0,self.unread(recipient))

    def test_session_department_and_role_changes_revoke_data_immediately(self):
        recipient=self.client_for("recipient")
        self.assertEqual(200,recipient.get(f"/tasks/{self.sent}").status_code)
        self.login()
        self.client.post(f'/admin/users/{self.ids["recipient"]}/edit',data=self.form("recipient",role="head",department_id=str(self.third_department_id),password=""))
        self.assertEqual(404,recipient.get(f"/tasks/{self.sent}").status_code)
        self.assertEqual(0,self.unread(recipient))
        self.assertEqual(404,recipient.post(f'/tasks/notifications/{self.sent_resources["notification"]}/read').status_code)
        with self.app.app_context():
            user=self.member("recipient")
            self.assertFalse(p.can_assign_task(user,db.session.get(Task,self.sent)))
            self.assertEqual([],visible_notifications_query(user).all())

    def test_blocked_session_cannot_read_file_task_or_notification(self):
        client=self.client_for("recipient")
        self.login();self.client.post(f'/admin/users/{self.ids["recipient"]}/block')
        for url in (f"/tasks/{self.sent}",f'/tasks/attachments/{self.sent_resources["attachment"]}/download',"/tasks/notifications"):
            self.assertEqual(302,client.get(url).status_code)
        with self.app.app_context():
            user=self.member("recipient")
            with self.assertRaises(NotFound):p.get_visible_task_or_404(self.sent,user)
            self.assertEqual([],visible_notifications_query(user).all())

    def test_generic_notifications_are_still_personal(self):
        with self.app.app_context():
            db.session.add(TaskNotification(user_id=self.ids["recipient"],event_type="system",message="GENERIC-OWN"))
            db.session.commit()
        self.assertIn("GENERIC-OWN",self.client_for("recipient").get("/tasks/notifications").get_data(as_text=True))
        self.assertNotIn("GENERIC-OWN",self.client_for("root").get("/tasks/notifications").get_data(as_text=True))

    def test_legacy_mutation_endpoints_are_closed_without_writing_files_or_database(self):
        with self.app.app_context():
            before={m.__tablename__:db.session.execute(select(m.__table__).order_by(m.id)).all() for m in
                (Task,TaskComment,TaskAttachment,TaskEvent,TaskNotification)}
        for name in ("employee","head","recipient","recipient-head","root","outsider"):
            client=self.client_for(name)
            expected=404 if name=="outsider" else 403
            for action in ("claim","comments","edit"):
                response=client.post(f"/tasks/{self.sent}/{action}",data={"status":"completed","comment":"Bypass","reason":"Bypass","due_at":"bad"})
                self.assertEqual(expected,response.status_code,(name,action))
            # T007 enabled these endpoints, but an unversioned/forged old request
            # still cannot mutate anything. Keep the original no-write assertion.
            for action in ("reassign", "status", "clarification", "confirm", "return", "cancel", "deadline"):
                response = client.post(f"/tasks/{self.sent}/{action}", data={
                    "status": "completed", "comment": "Bypass", "assignee_id": self.ids["recipient"]})
                self.assertIn(response.status_code, (expected,) if expected == 404 or name == "root" else (400, 403))
            with patch("app.tasks.save_upload") as save:
                self.assertEqual(expected,client.post(f"/tasks/{self.sent}/attachments",data={"attachment":(BytesIO(b"unsafe"),"file.pdf")}).status_code)
                save.assert_not_called()
        with self.app.app_context():
            after={m.__tablename__:db.session.execute(select(m.__table__).order_by(m.id)).all() for m in
                (Task,TaskComment,TaskAttachment,TaskEvent,TaskNotification)}
        self.assertEqual(before,after)

    def test_bad_identifiers_do_not_raise_database_errors(self):
        client=self.client_for("recipient")
        for ident in ("0","999999999999999999999999999999","missing"):
            self.assertEqual(404,client.get(f"/tasks/{ident}").status_code)
        with self.app.app_context():
            for ident in (True,None,"bad",10**100):
                with self.assertRaises(NotFound):p.get_visible_task_or_404(ident,self.member("root"))
