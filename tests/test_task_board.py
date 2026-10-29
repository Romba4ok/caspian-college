"""Current capabilities on the complete board and its mutation boundaries."""
from html.parser import HTMLParser
import json
from app.extensions import db
from app.models import Department,Task,User
from support.task_view_fixtures import ViewCase


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


class TaskBoardTests(ViewCase):
    def actions(self,ident,actor):
        direction="received" if actor in ("worker","peer","receiver-head","receiver-head2") else "sent"
        response,_=self.render(direction,actor)
        return {a["key"]:a for a in self.cards(response)[ident]["actions"]}

    def test_recipient_assignment_and_claim_are_work_targets(self):
        ident=self.card()
        for actor in ("receiver-head","receiver-head2"):
            actions=self.actions(ident,actor);self.assertEqual({"assign","claim","reject"},set(actions))
            self.assertEqual("in_progress",actions["assign"]["target"]);self.assertTrue(actions["assign"]["assignee"])
        self.assertEqual({"claim"},set(self.actions(ident,"worker")))
        self.assertEqual({"cancel"},set(self.actions(ident,"employee")))
        self.assertEqual({"cancel","deadline"},set(self.actions(ident,"head")))

    def test_work_actions_are_exact_for_executor_peer_and_both_heads(self):
        ident=self.card("in_progress")
        expected={"worker":{"release","complete","reject"},"peer":{"claim"},
            "receiver-head":{"claim","reassign","complete","reject"},"receiver-head2":{"claim","reassign","complete","reject"},
            "employee":{"cancel"},"head":{"cancel","deadline"},"root":set()}
        for actor,keys in expected.items():self.assertEqual(keys,set(self.actions(ident,actor)),actor)
        self.assertEqual("new",self.actions(ident,"worker")["release"]["target"])
        self.assertTrue(self.actions(ident,"worker")["complete"]["terminal"])
        self.assertEqual("in_progress",self.actions(ident,"receiver-head")["reassign"]["target"])

    def test_action_dialog_and_no_js_links_survive_board_refresh(self):
        ident=self.card();response,_=self.render("received","receiver-head");source=response.get_data(as_text=True)
        markup=BoardMarkup(source);self.assertFalse(markup.dialog_inside_root);self.assertEqual(1,source.count('id="board-dialog"'))
        for field in ('name="csrf_token"','name="revision"','id="board-comment"','name="assignee_ids" multiple size="5" disabled','name="due_at" disabled'):
            self.assertIn(field,source)
        self.assertRegex(source,r'id="board-comment"[^>]*required');self.assertIn(f'href="/tasks/{ident}"',source)
        self.assertTrue(self.cards(response)[ident]["actions"])

    def test_assignment_through_advertised_url_is_atomic_and_stale_replay_conflicts(self):
        ident=self.card();action=self.actions(ident,"receiver-head")["assign"];client=self.client_for("receiver-head")
        payload=dict(revision=0,comment="Назначаю ответственного",assignee_ids=[self.ids["worker"]])
        self.assertEqual(f"/tasks/{ident}/assign",action["url"])
        self.assertEqual(302,client.post(action["url"],data=payload).status_code)
        before=self.snapshot();self.assertEqual(409,client.post(action["url"],data=payload).status_code);self.assertEqual(before,self.snapshot())
        with self.app.app_context():self.assertEqual(("in_progress",1,self.ids["worker"]),(db.session.get(Task,ident).status,db.session.get(Task,ident).revision,db.session.get(Task,ident).assignee_id))

    def test_forged_action_empty_comment_and_retired_stage_do_not_mutate(self):
        ident=self.card();before=self.snapshot()
        self.assertEqual(403,self.client_for("employee").post(f"/tasks/{ident}/assign",data=dict(revision=0,comment="Подмена",assignee_id=self.ids["worker"])).status_code)
        self.assertEqual(400,self.client_for("worker").post(f"/tasks/{ident}/claim",data=dict(revision=0,comment="  ")).status_code)
        self.assertEqual(410,self.client_for("worker").post(f"/tasks/{ident}/execution/start",data=dict(revision=0,comment="Старый этап")).status_code)
        self.assertEqual(before,self.snapshot())

    def test_real_csrf_rejects_missing_token(self):
        ident=self.card();client=self.client_for("worker");self.app.config["WTF_CSRF_ENABLED"]=True
        before=self.snapshot();self.assertEqual(400,client.post(f"/tasks/{ident}/claim",data=dict(revision=0,comment="Без токена")).status_code)
        self.assertEqual(before,self.snapshot())

    def test_admin_is_readonly_even_with_forged_action(self):
        ident=self.card();before=self.snapshot()
        self.assertEqual({},self.actions(ident,"root"))
        self.assertEqual(403,self.client_for("root").post(f"/tasks/{ident}/claim",data=dict(revision=0,comment="Обход")).status_code)
        self.assertEqual(before,self.snapshot())

    def test_archived_departments_keep_old_work_but_no_create(self):
        ident=self.card()
        with self.app.app_context():
            for did in (self.department_id,self.other_department_id):db.session.get(Department,did).is_archived=True
            db.session.commit()
        for direction,actor in (("sent","head"),("received","receiver-head")):
            response,ctx=self.render(direction,actor);self.assertFalse(ctx["may_create"]);self.assertIn(ident,self.cards(response))
        self.assertIn("assign",self.actions(ident,"receiver-head"))

    def test_unavailable_executor_has_repair_hint_allows_join_but_not_completion(self):
        ident=self.card("in_progress",assignee_id=self.ids["blocked-worker"])
        for view in ("board","list"):
            response,_=self.render("received","receiver-head",view=view)
            self.assertIn("Нет доступных исполнителей",response.get_data(as_text=True))
        self.assertEqual({"claim","reassign","reject"},set(self.actions(ident,"receiver-head")))
        self.assertEqual({"claim"},set(self.actions(ident,"peer")))

    def test_title_and_candidate_names_are_escaped_and_json_remains_parseable(self):
        payload='" onmouseover=alert(1)><img src=x onerror=alert(2)>'
        ident=self.card(title=payload)
        with self.app.app_context():db.session.get(User,self.ids["worker"]).full_name=payload;db.session.commit()
        response,_=self.render("received","receiver-head");source=response.get_data(as_text=True)
        self.assertNotIn('<img src=x',source);self.assertEqual(payload,self.cards(response)[ident]["title"])
        self.assertIn(payload,[u["name"] for u in self.cards(response)[ident]["candidates"]])
