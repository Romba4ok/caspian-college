"""Rendered semantics and role navigation, independent of style implementation."""

from html.parser import HTMLParser
import json
import unittest
from unittest.mock import patch

from flask import render_template_string

from preview.server import create_preview_app
from preview.fixtures import task_context


class Markup(HTMLParser):
    VOID = {"input", "meta", "link", "br", "hr", "img", "area", "source", "wbr"}

    def __init__(self, html):
        super().__init__()
        self.nodes, self.stack = [], []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "text": [],
                "parent": self.stack[-1] if self.stack else None}
        self.nodes.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                self.stack = self.stack[:index]
                break

    def handle_data(self, value):
        for node in self.stack:
            node["text"].append(value)

    def with_class(self, name):
        return [node for node in self.nodes if name in node["attrs"].get("class", "").split()]

    def unexpected_images(self):
        # The trusted header logo is permitted; injected images/event attributes are not.
        logo={"class":"brand-logo","src":"/static/caspian-college-logo.png",
              "width":"1280","height":"1152","alt":"","aria-hidden":"true"}
        return [node for node in self.nodes if node["tag"]=="img" and node["attrs"]!=logo]


class MarkupCase(unittest.TestCase):
    def setUp(self):
        self.app = create_preview_app(testing=True)
        self.client = self.app.test_client()

    def test_role_navigation_order_and_destination_labels(self):
        expected = {
            "employee": ["Задачи", "Уведомления"],
            "head": ["Задачи", "Сотрудники", "Отчёты", "Уведомления"],
            "admin": ["Задачи", "Пользователи", "Отделы", "Журнал действий", "Отчёты", "Уведомления"],
        }
        for role, labels in expected.items():
            with self.subTest(role=role):
                markup = Markup(self.client.get("/__preview__/?role=" + role).text)
                actual = []
                for link in markup.with_class("nav-link"):
                    label = next(node for node in markup.nodes
                                 if node["parent"] is link and node["tag"] == "span")
                    actual.append("".join(label["text"]).strip())
                self.assertEqual(actual, labels)
                self.assertFalse(markup.with_class("nav-unavailable"))
                for forbidden in ("Настройки", "Категории", "Обучение", "Выбрать язык"):
                    self.assertNotIn(forbidden, actual)

    def test_admin_identity_has_no_fake_department_or_task_mutation_form(self):
        html = self.client.get("/__preview__/?role=admin").text
        self.assertNotIn("Маркетинг (демо)", html)
        self.assertIn("Администратор (демо)", html)
        forms = [node for node in Markup(html).nodes if node["tag"] == "form"]
        self.assertEqual([node["attrs"].get("action") for node in forms
                          if node["attrs"].get("method") == "post"], ["/logout"])

    def test_anonymous_login_does_not_show_role_navigation(self):
        markup = Markup(self.client.get("/login?role=admin").text)
        self.assertFalse(markup.with_class("nav-link"))
        self.assertFalse(markup.with_class("account-menu"))

    def test_supplied_logo_is_shared_by_login_and_dashboard_headers(self):
        for role in ("employee","head","admin"):
            html=self.client.get("/tasks/received?role="+role).text
            markup=Markup(html)
            images=[n for n in markup.nodes if n["tag"]=="img"]
            self.assertEqual(len(images),1)
            self.assertFalse(markup.unexpected_images())
            self.assertEqual(images[0]["parent"]["attrs"].get("class"),"brand")
        self.assertEqual(len(Markup(self.client.get("/login").text).with_class("brand-logo")),1)
        response=self.client.get("/static/caspian-college-logo.png")
        try:
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.mimetype,"image/png")
            self.assertTrue(response.data.startswith(b"\x89PNG\r\n\x1a\n"))
        finally:
            response.close()

    def test_ids_labels_menu_controls_and_decorative_icons(self):
        for path in ("/login", "/__preview__/?role=admin", "/__preview__/components?role=head"):
            with self.subTest(path=path):
                markup = Markup(self.client.get(path).text)
                ids = [node["attrs"]["id"] for node in markup.nodes if "id" in node["attrs"]]
                self.assertEqual(len(ids), len(set(ids)))
                for node in markup.nodes:
                    if node["tag"] == "label" and "for" in node["attrs"]:
                        self.assertIn(node["attrs"]["for"], ids)
                    if "aria-controls" in node["attrs"]:
                        self.assertIn(node["attrs"]["aria-controls"], ids)
                    if "data-nav-toggle" in node["attrs"]:
                        self.assertEqual(node["attrs"].get("aria-label"), "Основное меню")
                    if node["tag"] == "svg":
                        self.assertEqual(node["attrs"].get("aria-hidden"), "true")
                        self.assertEqual(node["attrs"].get("focusable"), "false")

    def test_component_text_is_escaped_and_missing_action_is_not_clickable(self):
        with self.app.test_request_context("/login"):
            html = render_template_string(
                "{% import 'components/ui.html' as ui %}{{ ui.message(text) }}"
                "{{ ui.link_button('Выполнить', none) }}", text='<script>alert("demo")</script>')
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        markup = Markup(html)
        self.assertFalse([node for node in markup.nodes if node["tag"] in ("a", "button")])
        self.assertTrue([node for node in markup.nodes if node["attrs"].get("aria-disabled") == "true"])

    def test_components_cover_five_statuses_and_four_priorities(self):
        html = self.client.get("/__preview__/components").text
        for label in ("Новая", "В работе", "Выполнено", "Отменена", "Отклонена",
                      "Срочный", "Высокий", "Обычный", "Низкий"):
            self.assertIn(label, html)
        self.assertNotIn("Средний", html)


