"""Closed groups and direction survive view changes, pagination and refresh."""
from urllib.parse import parse_qs,urlsplit
from support.task_view_fixtures import ViewCase,Markup


class CompletedBoardTests(ViewCase):
    def test_all_300_closed_cards_are_readonly_and_grouped_once(self):
        ids=self.many(300,statuses=("completed","cancelled","rejected"))
        response,ctx=self.render("completed",scope="sent")
        self.assertEqual(["completed","cancelled","rejected"],[g["key"] for g in ctx["board_groups"]])
        self.assertEqual(["Выполнено","Отменены","Отклонены"],[g["label"] for g in ctx["board_groups"]])
        self.assertEqual([100,100,100],[g["count"] for g in ctx["board_groups"]]);self.assertEqual(set(ids),set(self.cards(response)))
        self.assertTrue(all(c["actions"]==[] and c["candidates"]==[] for c in self.cards(response).values()))
        self.assertIn("возврат на доработку",response.get_data(as_text=True));self.assertNotIn('aria-label="Страницы задач"',response.get_data(as_text=True))

    def test_scope_preserved_by_list_pagination_and_view_switch_and_refresh(self):
        outgoing=self.many(30,statuses=("completed",))
        incoming=self.card("completed",sender_department_id=self.other_department_id,recipient_department_id=self.department_id,author_id=self.ids["worker"])
        for scope,expected in (("sent",set(outgoing)),("received",{incoming})):
            response,ctx=self.render("completed",scope=scope)
            self.assertEqual(expected,set(self.cards(response)));self.assertEqual(31,ctx["counts"]["completed"])
            links=Markup(response.get_data(as_text=True)).links
            list_link=next(a["href"] for a in links if parse_qs(urlsplit(a["href"]).query).get("view")==["list"])
            self.assertEqual([scope],parse_qs(urlsplit(list_link).query)["scope"])
            fragment=self.client_for().get("/tasks/completed",query_string=dict(scope=scope,view="board",fragment="board"))
            self.assertEqual(expected,set(self.cards(fragment)));self.assertIn(f"scope={scope}",fragment.get_data(as_text=True))
        response,ctx=self.render("completed",view="list",scope="sent",page=1)
        links=[a["href"] for a in Markup(response.get_data(as_text=True)).links]
        next_link=next(h for h in links if parse_qs(urlsplit(h).query).get("page")==["2"])
        query=parse_qs(urlsplit(next_link).query);self.assertEqual(["sent"],query["scope"]);self.assertEqual(["list"],query["view"])
        response=self.client_for().get(next_link);self.assertEqual(200,response.status_code);self.assertIn("Страница 2 из 2",response.get_data(as_text=True))
        self.assertEqual({},self.cards(response))

    def test_default_scope_is_received_and_admin_has_all_without_fake_direction(self):
        own=self.card("completed");foreign=self.foreign(status="completed")
        response,ctx=self.render("completed",view=None);self.assertEqual("received",ctx["completed_scope"]);self.assertEqual({},self.cards(response))
        response,ctx=self.render("completed",view=None,actor="root",scope="sent")
        self.assertIsNone(ctx["completed_scope"]);self.assertEqual({own,foreign},set(self.cards(response)))
        self.assertNotIn('aria-label="Направление завершённых задач"',response.get_data(as_text=True))

    def test_foreign_closed_tasks_and_readonly_panels_keep_privacy(self):
        ident=self.card("completed");hidden=self.foreign(status="rejected")
        response,_=self.render("completed",scope="sent");self.assertEqual({ident},set(self.cards(response)))
        client=self.client_for();self.assertEqual(404,client.get(f"/tasks/{hidden}/panel").status_code)
        for actor in ("employee","worker","head","root"):
            panel=self.client_for(actor).get(f"/tasks/{ident}/panel")
            self.assertEqual(200,panel.status_code);self.assertNotIn("<form",panel.get_data(as_text=True));self.assertIn("История",panel.get_data(as_text=True))

    def test_only_completed_can_be_returned_from_full_card_by_sender_author_or_head(self):
        for status in ("completed","cancelled","rejected"):
            ident=self.card(status)
            for actor in ("employee","head","worker","receiver-head","root"):
                source=self.client_for(actor).get(f"/tasks/{ident}").get_data(as_text=True)
                forms={f.get("action") for f in Markup(source).forms}
                self.assertEqual(status=="completed" and actor in ("employee","head"),f"/tasks/{ident}/return" in forms)
                self.assertNotIn(f"/tasks/{ident}/complete",forms)

    def test_empty_closed_board_retains_three_columns(self):
        response,ctx=self.render("completed",scope="invalid")
        self.assertEqual("received",ctx["completed_scope"]);self.assertEqual([0,0,0],[g["count"] for g in ctx["board_groups"]])
        self.assertIn("Закрытых задач в выбранном направлении пока нет.",response.get_data(as_text=True))
