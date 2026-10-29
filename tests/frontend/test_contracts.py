"""Form and shell integration contracts, not production authorization tests."""

from html.parser import HTMLParser
from datetime import timedelta
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, urlsplit

from flask import Flask, g, render_template, render_template_string
from jinja2 import DictLoader

from preview.server import ROOT, create_preview_app
from preview.fixtures import ALL_PAGES, DEMO_NOW, PAGES, READY_PAGES, TASK_PAGES, detail_context, ordered_tasks, presentation_user, task_datetime


class Tags(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class ContractsCase(unittest.TestCase):
    def setUp(self):
        self.app = create_preview_app(testing=True)
        self.client = self.app.test_client()

    def test_login_keeps_native_form_contract_and_one_ui_owner(self):
        tags = Tags(self.client.get("/login").text).tags
        forms = [attrs for tag, attrs in tags if tag == "form"]
        self.assertEqual(len(forms), 1)
        self.assertEqual(forms[0]["method"], "post")
        self.assertIn("data-ui-form", forms[0])
        self.assertNotIn("novalidate", forms[0])  # Native validation when JS is absent.
        inputs = {attrs.get("name"): attrs for tag, attrs in tags if tag == "input"}
        self.assertEqual(set(inputs), {"csrf_token", "username", "password"})
        self.assertTrue(inputs["csrf_token"]["value"])
        self.assertEqual(inputs["password"]["type"], "password")
        self.assertEqual(inputs["password"]["value"], "")
        self.assertEqual(inputs["username"]["autocomplete"], "username")
        self.assertEqual(inputs["password"]["autocomplete"], "current-password")
        self.assertIn("required", inputs["username"])
        self.assertIn("required", inputs["password"])
        scripts = [attrs["src"] for tag, attrs in tags if tag == "script" and "src" in attrs]
        self.assertEqual(scripts, ["/static/ui.js"])

    def test_login_errors_are_inline_and_associated_without_summary(self):
        html = self.client.get("/login?scenario=invalid-login").text
        tags = Tags(html).tags
        inputs = {attrs.get("name"): attrs for tag, attrs in tags if tag == "input"}
        links = {attrs.get("href") for tag, attrs in tags if tag == "a"}
        errors = {attrs.get("id"): attrs for _, attrs in tags if attrs.get("class") == "field-error"}
        summaries = [attrs for _, attrs in tags if "data-error-summary" in attrs]
        self.assertFalse(summaries)
        self.assertTrue(any(tag=="form" and "data-inline-errors-only" in attrs for tag,attrs in tags))
        for name in ("username", "password"):
            field = inputs[name]
            self.assertEqual(field.get("aria-invalid"), "true")
            self.assertIn(field["id"] + "-error", field["aria-describedby"].split())
            self.assertNotIn("hidden", errors[field["id"] + "-error"])

    def test_base_login_renders_without_preview_globals_or_auth_extension(self):
        plain = Flask("template_contract", template_folder=str(ROOT / "templates"),
                      static_folder=str(ROOT / "static"))
        plain.add_url_rule("/", endpoint="index", view_func=lambda: "")
        plain.jinja_env.filters["task_datetime"] = task_datetime
        with plain.test_request_context("/login"):
            g.user = None
            html = render_template("login.html", csrf_token=lambda: "synthetic-csrf")
        self.assertIn("Вход в платформу", html)
        self.assertNotIn("Демонстрация интерфейса", html)
        self.assertNotIn("/__preview__/", html)

    def test_missing_staff_url_is_explicitly_unavailable_not_an_invented_route(self):
        @self.app.context_processor
        def no_staff_contract():
            return {"staff_url": None}
        html = self.client.get("/__preview__/?role=head").text
        tags = Tags(html).tags
        unavailable = [attrs for _, attrs in tags if "nav-unavailable" in attrs.get("class", "")]
        self.assertEqual(len(unavailable), 1)
        self.assertEqual(unavailable[0]["aria-disabled"], "true")
        self.assertNotIn("href", unavailable[0])

    def test_password_macro_never_prefills_a_value(self):
        with self.app.test_request_context("/login"):
            html = render_template_string(
                "{% from 'components/forms.html' import field %}"
                "{{ field('secret','Пароль',input_type='password',value='must-not-render') }}")
        self.assertNotIn("must-not-render", html)
        self.assertIn('value=""', html)

    def test_department_uses_existing_current_user_contract_not_a_new_g_user_field(self):
        @self.app.before_request
        def mapping_user():
            g.user = {"id": 2, "role": "head", "full_name": "Начальник (демо)", "department_id": 1}

        @self.app.context_processor
        def model_user():
            return {"current_user": presentation_user("head")}

        html = self.client.get("/__preview__/?role=head").text
        self.assertRegex(html, r'(?s)class="account-panel".*?Маркетинг \(демо\).*?</p>')

    def test_imported_components_refresh_after_debug_is_disabled(self):
        sources = {"frame": "{% import 'component' as ui %}{{ ui.label() }}",
                   "component": "{% macro label() %}До изменения{% endmacro %}"}
        self.app.jinja_loader = DictLoader(sources)
        self.app.debug = False  # Same transition as app.run(debug=False).
        environment = self.app.jinja_env
        self.assertEqual(environment.get_template("frame").render(), "До изменения")
        sources["component"] = "{% macro label() %}После изменения{% endmacro %}"
        self.assertEqual(environment.get_template("frame").render(), "После изменения")

    def test_preview_banner_is_single_and_assets_are_served_locally(self):
        html = self.client.get("/__preview__/?role=head").text
        self.assertEqual(html.count('aria-label="Демонстрация интерфейса"'), 1)
        response = self.client.get("/static/ui.js")
        try:
            self.assertEqual(response.status_code, 200)
            self.assertIn("pageshow", response.text)
        finally:
            response.close()


class TaskContractsCase(unittest.TestCase):
    def setUp(self):
        self.client = create_preview_app(testing=True).test_client()

    def test_ready_routes_and_pending_routes_partition_the_catalog(self):
        self.assertEqual({p.endpoint for p in READY_PAGES} | {p.endpoint for p in PAGES}, {p.endpoint for p in ALL_PAGES})
        self.assertFalse({p.endpoint for p in READY_PAGES} & {p.endpoint for p in PAGES})
        self.assertEqual(self.client.get('/tasks/new').status_code,200)
        for page in TASK_PAGES:
            response = self.client.get(page.path)
            self.assertEqual(response.status_code, 200)
            self.assertIn('data-task-view="board"', response.text)
        for page in PAGES:
            self.assertEqual(self.client.get(page.path).status_code, 503)
        self.assertEqual(self.client.get('/tasks/1000').status_code, 200)
        self.assertEqual(self.client.post('/tasks/1000/complete').status_code, 409)

    def test_controls_keep_role_direction_scope_and_scenario(self):
        html = self.client.get('/tasks/completed?role=head&size=300&scope=sent&view=list&page=2').text
        tags = Tags(html).tags
        links = [attrs['href'] for tag, attrs in tags if tag == 'a' and attrs.get('href','').startswith('/tasks/completed')]
        self.assertTrue(links)
        for link in links:
            query = parse_qs(urlsplit(link).query)
            self.assertEqual(query.get('role'), ['head'])
            self.assertEqual(query.get('size'), ['300'])
        next_links = [parse_qs(urlsplit(link).query) for link in links if parse_qs(urlsplit(link).query).get('page') == ['3']]
        self.assertEqual(len(next_links), 1)
        self.assertEqual(next_links[0]['scope'], ['sent'])
        self.assertEqual(next_links[0]['view'], ['list'])
        self.assertNotIn('name="filter"', html)
        self.assertNotIn('name="search"', html)

    def test_fragment_has_full_projection_identity_and_no_active_content(self):
        response = self.client.get('/tasks/completed?role=admin&size=300&fragment=board')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['X-Task-Board'], 'completed')
        self.assertEqual(response.headers['X-Task-User'], '3')
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
        self.assertIn('Cookie',response.headers['Vary'])
        tags = Tags(response.text).tags
        self.assertFalse([tag for tag, _ in tags if tag in ('script','iframe','form')])
        self.assertEqual(len([1 for tag, attrs in tags if tag == 'article' and 'data-task-id' in attrs]), 300)

    def test_unsupported_scenario_or_fragment_is_rejected(self):
        for query in ('size=-1', 'size=301', 'size=wrong', 'view=unknown', 'scope=unknown', 'fragment=wrong', 'view=list&fragment=board'):
            with self.subTest(query=query):
                self.assertEqual(self.client.get('/tasks/received?' + query).status_code, 400)

    def test_fixture_order_resolves_unknown_dates_and_ties(self):
        now = DEMO_NOW
        def row(task_id, priority='high', due=now, created=now, completed=None, closed=None):
            return SimpleNamespace(id=task_id, priority=priority, due_at=due, created_at=created,
                                   completed_at=completed, closed_at=closed, status='completed')
        active = [row(1, due=None), row(2, due=now + timedelta(hours=1)), row(3), row(4), row(5, priority='urgent', due=None), row(6, created=now + timedelta(minutes=1))]
        self.assertEqual([r.id for r in ordered_tasks(active)], [5, 6, 4, 3, 2, 1])
        closed = [row(1), row(2, closed=now), row(3, completed=now - timedelta(days=1)), row(4, closed=now)]
        self.assertEqual([r.id for r in ordered_tasks(closed, closed=True)], [3, 4, 2, 1])