class TaskSurfacesCase(unittest.TestCase):
    def setUp(self):
        self.client = create_preview_app(testing=True).test_client()

    def test_complete_board_and_paginated_list_matrix(self):
        for role in ("employee", "head", "admin"):
            for direction, scope in (("received", "received"), ("sent", "sent"),
                                     ("completed", "received"), ("completed", "sent")):
                for size in (0, 1, 36, 300):
                    for view in ("board", "list"):
                        with self.subTest(role=role, direction=direction, scope=scope, size=size, view=view):
                            response = self.client.get("/tasks/" + direction, query_string=dict(role=role, size=size, view=view, scope=scope))
                            self.assertEqual(response.status_code, 200)
                            markup = Markup(response.text)
                            cards = markup.with_class("board-card" if view == "board" else "task-card")
                            self.assertEqual(len(cards), size if view == "board" else min(25, size))
                            ids = [card["attrs"]["data-task-id"] for card in cards]
                            self.assertEqual(len(ids), len(set(ids)))
                            if view == "board":
                                columns = markup.with_class("board-column")
                                expected = ["completed", "cancelled", "rejected"] if direction == "completed" else ["new", "in_progress"]
                                self.assertEqual([c["attrs"]["data-group"] for c in columns], expected)
                                self.assertEqual(sum(int("".join(c["text"])) for c in markup.with_class("board-column-count")), size)
                                root = next(n for n in markup.nodes if "data-board-root" in n["attrs"])
                                self.assertEqual(int(root["attrs"]["data-total"]), size)
                                counts = json.loads(root["attrs"]["data-counts"])
                                self.assertEqual(counts, {"received": size, "sent": size, "completed": size if role == "admin" else size * 2})
                                self.assertFalse(markup.with_class("pagination"))
                            else:
                                self.assertFalse(markup.with_class("board-column"))

    def test_all_list_pages_cover_same_tasks_without_duplicates(self):
        for direction, scope in (("received", "received"), ("completed", "sent")):
            with self.subTest(direction=direction):
                seen = []
                for page in range(1, 13):
                    response = self.client.get("/tasks/" + direction, query_string=dict(size=300, view="list", scope=scope, page=page))
                    seen.extend(card["attrs"]["data-task-id"] for card in Markup(response.text).with_class("task-card"))
                board = Markup(self.client.get("/tasks/" + direction, query_string=dict(size=300, scope=scope)).text)
                board_ids = {card["attrs"]["data-task-id"] for card in board.with_class("board-card")}
                self.assertEqual(len(seen), 300)
                self.assertEqual(len(set(seen)), 300)
                self.assertEqual(set(seen), board_ids)

    def test_active_order_metadata_rework_and_priorities(self):
        html = self.client.get("/tasks/received?role=head").text
        markup = Markup(html)
        cards = markup.with_class("board-card")
        self.assertEqual(len([n for n in markup.nodes if "data-drag-hint" in n["attrs"]]), len(cards))
        self.assertEqual([int(c["attrs"]["data-task-id"]) for c in cards], [1000, 1004, 1002, 1001, 1005, 1003])
        for text in ("Срочный", "Высокий", "Обычный", "Низкий", "На доработке", "Просрочена на", "Не назначен", "Комментарии:", "Вложения:", "Нужно переназначить исполнителя"):
            self.assertIn(text, html)
        for card in cards:
            data = json.loads(card["attrs"]["data-board-task"])
            self.assertEqual(data["id"], int(card["attrs"]["data-task-id"]))
            self.assertFalse(data["actions"])
            self.assertTrue(data["detail_url"].startswith("/tasks/"))
        self.assertFalse([n for n in markup.nodes if "data-board-action" in n["attrs"]])

        sent = self.client.get("/tasks/sent?role=head").text
        self.assertNotIn("Назначьте исполнителя", sent)
        self.assertNotIn("Нужно переназначить исполнителя", sent)

    def test_closed_dates_are_historical_and_not_current_overdue(self):
        markup = Markup(self.client.get("/tasks/completed?size=36").text)
        self.assertFalse(markup.with_class("task-rework"))
        self.assertFalse(markup.with_class("task-overdue"))
        self.assertFalse(markup.with_class("task-card-overdue"))
        self.assertFalse(markup.with_class("task-timing"))
        cards = {int(c["attrs"]["data-task-id"]): "".join(c["text"]) for c in markup.with_class("board-card")}
        self.assertIn("дата и время неизвестны", cards[3018])  # completed_at missing: closed_at is not a completion timestamp.
        self.assertNotIn("26.10.2026 05:00", cards[3018])
        for task_id in (3016, 3033):
            self.assertIn("дата и время неизвестны", cards[task_id])
            self.assertNotIn("Просрочена", cards[task_id])
        self.assertIn("Неизвестен — историческая задача", cards[3012])

    def test_admin_is_read_only_and_both_departments_are_visible(self):
        markup = Markup(self.client.get("/tasks/received?role=admin&size=36").text)
        self.assertFalse(markup.with_class("task-create"))
        self.assertFalse(markup.with_class("completed-direction-switch"))
        self.assertFalse([n for n in markup.nodes if "data-board-action" in n["attrs"]])
        forms = [n for n in markup.nodes if n["tag"] == "form"]
        self.assertEqual([n["attrs"].get("action") for n in forms], ["/logout"])
        for card in markup.with_class("board-card"):
            text = "".join(card["text"])
            self.assertIn("Отдел-заказчик", text)
            self.assertIn("Исполняющий отдел", text)
        self.assertIn("Все отделы", self.client.get("/tasks/received?role=admin").text)

    def test_templates_preserve_context_order_and_escape_user_content(self):
        context = task_context("head", "received")
        for group in context["board_groups"]:
            group.tasks.reverse()
        title = '<script>alert("synthetic")</script> Очень длинное название'
        context["board_groups"][0].tasks[0].title = title
        with patch("preview.server.task_context", return_value=context):
            html = self.client.get("/tasks/received?role=head").text
        ids = [int(c["attrs"]["data-task-id"]) for c in Markup(html).with_class("board-card")]
        self.assertEqual(ids, [1002, 1004, 1000, 1003, 1005, 1001])
        self.assertNotIn('<script>alert', html)
        self.assertIn('&lt;script&gt;', html)


