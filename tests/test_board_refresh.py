"""Fragments reflect real actions without leaking data or resetting identity."""
from app.extensions import db
from app.models import User
from support.task_view_fixtures import ViewCase
from test_task_board import BoardMarkup


class BoardRefreshTests(ViewCase):
    def fragment(self,client=None,direction="sent",**query):
        response=(client or self.client_for()).get(f"/tasks/{direction}",query_string=dict(view="board",fragment="board",page=999)|query)
        self.assertIn("no-store",response.headers.get("Cache-Control",""));return response

    def test_fragment_matches_all_initial_cards_and_counts_without_dialogs(self):
        ids=self.many(36);initial,_=self.render();response=self.fragment()
        self.assertEqual(200,response.status_code);self.assertEqual(set(ids),set(self.cards(response)));self.assertEqual(self.cards(initial),self.cards(response))
        markup=BoardMarkup(response.get_data(as_text=True));self.assertEqual(36,markup.counts["sent"]);self.assertEqual(0,markup.dialog_count)
        self.assertEqual(36,sum(len(g["ids"]) for g in markup.columns.values()))
        self.assertEqual("sent",response.headers["X-Task-Board"]);self.assertEqual(str(self.ids["employee"]),response.headers["X-Task-User"])
        for forbidden in ("<script","<form","<html","task-panel-dialog","board-dialog"):self.assertNotIn(forbidden,response.get_data(as_text=True))

    def test_fragment_keeps_visibility_and_identity_for_departments_and_admin(self):
        ident=self.card();self.foreign()
        received=self.fragment(self.client_for("worker"),"received");self.assertEqual({ident},set(self.cards(received)))
        foreign=self.fragment(self.client_for("peer"),"sent");self.assertEqual({},self.cards(foreign))
        admin=self.fragment(self.client_for("root"));self.assertEqual(2,len(self.cards(admin)))
        self.assertNotEqual(received.headers["X-Task-User"],admin.headers["X-Task-User"])

    def test_anonymous_or_disabled_session_never_gets_fragment(self):
        self.card();self.assertEqual(302,self.fragment(self.app.test_client()).status_code)
        client=self.client_for()
        with self.app.app_context():db.session.get(User,self.ids["employee"]).is_blocked=True;db.session.commit()
        response=self.fragment(client);self.assertEqual(302,response.status_code);self.assertNotIn(b"data-board-root",response.data)

    def test_explicit_list_or_unknown_fragment_is_bad_request(self):
        for query in (dict(view="list"),dict(fragment="unknown")):
            response=self.fragment(**query);self.assertEqual(400,response.status_code)

    def test_assign_release_claim_and_reassign_refresh_real_status_revision_and_actions(self):
        ident=self.card();head=self.client_for("receiver-head");worker=self.client_for("worker");peer=self.client_for("peer")
        steps=((head,"assign",{"assignee_ids":self.ids["worker"]},"in_progress",self.ids["worker"]),
            (worker,"release",{},"new",None),(peer,"claim",{},"in_progress",self.ids["peer"]),
            (head,"reassign",{"assignee_ids":self.ids["worker"]},"in_progress",self.ids["worker"]))
        for revision,(client,action,extra,status,assignee) in enumerate(steps):
            response=client.post(f"/tasks/{ident}/{action}",data=dict(revision=revision,comment="Пояснение действия")|extra)
            self.assertEqual(302,response.status_code)
            card=self.cards(self.fragment(head,"received"))[ident]
            self.assertEqual((status,status,revision+1,[] if assignee is None else [assignee],revision+1),(card["status"],card["group"],card["revision"],card["assignee_ids"],card["comment_count"]))

    def test_stale_action_conflicts_and_read_gets_latest_revision(self):
        ident=self.card();worker=self.client_for("worker");peer=self.client_for("peer")
        payload=dict(revision=0,comment="Назначаю себя")
        self.assertEqual(302,worker.post(f"/tasks/{ident}/claim",data=payload).status_code);before=self.snapshot()
        self.assertEqual(409,peer.post(f"/tasks/{ident}/claim",data=payload).status_code);self.assertEqual(before,self.snapshot())
        card=self.cards(self.fragment(peer,"received"))[ident];self.assertEqual(("in_progress",1,[self.ids["worker"]]),(card["status"],card["revision"],card["assignee_ids"]))

    def test_complete_return_and_complete_move_cards_and_counts(self):
        ident=self.card("in_progress");worker=self.client_for("worker");sender=self.client_for()
        for cycle in range(2):
            revision=cycle*2
            self.assertEqual(302,worker.post(f"/tasks/{ident}/complete",data=dict(revision=revision,comment="Работа выполнена")).status_code)
            active=self.fragment();self.assertNotIn(ident,self.cards(active));self.assertEqual({"sent":0,"received":0,"completed":1},BoardMarkup(active.get_data(as_text=True)).counts)
            closed=self.fragment(direction="completed",scope="sent");card=self.cards(closed)[ident]
            self.assertEqual(("completed",[],False),(card["status"],card["actions"],card["is_rework"]))
            if cycle==0:
                self.assertEqual(302,sender.post(f"/tasks/{ident}/return",data=dict(revision=1,comment="Уточните результат")).status_code)
                card=self.cards(self.fragment())[ident];self.assertEqual(("in_progress",True,1),(card["group"],card["is_rework"],card["rework_cycle"]))
                self.assertEqual({},self.cards(self.fragment(direction="completed",scope="sent")))

    def test_refresh_escapes_text_and_writes_nothing(self):
        self.card(title="<script>BAD</script>");before=self.snapshot()
        for _ in range(3):
            source=self.fragment().get_data(as_text=True);self.assertNotIn("<script>",source);self.assertIn("&lt;script&gt;",source)
        self.assertEqual(before,self.snapshot())
