"""Current five-state copy and rework marker never imply an approval stage."""
from support.task_view_fixtures import ViewCase


class TaskPresentationTests(ViewCase):
    def test_current_statuses_explained_in_boards_lists_details_and_panels(self):
        for status,text in (("new","Исполнитель ещё не назначен"),("in_progress","выполняет поручение"),("completed","Отдельная приёмка не требуется"),("cancelled","отменил поручение"),("rejected","отклонил поручение")):
            ident=self.card(status)
            for actor,direction in (("employee","sent"),("worker","received")):
                sources=[self.client_for(actor).get(f"/tasks/{ident}{suffix}").get_data(as_text=True) for suffix in ("","/panel")]
                for view in ("list","board"):
                    response,_=self.render("completed" if status in ("completed","cancelled","rejected") else direction,actor,view=view,scope="sent" if actor=="employee" else "received")
                    # Compact active board cards communicate exact status; explanatory copy is in list/detail.
                    if view=="list" or status in ("completed","cancelled","rejected"):sources.append(response.get_data(as_text=True))
                for source in sources:self.assertIn(text,source)

    def test_completed_directions_name_customer_and_executing_department(self):
        self.card("completed")
        self.card("completed",sender_department_id=self.other_department_id,recipient_department_id=self.department_id,author_id=self.ids["worker"])
        for scope,label in (("sent","Наш отдел поручал"),("received","Наш отдел выполнял")):
            response,_=self.render("completed",scope=scope);self.assertIn(label,response.get_data(as_text=True))
            self.assertNotIn("принял результат",response.get_data(as_text=True))

    def test_admin_copy_names_both_departments_without_own_department(self):
        self.card("in_progress");response,_=self.render(actor="root",view="list");source=response.get_data(as_text=True)
        self.assertIn("Заказчик:",source);self.assertIn("Исполняющий отдел:",source);self.assertNotIn("Наш отдел",source)

    def test_rework_is_marker_in_new_and_work_not_a_sixth_status(self):
        for status in ("new","in_progress"):
            ident=self.card(status,is_rework=True,rework_cycle=2)
            for view in ("list","board"):
                response,ctx=self.render(view=view);self.assertIn("На доработке",response.get_data(as_text=True));self.assertEqual(5,len(ctx["status_labels"]))
                if view=="board":self.assertEqual((status,True,2),(self.cards(response)[ident]["group"],self.cards(response)[ident]["is_rework"],self.cards(response)[ident]["rework_cycle"]))
            for suffix in ("","/panel"):self.assertIn("На доработке",self.client_for().get(f"/tasks/{ident}{suffix}").get_data(as_text=True))

    def test_completion_clears_rework_marker_everywhere(self):
        ident=self.card("in_progress",is_rework=True,rework_cycle=1);client=self.client_for("worker")
        self.assertEqual(302,client.post(f"/tasks/{ident}/complete",data=dict(revision=0,comment="Исправлено")).status_code)
        response,_=self.render("completed",scope="sent");self.assertFalse(self.cards(response)[ident]["is_rework"])
        self.assertNotIn('class="task-rework"',response.get_data(as_text=True))
        source=self.client_for().get(f"/tasks/{ident}/panel").get_data(as_text=True);self.assertNotIn('class="task-rework"',source)

    def test_active_views_do_not_advertise_retired_review_stages(self):
        self.many(4)
        for view in ("list","board"):
            response,_=self.render(view=view);source=response.get_data(as_text=True)
            for old in ("На согласовании","На проверке отдела-получателя","На приёмке отдела-отправителя","Заказчику результат ещё не передан"):
                self.assertNotIn(old,source)