class TaskReadingMarkupCase(unittest.TestCase):
    def setUp(self):
        self.app = create_preview_app(testing=True)
        self.client = self.app.test_client()

    def test_full_and_panel_sections_are_identical_except_heading_level(self):
        for task_id in (1001,1005,3000,3001,3002):
            full = Markup(self.client.get(f"/tasks/{task_id}?role=head").text)
            panel = Markup(self.client.get(f"/tasks/{task_id}/panel?role=head").text)
            for section in ("reading-overview","reading-comments","reading-attachments","reading-history"):
                self.assertEqual(full.with_class(section)[0]["text"], panel.with_class(section)[0]["text"])
            ids = [node["attrs"]["id"] for node in full.nodes if "id" in node["attrs"]]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertEqual(len([node for node in full.nodes if node["tag"] == "h1"]), 1)
            self.assertTrue(any(node["tag"] == "h2" for node in panel.nodes))
            self.assertTrue(any(node["tag"] == "h3" for node in panel.nodes))

    def test_closed_tasks_do_not_have_current_overdue_or_rework_badges(self):
        for task_id in (3000,3001,3002,3018,3033):
            response = self.client.get(f"/tasks/{task_id}/panel?role=head&size=36")
            markup = Markup(response.text)
            self.assertFalse(markup.with_class("task-late"))
            self.assertFalse(markup.with_class("task-rework"))
            self.assertNotIn("До срока", response.text)
            self.assertIn("Задача закрыта.", response.text)
            self.assertTrue(markup.with_class("reading-closed"))
        unknown = self.client.get("/tasks/3033/panel?role=head&size=36").text
        self.assertIn("дата и время неизвестны", unknown)
        fallback = self.client.get("/tasks/3018/panel?role=head&size=36").text
        self.assertIn("Выполнено", fallback)
        self.assertIn("datetime=", fallback)

    def test_unavailable_assignee_rework_and_missing_due_date_are_explicit(self):
        response = self.client.get("/tasks/1005/panel?role=head")
        self.assertIn("Прежний сотрудник", response.text)
        self.assertIn("Начальнику исполняющего отдела необходимо назначить состав", response.text)
        self.assertTrue(Markup(response.text).with_class("task-rework"))
        self.assertIn("Возвращена на доработку", response.text)
        unknown = self.client.get("/tasks/1012/panel?role=head&size=36").text
        self.assertIn("не указан срок", unknown)
        self.assertIn("дата и время неизвестны", unknown)

    def test_comment_file_association_and_history_changes_are_readable(self):
        markup = Markup(self.client.get("/tasks/1001/panel?role=head").text)
        comment_files = markup.with_class("comment-files")
        self.assertEqual(len(comment_files), 1)
        self.assertIn("План встречи.pdf", "".join(comment_files[0]["text"]))
        self.assertEqual(len(markup.with_class("reading-changes")), 2)
        text = "".join(markup.with_class("reading-history")[0]["text"])
        for value in ("Что изменилось", "Было: Не назначен", "Стало: Сотрудник (демо)"):
            self.assertIn(value, text)

    def test_untrusted_task_comment_history_and_filename_are_escaped(self):
        from preview.fixtures import detail_context
        context = detail_context("head",1001)
        attack = '<img src=x onerror="alert(1)">'
        context["task"].title = attack
        context["task"].description = attack
        context["task"].comments[0].body = attack
        context["task"].attachments[0].original_filename = attack
        context["history_entries"][0].comment = attack
        with patch("preview.server.detail_context", return_value=context):
            for path in ("/tasks/1001", "/tasks/1001/panel"):
                html = self.client.get(path + "?role=head").text
                self.assertIn("&lt;img", html)
                self.assertNotIn(attack, html)
                self.assertFalse(Markup(html).unexpected_images())

