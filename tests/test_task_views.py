"""HTTP task lists, dates, direct workflow controls and history."""
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit
from app.extensions import db
from app.models import Task,TaskEvent,User
from app.audit import record_task_event
from support.task_view_fixtures import ViewCase,Markup,NOW


class TaskViewTests(ViewCase):
    def test_exact_active_and_closed_membership_and_tab_totals(self):
        active=self.card();work=self.card("in_progress");closed=[self.card(s) for s in ("completed","cancelled","rejected")]
        incoming=self.card(sender_department_id=self.other_department_id,recipient_department_id=self.department_id,author_id=self.ids["worker"])
        self.foreign()
        for view in ("list","board"):
            for direction,expected in (("sent",{active,work}),("received",{incoming}),("completed",set(closed))):
                response,ctx=self.render(direction,view=view,scope="sent")
                self.assertEqual(expected,{t.id for t in ctx["tasks"]});self.assertEqual({"sent":2,"received":1,"completed":3},ctx["counts"])
                self.assertEqual({"new","in_progress","completed","cancelled","rejected"},set(ctx["status_labels"]))
                self.assertEqual(["Нашему отделу","Другим отделам","Завершённые"],list(ctx["list_labels"].values()))

    def test_default_and_invalid_view_use_board_in_every_section(self):
        self.card();self.card("completed")
        for direction in ("sent","received","completed"):
            for view in (None,"invalid",""):
                _,ctx=self.render(direction,view=view);self.assertEqual("board",ctx["view"])
            _,ctx=self.render(direction,view="list");self.assertEqual("list",ctx["view"])

    def test_old_filter_parameters_do_not_hide_cards_or_change_counts(self):
        ids=self.many(36)
        for view in ("board","list"):
            _,clean=self.render(view=view)
            _,filtered=self.render(view=view,status="draft",priority="low",overdue="1",q="NEVER MATCH",assignee_id="9999",department="9999")
            self.assertEqual([t.id for t in clean["tasks"]],[t.id for t in filtered["tasks"]]);self.assertEqual(clean["counts"],filtered["counts"])
            self.assertEqual(36,filtered["pagination"].total)

    def test_list_pages_do_not_overlap_and_clamp_invalid_pages(self):
        ids=self.many(36);_,one=self.render(view="list");_,two=self.render(view="list",page=2)
        self.assertEqual((25,11),(len(one["tasks"]),len(two["tasks"])));self.assertTrue({t.id for t in one["tasks"]}.isdisjoint(t.id for t in two["tasks"]))
        self.assertEqual(set(ids),{t.id for t in one["tasks"]+two["tasks"]})
        for page,expected in (("bad",1),(-9,1),(999,2)):
            _,ctx=self.render(view="list",page=page);self.assertEqual(expected,ctx["pagination"].page);self.assertEqual(36,ctx["counts"]["sent"])

    def test_second_list_page_switch_opens_complete_board_and_board_to_list_starts_at_first(self):
        self.many(36);response,_=self.render(view="list",page=2)
        links=Markup(response.get_data(as_text=True)).links
        href=next(a["href"] for a in links if parse_qs(urlsplit(a["href"]).query).get("view")==["board"])
        board=self.client_for().get(href);self.assertEqual(36,len(self.cards(board)))
        href=next(a["href"] for a in Markup(board.get_data(as_text=True)).links if parse_qs(urlsplit(a["href"]).query).get("view")==["list"])
        self.assertEqual(["1"],parse_qs(urlsplit(href).query)["page"])

    def test_overdue_and_remaining_text_only_on_active_tasks(self):
        late=self.card(due_at=NOW-timedelta(hours=2));soon=self.card("in_progress",due_at=NOW+timedelta(hours=3))
        done=self.card("completed",due_at=NOW-timedelta(hours=1));cancel=self.card("cancelled",due_at=NOW-timedelta(days=1))
        with patch("app.tasks._now",return_value=NOW):
            for view in ("list","board"):
                response,ctx=self.render(view=view);self.assertTrue(ctx["deadline_states"][late]["overdue"]);self.assertFalse(ctx["deadline_states"][soon]["overdue"])
                source=response.get_data(as_text=True);self.assertIn("Просрочена на 2 ч.",source);self.assertIn("Осталось 3 ч.",source);self.assertIn("task-card-overdue",source)
                response,ctx=self.render("completed",view=view,scope="sent");self.assertFalse(ctx["deadline_states"][done]["overdue"]);self.assertFalse(ctx["deadline_states"][cancel]["overdue"])
                self.assertIn("Завершена позже срока",response.get_data(as_text=True));self.assertNotIn("task-card-overdue",response.get_data(as_text=True))

    def test_creation_and_current_completion_dates_use_local_timezone(self):
        self.card("completed",created_at=NOW-timedelta(days=6),completed_at=NOW,closed_at=NOW+timedelta(days=1))
        for view in ("list","board"):
            response,_=self.render("completed",view=view,scope="sent");source=response.get_data(as_text=True)
            self.assertIn("Создана: 04.01.2030 15:00",source);self.assertIn("Завершена: 10.01.2030 15:00",source)
            self.assertNotIn("Завершена: 11.01.2030",source)

    def test_unknown_completed_date_does_not_substitute_closed_date(self):
        self.card("completed",completed_at=None,closed_at=NOW)
        for view in ("list","board"):
            response,_=self.render("completed",view=view,scope="sent");self.assertIn("дата и время неизвестны (историческая запись)",response.get_data(as_text=True))
            self.assertNotIn("Завершена: 10.01.2030",response.get_data(as_text=True))

    def test_admin_lists_all_departments_without_creation_or_fake_own_department(self):
        self.card();self.foreign()
        response,ctx=self.render(actor="root",view="list");self.assertEqual(2,len(ctx["tasks"]));self.assertFalse(ctx["may_create"])
        source=response.get_data(as_text=True);self.assertIn("Все отделы · Просмотр администратора",source)
        self.assertNotIn("Нашему отделу",source);self.assertNotIn('href="/tasks/new"',source)
        for ident in [t.id for t in ctx["tasks"]]:self.assertNotIn(f'/tasks/{ident}/complete',{f.get("action") for f in Markup(source).forms})

    def test_assignment_attention_shown_only_to_recipient_heads(self):
        self.card()
        for actor,direction,expected in (("receiver-head","received",True),("receiver-head2","received",True),("worker","received",False),("head","sent",False),("root","sent",False)):
            response,_=self.render(direction,actor);self.assertEqual(expected,"Назначьте исполнителя" in response.get_data(as_text=True))

    def test_work_controls_only_for_executor_and_recipient_heads(self):
        ident=self.card("in_progress")
        for actor in ("employee","head","worker","peer","receiver-head","receiver-head2","root"):
            response=self.client_for(actor).get(f"/tasks/{ident}");self.assertEqual(200,response.status_code)
            forms={f.get("action") for f in Markup(response.get_data(as_text=True)).forms}
            self.assertEqual(actor in ("worker","receiver-head","receiver-head2"),f"/tasks/{ident}/complete" in forms)
        self.assertEqual(404,self.client_for("outsider").get(f"/tasks/{ident}").status_code)

    def test_failed_action_retains_comment_and_current_revision(self):
        ident=self.card("in_progress")
        response=self.client_for("worker").post(f"/tasks/{ident}/complete",data=dict(revision=999,comment="Моё пояснение"))
        self.assertEqual(409,response.status_code);source=response.get_data(as_text=True)
        self.assertIn("Моё пояснение",source);self.assertIn("Обновить карточку",source);self.assertIn('name="revision" value="0"',source);self.assertRegex(source,r'<details open>')

    def test_direct_send_invalid_deadline_retains_values_and_error_label(self):
        client=self.client_for();payload=self.creation_form(client=client,title="Не потерять текст",due_at="")
        response=client.post("/tasks/new",data=payload);self.assertEqual(400,response.status_code)
        source=response.get_data(as_text=True);self.assertIn("Не потерять текст",source);self.assertIn('aria-invalid="true" aria-describedby="due_at-error"',source);self.assertIn('href="#due_at"',source)

    def test_historical_events_preserve_actor_snapshot_and_old_labels(self):
        ident=self.card("in_progress")
        with self.app.app_context():
            actor=db.session.get(User,self.ids["receiver-head"]);actor.full_name="Имя при назначении";db.session.flush()
            record_task_event(task=db.session.get(Task,ident),actor=actor,event_type="assigned",comment="Назначен ответственный",before={"status":"assigned","assignee_ids":None})
            db.session.commit();actor.full_name="Новое имя";actor.role="employee";actor.department_id=self.third_department_id;db.session.commit()
        source=self.client_for().get(f"/tasks/{ident}").get_data(as_text=True).split('class="card task-history"')[1]
        self.assertIn("Имя при назначении · Начальник отдела",source);self.assertNotIn("Новое имя",source)
        self.assertIn("Было: Назначена",source);self.assertIn("Стало: В работе",source)

    def test_unknown_historical_event_and_user_text_are_safe(self):
        ident=self.card(title='<script>alert("title")</script>')
        with self.app.app_context():db.session.add(TaskEvent(task_id=ident,event_type="unknown_legacy",details={"comment":"<script>bad</script>"}));db.session.commit()
        source=self.client_for().get(f"/tasks/{ident}").get_data(as_text=True)
        self.assertIn("Историческое событие",source);self.assertIn("исторический снимок недоступен",source);self.assertNotIn("unknown_legacy",source);self.assertNotIn("<script>",source)

    def test_empty_sections_have_russian_copy_and_no_educational_or_language_controls(self):
        for direction in ("received","sent","completed"):
            response,_=self.render(direction);source=response.get_data(as_text=True)
            self.assertIn("Задач пока нет" if direction!="completed" else "Закрытых задач в выбранном направлении пока нет.",source);self.assertIn('<html lang="ru">',source)
            for forbidden in ('action="/language"','>Студенты<','>Преподаватели<','>Группы<','Проверка знаний'):self.assertNotIn(forbidden,source)
