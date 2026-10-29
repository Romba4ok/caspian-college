"""Observable T001 contracts: honest preview, local requests and no application DB."""

from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
import os
import subprocess
import sys
import unittest
from urllib.parse import urlsplit

from flask import render_template_string
from jinja2 import UndefinedError

from preview.fixtures import PAGES, ROLE_LABELS, task_datetime
from preview.server import ROOT, create_preview_app


class Destinations(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in ("href", "src", "action") and value:
                self.urls.append(value)


class PreviewCase(unittest.TestCase):
    def setUp(self):
        self.app = create_preview_app(testing=True)
        self.client = self.app.test_client()

    def test_login_renders_existing_templates_and_static_css(self):
        response = self.client.get("/login")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        for text in ("Caspian College", "Демонстрация интерфейса", 'name="username"',
                     'name="password"', 'autocomplete="current-password"'):
            self.assertIn(text, html)
        self.assertEqual(Path(self.app.template_folder), ROOT / "templates")
        css = self.client.get("/static/style.css")
        try:
            self.assertEqual(css.status_code, 200)
            self.assertEqual(css.mimetype, "text/css")
        finally:
            css.close()
        self.assertEqual(response.headers["X-Frontend-Preview"], "demo")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_root_redirects_only_to_own_login(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/login")

    def test_unprepared_pages_are_not_reported_as_implemented(self):
        for page in PAGES:
            with self.subTest(page=page.path):
                response = self.client.get(page.path)
                self.assertEqual(response.status_code, 503)
                self.assertIn("ещё не подготовлена", response.get_data(as_text=True))
                self.assertIn("Демонстрация интерфейса", response.get_data(as_text=True))

    def test_catalog_roles_are_labels_not_authenticated_sessions(self):
        for role, label in ROLE_LABELS.items():
            with self.subTest(role=role):
                response = self.client.get("/__preview__/", query_string={"role": role})
                html = response.get_data(as_text=True)
                self.assertEqual(response.status_code, 200)
                self.assertIn(label, html)
                self.assertIn("T002", html)
                self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(self.client.get("/__preview__/?role=unknown").status_code, 400)
        self.assertNotIn("Учётная запись:", self.client.get("/login?role=admin").text)

    def test_login_post_does_not_authenticate_or_echo_credentials(self):
        response = self.client.post("/login", data={
            "username": "synthetic-login-value", "password": "synthetic-secret-value",
        })
        self.assertEqual(response.status_code, 409)
        self.assertIn("Данные не сохраняются", response.text)
        self.assertNotIn("synthetic-login-value", response.text)
        self.assertNotIn("synthetic-secret-value", response.text)
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(self.client.post("/logout").status_code, 409)

    def test_all_rendered_destinations_stay_same_origin(self):
        for path in ("/login", "/__preview__/?role=head", "/__preview__/?role=admin"):
            parser = Destinations()
            parser.feed(self.client.get(path).text)
            self.assertTrue(parser.urls)
            for destination in parser.urls:
                with self.subTest(path=path, destination=destination):
                    self.assertFalse(urlsplit(destination).scheme)
                    self.assertFalse(urlsplit(destination).netloc)
                    self.assertTrue(destination.startswith(("/", "#")))

    def test_missing_context_is_a_render_error(self):
        with self.app.app_context(), self.assertRaises(UndefinedError):
            render_template_string("{{ required_but_missing }}")

    def test_date_filter_uses_client_zone_without_guessing_naive_time(self):
        self.assertEqual(task_datetime(datetime(2026, 10, 28, 6, tzinfo=timezone.utc)),
                         "28.10.2026 11:00")
        with self.assertRaises(ValueError):
            task_datetime(datetime(2026, 10, 28, 6))

    def test_unknown_routes_do_not_fall_back_to_production(self):
        response = self.client.get("/not-a-preview-route")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("Location", response.headers)

    def test_qa_no_scripts_and_css_motion_emulation_are_preview_only_options(self):
        normal=self.client.get("/admin/audit?role=admin")
        self.assertNotIn("Content-Security-Policy",normal.headers)
        blocked=self.client.get("/admin/audit?role=admin&nojs=1&motion=reduce-css")
        self.assertEqual(blocked.headers["Content-Security-Policy"],"script-src 'none'")
        self.assertIn("animation:none!important",blocked.text)
        self.assertIn('name="nojs" value="1"',blocked.text)
        self.assertIn('name="motion" value="reduce-css"',blocked.text)
        self.assertIn("nojs=1",blocked.text)

    def test_preview_runs_with_production_imports_network_and_config_blocked(self):
        script = r'''
import builtins, os, pathlib, socket, sys
blocked = {"app", "sqlalchemy", "flask_sqlalchemy", "flask_login", "flask_migrate", "psycopg"}
original_import = builtins.__import__
def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 0 and name.split(".")[0] in blocked:
        raise AssertionError("Production dependency imported: " + name)
    return original_import(name, globals, locals, fromlist, level)
builtins.__import__ = guarded_import
def no_network(*args, **kwargs):
    raise AssertionError("Preview attempted an outbound connection")
socket.socket.connect = no_network
socket.socket.connect_ex = no_network
socket.create_connection = no_network
original_open = pathlib.Path.open
def guarded_open(path, *args, **kwargs):
    if path.name == "config.json" or path.name.startswith(".env"):
        raise AssertionError("Preview attempted to read runtime configuration")
    return original_open(path, *args, **kwargs)
pathlib.Path.open = guarded_open
from preview.server import create_preview_app
preview = create_preview_app(testing=True)
client = preview.test_client()
assert client.get("/login").status_code == 200
assert client.get("/__preview__/?role=admin").status_code == 200
assert client.get("/tasks/1001?role=head").status_code == 200
assert client.get("/tasks/1001/panel?role=head").status_code == 200
assert client.get("/tasks/new?role=head").status_code == 200
assert client.get("/staff?role=head").status_code == 200
assert client.get("/admin/users?role=admin").status_code == 200
assert client.get("/admin/departments?role=admin").status_code == 200
assert client.get("/tasks/notifications?role=head").status_code == 200
assert client.get("/reports/department?role=head").status_code == 200
assert client.get("/admin/statistics?role=admin").status_code == 200
assert client.get("/admin/audit?role=admin").status_code == 200
assert client.post("/tasks/notifications/100/read?role=head",data={
    "csrf_token":"frontend-preview-only-not-an-auth-token"
}).status_code == 303
assert client.post("/admin/departments/new?role=admin",data={
    "csrf_token":"frontend-preview-only-not-an-auth-token","name":"Synthetic isolated department"
}).status_code == 303
assert client.post("/staff/new?role=head",data={
    "csrf_token":"frontend-preview-only-not-an-auth-token","full_name":"Synthetic isolated employee",
    "username":"isolated.demo","password":"unused-synthetic"
}).status_code == 303
assert client.get("/tasks/1000?role=head&actions=demo").status_code == 200
assert client.post("/tasks/1000/claim?role=head&actions=demo&probe=isolation",data={
    "csrf_token":"frontend-preview-only-not-an-auth-token","revision":"1","comment":"Synthetic isolated action"
}).status_code == 303
assert client.get("/tasks/attachments/10011?role=head").data.startswith(b"%PDF-")
assert client.post("/login", data={"password": "fake"}).status_code == 409
assert not any(name.split(".")[0] in blocked for name in sys.modules)
assert "SQLALCHEMY_DATABASE_URI" not in preview.config
print("isolated-preview-ok")
'''
        environment = os.environ.copy()
        environment.update(
            PYTHONDONTWRITEBYTECODE="1",
            DATABASE_URL="postgresql://synthetic:unused@127.0.0.1:1/never-connect",
            FLASK_APP="app:create_app",
        )
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", script], cwd=ROOT,
                                env=environment, capture_output=True, text=True,
                                encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("isolated-preview-ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