class TaskCreationMarkupCase(unittest.TestCase):
    def setUp(self):
        self.app=create_preview_app(testing=True)
        self.client=self.app.test_client()

    def test_labels_ids_required_hints_and_error_links_are_connected(self):
        for role in ("employee","head"):
            markup=Markup(self.client.get("/tasks/new?role="+role).text)
            ids=[n["attrs"]["id"] for n in markup.nodes if "id" in n["attrs"]]
            self.assertEqual(len(ids),len(set(ids)))
            labels={n["attrs"].get("for") for n in markup.nodes if n["tag"]=="label"}
            for node in markup.nodes:
                if node["tag"] not in {"input","select","textarea"} or node["attrs"].get("type")=="hidden":
                    continue
                attrs=node["attrs"]
                self.assertIn(attrs["id"],labels)
                for target in attrs.get("aria-describedby","").split():
                    self.assertIn(target,ids)
            self.assertEqual(len([n for n in markup.nodes if n["tag"]=="h1"]),1)
            inputs={n["attrs"].get("name"):n for n in markup.nodes if n["tag"]=="input"}
            key=inputs["creation_key"]["attrs"]["value"]
            error=Markup(self.client.post("/tasks/new?role="+role,data=dict(creation_key=key,
                csrf_token=inputs["csrf_token"]["attrs"]["value"])).text)
            field=next(n for n in error.nodes if n["attrs"].get("id")=="recipient_department_id")
            self.assertEqual(field["attrs"]["aria-invalid"],"true")
            self.assertIn("recipient_department_id-error",field["attrs"]["aria-describedby"])

    def test_same_template_without_preview_flag_keeps_canonical_native_form(self):
        from flask import render_template
        from preview.fixtures import creation_context
        with self.app.test_request_context("/tasks/new?role=head"):
            self.app.preprocess_request()
            html=render_template("tasks/form.html",**creation_context("a"*64),preview_mode=False)
        self.assertIn('action="/tasks/new?role=head"',html)
        self.assertIn('name="creation_key" value="'+"a"*64+'"',html)
        self.assertNotIn("Демонстрация интерфейса",html)
        self.assertNotIn("data-creation-receipt",html)
        self.assertNotIn('name="create_case"',html)
        self.assertNotIn("Сохранить черновик",html)

class TaskActionMarkupCase(unittest.TestCase):
    def setUp(self):
        self.app=create_preview_app(testing=True)
        self.client=self.app.test_client()

    def test_shared_dialog_native_forms_and_labels_have_one_owner_and_unique_ids(self):
        for path in ("/tasks/received","/tasks/1000","/tasks/2000","/tasks/4000"):
            markup=Markup(self.client.get(path+"?role=head&actions=demo").text)
            ids=[n["attrs"]["id"] for n in markup.nodes if "id" in n["attrs"]]
            self.assertEqual(len(ids),len(set(ids)))
            dialogs=[n for n in markup.nodes if n["attrs"].get("id")=="board-dialog"]
            self.assertEqual(len(dialogs),1)
            form=next(n for n in markup.nodes if n["attrs"].get("id")=="board-action-form")
            self.assertNotIn("data-ui-form",form["attrs"])
            self.assertEqual(form["attrs"]["method"],"post")
            self.assertEqual(form["attrs"]["enctype"],"multipart/form-data")
            labels={n["attrs"].get("for") for n in markup.nodes if n["tag"]=="label"}
            for n in markup.nodes:
                if n["tag"] in ("input","select","textarea") and n["attrs"].get("type")!="hidden":
                    self.assertIn(n["attrs"]["id"],labels)
                if n["tag"]=="form":
                    parent=n["parent"]
                    while parent:
                        self.assertNotEqual(parent["tag"],"form")
                        parent=parent["parent"]
            scripts=[n["attrs"].get("src") for n in markup.nodes if n["tag"]=="script"]
            self.assertEqual(scripts.count("/static/tasks.js"),1)
            if path=="/tasks/4000":
                self.assertIn("Вернуть на доработку",self.client.get(path+"?role=head&actions=demo").text)

    def test_action_history_retains_previous_entries_without_regenerating_events(self):
        query="?role=head&actions=demo&probe=history"
        data=dict(csrf_token="frontend-preview-only-not-an-auth-token",revision="1",comment="Беру задачу.")
        self.assertEqual(self.client.post("/tasks/1000/claim"+query,data=data).status_code,303)
        data.update(revision="2",comment="Освобождаю задачу.")
        self.assertEqual(self.client.post("/tasks/1000/release"+query,data=data).status_code,303)
        markup=Markup(self.client.get("/tasks/1000"+query).text)
        history=markup.with_class("reading-history-list")[0]
        entries=[n for n in markup.nodes if n["tag"]=="li" and n["parent"] is history]
        self.assertEqual(len(entries),3)
        text="".join(history["text"])
        for label in ("Задача отправлена","Взять задачу","Снять задачу с себя"):
            self.assertEqual(text.count(label),1)

    def test_server_urls_not_groups_control_native_and_enhanced_actions(self):
        import json
        from preview.fixtures import detail_context
        context=detail_context("head",1000)
        context["task_actions"]=[dict(key="claim",label='<img src=x onerror="bad()">',url="/tasks/1000/claim?canonical=1",
            target="in_progress",assignee=False,deadline=False,terminal=False,group="arbitrary-group")]
        with patch("preview.server.detail_context",return_value=context):
            html=self.client.get("/tasks/1000?role=head").text
        markup=Markup(html)
        self.assertFalse(markup.unexpected_images())
        actions=json.loads(next(n["attrs"]["data-board-task"] for n in markup.nodes if "data-board-task" in n["attrs"]))["actions"]
        # Preview decorates canonical endpoints itself; independent Jinja rendering retains arbitrary canonical URLs.
        from flask import render_template
        context["task_actions"][0]["url"]="/tasks/1000/claim?canonical=1"
        with self.app.test_request_context("/tasks/1000?role=head"):
            self.app.preprocess_request()
            rendered=render_template("tasks/detail.html",**context)
        markup=Markup(rendered)
        state=json.loads(next(n["attrs"]["data-board-task"] for n in markup.nodes if "data-board-task" in n["attrs"]))
        self.assertEqual(state["actions"][0]["url"],"/tasks/1000/claim?canonical=1")
        self.assertTrue(any(n["tag"]=="form" and n["attrs"].get("action")=="/tasks/1000/claim?canonical=1" for n in markup.nodes))
        self.assertNotIn("arbitrary-group/",rendered)


