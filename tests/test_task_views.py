"""Three private paginated lists and the complete Russian workflow detail."""
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import re
from unittest.mock import patch

from app.extensions import db
from app.models import Department, Task, TaskEvent, User
from app.audit import record_task_event
from support.portal_fixtures import PortalCase

UTC = timezone.utc


class Forms(HTMLParser):
    def __init__(self, source):
        super().__init__(); self.actions=[]; self.feed(source)
    def handle_starttag(self, tag, attrs):
        if tag == "form": self.actions.append(dict(attrs).get("action", ""))


class TaskViewTests(PortalCase):
    def test_switch_from_second_list_page_opens_the_complete_board(self):
        ids=self.task_batch(36,statuses=("awaiting_assignment",))
        self.as_user()
        listing=self.page("/tasks/sent?view=list&page=2")
        self.assertEqual(11,len(self.ids_on(listing)))
        self.assertIn('href="/tasks/sent?view=board"',listing)
        board=self.page("/tasks/sent?view=board")
        self.assertEqual(set(ids),set(self.ids_on(board)))
        self.assertIn('href="/tasks/sent?view=list&amp;page=1"',board)

    def setUp(self):
        super().setUp()
        self.add_member("worker", department_id=self.other_department_id)
        self.add_member("receiver-head", role="head", department_id=self.other_department_id)
        self.add_member("sender-head-two", role="head")
        self.now=datetime(2030,1,10,10,tzinfo=UTC)
        self.clock=patch("app.tasks._now", return_value=self.now); self.clock.start(); self.addCleanup(self.clock.stop)

    def as_user(self, name="employee"):
        self.client=self.app.test_client(); response=self.login(name)
        self.assertEqual(200,response.status_code)

    def page(self, path):
        response=self.client.get(path); self.assertEqual(200,response.status_code)
        return response.get_data(as_text=True)

    def ids_on(self, source):
        return [int(i) for i in re.findall(r'data-task-id="(\d+)"',source)]

    def sent(self, **changes):
        return self.task(**(dict(status="in_progress",sent_at=self.now-timedelta(days=4),
            assignee_id=self.ids["worker"])|changes))

    def incoming(self, **changes):
        return self.sent(**(dict(author_id=self.ids["worker"], sender_department_id=self.other_department_id,
            recipient_department_id=self.department_id,assignee_id=self.ids["employee"])|changes))

    def counts(self, source):
        return [int(i) for i in re.findall(r'class="task-tab-count"[^>]*>(\d+)<',source)]

    def test_three_tabs_exact_membership_and_visible_total_counts(self):
        draft=self.task(); pending=self.task(status="pending_approval")
        outgoing=self.sent(); incoming=self.incoming()
        completed=self.sent(status="completed",completed_at=self.now,closed_at=self.now)
        cancelled=self.incoming(status="cancelled",closed_at=self.now)
        rejected=self.sent(status="rejected",closed_at=self.now)
        private_cancel=self.task(status="cancelled",closed_at=self.now)
        self.task(author_id=self.ids["outsider"],sender_department_id=self.third_department_id,
            recipient_department_id=self.other_department_id)
        self.as_user()
        for path, expected in (("received",{incoming}),("sent",{draft,pending,outgoing}),
            ("completed?scope=received",{cancelled}),("completed?scope=sent",{completed,rejected,private_cancel})):
            source=self.page("/tasks/"+path)
            self.assertEqual(expected,set(self.ids_on(source)))
            self.assertEqual([1,3,4],self.counts(source))
            self.assertEqual(3,source.count('class="task-tab-count"'))

    def test_cancelled_unsent_draft_not_revealed_to_receiver_even_in_counts(self):
        private=self.task(status="cancelled",title="Чужая неотправленная отмена")
        self.as_user("worker")
        for path in ("received","sent","completed"):
            source=self.page("/tasks/"+path)
            self.assertNotIn(private,self.ids_on(source));self.assertNotIn("Чужая неотправленная отмена",source)
            self.assertEqual([0,0,0],self.counts(source))
        self.assertEqual(404,self.client.get(f"/tasks/{private}").status_code)

    def test_admin_views_all_with_both_departments_and_no_creation(self):
        draft=self.task(); sent=self.sent(); closed=self.incoming(status="rejected")
        self.as_user("root")
        self.assertEqual({sent},set(self.ids_on(self.page("/tasks/received"))))
        source=self.page("/tasks/sent?view=list")
        self.assertEqual({draft,sent},set(self.ids_on(source)));self.assertNotIn('href="/tasks/new"',source)
        self.assertIn("Отправитель:",source);self.assertIn("Получатель:",source)
        self.assertEqual([1,2,1],self.counts(source))
        self.assertEqual({closed},set(self.ids_on(self.page("/tasks/completed?scope=sent&view=list"))))

    def test_obsolete_filters_cannot_change_either_list_or_counts(self):
        self.sent(priority="low");self.sent(priority="urgent");self.incoming()
        self.as_user()
        query="?status=completed&priority=low&assignee_id=999&due_from=1990-01-01&due_to=1990-01-02&deadline_state=no_deadline"
        for name in ("received","sent","completed"):
            plain=self.page("/tasks/"+name);filtered=self.page("/tasks/"+name+query)
            self.assertEqual(self.ids_on(plain),self.ids_on(filtered));self.assertEqual(self.counts(plain),self.counts(filtered))
            self.assertNotIn('class="task-filter',filtered);self.assertNotIn('name="status"',filtered)

    def test_open_order_uses_due_then_newer_created_then_id_and_unknown_last(self):
        date=self.now+timedelta(days=1)
        first=self.sent(due_at=date,created_at=self.now-timedelta(days=2))
        second=self.sent(due_at=date,created_at=self.now-timedelta(days=1))
        tie=self.sent(due_at=date,created_at=self.now-timedelta(days=1))
        near=self.sent(due_at=self.now)
        unknown=self.sent(due_at=None,legacy_missing_due=True,is_legacy_dispatched=True)
        self.as_user();self.assertEqual([near,tie,second,first,unknown],self.ids_on(self.page("/tasks/sent")))

    def test_closed_order_uses_closing_date_and_honest_unknown_dates(self):
        old=self.sent(status="completed",completed_at=self.now-timedelta(days=1),closed_at=self.now-timedelta(days=1))
        new=self.sent(status="cancelled",closed_at=self.now)
        unknown=self.sent(status="rejected",closed_at=None)
        self.as_user();source=self.page("/tasks/completed?scope=sent&view=list")
        self.assertEqual([new,old,unknown],self.ids_on(source))
        self.assertIn("дата и время неизвестны (историческая запись)",source)

    def test_pages_have_no_overlap_and_keep_all_tab_counts(self):
        ids=[self.sent(created_at=self.now,due_at=self.now) for _ in range(28)]
        self.as_user();first=self.page("/tasks/sent?view=list");second=self.page("/tasks/sent?view=list&page=2")
        self.assertEqual(list(reversed(ids))[:25],self.ids_on(first))
        self.assertEqual(list(reversed(ids))[25:],self.ids_on(second))
        self.assertEqual([0,28,0],self.counts(second));self.assertIn("Страница 2 из 2",second)
        self.assertNotIn("priority=",second)

    def test_invalid_and_out_of_range_pages_have_safe_navigation(self):
        self.sent();self.as_user()
        self.assertEqual(1,len(self.ids_on(self.page("/tasks/sent?view=list&page=broken"))))
        self.assertEqual(1,len(self.ids_on(self.page("/tasks/sent?view=list&page=-2"))))
        source=self.page("/tasks/sent?view=list&page=999");self.assertEqual(1,len(self.ids_on(source)))
        self.assertIn("Страница 1 из 1",source)

    def test_overdue_only_after_dispatch_and_never_active_after_closing(self):
        due=self.now-timedelta(hours=25,minutes=10)
        active=self.sent(due_at=due);draft=self.task(due_at=due);pending=self.task(due_at=due,status="pending_approval")
        completed=self.sent(due_at=due,status="completed",completed_at=self.now,closed_at=self.now)
        self.as_user()
        self.assertIn("Просрочена на 1 д. 1 ч.",self.page(f"/tasks/{active}"))
        for ident in (draft,pending,completed):
            source=self.page(f"/tasks/{ident}")
            self.assertNotIn('task-card-overdue',source)
            self.assertNotIn('>Просрочена<',source)
        self.assertIn("Завершена позже срока",self.page(f"/tasks/{completed}"))

    def test_remaining_time_and_real_local_creation_send_completion_dates(self):
        ident=self.sent(due_at=self.now+timedelta(hours=2,minutes=15),created_at=self.now-timedelta(days=2))
        self.as_user();source=self.page(f"/tasks/{ident}")
        self.assertIn("Осталось 2 ч. 15 мин.",source)
        self.assertIn("08.01.2030 15:00",source);self.assertIn("06.01.2030 15:00",source)
        with self.app.app_context():
            task=db.session.get(Task,ident);task.status="completed";task.completed_at=self.now;task.closed_at=self.now;db.session.commit()
        source=self.page(f"/tasks/{ident}");self.assertIn("Завершена: 10.01.2030 15:00",source)
        self.assertIn('href="/tasks/completed?scope=sent"',source)

    def test_unknown_historical_due_and_send_are_not_fabricated(self):
        ident=self.sent(due_at=None,legacy_missing_due=True,is_legacy_dispatched=True,sent_at=None)
        self.as_user();source=self.page(f"/tasks/{ident}")
        self.assertIn("Срок неизвестен",source);self.assertIn("точная дата неизвестна",source)
        self.assertNotIn("Просрочена на",source)

    def test_review_attention_for_correct_head_and_stage_without_extra_tab(self):
        for status,who,text in (("pending_approval","head","Требует согласования"),
            ("awaiting_assignment","receiver-head","Назначьте исполнителя"),
            ("recipient_review","receiver-head","Проверьте результат вашего отдела"),
            ("sender_acceptance","head","Ожидает вашей приёмки")):
            with self.subTest(status=status):
                ident=self.sent(status=status,sent_at=None if status=="pending_approval" else self.now,
                    assignee_id=None if status in ("pending_approval","awaiting_assignment") else self.ids["worker"])
                self.as_user(who);path="sent" if who=="head" else "received"
                self.assertIn(text,self.page("/tasks/"+path));self.assertIn(text,self.page(f"/tasks/{ident}"))
        self.as_user();self.assertNotIn('class="task-attention"',self.page("/tasks/sent"))

    def test_archived_department_hides_create_but_keeps_old_task_actions(self):
        ident=self.incoming(status="assigned")
        with self.app.app_context():db.session.get(Department,self.department_id).is_archived=True;db.session.commit()
        self.as_user("head");source=self.page("/tasks/received")
        self.assertNotIn('href="/tasks/new"',source);self.assertIn("Ваш отдел в архиве",source)
        self.assertIn(f"/tasks/{ident}/execution/start",self.page(f"/tasks/{ident}"))

    def test_only_executor_and_recipient_heads_see_work_actions(self):
        ident=self.sent(status="in_progress")
        for who,expected in (("employee",False),("worker",True),("receiver-head",True),("head",False),("root",False)):
            with self.subTest(who=who):
                self.as_user(who);actions=Forms(self.page(f"/tasks/{ident}")).actions
                self.assertEqual(expected,f"/tasks/{ident}/review/submit_result" in actions)
        self.as_user("outsider");self.assertEqual(404,self.client.get(f"/tasks/{ident}").status_code)

    def test_closed_states_have_no_mutation_forms_for_any_participant_or_admin(self):
        for status in ("completed","cancelled","rejected"):
            ident=self.sent(status=status)
            for who in ("employee","worker","head","receiver-head","root"):
                self.as_user(who);source=self.page(f"/tasks/{ident}")
                self.assertIn("Задача закрыта",source)
                self.assertFalse(any(a.startswith(f"/tasks/{ident}/") for a in Forms(source).actions))
                self.assertNotIn(f'href="/tasks/{ident}/edit"',source)

    def test_history_keeps_actor_snapshot_and_all_relevant_changed_values(self):
        ident=self.sent(status="assigned",category="Старая категория")
        with self.app.app_context():
            actor=db.session.get(User,self.ids["receiver-head"])
            actor.full_name="Имя при назначении";db.session.flush()
            record_task_event(task=db.session.get(Task,ident),actor=actor,event_type="assigned",comment="Назначен ответственный",
                before={"status":"awaiting_assignment","assignee_id":None,"category":"Прежняя категория"})
            db.session.commit();actor.full_name="Новое имя";actor.role="employee";actor.department_id=self.third_department_id;db.session.commit()
        self.as_user();source=self.page(f"/tasks/{ident}").split('class="card task-history"')[1]
        self.assertIn("Имя при назначении · Начальник отдела",source);self.assertNotIn("Новое имя",source)
        self.assertIn("Назначен ответственный",source);self.assertIn("Было: Ожидает назначения",source)
        self.assertIn("Стало: Назначена",source);self.assertIn("Прежняя категория",source);self.assertIn("Старая категория",source)
        self.assertIn("имя на момент действия не зафиксировано",source)

    def test_historical_events_have_clear_russian_fallback_and_escape_text(self):
        ident=self.sent(title='<script>alert("title")</script>')
        with self.app.app_context():
            db.session.add(TaskEvent(task_id=ident,event_type="unknown_legacy",details={"comment":"<script>bad</script>"}))
            db.session.commit()
        self.as_user();source=self.page(f"/tasks/{ident}")
        self.assertIn("Историческое событие",source);self.assertIn("исторический снимок недоступен",source)
        self.assertNotIn("unknown_legacy",source);self.assertNotIn("<script>",source);self.assertIn("&lt;script&gt;",source)

    def test_failed_action_retains_comment_current_revision_and_expanded_form(self):
        ident=self.sent(status="assigned");self.as_user("worker")
        response=self.client.post(f"/tasks/{ident}/execution/start",data={"revision":"999","comment":"Моё пояснение"})
        self.assertEqual(409,response.status_code);source=response.get_data(as_text=True)
        self.assertIn("Моё пояснение",source);self.assertIn("Обновить карточку",source)
        self.assertIn('name="revision" value="0"',source);self.assertRegex(source,r'<details open>')

    def test_forms_request_mandatory_comments_and_server_rejects_whitespace(self):
        ident=self.sent(status="assigned");self.as_user("worker")
        source=self.page(f"/tasks/{ident}");self.assertIn('name="comment"',source);self.assertIn('required',source)
        self.assertEqual(400,self.client.post(f"/tasks/{ident}/execution/start",data={"revision":"0","comment":"   "}).status_code)
        with self.app.app_context():self.assertEqual("assigned",db.session.get(Task,ident).status)

    def test_draft_error_preserves_values_and_labels_invalid_field(self):
        self.as_user();payload=self.draft_form(title="Не потерять текст",due_at="")
        response=self.client.post("/tasks/new",data=payload);self.assertEqual(400,response.status_code)
        source=response.get_data(as_text=True)
        self.assertIn("Не потерять текст",source);self.assertIn('aria-invalid="true" aria-describedby="due_at-error"',source)
        self.assertIn('href="#due_at"',source)

    def test_empty_lists_are_russian_without_educational_or_language_controls(self):
        self.as_user()
        for direction in ("received","sent","completed"):
            source=self.page("/tasks/"+direction)
            self.assertIn("Закрытых задач в выбранном направлении пока нет." if direction == "completed" else "Задач пока нет",source);self.assertIn('<html lang="ru">',source)
            for forbidden in ('action="/language"','>Студенты<','>Преподаватели<','>Группы<','Проверка знаний'):
                self.assertNotIn(forbidden,source)
