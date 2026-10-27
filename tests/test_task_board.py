"""Complete private boards and actions reuse the existing workflow API."""
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
import json
import re
from uuid import uuid4
from app.extensions import db
from app.models import Department, Task, TaskAttachment, TaskComment, TaskEvent, TaskNotification, User
from support.portal_fixtures import PortalCase


class BoardMarkup(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.columns = {}; self.current = None; self.root_depth = 0
        self.dialog_inside_root = False; self.dialog_count = 0; self.counts = {}; self.badges = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("role") == "img" and "aria-label" in attrs:
            self.badges.append(attrs["aria-label"])
        if tag == "div":
            if "data-board-root" in attrs:
                self.root_depth = 1; self.counts = json.loads(attrs["data-counts"])
            elif self.root_depth: self.root_depth += 1
        if tag == "section" and "data-group" in attrs:
            self.current = attrs["data-group"]
            self.columns[self.current] = dict(statuses=json.loads(attrs["data-statuses"]), ids=[])
        if tag == "article" and "data-task-id" in attrs and self.current:
            self.columns[self.current]["ids"].append(int(attrs["data-task-id"]))
        if tag == "dialog":
            self.dialog_count += 1; self.dialog_inside_root |= self.root_depth > 0

    def handle_endtag(self, tag):
        if tag == "div" and self.root_depth: self.root_depth -= 1
        if tag == "section": self.current = None


class TaskBoardTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.add_member("worker",department_id=self.other_department_id)
        self.add_member("receiver-head",role="head",department_id=self.other_department_id)
        self.add_member("blocked-worker",department_id=self.other_department_id,blocked=True)

    def as_user(self,who="employee"):
        self.client=self.app.test_client();self.assertEqual(200,self.login(who).status_code)

    def task_sent(self,**changes):
        return self.task(**(dict(status="in_progress",sent_at=datetime.now(timezone.utc),assignee_id=self.ids["worker"])|changes))

    def page(self,direction="sent",query="view=board"):
        response=self.client.get(f"/tasks/{direction}?{query}");self.assertEqual(200,response.status_code)
        return response.get_data(as_text=True)

    def cards(self,source):
        return {d["id"]:d for d in [json.loads(unescape(value)) for value in re.findall(r'data-board-task="([^\"]+)"',source)]}

    def ids_on(self,source):
        return {int(i) for i in re.findall(r'data-task-id="(\d+)"',source)}

    def actions(self,ident,who):
        self.as_user(who);direction="received" if who in ("worker","receiver-head","blocked-worker") else "sent"
        return {a["key"]:a for a in self.cards(self.page(direction))[ident]["actions"]}

    def test_small_board_and_list_share_visible_tasks_and_totals_in_both_directions(self):
        self.task();self.task(status="pending_approval");self.task_sent()
        self.task_sent(author_id=self.ids["worker"],sender_department_id=self.other_department_id,recipient_department_id=self.department_id)
        self.as_user()
        for direction in ("received","sent"):
            board=self.page(direction);listing=self.page(direction,"view=list")
            self.assertEqual(self.ids_on(listing),self.ids_on(board))
            counts=lambda s:re.findall(r'class="task-tab-count"[^>]*>(\d+)<',s)
            self.assertEqual(counts(listing),counts(board));self.assertIn('class="task-board"',board)

    def test_board_ignores_list_page_boundary_and_keeps_all_tasks(self):
        for i in range(28):self.task_sent(title=f"Поручение {i:02}")
        self.as_user();board=self.page(query="view=board&page=2");listing=self.page(query="view=list&page=2")
        self.assertEqual(3,len(self.ids_on(listing)));self.assertEqual(28,len(self.ids_on(board)))
        self.assertLess(self.ids_on(listing),self.ids_on(board))
        self.assertIn("Страница 2 из 2",listing)
        self.assertNotIn('aria-label="Страницы задач"',board)
        self.assertEqual(self.ids_on(board),self.ids_on(self.page(query="view=board&page=999")))

    def test_five_group_columns_exclude_terminal_and_received_presend_states(self):
        self.as_user()
        sent=self.page();received=self.page("received")
        states=lambda s:{status for column in BoardMarkup(s).columns.values() for status in column["statuses"]}
        self.assertEqual(5,len(BoardMarkup(sent).columns));self.assertEqual(5,len(BoardMarkup(received).columns))
        self.assertEqual(8,len(states(sent)));self.assertEqual(6,len(states(received)))
        self.assertTrue({"draft","pending_approval"}.isdisjoint(states(received)))
        for s in (sent,received):self.assertTrue({"completed","cancelled","rejected"}.isdisjoint(states(s)))

    def test_received_36_cards_render_once_in_their_visual_group(self):
        statuses=("awaiting_assignment","assigned","in_progress","awaiting_clarification","recipient_review","sender_acceptance")
        ids=self.task_batch(36,statuses=statuses)
        self.as_user("receiver-head");source=self.page("received");markup=BoardMarkup(source)
        self.assertEqual(["to_do","working","clarification","recipient_review","sender_acceptance"],list(markup.columns))
        expected={"to_do":12,"working":6,"clarification":6,"recipient_review":6,"sender_acceptance":6}
        self.assertEqual(expected,{key:len(value["ids"]) for key,value in markup.columns.items()})
        self.assertEqual(36,markup.counts["received"])
        self.assertEqual(set(ids),self.ids_on(source))
        self.assertEqual(36,sum(len(column["ids"]) for column in markup.columns.values()))
        for key,column in markup.columns.items():
            for ident in column["ids"]:
                self.assertEqual(key,self.cards(source)[ident]["group"])
                self.assertIn(self.cards(source)[ident]["status"],column["statuses"])
        self.assertNotIn('aria-label="Страницы задач"',source)

    def test_sent_groups_keep_exact_status_on_each_card(self):
        statuses=("draft","pending_approval","awaiting_assignment","assigned","in_progress","awaiting_clarification","recipient_review","sender_acceptance")
        ids=self.task_batch(8,statuses=statuses)
        self.as_user();source=self.page();markup=BoardMarkup(source);cards=self.cards(source)
        self.assertEqual(["drafts","approval","dispatched","working","review"],list(markup.columns))
        self.assertEqual(ids[2:4],sorted(markup.columns["dispatched"]["ids"]))
        self.assertEqual(ids[4:6],sorted(markup.columns["working"]["ids"]))
        self.assertEqual(ids[6:8],sorted(markup.columns["review"]["ids"]))
        self.assertEqual(list(statuses),[cards[ident]["status"] for ident in ids])

    def test_counts_are_visible_and_action_dialog_is_outside_refresh_fragment(self):
        ident=self.task_sent()
        with self.app.app_context():
            comment=TaskComment(task_id=ident,author_id=self.ids["employee"],body="Файл к обсуждению")
            db.session.add(comment);db.session.flush()
            db.session.add(TaskAttachment(task_id=ident,uploaded_by_id=self.ids["employee"],comment_id=comment.id,
                original_filename="Пример.pdf",storage_key=uuid4().hex,content_type="application/pdf",size_bytes=20))
            db.session.commit()
        self.as_user();source=self.page();markup=BoardMarkup(source)
        self.assertIn('aria-label="Комментариев: 1"',source)
        self.assertIn('aria-label="Вложений: 1"',source)
        self.assertEqual({"Комментариев: 1","Вложений: 1"},set(markup.badges))
        self.assertEqual(1,markup.dialog_count);self.assertFalse(markup.dialog_inside_root)
        self.assertEqual(1,source.count('id="board-action-form"'))

    def test_assignment_in_same_group_keeps_an_explicit_action(self):
        ident=self.task_sent(status="awaiting_assignment",assignee_id=None)
        self.as_user("receiver-head");source=self.page("received");markup=BoardMarkup(source)
        self.assertIn(ident,markup.columns["to_do"]["ids"])
        card=self.cards(source)[ident]
        self.assertEqual("to_do",card["group"])
        self.assertEqual("assigned",next(a for a in card["actions"] if a["key"]=="assign")["target"])
        self.assertIn('data-board-action href=',source)

    def test_terminal_view_has_no_board_or_switch(self):
        closed=self.task_sent(status="completed");self.as_user()
        page=self.page("completed",query="view=board&scope=sent");self.assertIn(closed,self.ids_on(page))
        self.assertNotIn('class="task-board"',page);self.assertNotIn('aria-label="Вид задач"',page)

    def test_private_drafts_cancelled_and_other_department_data_not_in_board_json(self):
        private=self.task(title="Секретный черновик")
        closed=self.task(status="cancelled",title="Секретная отмена")
        self.as_user("worker")
        for direction in ("received","sent"):
            source=self.page(direction);self.assertNotIn(private,self.cards(source));self.assertNotIn(closed,self.cards(source))
            self.assertNotIn("Секретный",source)
        self.as_user("outsider");self.assertEqual({},self.cards(self.page()))

    def test_draft_author_can_submit_but_cannot_skip_approval(self):
        ident=self.task();actions=self.actions(ident,"employee")
        self.assertEqual({"submit","cancel"},set(actions))
        self.assertEqual("pending_approval",actions["submit"]["target"])
        self.assertEqual(403,self.client.post(f"/tasks/{ident}/approval/send",data={"revision":0,"comment":"Обход"}).status_code)

    def test_head_own_draft_sends_and_pending_draft_approves(self):
        own=self.task(author_id=self.ids["head"]);pending=self.task(status="pending_approval")
        self.assertEqual({"send","cancel"},set(self.actions(own,"head")))
        actions=self.actions(pending,"head");self.assertEqual({"return_draft","approve","cancel"},set(actions))
        self.assertEqual("awaiting_assignment",actions["approve"]["target"])

    def test_recipient_head_gets_assign_with_only_active_local_candidates(self):
        ident=self.task_sent(status="awaiting_assignment",assignee_id=None)
        actions=self.actions(ident,"receiver-head");self.assertEqual({"assign","reject"},set(actions))
        self.assertTrue(actions["assign"]["assignee"]);self.assertEqual("assigned",actions["assign"]["target"])
        card=self.cards(self.page("received"))[ident]
        self.assertEqual({self.ids["worker"],self.ids["receiver-head"]},{u["id"] for u in card["candidates"]})
        self.assertEqual({},self.actions(ident,"worker"))

    def test_executor_and_heads_only_have_their_permitted_stage_targets(self):
        ident=self.task_sent(status="assigned")
        self.assertEqual({"start"},set(self.actions(ident,"worker")))
        self.assertEqual("in_progress",self.actions(ident,"worker")["start"]["target"])
        self.assertEqual({"cancel","deadline"},set(self.actions(ident,"head")))
        self.assertEqual({},self.actions(ident,"employee"))
        with self.app.app_context():db.session.get(Task,ident).status="in_progress";db.session.commit()
        self.assertEqual({"request_clarification","submit_result"},set(self.actions(ident,"worker")))
        self.assertEqual("recipient_review",self.actions(ident,"worker")["submit_result"]["target"])

    def test_both_review_stages_and_terminal_buttons_derive_from_permissions(self):
        ident=self.task_sent(status="recipient_review")
        actions=self.actions(ident,"receiver-head")
        self.assertEqual("sender_acceptance",actions["forward_result"]["target"])
        self.assertEqual("in_progress",actions["return_result"]["target"])
        self.assertNotIn("complete",actions)
        with self.app.app_context():db.session.get(Task,ident).status="sender_acceptance";db.session.commit()
        actions=self.actions(ident,"head")
        self.assertTrue(actions["complete"]["terminal"]);self.assertTrue(actions["cancel"]["terminal"])
        self.assertEqual("in_progress",actions["return_result"]["target"])
        self.assertEqual({},self.actions(ident,"receiver-head"))

    def test_admin_board_is_read_only_even_when_query_or_payload_is_forged(self):
        ident=self.task_sent(status="assigned");actions=self.actions(ident,"root");self.assertEqual({},actions)
        source=self.page();self.assertNotIn('data-board-action href=',source)
        self.assertEqual(403,self.client.post(f"/tasks/{ident}/execution/start",data={"revision":0,"comment":"Подмена"}).status_code)

    def test_archived_department_keeps_old_actions_but_cannot_send_draft(self):
        draft=self.task(author_id=self.ids["head"]);sent=self.task_sent(status="assigned")
        with self.app.app_context():
            db.session.get(Department,self.department_id).is_archived=True;db.session.commit()
        self.assertEqual({"cancel"},set(self.actions(draft,"head")))
        self.assertEqual({"cancel","deadline"},set(self.actions(sent,"head")))

    def test_view_switch_does_not_change_tasks_history_or_notifications(self):
        ident=self.task();self.as_user()
        self.page();self.page(query="view=list");self.page(query="view=invalid")
        with self.app.app_context():
            self.assertEqual("draft",db.session.get(Task,ident).status)
            self.assertEqual(0,TaskEvent.query.count());self.assertEqual(0,TaskComment.query.count());self.assertEqual(0,TaskNotification.query.count())

    def test_board_form_uses_existing_csrf_revision_and_required_comment_fields(self):
        self.task_sent();self.as_user();source=self.page()
        for value in ('name="csrf_token"','name="revision"','name="comment"','id="board-comment"','id="board-cancel"'):
            self.assertIn(value,source)
        self.assertRegex(source,r'id="board-comment"[^>]*required')
        self.assertIn('name="assignee_id" disabled',source);self.assertIn('name="due_at" disabled',source)
        self.assertIn('href="/tasks/',source)  # All cards keep the no-JS detail fallback.

    def test_comment_and_assignment_submit_one_real_action_not_direct_status_patch(self):
        ident=self.task_sent(status="awaiting_assignment",assignee_id=None);actions=self.actions(ident,"receiver-head")
        response=self.client.post(actions["assign"]["url"],data={"revision":0,"comment":"Назначаю исполнителя","assignee_id":self.ids["worker"]})
        self.assertEqual(302,response.status_code)
        with self.app.app_context():
            task=db.session.get(Task,ident);self.assertEqual("assigned",task.status);self.assertEqual(self.ids["worker"],task.assignee_id)
            self.assertEqual(1,TaskEvent.query.filter_by(task_id=ident,event_type="assigned").count())
        self.assertEqual(409,self.client.post(actions["assign"]["url"],data={"revision":0,"comment":"Повтор","assignee_id":self.ids["worker"]}).status_code)

    def test_forbidden_skip_and_empty_comment_leave_no_event_or_status_change(self):
        ident=self.task_sent(status="assigned");self.as_user("worker")
        for path,data in (("review/complete",{"comment":"Обход"}),("execution/start",{"comment":"   "}),
            ("execution/start",{"comment":"Подмена статуса","status":"completed"})):
            response=self.client.post(f"/tasks/{ident}/{path}",data={"revision":0}|data)
            self.assertIn(response.status_code,(400,403))
        with self.app.app_context():
            self.assertEqual("assigned",db.session.get(Task,ident).status);self.assertEqual(0,TaskEvent.query.count())

    def test_stale_assignment_and_real_csrf_are_rejected(self):
        ident=self.task_sent(status="awaiting_assignment",assignee_id=None);self.as_user("receiver-head")
        self.app.config["WTF_CSRF_ENABLED"]=True
        response=self.client.post(f"/tasks/{ident}/execution/assign",data={"revision":0,"comment":"Нет CSRF","assignee_id":self.ids["worker"]})
        self.assertEqual(400,response.status_code)
        with self.app.app_context():self.assertEqual("awaiting_assignment",db.session.get(Task,ident).status)

    def test_titles_and_candidates_cannot_break_json_attributes_or_execute_html(self):
        ident=self.task_sent(title='</script><script>alert(1)</script>"')
        self.as_user();source=self.page();self.assertEqual('</script><script>alert(1)</script>"',self.cards(source)[ident]["title"])
        self.assertNotIn('<script>alert(1)</script>',source)