class PeopleMarkupCase(unittest.TestCase):
    def test_server_error_links_and_unsaved_form_marker(self):
        client=create_preview_app(testing=True).test_client()
        response=client.post("/staff/new?role=head",data={"csrf_token":"frontend-preview-only-not-an-auth-token",
            "full_name":"Несохранённый ввод","username":"aliya.demo","password":"synthetic-unused"})
        self.assertEqual(response.status_code,400)
        markup=Markup(response.text)
        form=next(n for n in markup.nodes if "data-account-form" in n["attrs"])
        self.assertEqual(form["attrs"]["data-account-unsaved"],"true")
        field=next(n for n in markup.nodes if n["attrs"].get("id")=="field-username")
        self.assertEqual(field["attrs"]["aria-invalid"],"true")
        self.assertIn("field-username-error",field["attrs"]["aria-describedby"])
        self.assertTrue(any(n["tag"]=="a" and n["attrs"].get("href")=="#field-username" for n in markup.nodes))

    def setUp(self):
        self.client=create_preview_app(testing=True).test_client()

    def test_native_forms_labels_unique_ids_and_allowed_fields(self):
        for path,role,names in (("/staff/new","head",{"csrf_token","full_name","username","password"}),
                ("/staff/22/edit","head",{"csrf_token","full_name","username","password"}),
                ("/admin/users/new","admin",{"csrf_token","full_name","username","password","role","department_id"}),
                ("/admin/users/3/edit","admin",{"csrf_token","full_name","username","password","role","department_id"})):
            markup=Markup(self.client.get(path+"?role="+role).text)
            ids=[n["attrs"]["id"] for n in markup.nodes if n["attrs"].get("id")]
            self.assertEqual(len(ids),len(set(ids)))
            form=next(n for n in markup.nodes if "data-account-form" in n["attrs"])
            self.assertEqual(form["attrs"]["method"],"post")
            self.assertIn("data-ui-form",form["attrs"])
            def inside(node):
                parent=node["parent"]
                while parent:
                    if parent is form:return True
                    parent=parent["parent"]
                return False
            fields=[n for n in markup.nodes if n["tag"] in ("input","select") and inside(n)]
            self.assertEqual({n["attrs"]["name"] for n in fields},names)
            for field in fields:
                if field["attrs"].get("type")=="hidden":continue
                self.assertTrue(any(n["tag"]=="label" and n["attrs"].get("for")==field["attrs"]["id"] for n in markup.nodes))
            password=next(n for n in fields if n["attrs"]["name"]=="password")
            self.assertEqual(password["attrs"]["value"],"")
            self.assertEqual("required" in password["attrs"],path.endswith("/new"))
            self.assertFalse(any(n["tag"]=="form" and inside(n) for n in markup.nodes))

    def test_account_target_is_not_current_header_user_and_admin_has_no_disable(self):
        html=self.client.get("/admin/users/20/edit?role=admin").text
        self.assertIn('value="Алия Садыкова (демо)"',html)
        self.assertIn("Администратор (демо)",html)
        html=self.client.get("/admin/users?role=admin").text
        self.assertNotIn("/admin/users/3/block",html)
        self.assertNotIn("/delete",html)
        self.assertNotIn('value="admin"',self.client.get("/admin/users/new?role=admin").text)
        self.assertIn('name="role" value="admin"',self.client.get("/admin/users/3/edit?role=admin").text)

    def test_table_semantics_escaping_empty_and_confirmations(self):
        response=self.client.post("/staff/new?role=head",data={"csrf_token":"frontend-preview-only-not-an-auth-token","full_name":'<img src=x onerror=alert(1)>',"username":"escaped.demo","password":"synthetic-unused"})
        self.assertEqual(response.status_code,303)
        html=self.client.get(response.location).text
        self.assertIn("&lt;img",html)
        self.assertNotIn('<img src=x',html)
        markup=Markup(html)
        self.assertTrue(any(n["tag"]=="th" and n["attrs"].get("scope")=="col" for n in markup.nodes))
        self.assertTrue(any(n["tag"]=="caption" for n in markup.nodes))
        self.assertTrue(any(n["tag"]=="form" and n["attrs"].get("data-confirm-message") for n in markup.nodes))
        self.assertTrue(any(n["tag"]=="dialog" and n["attrs"].get("aria-describedby")=="account-confirm-message" for n in markup.nodes))
        empty=Markup(self.client.get("/staff?role=head&people_case=empty").text)
        self.assertFalse(any(n["tag"]=="table" for n in empty.nodes))


