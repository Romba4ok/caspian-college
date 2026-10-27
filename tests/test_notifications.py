"""Stage-addressed notifications and owner-only signed read snapshots."""
from datetime import datetime, timezone
import re
from unittest.mock import patch
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models import Task, TaskEvent, TaskComment, TaskNotification, User
from app.notifications import (deliver_event_notifications, notification_read_boundary,
    mark_all_notifications_read, notification_text, visible_notifications_query)
from support.portal_fixtures import PortalCase


class NotificationTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.add_member("sender-head-two",role="head")
        self.add_member("sender-peer")
        for name,role,blocked in (("receiver","employee",False),("receiver-peer","employee",False),
            ("receiver-head","head",False),("receiver-head-two","head",False),("blocked-head","head",True)):
            self.add_member(name,role=role,department_id=self.other_department_id,blocked=blocked)
        self.work=self.task(status="in_progress",sent_at=datetime.now(timezone.utc),assignee_id=self.ids["receiver"])

    def login(self,username="employee",password=None,**kwargs):
        self.client=self.app.test_client()
        response=super().login(username,password,**kwargs)
        self.assertEqual(200,response.status_code)
        return response

    def event(self,kind,status="in_progress",actor="receiver",sent=True,before=None,comment="Пояснение",**changes):
        task_id=self.task(**(dict(status=status,sent_at=datetime.now(timezone.utc) if sent else None,
            assignee_id=self.ids["receiver"]) | changes))
        with self.app.app_context():
            task=db.session.get(Task,task_id)
            event=TaskEvent(task_id=task_id,actor_id=self.ids[actor],event_type=kind,
                details={"before":before or {}, "after":{"title":task.title,"status":status},"comment":comment})
            db.session.add(event);db.session.flush()
            delivered=deliver_event_notifications(task,event)
            event_id=event.id
            db.session.commit()
            rows=TaskNotification.query.filter(TaskNotification.deduplication_key.like(f"event:{event_id}:user:%")).all()
            owners={row.user.username for row in rows}
            return event_id,task_id,owners,delivered

    def note(self,owner="employee",*,task_id=None,message="Личное сообщение",ident=None):
        with self.app.app_context():
            n=TaskNotification(user_id=self.ids[owner],task_id=task_id,event_type="synthetic",message=message)
            if ident is not None:n.id=ident
            db.session.add(n);db.session.commit()
            return n.id

    def boundary(self):
        page=self.client.get("/tasks/notifications")
        self.assertEqual(200,page.status_code)
        return re.search(r'name="boundary" value="([^"]+)"',page.get_data(as_text=True)).group(1)

    def is_read(self,ident):
        with self.app.app_context():return db.session.get(TaskNotification,ident).is_read

    def unread_count(self):
        page=self.client.get("/tasks/received").get_data(as_text=True)
        return int(re.search(r'class="notification-count"[^>]*>(\d+)<',page).group(1))

    def test_exact_event_matrix_including_every_review_and_closing_stage(self):
        s={"head","sender-head-two"};r={"receiver-head","receiver-head-two"};a={"employee"};e={"receiver"}
        cases=[
            ("submitted_for_approval","pending_approval","employee",False,s,None),
            ("approval_withdrawn","draft","employee",False,s,None),
            ("approval_returned","draft","head",False,a,None),
            ("sent","awaiting_assignment","head",True,a|r,None),
            ("approved_and_sent","awaiting_assignment","head",True,a|r,None),
            ("assigned","assigned","receiver-head",True,a|e,None),
            ("reassigned","in_progress","receiver-head",True,a|e|{"receiver-peer"},{"assignee_id":self.ids["receiver-peer"]}),
            ("work_started","in_progress","receiver",True,a|r,None),
            ("clarification_requested","awaiting_clarification","receiver",True,a|s,None),
            ("clarification_answered","in_progress","employee",True,e|r,None),
            ("result_submitted","recipient_review","receiver",True,a|r,None),
            ("result_forwarded","sender_acceptance","receiver-head-two",True,a|s,None),
            ("returned_for_revision","in_progress","head",True,a|e|r,None),
            ("completed","completed","head",True,a|e|r,None),
            ("cancelled","cancelled","head",True,a|e|r|{"sender-head-two"},None),
            ("deadline_changed","in_progress","head",True,a|e|r|{"sender-head-two"},None),
            ("cancelled","cancelled","employee",False,s,None),
            ("rejected","rejected","receiver-head-two",True,a|s|e,None),
        ]
        for kind,status,actor,sent,expected,before in cases:
            with self.subTest(kind=kind,status=status,sent=sent):
                _,_,owners,count=self.event(kind,status,actor,sent,before)
                self.assertEqual(expected,owners)
                self.assertEqual(len(expected),count)

    def test_comments_notify_stage_heads_not_whole_departments(self):
        stages=[
            ("draft",False,{"employee","head","sender-head-two"}),
            ("pending_approval",False,{"employee","head","sender-head-two"}),
            ("awaiting_assignment",True,{"employee","receiver","receiver-head","receiver-head-two"}),
            ("assigned",True,{"employee","receiver","receiver-head","receiver-head-two"}),
            ("in_progress",True,{"employee","receiver","receiver-head","receiver-head-two"}),
            ("recipient_review",True,{"employee","receiver","receiver-head","receiver-head-two"}),
            ("awaiting_clarification",True,{"employee","receiver","head","sender-head-two","receiver-head","receiver-head-two"}),
            ("sender_acceptance",True,{"employee","receiver","head","sender-head-two"}),
        ]
        for status,sent,expected in stages:
            _,_,owners,_=self.event("comment_added",status,actor="sender-peer",sent=sent)
            self.assertEqual(expected,owners,status)

    def test_draft_creation_and_plain_edit_do_not_send_department_broadcasts(self):
        self.assertEqual(set(),self.event("created","draft","employee",False)[2])
        self.assertEqual(set(),self.event("edited","draft","employee",False)[2])
        self.login()
        response=self.client.post("/tasks/new",data=self.draft_form())
        self.assertEqual(302,response.status_code)
        ident=int(response.headers["Location"].rsplit("/",1)[1])
        with self.app.app_context():self.assertEqual(0,TaskNotification.query.filter_by(task_id=ident).count())

    def test_real_submission_approval_and_assignment_emit_persisted_event_notifications(self):
        task_id=self.task()
        self.login()
        self.assertEqual(302,self.client.post(f"/tasks/{task_id}/approval/submit",
            data={"revision":"0","comment":"Прошу согласовать"}).status_code)
        self.login("head")
        self.assertEqual(302,self.client.post(f"/tasks/{task_id}/approval/approve",
            data={"revision":"1","comment":"Одобрено"}).status_code)
        self.login("receiver-head")
        self.assertEqual(302,self.client.post(f"/tasks/{task_id}/execution/assign",
            data={"revision":"2","comment":"Назначаю","assignee_id":str(self.ids["receiver"])}).status_code)
        with self.app.app_context():
            events=TaskEvent.query.filter_by(task_id=task_id).order_by(TaskEvent.id).all()
            self.assertEqual(3,len(events))
            for event in events:
                self.assertGreater(TaskNotification.query.filter(TaskNotification.deduplication_key.like(
                    f"event:{event.id}:user:%")).count(),0)
            self.assertEqual(7,TaskNotification.query.filter_by(task_id=task_id).count())

    def test_head_author_gets_confirmation_of_own_send(self):
        task_id=self.task(author_id=self.ids["head"])
        self.login("head")
        self.assertEqual(302,self.client.post(f"/tasks/{task_id}/approval/send",
            data={"revision":"0","comment":"Отправляю"}).status_code)
        with self.app.app_context():
            owners={n.user.username for n in TaskNotification.query.filter_by(task_id=task_id).all()}
            self.assertEqual({"head","receiver-head","receiver-head-two"},owners)

    def test_rejection_always_includes_author_even_if_initiator_and_shows_full_reason(self):
        reason="Причина отказа: "+"Объём работы не соответствует компетенции отдела. "*30
        _,task_id,owners,_=self.event("rejected","rejected","receiver-head",True,
            comment=reason,author_id=self.ids["receiver-head"])
        self.assertEqual({"receiver-head","head","sender-head-two","receiver"},owners)
        self.login("receiver-head")
        page=self.client.get("/tasks/notifications").get_data(as_text=True)
        self.assertIn(reason,page)
        self.assertIn("Задача отклонена получателем",page)

    def test_initiator_excluded_and_overlapping_author_executor_head_deduplicated(self):
        _,_,owners,count=self.event("completed","completed","head",True,
            author_id=self.ids["receiver-head"],assignee_id=self.ids["receiver-head"])
        self.assertEqual({"receiver-head","receiver-head-two"},owners)
        self.assertEqual(2,count)
        self.assertNotIn("head",owners)

    def test_blocked_and_lost_access_recipients_and_non_heads_excluded(self):
        with self.app.app_context():
            author=db.session.get(User,self.ids["employee"]);author.department_id=self.third_department_id
            db.session.get(User,self.ids["receiver"]).is_blocked=True
            db.session.get(User,self.ids["receiver-head-two"]).role="employee"
            db.session.commit()
        _,_,owners,_=self.event("work_started","in_progress","receiver-peer")
        self.assertEqual({"receiver-head"},owners)

    def test_repeat_delivery_and_repeated_post_do_not_duplicate_notes(self):
        event_id,task_id,owners,_=self.event("work_started")
        with self.app.app_context():
            self.assertEqual(0,deliver_event_notifications(db.session.get(Task,task_id),db.session.get(TaskEvent,event_id)))
            db.session.commit()
            self.assertEqual(len(owners),TaskNotification.query.filter_by(task_id=task_id).count())
        self.login("receiver")
        self.assertEqual(302,self.client.post(f"/tasks/{self.work}/review/submit_result",
            data={"revision":"0","comment":"Результат"}).status_code)
        with self.app.app_context():count=TaskNotification.query.count()
        self.assertEqual(409,self.client.post(f"/tasks/{self.work}/review/submit_result",
            data={"revision":"0","comment":"Повтор"}).status_code)
        with self.app.app_context():self.assertEqual(count,TaskNotification.query.count())

    def test_failed_send_does_not_confirm_author_or_notify_recipient(self):
        task_id=self.task(author_id=self.ids["head"])
        self.login("head")
        with patch("app.workflow.deliver_event_notifications",side_effect=SQLAlchemyError("synthetic failure")):
            response=self.client.post(f"/tasks/{task_id}/approval/send",data={"revision":"0","comment":"Отправить"})
        self.assertEqual(500,response.status_code)
        with self.app.app_context():
            self.assertEqual("draft",db.session.get(Task,task_id).status)
            self.assertEqual(0,TaskEvent.query.filter_by(task_id=task_id).count())
            self.assertEqual(0,TaskComment.query.filter_by(task_id=task_id).count())
            self.assertEqual(0,TaskNotification.query.filter_by(task_id=task_id).count())

    def test_replay_does_not_rebroadcast_old_event_to_a_new_head(self):
        event_id,task_id,owners,_=self.event("work_started")
        self.add_member("new-head",role="head",department_id=self.other_department_id)
        with self.app.app_context():
            self.assertEqual(0,deliver_event_notifications(db.session.get(Task,task_id),db.session.get(TaskEvent,event_id)))
            db.session.commit()
            self.assertEqual(owners,{n.user.username for n in TaskNotification.query.filter_by(task_id=task_id).all()})

    def test_delivery_requires_a_persisted_real_event(self):
        with self.app.app_context():
            event=TaskEvent(id=999,task_id=self.work,actor_id=self.ids["receiver"],
                event_type="work_started",details={})
            with self.assertRaises(ValueError):
                deliver_event_notifications(db.session.get(Task,self.work),event)
            self.assertEqual(0,TaskNotification.query.count())

    def test_unread_counter_center_and_one_read_are_private_even_for_admin(self):
        own=self.note(message="OWN-NOTE")
        other=self.note("receiver",message="PRIVATE-OTHER")
        admin=self.note("root",message="ADMIN-ONLY")
        self.login()
        self.assertEqual(1,self.unread_count())
        page=self.client.get("/tasks/notifications").get_data(as_text=True)
        self.assertIn("OWN-NOTE",page);self.assertNotIn("PRIVATE-OTHER",page)
        self.assertEqual(404,self.client.post(f"/tasks/notifications/{other}/read").status_code)
        self.assertEqual(302,self.client.post(f"/tasks/notifications/{own}/read").status_code)
        self.assertEqual(302,self.client.post(f"/tasks/notifications/{own}/read").status_code)
        self.assertEqual(0,self.unread_count())
        self.login("root")
        self.assertEqual(1,self.unread_count())
        self.assertEqual(404,self.client.post(f"/tasks/notifications/{other}/read").status_code)
        self.assertFalse(self.is_read(other));self.assertFalse(self.is_read(admin))

    def test_hidden_draft_notification_never_leaks_to_recipient_center_or_counter(self):
        task_id=self.task()
        ident=self.note("receiver",task_id=task_id,message="SECRET-DRAFT")
        self.login("receiver")
        self.assertEqual(0,self.unread_count())
        self.assertNotIn("SECRET-DRAFT",self.client.get("/tasks/notifications").get_data(as_text=True))
        self.assertEqual(404,self.client.post(f"/tasks/notifications/{ident}/read").status_code)
        self.assertEqual(302,self.client.post("/tasks/notifications/read-all",data={"boundary":self.boundary()}).status_code)
        self.assertFalse(self.is_read(ident))

    def test_mark_all_uses_exact_signed_snapshot_and_keeps_new_lower_id_unread(self):
        existing=self.note(ident=50)
        other=self.note("receiver",ident=60)
        self.login()
        token=self.boundary()
        late=self.note(ident=40,message="Появилось после открытия")
        newer=self.note(ident=70,message="Новое сообщение")
        self.assertEqual(302,self.client.post("/tasks/notifications/read-all",data={"boundary":token}).status_code)
        self.assertTrue(self.is_read(existing))
        for ident in (late,newer,other):self.assertFalse(self.is_read(ident))
        self.assertEqual(2,self.unread_count())
        self.assertEqual(302,self.client.post("/tasks/notifications/read-all",data={"boundary":token}).status_code)
        self.assertFalse(self.is_read(late))

    def test_mark_all_does_not_trust_changed_or_foreign_boundary_tokens(self):
        ident=self.note()
        self.login();token=self.boundary()
        self.assertEqual(400,self.client.post("/tasks/notifications/read-all",data={"boundary":token+"tampered"}).status_code)
        self.login("head")
        self.assertEqual(404,self.client.post("/tasks/notifications/read-all",data={"boundary":token}).status_code)
        self.assertFalse(self.is_read(ident))

    def test_read_all_batches_all_pages_and_never_marks_foreign_owner(self):
        with self.app.app_context():
            db.session.add_all([TaskNotification(user_id=self.ids["employee"],event_type="synthetic",message=f"Сообщение {i}") for i in range(510)])
            db.session.commit()
        other=self.note("receiver")
        self.login()
        page=self.client.get("/tasks/notifications").get_data(as_text=True)
        self.assertIn("page=2",page)
        self.assertEqual(510,self.unread_count())
        self.assertEqual(302,self.client.post("/tasks/notifications/read-all",data={"boundary":self.boundary()}).status_code)
        self.assertEqual(0,self.unread_count());self.assertFalse(self.is_read(other))

    def test_role_and_department_changes_revoke_center_counter_and_old_read_snapshot(self):
        ident=self.note(task_id=self.work,message="OLD-DEPARTMENT")
        self.login();token=self.boundary()
        with self.app.app_context():
            db.session.get(User,self.ids["employee"]).department_id=self.third_department_id
            db.session.commit()
        self.assertEqual(0,self.unread_count())
        self.assertNotIn("OLD-DEPARTMENT",self.client.get("/tasks/notifications").get_data(as_text=True))
        self.assertEqual(404,self.client.post(f"/tasks/notifications/{ident}/read").status_code)
        self.assertEqual(302,self.client.post("/tasks/notifications/read-all",data={"boundary":token}).status_code)
        self.assertFalse(self.is_read(ident))

    def test_notification_reading_does_not_change_task_revision_or_history(self):
        ident=self.note(task_id=self.work)
        with self.app.app_context():
            before=(db.session.get(Task,self.work).revision,TaskComment.query.count(),TaskEvent.query.count())
        self.login()
        self.assertEqual(302,self.client.post(f"/tasks/notifications/{ident}/read").status_code)
        self.assertEqual(302,self.client.post("/tasks/notifications/read-all",data={"boundary":self.boundary()}).status_code)
        with self.app.app_context():
            self.assertEqual(before,(db.session.get(Task,self.work).revision,TaskComment.query.count(),TaskEvent.query.count()))

    def test_read_failure_rolls_back_owner_flags_without_exposing_error(self):
        ident=self.note()
        self.login();token=self.boundary()
        with patch("app.tasks.db.session.commit",side_effect=SQLAlchemyError("synthetic detail")):
            response=self.client.post("/tasks/notifications/read-all",data={"boundary":token})
        self.assertEqual(500,response.status_code)
        self.assertNotIn("synthetic detail",response.get_data(as_text=True))
        self.assertFalse(self.is_read(ident))

    def test_post_only_csrf_protection_and_escaped_content(self):
        ident=self.note(message="<script>notification</script>")
        self.login()
        self.app.config["WTF_CSRF_ENABLED"]=True
        page=self.client.get("/tasks/notifications").get_data(as_text=True)
        self.assertIn("&lt;script&gt;notification&lt;/script&gt;",page)
        self.assertEqual(405,self.client.get("/tasks/notifications/read-all").status_code)
        self.assertEqual(400,self.client.post(f"/tasks/notifications/{ident}/read").status_code)
        self.assertEqual(400,self.client.post("/tasks/notifications/read-all",data={"boundary":self.boundary()}).status_code)
        csrf=re.search(r'name="csrf_token" value="([^"]+)"',page).group(1)
        self.assertEqual(302,self.client.post("/tasks/notifications/read-all",data={
            "csrf_token":csrf,"boundary":self.boundary()}).status_code)
        self.assertTrue(self.is_read(ident))

    def test_unread_filter_and_empty_states_keep_the_header_counter_correct(self):
        old=self.note(message="ALREADY-READ")
        new=self.note(message="UNREAD-MESSAGE")
        self.login()
        self.client.post(f"/tasks/notifications/{old}/read")
        page=self.client.get("/tasks/notifications?unread=1").get_data(as_text=True)
        self.assertIn("UNREAD-MESSAGE",page);self.assertNotIn("ALREADY-READ",page)
        self.client.post(f"/tasks/notifications/{new}/read")
        page=self.client.get("/tasks/notifications?unread=1").get_data(as_text=True)
        self.assertIn("Непрочитанных уведомлений нет",page)
        self.assertEqual(0,self.unread_count())

