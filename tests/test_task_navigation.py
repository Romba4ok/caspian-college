"""Task landing, navigation order and private closed-task direction selection."""

from datetime import datetime, timezone
from html.parser import HTMLParser

from support.portal_fixtures import PortalCase


class NavigationLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and "nav-link" in attrs.get("class", "").split():
            self.links.append(attrs["href"])


class TaskNavigationTests(PortalCase):
    def as_user(self, name):
        self.client = self.app.test_client()
        self.login(name)

    def test_default_is_board_but_explicit_list_remains_available(self):
        self.as_user("head")
        for direction in ("received", "sent", "completed"):
            default = self.client.get(f"/tasks/{direction}").get_data(as_text=True)
            listed = self.client.get(f"/tasks/{direction}?view=list").get_data(as_text=True)
            self.assertIn('data-task-view="board"', default)
            self.assertIn('aria-label="Доска задач"', default)
            self.assertIn('data-task-view="list"', listed)
            self.assertNotIn('aria-label="Доска задач"', listed)

    def test_menu_order_and_tasks_landing_for_all_roles(self):
        for name in ("root", "head", "employee"):
            self.as_user(name)
            self.assertEqual("/tasks/received", self.client.get("/").location)
            page = self.client.get("/tasks/received").get_data(as_text=True)
            parser = NavigationLinks()
            parser.feed(page)
            self.assertEqual("/tasks/received", parser.links[0])
            self.assertEqual("/tasks/notifications", parser.links[-1])
            if name in ("root", "head"):
                self.assertEqual("/admin/statistics" if name == "root" else "/reports/department", parser.links[-2])
            else:
                self.assertEqual(2, len(parser.links))

    def test_closed_direction_switch_is_private_and_filters_both_ways(self):
        sent = datetime(2029, 1, 1, tzinfo=timezone.utc)
        out = self.task(title="Наше исходящее поручение", status="completed", sent_at=sent)
        incoming = self.task(title="Входящее поручение нашему отделу", status="rejected", sent_at=sent,
                             sender_department_id=self.other_department_id, recipient_department_id=self.department_id)
        unrelated = self.task(title="Чужая закрытая задача", status="cancelled", sent_at=sent,
                              sender_department_id=self.other_department_id, recipient_department_id=self.third_department_id)
        self.as_user("head")
        for scope, visible, hidden in (("received", incoming, out), ("sent", out, incoming)):
            page = self.client.get(f"/tasks/completed?scope={scope}").get_data(as_text=True)
            self.assertIn(f'data-task-id="{visible}"', page)
            self.assertNotIn(f'data-task-id="{hidden}"', page)
            self.assertNotIn(f'data-task-id="{unrelated}"', page)
        default = self.client.get("/tasks/completed?scope=invalid").get_data(as_text=True)
        self.assertIn(f'data-task-id="{incoming}"', default)
        self.assertNotIn(f'data-task-id="{out}"', default)

    def test_closed_pagination_and_back_link_keep_direction(self):
        ids = self.task_batch(30, statuses=("completed",))
        self.as_user("head")
        first = self.client.get("/tasks/completed?scope=sent").get_data(as_text=True)
        self.assertIn("scope=sent", first)
        last = self.client.get("/tasks/completed?scope=sent&view=list&page=999").get_data(as_text=True)
        self.assertIn("Страница 2 из 2", last)
        self.assertEqual(5, last.count('data-task-id="'))
        detail = self.client.get(f"/tasks/{ids[0]}").get_data(as_text=True)
        self.assertIn('href="/tasks/completed?scope=sent"', detail)

    def test_admin_keeps_all_departments_in_closed_archive(self):
        task_id = self.task(status="cancelled")
        self.as_user("root")
        html = self.client.get("/tasks/completed?scope=received").get_data(as_text=True)
        self.assertIn(f'data-task-id="{task_id}"', html)
        self.assertNotIn('aria-label="Направление завершённых задач"', html)