class DepartmentMarkupCase(unittest.TestCase):
    def setUp(self):
        self.client=create_preview_app(testing=True).test_client()

    def test_native_forms_have_canonical_fields_and_labels(self):
        for path in ("/admin/departments/new","/admin/departments/3/edit"):
            html=self.client.get(path+"?role=admin").text
            markup=Markup(html)
            form=next(n for n in markup.nodes if "data-department-form" in n["attrs"])
            self.assertEqual(form["attrs"]["method"],"post")
            self.assertIn("data-ui-form",form["attrs"])
            self.assertTrue(form["attrs"]["action"].startswith(path+"?"))
            fields=[n for n in markup.nodes if n["tag"]=="input" and n["parent"] is form or n["attrs"].get("id")=="field-name"]
            self.assertEqual({n["attrs"].get("name") for n in fields},{"csrf_token","name"})
            name=next(n for n in fields if n["attrs"].get("name")=="name")
            self.assertEqual(name["attrs"]["maxlength"],"100")
            self.assertIn("required",name["attrs"])
            self.assertTrue(any(n["tag"]=="label" and n["attrs"].get("for")==name["attrs"]["id"] for n in markup.nodes))
            ids=[n["attrs"]["id"] for n in markup.nodes if n["attrs"].get("id")]
            self.assertEqual(len(ids),len(set(ids)))
            self.assertNotIn("Отдел в архиве",html) if path.endswith("/new") else self.assertIn("Переименование сохраняет архивный статус",html)

    def test_native_delete_needs_explicit_intent_and_nonempty_has_no_delete(self):
        markup=Markup(self.client.get("/admin/departments?role=admin").text)
        details=[n for n in markup.nodes if "data-delete-fallback" in n["attrs"]]
        self.assertEqual(len(details),1)
        self.assertNotIn("open",details[0]["attrs"])
        self.assertIn("Восстановить его после удаления нельзя","".join(details[0]["text"]))
        forms=[n for n in markup.nodes if n["tag"]=="form" and "/delete?" in n["attrs"].get("action","")]
        self.assertEqual(len(forms),1)
        self.assertIn("/4/delete?",forms[0]["attrs"]["action"])
        self.assertEqual(forms[0]["attrs"]["data-confirm-title"],"Удалить пустой отдел?")
        self.assertIn("Подтвердить удаление","".join(forms[0]["text"]))
        self.assertTrue(any(n["tag"]=="th" and n["attrs"].get("scope")=="col" for n in markup.nodes))
        self.assertTrue(any(n["tag"]=="caption" for n in markup.nodes))

    def test_error_focus_links_and_unsaved_values(self):
        response=self.client.post("/admin/departments/new?role=admin",data={"csrf_token":"frontend-preview-only-not-an-auth-token","name":"МАРКЕТИНГ (ДЕМО)"})
        self.assertEqual(response.status_code,400)
        markup=Markup(response.text)
        form=next(n for n in markup.nodes if "data-department-form" in n["attrs"])
        self.assertEqual(form["attrs"]["data-account-unsaved"],"true")
        field=next(n for n in markup.nodes if n["attrs"].get("id")=="field-name")
        self.assertEqual(field["attrs"]["value"],"МАРКЕТИНГ (ДЕМО)")
        self.assertEqual(field["attrs"]["aria-invalid"],"true")
        self.assertIn("field-name-error",field["attrs"]["aria-describedby"])
        summary=markup.with_class("error-summary")[0]
        self.assertEqual(summary["attrs"]["tabindex"],"-1")
        self.assertEqual(summary["attrs"]["role"],"alert")

    def test_names_are_escaped_in_text_and_confirmation_attributes(self):
        response=self.client.post("/admin/departments/new?role=admin",data={"csrf_token":"frontend-preview-only-not-an-auth-token","name":'<img src=x onerror=alert(1)>'})
        self.assertEqual(response.status_code,303)
        html=self.client.get(response.location).text
        self.assertIn("&lt;img",html)
        self.assertFalse(Markup(html).unexpected_images())
        self.assertIn("Отделов пока нет",self.client.get("/admin/departments?role=admin&dept_case=empty").text)