class TaskReadingContractsCase(unittest.TestCase):
    def test_board_uses_full_page_links_without_panel_and_preserves_return_context(self):
        client=create_preview_app(testing=True).test_client()
        for path in ("/tasks/received","/tasks/sent","/tasks/completed?scope=sent","/tasks/sent?view=list&page=2"):
            response=client.get(path+("&" if "?" in path else "?")+"role=head&size=36")
            self.assertEqual(response.status_code,200)
            self.assertNotIn("task-panel-dialog",response.text)
            self.assertNotIn("task-panel.js",response.text)
            links=[attrs["href"] for tag,attrs in Tags(response.text).tags
                   if tag=="a" and "origin_section=" in attrs.get("href","") and "task-open" in attrs.get("class","")]
            self.assertTrue(links)
            detail=client.get(links[0])
            self.assertEqual(detail.status_code,200)
            back=next(attrs["href"] for tag,attrs in Tags(detail.text).tags if "reading-back" in attrs.get("class",""))
            self.assertEqual(urlsplit(back).path,urlsplit(path).path)
            args=parse_qs(urlsplit(back).query)
            if "scope=sent" in path:self.assertEqual(args["scope"],["sent"])
            if "view=list" in path:
                self.assertEqual(args["view"],["list"])
                self.assertEqual(args["page"],["2"])
            self.assertEqual(client.get(back).status_code,200)

    def setUp(self):
        self.client = create_preview_app(testing=True).test_client()

    def test_private_fragment_identity_and_read_only_semantics(self):
        for role in ("employee", "head", "admin"):
            for task_id in (1000, 1001, 1005, 3000, 3001, 3002):
                with self.subTest(role=role, task=task_id):
                    response = self.client.get(f"/tasks/{task_id}/panel?role={role}")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers["X-Task-Panel"], str(task_id))
                    self.assertEqual(response.headers["X-Task-User"], str(presentation_user(role).id))
                    self.assertIn("private", response.headers["Cache-Control"])
                    self.assertIn("no-store", response.headers["Cache-Control"])
                    self.assertIn("Cookie", response.headers["Vary"])
                    tags = Tags(response.text).tags
                    roots = [attrs for _, attrs in tags if "data-task-panel-content" in attrs]
                    self.assertEqual([node["data-task-id"] for node in roots], [str(task_id)])
                    self.assertFalse(set(tag for tag, _ in tags) & {"form","script","iframe","input","button"})
                    self.assertFalse(any("data-panel-action" in attrs for _, attrs in tags))
                    for tag, attrs in tags:
                        self.assertFalse(any(name.lower().startswith("on") for name in attrs))
                        if tag == "a":
                            destination = urlsplit(attrs["href"])
                            self.assertFalse(destination.scheme or destination.netloc)
                            self.assertEqual(parse_qs(destination.query)["role"], [role])
                    self.assertNotIn('name="password"', response.text)

    def test_panel_and_full_page_share_all_information_and_downloads(self):
        for role in ("employee", "head", "admin"):
            for task_id in (1000, 1001, 1005, 3000, 3001, 3002):
                with self.subTest(role=role, task=task_id):
                    full = self.client.get(f"/tasks/{task_id}?role={role}")
                    panel = self.client.get(f"/tasks/{task_id}/panel?role={role}")
                    self.assertEqual(full.status_code, 200)
                    context = detail_context(role, task_id)
                    task = context["task"]
                    for text in (task.title, task.description, task.sender_department.name,
                                 task.recipient_department.name, task.author.full_name,
                                 "История задачи",
                                 f"Материалы · {len(task.attachments)}",
                                 f"Комментарии · {len(task.comments)}"):
                        self.assertIn(text, full.text)
                        self.assertIn(text, panel.text)
                    for comment in task.comments:
                        self.assertIn(comment.body, full.text)
                        self.assertIn(comment.body, panel.text)
                    for entry in context["history_entries"]:
                        for text in (entry.label, entry.actor, entry.comment):
                            self.assertIn(text, full.text)
                            self.assertIn(text, panel.text)
                    for response in (full, panel):
                        for attachment in task.attachments:
                            self.assertIn(f'/tasks/attachments/{attachment.id}?role={role}', response.text)
                        self.assertNotIn('name="comment"', response.text)
                        self.assertNotIn('name="due_at"', response.text)

    def test_downloads_are_real_private_files_and_missing_files_are_404(self):
        for role, task_id in (("head",1001),("employee",3002),("admin",1005)):
            context = detail_context(role, task_id)
            for attachment in context["task"].attachments:
                response = self.client.get(f"/tasks/attachments/{attachment.id}?role={role}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, "application/pdf")
                self.assertEqual(len(response.data), attachment.size_bytes)
                self.assertTrue(response.data.startswith(b"%PDF-1.4"))
                self.assertTrue(response.data.endswith(b"%%EOF\n"))
                self.assertIn(b"xref\n0 6\n", response.data)
                self.assertIn("attachment;", response.headers["Content-Disposition"])
                self.assertIn("filename*=UTF-8", response.headers["Content-Disposition"])
                self.assertIn("private", response.headers["Cache-Control"])
                self.assertIn("no-store", response.headers["Cache-Control"])
        for path in ("/tasks/999999", "/tasks/999999/panel", "/tasks/attachments/999999",
                     "/tasks/attachments/10019"):
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_viewing_and_downloading_do_not_change_revision_or_history(self):
        before = detail_context("head", 1001)
        panel = self.client.get("/tasks/1001/panel?role=head").data
        full = self.client.get("/tasks/1001?role=head").data
        self.client.get("/tasks/attachments/10011?role=head")
        self.assertEqual(self.client.get("/tasks/1001/panel?role=head").data, panel)
        self.assertEqual(self.client.get("/tasks/1001?role=head").data, full)
        after = detail_context("head", 1001)
        self.assertEqual(before["task"].revision, after["task"].revision)
        self.assertEqual([vars(event) for event in before["history_entries"]],
                         [vars(event) for event in after["history_entries"]])
        for path in ("/tasks/1001", "/tasks/1001/panel", "/tasks/1001/start"):
            self.assertIn(self.client.post(path).status_code, (404,405))

    def test_fault_harness_covers_retry_identity_and_invalid_fragment(self):
        def panel(case):
            return self.client.get(f"/tasks/1001/panel?role=head&panel_case={case}&probe=unit")
        for case, code in (("error",503),("401",401),("403",403),("404",404),("login",302)):
            response = panel(case)
            self.assertEqual(response.status_code, code)
            self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(panel("retry").status_code, 503)
        self.assertEqual(panel("retry").status_code, 200)
        self.assertEqual(panel("user").headers["X-Task-User"], "999")
        self.assertNotIn("X-Task-User", panel("missing-user").headers)
        self.assertEqual(panel("id").headers["X-Task-Panel"], "1002")
        self.assertIn("<form>", panel("form").text)
        self.assertIn("javascript:", panel("unsafe").text)
        self.assertEqual(panel("slow").status_code, 200)
        self.assertEqual(panel("stale").status_code, 200)
        self.assertEqual(panel("unknown").status_code, 400)

class TaskCreationContractsCase(unittest.TestCase):
    def setUp(self):
        self.client = create_preview_app(testing=True).test_client()

    def payload(self, role="head", case=""):
        response = self.client.get("/tasks/new",query_string={"role":role,"create_case":case})
        inputs = {a.get("name"):a for t,a in Tags(response.text).tags if t=="input"}
        return dict(csrf_token=inputs["csrf_token"]["value"],creation_key=inputs["creation_key"]["value"],
                    recipient_department_id="2",title="Материалы для встречи",description="Подготовить перечень участников.",
                    due_at="2026-11-02T18:00",priority="normal")

    def test_fields_default_policy_and_native_post_contract(self):
        for role in ("employee","head"):
            response=self.client.get("/tasks/new?role="+role)
            tags=Tags(response.text).tags
            forms=[a for t,a in tags if t=="form" and "data-task-create" in a]
            self.assertEqual(len(forms),1)
            self.assertEqual(forms[0]["method"],"post")
            self.assertEqual(forms[0]["enctype"],"multipart/form-data")
            self.assertIn("/tasks/new?role="+role,forms[0]["action"])
            self.assertNotIn("novalidate",forms[0])
            fields={a.get("name"):a for t,a in tags if t in {"input","select","textarea"} and a.get("name")}
            self.assertEqual(set(fields),{"csrf_token","creation_key","recipient_department_id","title","description","priority","due_at","attachment"})
            for name in ("recipient_department_id","title","description","due_at"):
                self.assertIn("required",fields[name])
            for name in ("attachment",):
                self.assertNotIn("required",fields[name])
            self.assertEqual(fields["due_at"]["type"],"datetime-local")
            self.assertRegex(fields["creation_key"]["value"],r"^[0-9a-f]{64}$")
            self.assertEqual(fields["attachment"]["accept"],".pdf,.docx,.xlsx,.pptx,.jpg,.png")
            self.assertIn("multiple",fields["attachment"])
            self.assertTrue(any(t=="option" and a.get("value")=="normal" and "selected" in a for t,a in tags))
            self.assertNotIn("черновик",response.text.lower())
            self.assertNotIn('name="assignee_id"',response.text)
            self.assertNotIn('name="comment"',response.text)

    def test_only_confirmed_response_redirects_to_demo_receipt(self):
        data=self.payload()
        response=self.client.post("/tasks/new?role=head",data=data)
        self.assertEqual(response.status_code,303)
        self.assertIn("receipt="+data["creation_key"],response.location)
        receipt=self.client.get(response.location)
        self.assertEqual(receipt.status_code,200)
        self.assertIn("Демонстрационная отправка подтверждена",receipt.text)
        self.assertIn("Рабочая задача не создавалась",receipt.text)
        self.assertIn(data["title"],receipt.text)
        self.assertIn("02.11.2026 18:00",receipt.text)
        self.assertIn("private",receipt.headers["Cache-Control"])
        self.assertNotIn("data-task-create",receipt.text)
        self.assertNotEqual(self.payload()["creation_key"],data["creation_key"])

    def test_required_validation_and_errors_preserve_key_and_text(self):
        for name in ("recipient_department_id","title","description","due_at"):
            data=self.payload()
            data[name]=""
            response=self.client.post("/tasks/new?role=head",data=data)
            self.assertEqual(response.status_code,400)
            tags=Tags(response.text).tags
            fields={a.get("name"):a for t,a in tags if t in {"input","select","textarea"}}
            self.assertEqual(fields["creation_key"]["value"],data["creation_key"])
            self.assertEqual(fields[name]["aria-invalid"],"true")
            self.assertIn(name+"-error",fields[name]["aria-describedby"])
            self.assertTrue(any(a.get("href")=="#"+name for t,a in tags if t=="a"))
            self.assertNotIn("data-creation-receipt",response.text)
        data=self.payload()
        data.update(title='<svg onload="alert(1)">',description="<script>unsafe</script>",due_at="invalid")
        response=self.client.post("/tasks/new?role=head",data=data)
        self.assertIn("&lt;script&gt;unsafe",response.text)
        self.assertNotIn("<script>unsafe",response.text)
        self.assertIn("&lt;svg",response.text)
        self.assertEqual(response.status_code,400)

    def test_uncertain_retry_same_key_is_idempotent_and_changed_payload_conflicts(self):
        data=self.payload(case="uncertain")
        url="/tasks/new?role=head&create_case=uncertain"
        first=self.client.post(url,data=data)
        self.assertEqual(first.status_code,503)
        self.assertIn("Результат отправки не подтверждён",first.text)
        self.assertIn(data["creation_key"],first.text)
        self.assertNotIn("data-creation-receipt",first.text)
        second=self.client.post(url,data=data)
        third=self.client.post(url,data=data)
        self.assertEqual(second.status_code,303)
        self.assertEqual(third.location,second.location)
        changed=self.client.post(url,data={**data,"description":"Изменённое содержание"})
        self.assertEqual(changed.status_code,409)
        self.assertIn(data["creation_key"],changed.text)
        self.assertNotIn("data-creation-receipt",changed.text)

    def test_file_rejection_413_and_repeated_optional_uploads(self):
        from io import BytesIO
        data=self.payload()
        response=self.client.post("/tasks/new?role=head",data={**data,"attachment":[
            (BytesIO(b"synthetic-pdf"),"plan.pdf"),(BytesIO(b"synthetic-png"),"chart.png")]})
        self.assertEqual(response.status_code,303)
        self.assertIn("<dd>2</dd>",self.client.get(response.location).text)
        for case,code in (("file-error",400),("too-large",413)):
            data=self.payload(case=case)
            error=self.client.post("/tasks/new?role=head&create_case="+case,data=data)
            self.assertEqual(error.status_code,code)
            self.assertIn("выберите",error.text.lower())
            self.assertIn(data["creation_key"],error.text)
            self.assertIn('aria-invalid="true"',error.text)
        for filename,content in (("bad.json",b"{}"),("large.pdf",b"x"*(20*1024*1024+1))):
            data=self.payload()
            error=self.client.post("/tasks/new?role=head",data={**data,"attachment":(BytesIO(content),filename)})
            error.request.environ["wsgi.input"].close()
            self.assertEqual(error.status_code,400)
            self.assertIn("attachment-error",error.text)

    def test_admin_archived_and_cross_identity_do_not_get_or_send_form(self):
        for url in ("/tasks/new?role=admin","/tasks/new?role=head&create_case=archived"):
            for method in (self.client.get,self.client.post):
                response=method(url)
                self.assertEqual(response.status_code,403)
                self.assertNotIn("data-task-create",response.text)
                self.assertNotIn('name="creation_key"',response.text)
        data=self.payload()
        self.assertEqual(self.client.post("/tasks/new?role=employee",data=data).status_code,409)
        data["creation_key"]="unissued"
        response=self.client.post("/tasks/new?role=head",data=data)
        self.assertEqual(response.status_code,409)
        self.assertIn('value="unissued"',response.text)
        self.assertEqual(self.client.get("/tasks/new?role=head&receipt=unknown").status_code,404)

    def test_rejects_wrong_fields_date_csrf_and_explains_conflict(self):
        for mutation in ({"comment":"extra"},{"due_at":"2026-11-02T18:00:15"},{"csrf_token":"wrong"},
                         {"priority":"urgentest"},{"recipient_department_id":"1"},{"category_id":"99"}):
            data=self.payload()
            response=self.client.post("/tasks/new?role=head",data={**data,**mutation})
            self.assertEqual(response.status_code,400)
            self.assertNotIn("data-creation-receipt",response.text)
        for case in ("conflict","unavailable"):
            data=self.payload(case=case)
            response=self.client.post("/tasks/new?role=head&create_case="+case,data=data)
            self.assertEqual(response.status_code,409)
            self.assertIn(data["creation_key"],response.text)
        self.assertEqual(self.client.get("/tasks/new?create_case=unknown").status_code,400)

class TaskActionContractsCase(unittest.TestCase):
    def setUp(self):
        self.client=create_preview_app(testing=True).test_client()

    def url(self,task=1000,role="head",probe="test",case=""):
        from urllib.parse import urlencode
        return f"/tasks/{task}?"+urlencode(dict(role=role,actions="demo",probe=probe,action_case=case))

    def state(self,task=1000,role="head",probe="test",case=""):
        response=self.client.get(self.url(task,role,probe,case))
        states=[a for t,a in Tags(response.text).tags if "data-board-task" in a]
        import json
        return json.loads(states[0]["data-board-task"]) if states else None

    def post(self,key,task=1000,role="head",probe="test",case="",**extra):
        from urllib.parse import urlencode
        path={"comment":"comments","upload":"attachments"}.get(key,key)
        data=dict(csrf_token="frontend-preview-only-not-an-auth-token",revision="1",comment="Проверка действия на демоданных.")
        data.update(extra)
        return self.client.post(f"/tasks/{task}/{path}?"+urlencode(dict(role=role,actions="demo",probe=probe,action_case=case)),data=data)

    def board_card(self,task,role="head",probe="test",direction="received",scope="received"):
        import json
        response=self.client.get(f"/tasks/{direction}?role={role}&actions=demo&probe={probe}&view=board&fragment=board&scope={scope}")
        cards=[json.loads(a["data-board-task"]) for t,a in Tags(response.text).tags if "data-board-task" in a]
        return next((card for card in cards if card["id"]==task),None)

    def test_canonical_action_flags_and_profile_matrix(self):
        expected={(1000,"head"):{"assign","claim","reject","comment","upload"},
                  (1000,"employee"):{"claim","comment","upload"},
                  (1001,"employee"):{"release","complete","reject","comment","upload"},
                  (1005,"head"):{"reassign","reject","comment","upload"},
                  (2000,"head"):{"cancel","deadline","comment","upload"},
                  (4000,"head"):{"return"}}
        for (task,role),keys in expected.items():
            state=self.state(task,role)
            self.assertEqual({a["key"] for a in state["actions"]},keys)
            for action in state["actions"]:
                parsed=urlsplit(action["url"])
                suffix={"comment":"comments","upload":"attachments"}.get(action["key"],action["key"])
                self.assertEqual(parsed.path,f"/tasks/{task}/{suffix}")
                self.assertFalse(parsed.scheme or parsed.netloc)
                self.assertEqual(action["assignee"],action["key"] in ("assign","reassign"))
                self.assertEqual(action["deadline"],action["key"]=="deadline")
            if task==1005:
                self.assertNotIn(21,[c["id"] for c in state["candidates"]])
        for task in (1000,1001,4000):
            self.assertIsNone(self.state(task,"admin"))
        self.assertIsNone(self.state(3000,"head"))
        self.assertNotIn("data-board-task",self.client.get("/tasks/1000?role=head&actions=empty").text)

    def test_all_nine_actions_update_only_synthetic_projection(self):
        cases=[("assign",1000,"head",{"assignee_ids":["20"]},"in_progress"),
               ("claim",1000,"employee",{},"in_progress"),
               ("reassign",1001,"head",{"assignee_ids":["22"]},"in_progress"),
               ("release",1001,"employee",{},"new"),
               ("complete",1001,"head",{},"completed"),
               ("return",4000,"head",{},"in_progress"),
               ("cancel",2000,"head",{},"cancelled"),
               ("reject",1000,"head",{},"rejected"),
               ("deadline",2000,"head",{"due_at":"2026-11-05T18:30"},"new")]
        for key,task,role,extra,status in cases:
            with self.subTest(action=key):
                response=self.post(key,task,role,probe=key,**extra)
                self.assertEqual(response.status_code,303)
                full=self.client.get(response.location)
                self.assertEqual(full.status_code,200)
                self.assertIn("badge status-"+status,full.text)
                outgoing=task>=2000
                card=self.board_card(task,role,key,
                    "completed" if status in ("completed","cancelled","rejected") else ("sent" if outgoing else "received"),
                    "sent" if outgoing else "received")
                self.assertEqual(card["status"],status)
                self.assertEqual(card["revision"],2)
                if key=="return":
                    self.assertIn("На доработке",full.text)
                    self.assertNotIn('class="task-fact reading-closed"',full.text)
                if key=="deadline":
                    self.assertEqual(card["due_at"],"2026-11-05T18:30")
                if key in ("release","claim","assign","reassign"):
                    self.assertEqual(card["assignee_ids"],{"release":[],"claim":[1],"assign":[20],"reassign":[22]}[key])

    def test_closed_board_and_panel_keep_return_on_full_page_only(self):
        response=self.client.get("/tasks/completed?role=head&actions=demo&scope=sent&fragment=board")
        import json
        for tag,attrs in Tags(response.text).tags:
            if "data-board-task" in attrs:
                self.assertEqual(json.loads(attrs["data-board-task"])["actions"],[])
        panel=self.client.get("/tasks/4000/panel?role=head&actions=demo")
        self.assertIn("#task-permitted-actions",panel.text)
        self.assertNotIn("data-panel-action",panel.text)
        self.assertFalse({t for t,a in Tags(panel.text).tags}&{"form","script","iframe"})
        self.assertEqual(panel.headers["X-Task-Panel"],"4000")
        self.assertIn("no-store",panel.headers["Cache-Control"])

    def test_comment_files_are_one_comment_and_download_exact_bytes(self):
        from io import BytesIO
        response=self.post("upload",1001,attachment=[(BytesIO(b"%PDF-demo"),"Материалы.pdf"),(BytesIO(b"png-demo"),"Схема.png")])
        self.assertEqual(response.status_code,303)
        full=self.client.get(response.location)
        self.assertIn("Материалы.pdf",full.text)
        self.assertIn("Схема.png",full.text)
        self.assertIn("Комментарии · 2",full.text)
        self.assertIn("Материалы · 3",full.text)
        card=self.board_card(1001)
        self.assertEqual((card["revision"],card["comment_count"],card["attachment_count"]),(2,2,3))
        self.assertEqual(card["status"],"in_progress")
        urls=[a["href"] for t,a in Tags(full.text).tags if t=="a" and a.get("href","").startswith("/tasks/attachments/100110")]
        self.assertTrue(urls)
        data={self.client.get(url).data for url in urls}
        self.assertEqual(data,{b"%PDF-demo",b"png-demo"})
        self.assertEqual(self.post("comment",1001,revision="1").status_code,409)
        self.assertEqual(self.board_card(1001)["comment_count"],2)

    def test_required_comment_revision_csrf_and_extra_fields_are_rejected(self):
        for fields in ({"comment":""},{"comment":"   "},{"csrf_token":"invalid"},{"assignee_id":"20"}):
            response=self.post("claim",**fields)
            self.assertEqual(response.status_code,400)
            self.assertEqual(self.board_card(1000)["revision"],1)
        self.assertEqual(self.post("assign").status_code,400)
        self.assertEqual(self.post("assign",assignee_id="21").status_code,400)
        self.assertEqual(self.post("deadline",2000,due_at="not-date").status_code,400)
        self.assertEqual(self.post("upload").status_code,400)
        self.assertEqual(self.post("complete",1005).status_code,403)
        self.assertEqual(self.post("claim",role="admin").status_code,403)

    def test_error_cases_preserve_comment_and_conflict_requires_new_read(self):
        for case,code in (("400",400),("413",413),("unavailable",409),("403",403),("404",404)):
            response=self.post("claim",probe=case,case=case)
            self.assertEqual(response.status_code,code)
            self.assertIn("no-store",response.headers["Cache-Control"])
            self.assertEqual(self.board_card(1000,probe=case)["revision"],1)
        response=self.post("claim",case="409",probe="conflict",comment="Сохраните мой текст")
        self.assertEqual(response.status_code,409)
        self.assertIn("Сохраните мой текст",response.text)
        self.assertEqual(self.board_card(1000,probe="conflict")["revision"],2)
        self.assertEqual(self.post("claim",case="409",probe="conflict",revision="2").status_code,303)

    def test_saved_post_then_failed_get_only_needs_read_retry(self):
        response=self.post("claim",case="refresh-error",probe="read")
        self.assertEqual(response.status_code,303)
        url="/tasks/received?role=head&actions=demo&action_case=refresh-error&probe=read&fragment=board"
        self.assertEqual(self.client.get(url,headers={"X-Task-Read":"1"}).status_code,503)
        second=self.client.get(url,headers={"X-Task-Read":"1"})
        self.assertEqual(second.status_code,200)
        self.assertEqual(second.headers["X-Task-User"],"2")
        self.assertIn("Cookie",second.headers["Vary"])
        self.assertIn("private",second.headers["Cache-Control"])
        self.assertEqual(self.board_card(1000,probe="read")["comment_count"],1)
        self.assertEqual(self.post("claim",probe="read").status_code,403)
        self.assertEqual(self.board_card(1000,probe="read")["revision"],2)

    def test_uncertain_post_changes_fixture_but_never_reports_http_success(self):
        response=self.post("claim",case="uncertain",probe="uncertain")
        self.assertEqual(response.status_code,500)
        self.assertNotIn("Location",response.headers)
        card=self.board_card(1000,probe="uncertain")
        self.assertEqual((card["status"],card["revision"],card["comment_count"]),("in_progress",2,1))
        # Reading repeatedly cannot create another comment or event.
        self.assertEqual(self.board_card(1000,probe="uncertain"),card)


class PeopleContractsCase(unittest.TestCase):
    def test_duplicate_fields_and_files_are_rejected(self):
        from io import BytesIO
        from werkzeug.datastructures import MultiDict
        client=create_preview_app(testing=True).test_client()
        fields=[("csrf_token","frontend-preview-only-not-an-auth-token"),("full_name","Синтетический ввод"),
            ("username","duplicate.demo"),("password","synthetic-unused")]
        response=client.post("/staff/new?role=head",data=MultiDict(fields+[("username","another.demo")]))
        self.assertEqual(response.status_code,400)
        response=client.post("/staff/new?role=head",data=dict(fields,attachment=(BytesIO(b"synthetic"),"file.txt")))
        self.assertEqual(response.status_code,400)
        self.assertNotIn("duplicate.demo",client.get("/staff?role=head").text)

    def setUp(self):
        self.client=create_preview_app(testing=True).test_client()
        self.csrf={"csrf_token":"frontend-preview-only-not-an-auth-token"}

    def post(self,path,role="head",**values):
        return self.client.post(path+("?role="+role),data=dict(self.csrf,**values))

    def test_staff_scope_and_forbidden_profiles(self):
        html=self.client.get("/staff?role=head").text
        for name in ("Алия Садыкова","Данияр Ахметов","Мария Волкова"):self.assertIn(name,html)
        self.assertNotIn('data-account-id="2"',html)
        self.assertNotIn('data-account-id="30"',html)
        for role in ("employee","admin"):
            self.assertEqual(self.client.get("/staff?role="+role).status_code,403)
            self.assertEqual(self.client.post("/staff/22/disable?role="+role,data=self.csrf).status_code,403)
        for target in (2,3,30,999):
            self.assertEqual(self.client.get(f"/staff/{target}/edit?role=head").status_code,404)
            self.assertEqual(self.post(f"/staff/{target}/disable").status_code,404)
        self.assertEqual(self.client.get("/admin/users?role=head").status_code,403)
        self.assertEqual(self.client.get("/admin/users/new?role=employee").status_code,403)

    def test_staff_create_edit_disable_preserves_identity_and_never_restores(self):
        values=dict(full_name="Синтетический сотрудник",username="new.demo",password="synthetic-unused")
        response=self.post("/staff/new",**values)
        self.assertEqual(response.status_code,303)
        self.assertIn("Учётная запись сохранена",self.client.get(response.location).text)
        self.assertIn("Синтетический сотрудник",self.client.get("/staff?role=head").text)
        response=self.post("/staff/22/edit",full_name="Новое имя",username="daniyar.changed",password="")
        self.assertEqual(response.status_code,303)
        response=self.post("/staff/22/disable")
        self.assertEqual(response.status_code,303)
        html=self.client.get(response.location).text
        self.assertIn("Данные и история сохранены",html)
        self.assertIn("Новое имя",html)
        self.assertIn('data-account-id="22"',html)
        self.assertNotIn("/staff/22/unblock",html)
        self.assertEqual(self.client.post("/staff/22/unblock?role=head",data=self.csrf).status_code,404)

    def test_required_unique_extra_csrf_and_passwords_are_not_echoed(self):
        data=dict(full_name="Сохранённый ввод",username="aliya.demo",password="synthetic-unused")
        response=self.post("/staff/new",**data)
        self.assertEqual(response.status_code,400)
        self.assertIn('value="Сохранённый ввод"',response.text)
        self.assertIn("уже существует",response.text)
        self.assertNotIn('value="synthetic-unused"',response.text)
        for extras in ({"role":"head"},{"department_id":"2"},{"is_blocked":"true"},{"actor_id":"3"}):
            self.assertEqual(self.client.post("/staff/new?role=head",data=dict(self.csrf,**dict(data,username="safe.demo",**extras))).status_code,400)
        self.assertEqual(self.client.post("/staff/new?role=head",data=data).status_code,400)
        for missing in ("full_name","username","password"):
            changed=dict(data,username="safe.demo");changed[missing]=" "
            if missing=="password":changed[missing]=""
            self.assertEqual(self.post("/staff/new",**changed).status_code,400)
        self.assertNotIn("safe.demo",self.client.get("/staff?role=head").text)

    def test_active_assignment_refusal_is_not_mutation_or_hidden_transfer(self):
        response=self.post("/staff/20/disable")
        self.assertEqual(response.status_code,409)
        self.assertIn("Сначала переназначьте",response.text)
        fresh=self.client.get("/staff?role=head").text
        self.assertIn('data-account-id="20"',fresh)
        self.assertNotIn("/staff/20/unblock",fresh)

    def test_admin_create_transfer_block_restore_and_protected_admin(self):
        data=dict(self.csrf,full_name="Новый начальник",username="new.head.demo",password="synthetic-unused",role="head",department_id="2")
        response=self.client.post("/admin/users/new?role=admin",data=data)
        self.assertEqual(response.status_code,303)
        self.assertIn("Новый начальник",self.client.get(response.location).text)
        for url in ("/admin/users/22/block","/admin/users/22/unblock"):
            self.assertEqual(self.post(url,role="admin").status_code,303)
        response=self.client.post("/admin/users/22/edit?role=admin",data=dict(data,full_name="Переведённый сотрудник",username="daniyar.demo",password="",role="employee"))
        self.assertEqual(response.status_code,303)
        html=self.client.get("/admin/users/22/edit?role=admin").text
        self.assertIn('value="Переведённый сотрудник"',html)
        self.assertIn('value="2" selected',html)
        self.assertEqual(self.post("/admin/users/3/block",role="admin").status_code,400)
        self.assertEqual(self.client.post("/admin/users/new?role=admin",data=dict(data,role="admin")).status_code,400)
        self.assertEqual(self.client.post("/admin/users/3/edit?role=admin",data=dict(data,role="employee")).status_code,400)
        self.assertEqual(self.client.post("/admin/users/3/edit?role=admin",data=dict(data,role="admin",department_id="",username="admin.demo",password="")).status_code,303)

    def test_transfer_conflict_keeps_inputs_and_original_account(self):
        response=self.client.post("/admin/users/20/edit?role=admin",data=dict(self.csrf,full_name="Несохранённое имя",username="aliya.changed",password="",role="employee",department_id="2"))
        self.assertEqual(response.status_code,409)
        self.assertIn("Сначала переназначьте",response.text)
        self.assertIn('value="Несохранённое имя"',response.text)
        self.assertIn('value="2" selected',response.text)
        fresh=self.client.get("/admin/users/20/edit?role=admin").text
        self.assertIn('value="Алия Садыкова (демо)"',fresh)
        self.assertIn('value="1" selected',fresh)

    def test_archived_empty_long_and_private_responses(self):
        self.assertEqual(self.client.get("/staff/new?role=head&people_case=archived").status_code,400)
        html=self.client.get("/staff?role=head&people_case=archived").text
        self.assertNotIn('href="/staff/new',html)
        for path,role in (("/staff","head"),("/admin/users","admin")):
            self.assertIn("пока нет",self.client.get(path+"?role="+role+"&people_case=empty").text)
            response=self.client.get(path+"?role="+role+"&people_case=long")
            self.assertEqual(response.status_code,200)
            self.assertIn("Константинопольская",response.text)
            self.assertEqual(response.headers["Cache-Control"],"private, no-store")
            self.assertIn("Cookie",response.headers["Vary"])
        new=self.client.get("/admin/users/new?role=admin").text
        self.assertNotIn("Архивный отдел",new)
        old=self.client.get("/admin/users/31/edit?role=admin").text
        self.assertIn("Архивный отдел",old)

    def test_server_error_preserves_public_inputs_and_has_no_false_success(self):
        response=self.client.post("/staff/new?role=head&people_case=error",data=dict(self.csrf,full_name="Сохранённый текст",username="new.demo",password="synthetic-unused"))
        self.assertEqual(response.status_code,500)
        self.assertIn('value="Сохранённый текст"',response.text)
        self.assertNotIn("Учётная запись сохранена.",response.text)
        self.assertNotIn('value="synthetic-unused"',response.text)


class DepartmentContractsCase(unittest.TestCase):
    def setUp(self):
        self.client=create_preview_app(testing=True).test_client()
        self.csrf={"csrf_token":"frontend-preview-only-not-an-auth-token"}

    def send(self,path,**values):
        return self.client.post(path+"?role=admin",data=dict(self.csrf,**values))

    def test_admin_only_canonical_methods_and_unknown_ids(self):
        for role in ("employee","head"):
            for path in ("/admin/departments","/admin/departments/new","/admin/departments/1/edit"):
                self.assertEqual(self.client.get(path+"?role="+role).status_code,403)
                self.assertEqual(self.client.post(path+"?role="+role,data=dict(self.csrf,name="Запрещённый отдел")).status_code,403 if not path.endswith("departments") else 405)
            for action in ("archive","restore","delete"):
                self.assertEqual(self.client.post("/admin/departments/1/"+action+"?role="+role,data=self.csrf).status_code,403)
        self.assertEqual(self.client.get("/admin/departments/999/edit?role=admin").status_code,404)
        self.assertEqual(self.send("/admin/departments/999/delete").status_code,404)
        for action in ("archive","restore","delete"):
            self.assertEqual(self.client.get("/admin/departments/1/"+action+"?role=admin").status_code,405)

    def test_create_rename_archive_restore_and_delete_empty(self):
        response=self.send("/admin/departments/new",name="  Новый отдел для проверки  ")
        self.assertEqual(response.status_code,303)
        self.assertIn("Отдел создан.",self.client.get(response.location).text)
        self.assertIn('value="Новый отдел для проверки"',self.client.get("/admin/departments/101/edit?role=admin").text)
        response=self.send("/admin/departments/3/edit",name="Архивное новое название")
        self.assertEqual(response.status_code,303)
        archived=self.client.get("/admin/departments/3/edit?role=admin").text
        self.assertIn("Отдел в архиве",archived)
        for action,message in (("archive","Отдел архивирован."),("restore","Отдел восстановлен.")):
            response=self.send("/admin/departments/1/"+action)
            self.assertEqual(response.status_code,303)
            self.assertIn(message,self.client.get(response.location).text)
        fresh=self.client.get("/admin/departments?role=admin").text
        self.assertIn('data-department-id="1"',fresh)
        self.assertIn('data-label="Пользователи">4',fresh)
        self.assertIn('data-label="Задачи">12',fresh)
        response=self.send("/admin/departments/4/delete")
        self.assertEqual(response.status_code,303)
        self.assertIn("Пустой отдел удалён.",self.client.get(response.location).text)
        self.assertEqual(self.client.get("/admin/departments/4/edit?role=admin").status_code,404)

    def test_blank_duplicate_casefold_archived_and_length(self):
        for name in ("","   ","мАРКЕТИНГ (ДЕМО) ","Учебный отдел (демо)","Ж"*101):
            response=self.send("/admin/departments/new",name=name)
            self.assertEqual(response.status_code,400)
            self.assertIn('aria-invalid="true"',response.text)
            self.assertIn('href="#field-name"',response.text)
        response=self.send("/admin/departments/2/edit",name="  МАРКЕТИНГ (ДЕМО)")
        self.assertEqual(response.status_code,400)
        self.assertIn("уже существует",response.text)
        self.assertIn('value="  МАРКЕТИНГ (ДЕМО)"',response.text)
        self.assertIn('value="Практика (демо)"',self.client.get("/admin/departments/2/edit?role=admin").text)
        self.assertEqual(self.send("/admin/departments/new",name="Ж"*100).status_code,303)

    def test_nonempty_refusal_uses_redirect_and_leaves_data(self):
        for target in (1,2,3):
            response=self.send(f"/admin/departments/{target}/delete")
            self.assertEqual(response.status_code,303)
            html=self.client.get(response.location).text
            self.assertIn("Нельзя удалить отдел с пользователями или задачами",html)
            self.assertIn("Используйте архивирование",html)
            self.assertIn(f'data-department-id="{target}"',html)
            self.assertNotIn("Пустой отдел удалён.",html)

    def test_changed_links_refuse_delete_and_reveal_fresh_count(self):
        url="/admin/departments/4/delete?role=admin&dept_case=linked"
        before=self.client.get("/admin/departments?role=admin&dept_case=linked").text
        self.assertIn('/admin/departments/4/delete?',before)
        response=self.client.post(url,data=self.csrf)
        self.assertEqual(response.status_code,303)
        html=self.client.get(response.location).text
        self.assertIn('data-department-id="4"',html)
        self.assertIn('data-label="Задачи">1',html)
        self.assertNotIn('/admin/departments/4/delete?',html)
        self.assertIn("Используйте архивирование",html)

    def test_csrf_extras_duplicates_and_files_cannot_mutate(self):
        from io import BytesIO
        from werkzeug.datastructures import MultiDict
        self.assertEqual(self.client.post("/admin/departments/4/delete?role=admin").status_code,400)
        for data in (dict(self.csrf,name="Безопасный отдел",role="admin"),
                MultiDict([*self.csrf.items(),("name","Первое"),("name","Второе")]),
                dict(self.csrf,name="Безопасный отдел",attachment=(BytesIO(b"test"),"test.txt"))):
            self.assertEqual(self.client.post("/admin/departments/new?role=admin",data=data).status_code,400)
        self.assertEqual(self.send("/admin/departments/4/delete",name="лишнее").status_code,400)
        self.assertEqual(self.client.get("/admin/departments/4/edit?role=admin").status_code,200)

    def test_empty_long_private_and_error_responses(self):
        self.assertIn("Отделов пока нет",self.client.get("/admin/departments?role=admin&dept_case=empty").text)
        response=self.client.get("/admin/departments?role=admin&dept_case=long")
        self.assertIn("координации образовательных",response.text)
        self.assertEqual(response.headers["Cache-Control"],"private, no-store")
        self.assertIn("Cookie",response.headers["Vary"])
        self.assertNotIn("Set-Cookie",response.headers)
        response=self.client.post("/admin/departments/new?role=admin&dept_case=error",data=dict(self.csrf,name="Сохранённый ввод"))
        self.assertEqual(response.status_code,500)
        self.assertIn('value="Сохранённый ввод"',response.text)
        self.assertNotIn("Отдел создан.",response.text)
        response=self.client.post("/admin/departments/1/archive?role=admin&dept_case=error",data=self.csrf)
        self.assertEqual(response.status_code,303)
        html=self.client.get(response.location).text
        self.assertIn("Изменения не выполнены",html)
        self.assertNotIn("Отдел архивирован.",html)
        self.assertEqual(self.client.get("/admin/departments?role=admin&dept_case=unknown").status_code,400)


class NotificationContractsCase(unittest.TestCase):
    def setUp(self):
        self.client=create_preview_app(testing=True).test_client()
        self.csrf={"csrf_token":"frontend-preview-only-not-an-auth-token"}

    def boundary(self,response):
        return next(a["value"] for tag,a in Tags(response.text).tags if a.get("name")=="boundary")

    def count(self,response):
        return next(a["aria-label"] for tag,a in Tags(response.text).tags if "data-notification-count" in a)

    def test_get_does_not_read_and_personal_profiles_have_same_contract(self):
        for role in ("employee","head","admin"):
            url="/tasks/notifications?role="+role
            first=self.client.get(url)
            self.assertEqual(first.status_code,200)
            self.assertIn("3",self.count(first))
            self.assertEqual(first.text.count('data-read="false"'),3)
            again=self.client.get(url)
            self.assertIn("3",self.count(again))
            self.assertEqual(again.text.count('data-read="false"'),3)
            self.assertEqual(first.headers["Cache-Control"],"private, no-store")
            self.assertIn("Cookie",first.headers["Vary"])
            self.assertNotIn("Set-Cookie",first.headers)

    def test_single_read_changes_counter_only_after_success_and_is_idempotent(self):
        response=self.client.post("/tasks/notifications/100/read?role=head",data=self.csrf)
        self.assertEqual(response.status_code,303)
        fresh=self.client.get(response.location)
        self.assertIn("2",self.count(fresh))
        self.assertEqual(fresh.text.count('data-read="false"'),2)
        response=self.client.post("/tasks/notifications/100/read?role=head",data=self.csrf)
        self.assertEqual(response.status_code,303)
        self.assertIn("2",self.count(self.client.get(response.location)))
        task=self.client.get("/tasks/1000?role=head").text
        self.assertIn('data-full-action-root',task)
        self.assertIn("2",self.count(self.client.get("/tasks/received?role=head")))

    def test_bulk_boundary_covers_unread_records_on_every_page(self):
        page=self.client.get("/tasks/notifications?role=head&notify_case=pages&unread=1&page=2")
        self.assertEqual(page.text.count('data-read="false"'),4)
        response=self.client.post("/tasks/notifications/read-all?role=head&notify_case=pages",data=dict(self.csrf,boundary=self.boundary(page)))
        self.assertEqual(response.status_code,303)
        self.assertIn("0",self.count(self.client.get(response.location)))
        unread=self.client.get("/tasks/notifications?role=head&notify_case=pages&unread=1")
        self.assertIn("Непрочитанных уведомлений нет",unread.text)

    def test_new_records_and_late_lower_id_are_outside_original_boundary(self):
        url="/tasks/notifications?role=employee&notify_case=newer"
        boundary=self.boundary(self.client.get(url))
        for _ in range(2):
            response=self.client.post("/tasks/notifications/read-all?role=employee&notify_case=newer",data=dict(self.csrf,boundary=boundary))
            self.assertEqual(response.status_code,303)
            fresh=self.client.get(response.location)
            self.assertEqual(fresh.text.count('data-read="false"'),2)
            self.assertIn('data-notification-id="900" data-read="false"',fresh.text)
            self.assertIn('data-notification-id="7" data-read="false"',fresh.text)
            self.assertIn("2",self.count(fresh))

    def test_csrf_boundary_duplicates_extras_and_foreign_scope(self):
        from werkzeug.datastructures import MultiDict
        snapshot=self.boundary(self.client.get("/tasks/notifications?role=head"))
        for payload in ({},dict(self.csrf),dict(self.csrf,boundary="tampered"),
                dict(self.csrf,boundary=snapshot,unread="1"),
                MultiDict([*self.csrf.items(),("boundary",snapshot),("boundary",snapshot)])):
            self.assertEqual(self.client.post("/tasks/notifications/read-all?role=head",data=payload).status_code,400)
        self.assertEqual(self.client.post("/tasks/notifications/100/read?role=head",data=dict(self.csrf,boundary=snapshot)).status_code,400)
        self.assertEqual(self.client.post("/tasks/notifications/read-all?role=admin",data=dict(self.csrf,boundary=snapshot)).status_code,403)
        self.assertIn("3",self.count(self.client.get("/tasks/notifications?role=head")))
        self.assertEqual(self.client.post("/tasks/notifications/999/read?role=head",data=self.csrf).status_code,404)

    def test_failure_or_expired_session_never_renders_private_task(self):
        for case,code in (("error",500),("403",403),("session",302)):
            response=self.client.post("/tasks/notifications/100/read?role=head&notify_case="+case,data=self.csrf)
            self.assertEqual(response.status_code,code)
            self.assertNotIn("Подготовить зал к встрече",response.text)
            self.assertNotIn("data-full-action-root",response.text)
            if case=="session":
                self.assertEqual(response.location,"/login")
                self.assertNotIn("account-menu",self.client.get(response.location).text)
            later=self.client.get("/tasks/notifications?role=head&notify_case="+case)
            if case in ("403","session"):
                self.assertNotIn("data-notification-id",later.text)
        for case,code in (("403",403),("session",302)):
            response=self.client.get("/tasks/1000?role=head&notify_case="+case)
            self.assertEqual(response.status_code,code)
            self.assertNotIn("Подготовить зал к встрече",response.text)
        self.assertIn("3",self.count(self.client.get("/tasks/notifications?role=head&notify_case=error")))

    def test_pagination_filter_empty_and_detached_notification(self):
        response=self.client.get("/tasks/notifications?role=admin&notify_case=pages&unread=1&page=2")
        self.assertIn("Страница 2 из 2",response.text)
        links=[urlsplit(a["href"]) for tag,a in Tags(response.text).tags if tag=="a" and a.get("href") and "page=1" in a["href"]]
        self.assertTrue(links)
        self.assertTrue(all(parse_qs(url.query).get("unread")==["1"] for url in links))
        self.assertIn("Уведомлений пока нет",self.client.get("/tasks/notifications?notify_case=empty").text)
        self.assertIn("Непрочитанных уведомлений нет",self.client.get("/tasks/notifications?notify_case=empty&unread=1").text)
        self.assertEqual(self.client.get("/tasks/notifications?page=bad").status_code,400)
        self.assertEqual(self.client.get("/tasks/notifications?page=0").status_code,400)
        self.assertEqual(self.client.get("/tasks/notifications?notify_case=unknown").status_code,400)
        self.assertEqual(self.client.get("/tasks/notifications/100/read").status_code,405)


class ReportContractsCase(unittest.TestCase):
    def setUp(self):
        self.client=create_preview_app(testing=True).test_client()

    def test_report_role_boundaries_and_employee_navigation(self):
        for path,allowed in (("/reports/department","head"),("/admin/statistics","admin")):
            for role in ("employee","head","admin"):
                response=self.client.get(path+"?role="+role)
                self.assertEqual(response.status_code,200 if role==allowed else 403)
                if role!=allowed:self.assertNotIn("data-report-root",response.text)
        html=self.client.get("/tasks/received?role=employee").text
        self.assertNotIn('href="/reports/department',html)
        self.assertNotIn('href="/admin/statistics',html)

    def test_head_department_query_cannot_expand_scope(self):
        self.assertEqual(self.client.get("/reports/department?role=head&department_id=1").status_code,200)
        self.assertEqual(self.client.get("/reports/department?role=head&department_id=2").status_code,403)
        for query in ("department_id=bad","department_id=-1","department_id=１","department_id=1&department_id=1","department_id="):
            self.assertEqual(self.client.get("/reports/department?role=head&"+query).status_code,400)

    def test_server_aggregates_not_direction_sums_are_authoritative(self):
        from unittest.mock import patch
        from preview.fixtures import report_fixture
        own,overall=report_fixture()
        own["overall"].update(total=907,average_hours=123.456)
        overall["overall"].update(total=991,average_hours=321.987)
        with patch("preview.server.report_fixture",return_value=(own,overall)):
            head=self.client.get("/reports/department?role=head").text
            admin=self.client.get("/admin/statistics?role=admin").text
        self.assertIn("<strong>907</strong>",head)
        self.assertIn("123,46 ч",head)
        self.assertIn("<strong>991</strong>",admin)
        self.assertIn("321,99 ч",admin)
        self.assertIn("не является общим итогом",admin)

    def test_none_zero_average_and_empty_are_distinct(self):
        for path,role in (("/reports/department","head"),("/admin/statistics","admin")):
            none=self.client.get(path+"?role="+role+"&report_case=no-average").text
            self.assertIn("Нет данных",none)
            zero=self.client.get(path+"?role="+role+"&report_case=zero-average").text
            self.assertIn('class="report-average">0,00 ч',zero)
            empty=self.client.get(path+"?role="+role+"&report_case=empty").text
            self.assertIn("Задач пока нет",empty)
            self.assertIn('class="report-average">Нет данных',empty)

    def test_rework_and_recompletion_use_supplied_current_metrics(self):
        normal=self.client.get("/reports/department?role=head").text
        rework=self.client.get("/reports/department?role=head&report_case=rework").text
        completed=self.client.get("/reports/department?role=head&report_case=recompleted").text
        self.assertIn('class="report-average">19,00 ч',normal)
        self.assertIn('class="report-average">20,00 ч',rework)
        self.assertIn('class="report-average">20,67 ч',completed)
        self.assertIn("до повторного выполнения",rework)
        self.assertNotIn("приёмки",rework)
        self.assertNotIn("черновик",rework)
        self.assertIn("18,67 ч",self.client.get("/admin/statistics?role=admin&report_case=recompleted").text)

    def test_archived_long_private_and_no_double_global_total(self):
        head=self.client.get("/reports/department?role=head&report_case=archived")
        self.assertIn("Отдел в архиве",head.text)
        self.assertIn("0,00 ч",self.client.get("/admin/statistics?role=admin").text)
        admin=self.client.get("/admin/statistics?role=admin&report_case=long")
        self.assertIn("Отдел без задач",admin.text)
        self.assertIn("организаций-партнёров",admin.text)
        self.assertIn("<strong>32</strong>",admin.text)
        self.assertIn("Архивный отдел",admin.text)
        for response in (head,admin):
            self.assertEqual(response.headers["Cache-Control"],"private, no-store")
            self.assertIn("Cookie",response.headers["Vary"])
            self.assertNotIn("Set-Cookie",response.headers)
            self.assertIn("Демонстрационные показатели",response.text)

    def test_read_only_errors_and_generated_timestamp(self):
        for path,role in (("/reports/department","head"),("/admin/statistics","admin")):
            first=self.client.get(path+"?role="+role)
            self.assertEqual(first.text,self.client.get(path+"?role="+role).text)
            self.assertEqual(self.client.post(path+"?role="+role).status_code,405)
            self.assertIn("28.10.2026 11:00",first.text)
            self.assertIn("Asia/Qyzylorda",first.text)
            error=self.client.get(path+"?role="+role+"&report_case=error")
            self.assertEqual(error.status_code,503)
            self.assertNotIn("data-report-root",error.text)
        self.assertEqual(self.client.get("/admin/statistics?role=admin&report_case=unknown").status_code,400)

class AuditContractsCase(unittest.TestCase):
    def setUp(self):
        self.app=create_preview_app(testing=True)
        self.client=self.app.test_client()
        self.path="/admin/audit?role=admin"

    def events(self,response):
        return [(a["data-event-source"],a["data-event-id"]) for tag,a in Tags(response.text).tags if "data-event-id" in a]

    def test_roles_menu_and_private_read_only_get(self):
        for role in ("head","employee"):
            response=self.client.get("/admin/audit?role="+role)
            self.assertEqual(response.status_code,403)
            self.assertNotIn("data-event-id",response.text)
            menu=self.client.get("/__preview__/?role="+role).text
            self.assertNotIn('href="/admin/audit',menu)
        normal=self.client.get(self.path)
        self.assertEqual(normal.status_code,200)
        self.assertIn("private",normal.headers["Cache-Control"])
        self.assertIn("no-store",normal.headers["Cache-Control"])
        self.assertIn("Cookie",normal.headers["Vary"])
        self.assertEqual(self.client.post(self.path,data={"action":"delete","id":"41"}).status_code,405)
        self.assertEqual(self.client.get(self.path).text,normal.text)

    def test_sources_equal_ids_and_historical_actor_are_preserved(self):
        response=self.client.get(self.path)
        self.assertIn(("task","41"),self.events(response))
        self.assertIn(("admin","41"),self.events(response))
        self.assertEqual(len(self.events(response)),8)
        for text in ("Дана — исторический снимок (демо)","Прежний отдел сопровождения (демо)",
                     "Системное пояснение: Учётная запись изменена","Историческая роль неизвестна"):
            self.assertIn(text,response.text)
        actor=self.client.get(self.path+"&user_id=99")
        self.assertEqual(self.events(actor),[("admin","39")])
        self.assertIn("Дана — исторический снимок (демо)",actor.text)

    def test_inclusive_local_days_and_combined_filters(self):
        response=self.client.get(self.path+"&audit_case=boundary&user_id=2&date_from=2026-10-27&date_to=2026-10-27")
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.events(response),[("task","503"),("task","502")])
        self.assertIn("27.10.2026 23:59",response.text)
        self.assertIn("27.10.2026 00:00",response.text)
        self.assertIn('datetime="2026-10-26T19:00:00+00:00"',response.text)
        before=self.client.get(self.path+"&audit_case=boundary&date_to=2026-10-26")
        self.assertEqual(self.events(before),[("task","501")])
        after=self.client.get(self.path+"&audit_case=boundary&date_from=2026-10-28")
        self.assertEqual(self.events(after),[("task","504")])
        missing=self.client.get(self.path+"&audit_case=boundary&user_id=99&date_from=2026-10-27&date_to=2026-10-27")
        self.assertFalse(self.events(missing))
        self.assertIn("Событий не найдено",missing.text)

    def test_pagination_preserves_filters_reset_and_clamps(self):
        query="&audit_case=pages&probe=t011-pages&user_id=2&date_from=2026-10-01&date_to=2026-10-28"
        first=self.client.get(self.path+query)
        self.assertEqual(len(self.events(first)),20)
        next_url=next(a["href"] for tag,a in Tags(first.text).tags if a.get("rel")=="next")
        args=parse_qs(urlsplit(next_url).query)
        for key,value in dict(user_id="2",date_from="2026-10-01",date_to="2026-10-28",page="2",audit_case="pages",role="admin").items():
            self.assertEqual(args[key],[value])
        second=self.client.get(next_url)
        self.assertEqual(len(self.events(second)),4)
        self.assertTrue(set(self.events(first)).isdisjoint(self.events(second)))
        self.assertIn("Страница 2 из 2",second.text)
        last=self.client.get(self.path+query+"&page=2147483647")
        self.assertEqual(self.events(last),self.events(second))
        reset=next(a["href"] for tag,a in Tags(first.text).tags if tag=="a" and a.get("class")=="button secondary-button" and "/admin/audit" in a.get("href","") and "page=" not in a["href"])
        reset_args=parse_qs(urlsplit(reset).query)
        self.assertFalse(set(reset_args)&{"user_id","date_from","date_to","page"})
        form=[a for tag,a in Tags(first.text).tags if tag=="form" and a.get("method")=="get"]
        self.assertEqual(len(form),1)
        names=[a.get("name") for tag,a in Tags(first.text).tags if tag in ("input","select")]
        self.assertNotIn("page",names)
        self.assertEqual(self.client.get(reset).status_code,200)

    def test_invalid_filter_fields_duplicates_and_dates_return_400(self):
        bad=[("user_id=0","user_id"),("user_id=-1","user_id"),("user_id=١","user_id"),
             ("user_id=2147483648","user_id"),("user_id=01","user_id"),("page=","page"),("page=-2","page"),
             ("page=1&page=2","page"),("user_id=2&user_id=3","user_id"),
             ("date_from=2026-02-30","date_from"),("date_from=20261028","date_from"),
             ("date_to=9999-12-31","date_to"),("date_to=2026-10-27&date_from=2026-10-28","date_to"),
             ("date_to=2026-10-28&date_to=2026-10-28","date_to")]
        for query,field in bad:
            with self.subTest(query=query):
                response=self.client.get(self.path+"&"+query)
                self.assertEqual(response.status_code,400)
                self.assertFalse(self.events(response))
                self.assertIn('data-error-summary',response.text)
                if field!="page":
                    self.assertIn('href="#field-'+field+'"',response.text)
                self.assertIn("no-store",response.headers["Cache-Control"])

    def test_empty_unknown_initiator_and_transport_failure_are_distinct(self):
        for query in ("&audit_case=empty","&user_id=2147483647"):
            response=self.client.get(self.path+query)
            self.assertEqual(response.status_code,200)
            self.assertIn("Событий не найдено",response.text)
            self.assertIn("Найдено событий: 0",response.text)
        unknown=self.client.get(self.path+"&user_id=2147483647")
        self.assertIn("Инициатор без записей · #2147483647",unknown.text)
        error=self.client.get(self.path+"&audit_case=error")
        self.assertEqual(error.status_code,503)
        self.assertNotIn("Найдено событий: 0",error.text)
        self.assertNotIn("data-audit-root",error.text)
        self.assertEqual(self.client.get(self.path+"&audit_case=unknown").status_code,400)

    def test_server_dto_is_rendered_without_replacing_names_or_values(self):
        from unittest.mock import patch
        from preview.fixtures import audit_fixture
        journal=audit_fixture()
        journal["total"]=907
        journal["entries"][0].update(actor_name="Сохранённый инициатор X",actor_role="Сохранённая роль X",
            actor_department="Сохранённый отдел X",action="Серверная подпись X",
            changes=[("Серверное поле","До X","После X")])
        with patch("preview.server.audit_fixture",return_value=journal):
            html=self.client.get(self.path).text
        for value in ("Найдено событий: 907","Сохранённый инициатор X","Сохранённая роль X",
                      "Сохранённый отдел X","Серверная подпись X","Серверное поле","До X","После X"):
            self.assertIn(value,html)

    def test_catalog_contains_only_ready_pages_and_no_sensitive_demo_fields(self):
        self.assertFalse(PAGES)
        self.assertEqual(len(ALL_PAGES),len(READY_PAGES))
        catalog=self.client.get("/__preview__/?role=admin").text
        self.assertIn("Готово: журнал",catalog)
        from preview.fixtures import audit_fixture
        import json
        sample=json.dumps(audit_fixture(),ensure_ascii=False,default=str).casefold()
        for forbidden in ("password_hash","secret_key","postgresql://","bearer ","session_cookie","api_key"):
            self.assertNotIn(forbidden,sample)


if __name__ == "__main__":
    unittest.main()