class CurrentActionFormsTests(PortalCase):
    def setUp(self):
        super().setUp();self.add_member("worker",department_id=self.other_department_id)
        self.add_member("receiver-head",role="head",department_id=self.other_department_id)

    def test_exact_menus_and_current_item_for_every_role(self):
        expected={"root":["/tasks/received","/admin/users","/admin/departments","/admin/audit","/admin/statistics","/tasks/notifications"],
            "head":["/tasks/received","/staff","/reports/department","/tasks/notifications"],"employee":["/tasks/received","/tasks/notifications"]}
        for name,paths in expected.items():
            client=self.app.test_client();self.login(name,client=client)
            for path in paths:
                response=client.get(path);self.assertEqual(200,response.status_code)
                parser=NavigationLinks();parser.feed(response.get_data(as_text=True));self.assertEqual(paths,parser.links)
                source=response.get_data(as_text=True)
                self.assertNotIn('href="/admin/categories"',source);self.assertNotIn('href="/admin/settings"',source)
                if name=="root":self.assertIn("Журнал действий",source)

    def test_current_forms_follow_server_actions_and_have_required_comment_and_files(self):
        from flask import template_rendered
        from support.task_view_fixtures import Markup
        ident=self.task(status="in_progress",assignee_id=self.ids["worker"])
        for name in ("employee","head","worker","receiver-head","root"):
            client=self.app.test_client();self.login(name,client=client);contexts=[]
            def capture(sender,template,context,**kw):contexts.append(context)
            with template_rendered.connected_to(capture,self.app):response=client.get(f"/tasks/{ident}")
            source=response.get_data(as_text=True);ctx=contexts[-1];forms={f.get("action") for f in Markup(source).forms}
            for action in ctx["task_actions"]:
                self.assertIn(action["url"],forms);self.assertIn(f'id="{action["key"]}-comment"',source);self.assertIn(f'id="{action["key"]}-attachment"',source)
            for retired in ("/approval/","/review/","/confirm","/execution/start","/clarification","Редактировать черновик"):
                self.assertNotIn(retired,source)
            if name=="root":self.assertFalse(any(a and a.startswith(f"/tasks/{ident}/") for a in forms))

    def test_invalid_assignment_retains_input_and_links_inline_error(self):
        ident=self.task();client=self.app.test_client();self.login("receiver-head",client=client)
        response=client.post(f"/tasks/{ident}/assign",data=dict(revision=0,assignee_ids=["99999"],comment="Не потерять пояснение"))
        self.assertEqual(400,response.status_code);source=response.get_data(as_text=True)
        for text in ('href="#assign-assignee_ids"','id="assign-assignee_ids-error"','aria-invalid="true"','Не потерять пояснение','<details open>'):self.assertIn(text,source)

    def test_whitespace_comment_has_linked_error_without_changing_task(self):
        from app.extensions import db
        from app.models import Task
        ident=self.task();client=self.app.test_client();self.login("worker",client=client)
        response=client.post(f"/tasks/{ident}/claim",data=dict(revision=0,comment="  "))
        self.assertEqual(400,response.status_code);source=response.get_data(as_text=True)
        self.assertIn('href="#claim-comment"',source);self.assertIn('id="claim-comment-error"',source)
        with self.app.app_context():self.assertEqual(("new",0),(db.session.get(Task,ident).status,db.session.get(Task,ident).revision))

    def test_invalid_deadline_keeps_value_comment_and_inline_error(self):
        ident=self.task();client=self.app.test_client();self.login("head",client=client)
        response=client.post(f"/tasks/{ident}/deadline",data=dict(revision=0,due_at="invalid",comment="Пояснение переноса"))
        self.assertEqual(400,response.status_code);source=response.get_data(as_text=True)
        self.assertIn('value="invalid"',source);self.assertIn('href="#deadline-due_at"',source);self.assertIn('id="deadline-due_at-error"',source);self.assertIn("Пояснение переноса",source)

    def test_new_task_has_direct_send_without_category_contact_or_saved_draft(self):
        client=self.app.test_client();self.login("employee",client=client)
        source=client.get("/tasks/new").get_data(as_text=True)
        for field in ('name="creation_key"','name="description"','name="attachment"'):self.assertIn(field,source)
        self.assertNotIn('name="category_id"',source);self.assertNotIn('name="contact_person"',source)
        self.assertIn("Отправить задачу",source);self.assertNotIn('name="revision"',source);self.assertNotIn("Сохранить черновик",source)

    def test_notification_copy_read_controls_and_unread_count(self):
        from app.extensions import db
        from app.models import TaskNotification
        ident=self.task()
        with self.app.app_context():
            notification=TaskNotification(task_id=ident,user_id=self.ids["employee"],event_type="task_sent",message="Системное сообщение")
            db.session.add(notification);db.session.commit();nid=notification.id
        client=self.app.test_client();self.login("employee",client=client)
        page=client.get("/tasks/notifications").get_data(as_text=True)
        self.assertNotIn("согласования",page);self.assertIn("Непрочитанных уведомлений: 1",page);self.assertIn(f'/tasks/notifications/{nid}/read',page)
        self.assertEqual(302,client.post(f"/tasks/notifications/{nid}/read").status_code)
        self.assertIn("Непрочитанных уведомлений: 0",client.get("/tasks/notifications").get_data(as_text=True))

    def test_creation_validation_marks_priority_and_bad_file_without_losing_text(self):
        from io import BytesIO
        client=self.app.test_client();self.login("employee",client=client)
        cases=(("priority","unknown"),("attachment",(BytesIO(b"not-a-pdf"),"bad.pdf")))
        for field,value in cases:
            payload=self.creation_form(client=client,title="Сохранить текст поручения",**{field:value})
            response=client.post("/tasks/new",data=payload);self.assertEqual(400,response.status_code)
            source=response.get_data(as_text=True)
            for expected in (f'href="#{field}"',f'id="{field}-error"',f'aria-describedby="{field}-error"',"Сохранить текст поручения"):
                self.assertIn(expected,source)