class NotificationMarkupCase(unittest.TestCase):
    def test_notification_titles_match_the_linked_task(self):
        markup=Markup(self.client.get("/tasks/notifications?role=head").text)
        for article in (n for n in markup.nodes if "data-notification-id" in n["attrs"]):
            def inside(node):
                parent=node["parent"]
                while parent:
                    if parent is article:return True
                    parent=parent["parent"]
                return False
            links=[n for n in markup.nodes if n["tag"]=="a" and inside(n)]
            if not links:continue
            detail=Markup(self.client.get(links[0]["attrs"]["href"]).text)
            heading=next(n for n in detail.nodes if n["tag"]=="h1")
            self.assertIn("".join(heading["text"]).casefold(),"".join(article["text"]).casefold())

    def setUp(self):
        self.client=create_preview_app(testing=True).test_client()

    def test_native_forms_have_only_csrf_and_original_bulk_boundary(self):
        html=self.client.get("/tasks/notifications?role=head").text
        markup=Markup(html)
        forms=[n for n in markup.nodes if "data-notification-action" in n["attrs"]]
        self.assertEqual(len(forms),4)
        for form in forms:
            self.assertEqual(form["attrs"]["method"],"post")
            self.assertIn("data-ui-form",form["attrs"])
            fields=[n["attrs"]["name"] for n in markup.nodes if n["tag"]=="input" and n["parent"] is form]
            self.assertEqual(set(fields),{"csrf_token","boundary"} if "/read-all?" in form["attrs"]["action"] else {"csrf_token"})
        self.assertNotIn("согласования",html)
        self.assertIn('data-notification-count',html)
        ids=[n["attrs"]["id"] for n in markup.nodes if n["attrs"].get("id")]
        self.assertEqual(len(ids),len(set(ids)))

    def test_read_state_dates_task_links_and_disabled_empty_bulk(self):
        markup=Markup(self.client.get("/tasks/notifications?role=admin").text)
        articles=[n for n in markup.nodes if "data-notification-id" in n["attrs"]]
        self.assertEqual(len(articles),8)
        self.assertEqual(sum(n["attrs"]["data-read"]=="false" for n in articles),3)
        self.assertTrue(all(n["attrs"].get("datetime") for n in markup.nodes if n["tag"]=="time"))
        detached=next(n for n in articles if n["attrs"]["data-notification-id"]=="107")
        self.assertFalse(any(n["tag"]=="a" and n["parent"]["parent"] is detached for n in markup.nodes if n["parent"]))
        empty=Markup(self.client.get("/tasks/notifications?role=admin&notify_case=empty").text)
        self.assertTrue(any(n["tag"]=="button" and "disabled" in n["attrs"] for n in empty.nodes))
        self.assertTrue(any(n["tag"]=="nav" and n["attrs"].get("aria-label")=="Показать уведомления" for n in markup.nodes))

    def test_long_and_untrusted_server_text_is_not_active_markup(self):
        from unittest.mock import patch
        from preview.fixtures import notification_fixture
        rows=notification_fixture()
        rows[0]["message"]='<img src=x onerror=alert(1)>'
        with patch("preview.server.notification_fixture",return_value=rows):
            html=self.client.get("/tasks/notifications?role=head").text
        self.assertIn("&lt;img",html)
        self.assertFalse(Markup(html).unexpected_images())
        html=self.client.get("/tasks/notifications?notify_case=long").text
        self.assertIn("расписание консультаций",html)


class ReportMarkupCase(unittest.TestCase):
    def setUp(self):
        self.client=create_preview_app(testing=True).test_client()

    def test_five_status_tables_captions_headers_and_values(self):
        markup=Markup(self.client.get("/reports/department?role=head").text)
        statuses=[n["attrs"]["data-report-status"] for n in markup.nodes if "data-report-status" in n["attrs"]]
        self.assertEqual(statuses,["new","in_progress","completed","cancelled","rejected"])
        rows=[n for n in markup.nodes if n["attrs"].get("data-report-status")=="in_progress"]
        cells=[n for n in markup.nodes if n["parent"] is rows[0] and n["tag"]=="td"]
        self.assertEqual(["".join(n["text"]) for n in cells],["4","2"])
        self.assertTrue(any(n["tag"]=="caption" for n in markup.nodes))
        self.assertTrue(any(n["tag"]=="th" and n["attrs"].get("scope")=="row" for n in markup.nodes))
        self.assertTrue(any(n["tag"]=="th" and n["attrs"].get("scope")=="col" for n in markup.nodes))
        rework=Markup(self.client.get("/reports/department?role=head&report_case=rework").text)
        row=next(n for n in rework.nodes if n["attrs"].get("data-report-status")=="in_progress")
        self.assertEqual(["".join(n["text"]) for n in rework.nodes if n["parent"] is row and n["tag"]=="td"],["5","2"])

    def test_global_read_only_native_details_and_unique_ids(self):
        markup=Markup(self.client.get("/admin/statistics?role=admin").text)
        departments=[n for n in markup.nodes if "data-report-department" in n["attrs"]]
        self.assertEqual(len(departments),4)
        details=[n for n in markup.nodes if n["tag"]=="details" and "report-department-details" in n["attrs"].get("class","")]
        self.assertEqual(len(details),4)
        self.assertTrue(all("open" not in n["attrs"] for n in details))
        self.assertTrue(all(any(n["tag"]=="summary" and n["parent"] is detail for n in markup.nodes) for detail in details))
        ids=[n["attrs"]["id"] for n in markup.nodes if n["attrs"].get("id")]
        self.assertEqual(len(ids),len(set(ids)))
        root=next(n for n in markup.nodes if "data-report-root" in n["attrs"])
        def inside(node):
            p=node["parent"]
            while p:
                if p is root:return True
                p=p["parent"]
            return False
        self.assertFalse(any(n["tag"] in ("form","input","textarea","select","canvas") and inside(n) for n in markup.nodes))
        self.assertTrue(all(n["attrs"].get("datetime") for n in markup.nodes if n["tag"]=="time"))

    def test_average_inclusions_exclusions_and_no_data_come_from_context(self):
        markup=Markup(self.client.get("/reports/department?role=head&report_case=no-average").text)
        group=next(n for n in markup.nodes if n["attrs"].get("data-report-average")=="overall")
        text="".join(group["text"])
        self.assertIn("Нет данных",text)
        self.assertIn("Учтено выполненных0",text)
        self.assertIn("Исключено завершённых8",text)
        normal=Markup(self.client.get("/reports/department?role=head").text)
        group=next(n for n in normal.nodes if n["attrs"].get("data-report-average")=="overall")
        self.assertIn("Учтено выполненных6","".join(group["text"]))
        self.assertIn("Исключено завершённых2","".join(group["text"]))

    def test_department_names_are_escaped_in_headings_and_captions(self):
        from unittest.mock import patch
        from preview.fixtures import report_fixture
        own,overall=report_fixture()
        own["department"]["name"]='<img src=x onerror=alert(1)>'
        overall["departments"][0]["name"]=own["department"]["name"]
        with patch("preview.server.report_fixture",return_value=(own,overall)):
            for path in ("/reports/department?role=head","/admin/statistics?role=admin"):
                html=self.client.get(path).text
                self.assertIn("&lt;img",html)
                self.assertFalse(Markup(html).unexpected_images())

class AuditMarkupCase(unittest.TestCase):
    def setUp(self):
        self.app=create_preview_app(testing=True)
        self.client=self.app.test_client()

    def filter_fields(self,markup):
        form=markup.with_class("audit-filters")[0]
        fields=[]
        for node in markup.nodes:
            if node["tag"] not in ("input","select"):continue
            parent=node["parent"]
            while parent and parent is not form:parent=parent["parent"]
            if parent is form:fields.append(node["attrs"].get("name"))
        return fields

    def test_native_history_disclosures_and_immutable_entries(self):
        markup=Markup(self.client.get("/admin/audit?role=admin").text)
        self.assertEqual(len(markup.with_class("audit-entry")),8)
        pairs=[(n["attrs"]["data-event-source"],n["attrs"]["data-event-id"]) for n in markup.with_class("audit-entry")]
        self.assertEqual(len(set(pairs)),8)
        details=markup.with_class("audit-changes")
        self.assertEqual(len(details),7)
        for node in details:
            self.assertEqual(node["tag"],"details")
            self.assertTrue(any(n["tag"]=="summary" and n["parent"] is node for n in markup.nodes))
        for node in markup.nodes:
            if node["tag"] in ("form","input","button","textarea","select"):
                parent=node["parent"]
                while parent:
                    self.assertNotIn("audit-entry",parent["attrs"].get("class","").split())
                    parent=parent["parent"]
        self.assertTrue(all(n["attrs"]["datetime"].endswith("+00:00") for n in markup.nodes if n["tag"]=="time"))
        self.assertEqual(sum(n["tag"]=="h1" for n in markup.nodes),1)
        ids=[n["attrs"]["id"] for n in markup.nodes if "id" in n["attrs"]]
        self.assertEqual(len(ids),len(set(ids)))
        self.assertIn("Было", "".join(markup.with_class("audit-change")[0]["text"]))
        self.assertIn("Стало", "".join(markup.with_class("audit-change")[0]["text"]))

    def test_filter_labels_errors_and_get_protocol(self):
        response=self.client.get("/admin/audit?role=admin&date_from=2026-10-28&date_to=2026-10-27")
        self.assertEqual(response.status_code,400)
        markup=Markup(response.text)
        summary=markup.with_class("error-summary")[0]
        self.assertEqual(summary["attrs"]["role"],"alert")
        self.assertEqual(summary["attrs"]["tabindex"],"-1")
        self.assertNotIn("hidden",summary["attrs"])
        field=next(n for n in markup.nodes if n["attrs"].get("id")=="field-date_to")
        self.assertEqual(field["attrs"]["aria-invalid"],"true")
        self.assertIn("field-date_to-error",field["attrs"]["aria-describedby"])
        for name in ("user_id","date_from","date_to"):
            self.assertTrue(any(n["tag"]=="label" and n["attrs"].get("for")=="field-"+name for n in markup.nodes))
        form=markup.with_class("audit-filters")[0]
        self.assertEqual(form["attrs"]["method"],"get")
        self.assertIn("data-ui-form",form["attrs"])
        self.assertNotIn("csrf_token",self.filter_fields(markup))
        self.assertNotIn("page",self.filter_fields(markup))
        self.assertIn('value="2026-10-28"',response.text)
        self.assertIn('value="2026-10-27"',response.text)

    def test_event_values_and_invalid_filter_values_are_escaped(self):
        from preview.fixtures import audit_fixture
        journal=audit_fixture()
        attack='<img src=x onerror="bad()">'
        journal["entries"][0].update(actor_name=attack,actor_department=attack,action=attack,comment=attack,
            changes=[(attack,attack,attack)])
        with patch("preview.server.audit_fixture",return_value=journal):
            html=self.client.get("/admin/audit?role=admin").text
        self.assertIn("&lt;img",html)
        self.assertFalse(Markup(html).unexpected_images())
        error=self.client.get("/admin/audit",query_string={"role":"admin","user_id":attack,"date_from":attack})
        self.assertEqual(error.status_code,400)
        self.assertIn("&lt;img",error.text)
        self.assertFalse(Markup(error.text).unexpected_images())
        self.assertNotIn(attack,error.text)

    def test_long_values_and_production_context_use_native_controls(self):
        html=self.client.get("/admin/audit?role=admin&audit_case=long").text
        self.assertIn("ОченьДлинноеЗначениеБезПробеловДляПроверкиПереноса",html)
        self.assertIn("Описание",html)
        from preview.fixtures import audit_fixture
        from flask import render_template
        journal=audit_fixture()
        with self.app.test_request_context("/admin/audit?role=admin"):
            self.app.preprocess_request()
            production_html=render_template("admin/audit.html",journal=journal,filters=journal["filters"],
                error=None,preview_mode=False)
        markup=Markup(production_html)
        form=markup.with_class("audit-filters")[0]
        fields=self.filter_fields(markup)
        self.assertEqual(fields,["user_id","date_from","date_to"])
        self.assertEqual(form["attrs"]["method"],"get")
        self.assertNotIn("Демонстрационные события",production_html)


if __name__ == "__main__":
    unittest.main()
