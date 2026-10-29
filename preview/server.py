"""Standalone presentation preview, independent of production auth and databases."""

import argparse
from datetime import datetime, timedelta
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from secrets import token_hex
from threading import Lock, RLock
from types import SimpleNamespace
from time import sleep
from urllib.parse import urlsplit

from flask import Flask, abort, g, has_request_context, make_response, redirect
from flask import render_template, render_template_string, request, send_file, url_for
from jinja2 import StrictUndefined

from .fixtures import preview_progress
from .fixtures import ALL_PAGES, DEMO_NOW, IDENTITIES, PAGES, ROLE_LABELS, SCENARIO_SIZES
from .fixtures import PEOPLE_ENDPOINTS, DEPARTMENT_ENDPOINTS, people_fixture, department_fixture
from .fixtures import NOTIFICATION_ENDPOINTS, notification_fixture
from .fixtures import REPORT_ENDPOINTS, STATUS_LABELS, report_fixture, report_hours
from .fixtures import AUDIT_ENDPOINTS, AuditPreviewFilterError, audit_fixture
from .fixtures import TASK_ENDPOINTS, TASK_PAGES, DEMO_ACTIONS, DEMO_ZONE, creation_context, detail_context, identity_for, presentation_user, preview_pdf, task_context, task_datetime

ROOT = Path(__file__).resolve().parents[1]

SHELL = """{% extends 'base.html' %}
{% from 'components/icons.html' import icon %}
{% import 'components/ui.html' as ui %}
{% block title %}{{ title }} · Демонстрация{% endblock %}
{% block content %}
<div class="preview-heading"><div><span class="eyebrow">ДЕМОНСТРАЦИОННЫЙ СТЕНД</span>
<h1>{{ title }}</h1><p>{{ message }}</p></div>
{{ ui.link_button('Страница входа', url_for('auth.login'), symbol='lock') }}</div>
{% if catalog %}
<form method="get" action="{{ url_for('preview_catalog') }}" class="preview-controls">
<div class="field"><label for="preview-role">Демонстрационная роль</label>
<select id="preview-role" name="role">{% for key,label in role_labels.items() %}
<option value="{{ key }}"{% if key == identity.role %} selected{% endif %}>{{ label }}</option>
{% endfor %}</select></div><button type="submit">Показать</button>
{{ ui.link_button('Компоненты интерфейса', url_for('preview_components'), symbol='empty') }}
</form>
<p class="hint">{{ identity.name }}{% if identity.department %} · {{ identity.department }}{% endif %}
 · Дата примеров: {{ demo_now|task_datetime }} · Asia/Qyzylorda</p>
<div class="preview-grid">{% for page in pages %}
<a class="preview-card" href="{{ url_for(page.endpoint) }}">{{ icon('file') }}<span>
<strong>{{ page.title }}</strong><small>{{ 'Готово: доска и список' if page.endpoint in task_endpoints else ('Готово: форма отправки' if page.endpoint == 'tasks.task_new' else ('Готово: учётные записи' if page.endpoint in people_endpoints else ('Готово: отделы' if page.endpoint in department_endpoints else ('Готово: уведомления' if page.endpoint in notification_endpoints else ('Готово: отчёты' if page.endpoint in report_endpoints else 'Готово: журнал'))))) }}</small></span></a>
{% endfor %}</div>
{% elif components %}
<section class="card component-group"><h2>Статусы задач</h2><div class="component-row">
{% for status in ['new','in_progress','completed','cancelled','rejected'] %}{{ ui.status_badge(status) }}{% endfor %}
</div><h2>Приоритеты</h2><div class="component-row">
{% for priority in ['urgent','high','normal','low'] %}{{ ui.priority_badge(priority) }}{% endfor %}
</div><h2>Действия</h2><div class="component-row">
{{ ui.link_button('Основное действие', url_for('preview_catalog'), 'primary', 'arrow') }}
{{ ui.link_button('Вторичное действие', url_for('preview_catalog')) }}
{{ ui.link_button('Действие недоступно') }}</div>
{{ ui.message('Сообщение объясняет результат и следующий шаг.', 'info', 'Информация') }}
{{ ui.message('Проверьте данные и повторите действие.', 'error', 'Не удалось сохранить') }}
{{ ui.empty_state('Задач пока нет', 'Здесь появятся поручения, доступные вашему отделу.') }}</section>
{% endif %}
<p><a href="{{ url_for('preview_catalog') }}">Карта демонстрации</a></p>
{% endblock %}"""


def create_preview_app(*, testing: bool = False) -> Flask:
    app = Flask("frontend_preview", template_folder=str(ROOT / "templates"),
                static_folder=str(ROOT / "static"))
    app.config.update(TESTING=testing, MAX_CONTENT_LENGTH=21 * 1024 * 1024,
                      TEMPLATES_AUTO_RELOAD=True)  # No Python reloader; app.run keeps this setting.
    app.jinja_env.undefined = StrictUndefined
    app.jinja_env.filters["task_progress"] = lambda task: preview_progress(task,g.user)
    app.jinja_env.filters["task_datetime"] = task_datetime
    app.jinja_env.filters["report_hours"] = report_hours
    panel_cases = {"slow", "stale", "retry", "error", "401", "403", "404", "user", "missing-user", "id", "login", "form", "unsafe"}
    attempts, attempts_lock = {}, Lock()  # Fault harness only, never task/business state.
    creation_cases = {"slow","uncertain","file-error","too-large","conflict","archived","unavailable"}
    issued_keys, receipts = {}, {}  # Only synthetic transport acknowledgements; no task records.
    creation_lock = Lock()
    action_lock, action_states, read_faults = RLock(), {}, set()
    action_cases={"400","409","413","403","404","unavailable","uncertain","slow","refresh-error","refresh-user","refresh-invalid","read403"}
    people_lock,people_states=RLock(),{}
    department_lock,department_states=RLock(),{}
    notification_lock,notification_states,notification_snapshots=RLock(),{},{}

    def notification_state():
        case=request.args.get("notify_case","")
        if case not in ("","empty","pages","long","newer","error","403","session","slow"):
            abort(400)
        key=(g.preview_identity.role,request.args.get("probe","default"),case)
        if key not in notification_states:
            notification_states[key]=dict(rows=notification_fixture(case),notices={},injected=False,revoked=False)
        return key,notification_states[key]

    @app.before_request
    def synthetic_context():
        role = request.args.get("role", "employee")
        if role not in IDENTITIES:
            abort(400, description="Неизвестная демонстрационная роль.")
        g.preview_identity = identity_for(role)
        # Presentation fixture only; no login, session or permission service.
        g.user = None if request.endpoint in ("auth.login", "auth.logout", "static") else presentation_user(role)
        if request.endpoint in (*TASK_ENDPOINTS.values(),"tasks.task_detail") and request.args.get("notify_case") in ("403","session"):
            g.notification_revoked=True
            with notification_lock:
                _,state=notification_state();state["revoked"]=True
            if request.args["notify_case"]=="session":
                return private_response(redirect(url_for("auth.login")))
            return private_response(make_response(render_template_string(SHELL,title="Задача недоступна",
                message="Доступ к задаче изменился. Материалы не показаны.",catalog=False,components=False),403))

    @app.url_defaults
    def retain_demo_role(endpoint, values):
        if has_request_context() and endpoint!="static":
            for key in ("nojs","motion"):
                if request.args.get(key):values.setdefault(key,request.args[key])
        if has_request_context() and endpoint not in ("static", "auth.login", "auth.logout"):
            values.setdefault("role", getattr(g, "preview_identity", identity_for("employee")).role)
            if request.args.get("size") and endpoint in (*TASK_ENDPOINTS.values(), "tasks.task_detail", "tasks.task_download", "preview_catalog"):
                values.setdefault("size", request.args["size"])
            if endpoint in (*TASK_ENDPOINTS.values(), "tasks.task_detail", "tasks.task_panel", "tasks.task_download") or endpoint.startswith("tasks.task_") and endpoint not in ("tasks.task_new",):
                for name in ("panel_case", "probe", "actions", "action_case", "size"):
                    if request.args.get(name):
                        values.setdefault(name, request.args[name])
            if endpoint == "tasks.task_new":
                for name in ("create_case","probe"):
                    if request.args.get(name):
                        values.setdefault(name, request.args[name])
            if endpoint.startswith(("staff.","admin.admin_user")):
                for name in ("people_case","probe"):
                    if request.args.get(name):
                        values.setdefault(name,request.args[name])
            if endpoint.startswith("admin.admin_department"):
                for name in ("dept_case","probe"):
                    if request.args.get(name):
                        values.setdefault(name,request.args[name])
            if endpoint.startswith("tasks.notification") or endpoint in (*TASK_ENDPOINTS.values(),"tasks.task_detail"):
                for name in ("notify_case","probe"):
                    if request.args.get(name):
                        values.setdefault(name,request.args[name])
            if endpoint in AUDIT_ENDPOINTS:
                for name in ("audit_case","probe"):
                    if request.args.get(name):
                        values.setdefault(name,request.args[name])
            if endpoint in REPORT_ENDPOINTS:
                for name in ("report_case","probe","notify_case"):
                    if request.args.get(name):
                        values.setdefault(name,request.args[name])

    @app.context_processor
    def presentation_context():
        return {
            "language": "ru",
            "t": lambda text, **values: text.format(**values),
            "csrf_token": lambda: "frontend-preview-only-not-an-auth-token",
            "roles": ROLE_LABELS, "portal_roles": ROLE_LABELS,
            "unread_notification_count": notification_count(),
            "current_user": getattr(g, "user", None),
            "preview_mode": True,
            "staff_url": url_for("staff.staff_list") if has_request_context() else None,
        }

    @app.after_request
    def mark_preview(response):
        response.headers["X-Frontend-Preview"] = "demo"
        response.headers.setdefault("Cache-Control", "no-store")
        if request.args.get("nojs")=="1":
            response.headers["Content-Security-Policy"]="script-src 'none'"
        if response.mimetype=="text/html" and request.args.get("motion")=="reduce-css":
            # CSS emulation only: the browser/system media preference is unchanged.
            response.set_data(response.get_data(as_text=True).replace("</head>",
                "<style>*,*::before,*::after{animation:none!important;transition:none!important;scroll-behavior:auto!important}</style></head>"))
        return response

    @app.get("/")
    def index():
        return redirect(url_for("auth.login"))

    def login():
        if request.method == "POST":
            # Never read/echo/save credentials or create an authenticated session.
            return make_response(render_template_string(
                SHELL, title="Вход не подключён",
                message="Это демонстрация формы. Данные не сохраняются, вход не выполняется.",
                catalog=False, components=False), 409)
        errors = {}
        if request.args.get("scenario") == "invalid-login":
            errors = {"username": "Проверьте логин.", "password": "Проверьте пароль."}
        return render_template("login.html", form_errors=errors, login_value="")

    app.add_url_rule("/login", endpoint="auth.login", view_func=login, methods=["GET", "POST"])

    @app.post("/logout", endpoint="auth.logout")
    def logout():
        return make_response(render_template_string(
            SHELL, title="Нет активного сеанса",
            message="Демонстрация не создаёт учётный сеанс.",
            catalog=False, components=False), 409)

    @app.get("/__preview__/")
    def preview_catalog():
        identity = g.preview_identity
        return render_template_string(
            SHELL, title="Карта страниц",
            message="T002: каркас и вход. T003: доска и список. T004: просмотр задачи. T005: отправка. T006: действия. T007: сотрудники и пользователи. T008: отделы. T009: уведомления. T010: отчёты. T011: журнал. Все страницы карты подготовлены на синтетических данных; общая приёмка — T012, интеграция — отдельно.",
            catalog=True, components=False, identity=identity, role_labels=ROLE_LABELS,
            demo_now=DEMO_NOW, pages=[page for page in ALL_PAGES if identity.role in page.roles],
            task_endpoints=tuple(TASK_ENDPOINTS.values()),people_endpoints=PEOPLE_ENDPOINTS,department_endpoints=DEPARTMENT_ENDPOINTS,notification_endpoints=NOTIFICATION_ENDPOINTS,report_endpoints=REPORT_ENDPOINTS)

    @app.get("/__preview__/components")
    def preview_components():
        return render_template_string(
            SHELL, title="Компоненты интерфейса",
            message="Единые состояния, кнопки и сообщения для страниц платформы.",
            catalog=False, components=True)

    def pending_page(page):
        return make_response(render_template_string(
            SHELL, title=page.title,
            message="Страница ещё не подготовлена. Реализация — в следующих задачах Spec Kit.",
            catalog=False, components=False), 503)

    def preview_size():
        size = request.args.get("size", "6")
        if size not in {str(value) for value in SCENARIO_SIZES}:
            abort(400, description="Неизвестный размер демонстрационного набора.")
        if request.args.get("panel_case") and request.args["panel_case"] not in panel_cases:
            abort(400)
        return int(size)

    def action_options():
        mode=request.args.get("actions","")
        if mode not in ("","demo","empty") or request.args.get("action_case","") not in action_cases|{""}:
            abort(400)
        identity=(g.preview_identity.role,request.args.get("probe","default"))
        with action_lock:
            overrides=dict(action_states.get(identity,{}))
        return dict(actions=mode=="demo",overrides=overrides)

    def decorate(context):
        for action in context.get("task_actions",[]):
            action["url"]=url_for("tasks.task_"+action["key"],task_id=context["task"].id)
        if context.get("may_contribute"):
            context["comment_url"]=url_for("tasks.task_comment",task_id=context["task"].id)
            context["upload_url"]=url_for("tasks.task_upload",task_id=context["task"].id)
        return context

    def action_detail(task_id):
        return decorate(detail_context(g.preview_identity.role,task_id,size=preview_size(),**action_options()) or {})

    def read_fault():
        if request.headers.get("X-Task-Read")!="1" or request.args.get("actions")!="demo":
            return None
        identity=(g.preview_identity.role,request.args.get("probe","default"))
        case=request.args.get("action_case")
        with action_lock:
            changed=bool(action_states.get(identity))
            marker=(*identity,case)
            fail=changed and marker not in read_faults
            if fail:
                read_faults.add(marker)
        if not fail:
            return None
        if case=="refresh-error":
            return private_response(make_response("Демонстрационная ошибка чтения после сохранения",503))
        if case=="read403":
            return private_response(make_response("Доступ изменился",403))
        if case=="refresh-invalid":
            response=private_response(make_response('<div data-board-root><form>Неверный фрагмент</form></div>'))
            response.headers["X-Task-Board"]=request.path.rsplit("/",1)[-1]
            response.headers["X-Task-User"]=str(g.user.id)
            return response
        return None

    def task_page(direction):
        fault=read_fault()
        if fault is not None:
            return fault
        size = preview_size()
        view = request.args.get("view", "board")
        scope = request.args.get("scope", "received")
        if view not in ("board", "list") or scope not in ("received", "sent"):
            abort(400)
        context = task_context(g.preview_identity.role, direction, size=size, view=view,
                               scope=scope, page=request.args.get("page", 1, type=int),**action_options())
        for task_id, card in context["board_data"].items():
            card["detail_url"] = url_for("tasks.task_detail", task_id=task_id)
            for action in card["actions"]:
                action["url"]=url_for("tasks.task_"+action["key"],task_id=task_id)
        if request.args.get("fragment") is not None:
            if view != "board" or request.args["fragment"] != "board":
                abort(400)
            response = make_response(render_template("tasks/_board_content.html", **context))
            response.headers["X-Task-Board"] = direction
            response.headers["X-Task-User"] = str(999 if request.args.get("action_case")=="refresh-user" and request.headers.get("X-Task-Read")=="1" else g.user.id)
            return private_response(response)
        return render_template("tasks/list.html", **context)

    for direction, endpoint in TASK_ENDPOINTS.items():
        page = next(page for page in TASK_PAGES if page.endpoint == endpoint)
        app.add_url_rule(page.path, endpoint=endpoint,
                         view_func=lambda direction=direction: task_page(direction), methods=["GET"])

    @app.get("/tasks/<int:task_id>", endpoint="tasks.task_detail")
    def task_detail(task_id):
        fault=read_fault()
        if fault is not None:
            return fault
        context = action_detail(task_id)
        if not context:
            abort(404)
        response=private_response(make_response(render_template("tasks/detail.html", **context)))
        if request.args.get("action_case")=="refresh-user" and request.headers.get("X-Task-Read")=="1":
            response.headers["X-Task-User"]="999"
        return response

    def private_response(response):
        response.headers["Cache-Control"] = "private, no-store"
        response.vary.add("Cookie")
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.route("/tasks/new", methods=["GET","POST"], endpoint="tasks.task_new")
    def task_new():
        role, case = g.preview_identity.role, request.args.get("create_case","")
        if case and case not in creation_cases:
            abort(400)
        if role == "admin" or case == "archived":
            return private_response(make_response(render_template_string(SHELL, title="Создание недоступно",
                message="Отправлять задачи могут сотрудники и начальники действующих отделов.",
                catalog=False, components=False),403))
        if request.method == "GET":
            receipt_key = request.args.get("receipt")
            with creation_lock:
                if receipt_key:
                    record = receipts.get(receipt_key)
                    if not record or issued_keys.get(receipt_key) != role:
                        abort(404)
                    context = creation_context(receipt_key,receipt=record["receipt"])
                else:
                    key = token_hex(32)
                    issued_keys[key] = role
                    context = creation_context(key)
            return private_response(make_response(render_template("tasks/form.html",**context)))
        values = request.form.to_dict()
        key = values.get("creation_key","")
        def error(message, field=None, code=400):
            return private_response(make_response(render_template("tasks/form.html",
                **creation_context(key,error=message,field=field)),code))
        with creation_lock:
            valid_key = issued_keys.get(key) == role
        if not valid_key:
            return error("Форма устарела или ключ отправки недействителен. Для нового поручения откройте новую форму.",code=409)
        allowed = {"csrf_token","creation_key","title","description","recipient_department_id","due_at","priority"}
        if set(values)-allowed or set(request.files)-{"attachment"}:
            return error("Форма содержит неподдерживаемые поля.")
        if values.get("csrf_token") != "frontend-preview-only-not-an-auth-token":
            return error("Не удалось проверить форму. Отправка не подтверждена.")
        context = creation_context(key)
        for name,label in (("recipient_department_id","Отдел-получатель"),("title","Название"),
                           ("description","Описание"),("due_at","Срок выполнения")):
            if not values.get(name,"").strip():
                return error(f"Заполните поле «{label}».",name)
        if values["recipient_department_id"] not in {str(item.id) for item in context["departments"]}:
            return error("Выберите действующий отдел-получатель.", "recipient_department_id")
        if len(values["title"])>200:
            return error("Название должно содержать до 200 символов.","title")
        if values.get("priority","normal") not in context["priority_labels"]:
            return error("Выберите приоритет из списка.","priority")
        try:
            deadline = datetime.strptime(values["due_at"],"%Y-%m-%dT%H:%M")
            if deadline.strftime("%Y-%m-%dT%H:%M") != values["due_at"]:
                raise ValueError
        except ValueError:
            return error("Укажите дату и время в формате ГГГГ-ММ-ДД ЧЧ:ММ.","due_at")
        files = []
        for upload in request.files.getlist("attachment"):
            if not upload.filename:
                continue
            if upload.filename.rsplit(".",1)[-1].lower() not in context["upload_policy"].allowed_extensions:
                return error("Допустимы PDF, DOCX, XLSX, PPTX, JPG, PNG. Выберите файлы повторно.","attachment")
            data = upload.read(20*1024*1024+1)
            if len(data)>20*1024*1024:
                return error("Размер файла превышает 20 МБ. Выберите файлы повторно.","attachment")
            files.append((upload.filename,sha256(data).hexdigest()))
        if case in {"file-error","too-large"}:
            return error("Файл не прошёл демонстрационную проверку. Выберите файлы повторно." if case=="file-error" else
                         "Превышен размер запроса 21 МБ. Уменьшите набор файлов и выберите их повторно.",
                         "attachment",413 if case=="too-large" else 400)
        if case in {"conflict","unavailable"}:
            return error("Конфликт отправки: проверьте уже созданную задачу. Для другого поручения откройте новую форму."
                         if case=="conflict" else "Отправка в этом демонстрационном сценарии недоступна.",code=409)
        fingerprint = sha256(repr((sorted((k,v) for k,v in values.items() if k!="csrf_token"),files)).encode()).hexdigest()
        with creation_lock:
            existing = receipts.get(key)
            if existing and existing["fingerprint"] != fingerprint:
                return error("Содержимое уже отправленной формы изменилось. Проверьте отправленную задачу; для нового поручения откройте новую форму.",code=409)
            if not existing:
                receipts[key] = dict(fingerprint=fingerprint,receipt=dict(title=values["title"],
                    recipient=next(item.name for item in context["departments"] if str(item.id)==values["recipient_department_id"]),
                    due_at=deadline.strftime("%d.%m.%Y %H:%M"),file_count=len(files)))
        if case == "slow":
            sleep(0.01 if testing else 3)
        if case == "uncertain" and not existing:
            return error("Результат отправки не подтверждён. Повторите отправку с теми же данными и файлами: ключ этой формы сохранён.",code=503)
        return private_response(redirect(url_for("tasks.task_new",receipt=key),code=303))

    @app.get("/tasks/<int:task_id>/panel", endpoint="tasks.task_panel")
    def task_panel(task_id):
        context = action_detail(task_id)
        case = request.args.get("panel_case", "")
        status, body = 200, None
        if case == "slow" or case == "stale" and task_id % 1000 == 0:
            sleep(0.01 if testing else (4 if case == "stale" else 1.5))
        if case == "retry":
            key = (g.preview_identity.role, task_id, request.args.get("probe", "default"))
            with attempts_lock:
                number = attempts.get(key, 0)
                attempts[key] = number + 1
            if number == 0:
                status, body = 503, "Демонстрация временной ошибки загрузки"
        if case in ("401", "403", "404"):
            status, body = int(case), "Нет доступа к демонстрационной задаче"
        elif case == "error":
            status, body = 503, "Загрузка временно недоступна"
        elif case == "login":
            return private_response(redirect(url_for("auth.login")))
        elif not context:
            status, body = 404, "Задача не найдена"
        elif case == "form":
            body = f'<article data-task-panel-content data-task-id="{task_id}"><form><input name="username"></form></article>'
        elif case == "unsafe":
            body = f'<article data-task-panel-content data-task-id="{task_id}"><a href="javascript:void(0)">Небезопасная ссылка</a></article>'
        response = make_response(body if body is not None else render_template("tasks/_task_panel.html", **context), status)
        response.headers["X-Task-Panel"] = str(task_id + 1 if case == "id" else task_id)
        if case != "missing-user":
            response.headers["X-Task-User"] = str(999 if case == "user" else g.user.id)
        return private_response(response)

    @app.get("/tasks/attachments/<int:attachment_id>", endpoint="tasks.task_download")
    def task_download(attachment_id):
        context = action_detail(attachment_id // 10)
        attachment = next((file for file in context["task"].attachments if file.id == attachment_id), None) if context else None
        if attachment is None:
            for task_id,state in action_options()["overrides"].items():
                attachment=next((file for file in state.get("demo_files",[]) if file.id==attachment_id),None)
                if attachment:
                    break
        if attachment is None:
            abort(404)
        return private_response(send_file(BytesIO(getattr(attachment,"data",preview_pdf(attachment_id))), mimetype=getattr(attachment,"mimetype","application/pdf"), as_attachment=True,
                                         download_name=attachment.original_filename, etag=False, conditional=False))

    def action_error(message,field=None,code=400):
        task_id=(request.view_args or {}).get("task_id")
        context=action_detail(task_id) if task_id else {}
        if context:
            context.update(action_error=message,action_error_field=field,failed_action=getattr(g,"demo_action_key",None))
            return private_response(make_response(render_template("tasks/detail.html",**context),code))
        return private_response(make_response(render_template_string('<div class="flash error" role="alert" data-error-field="{{ field or "" }}"><p>{{ message }}</p></div>',
            message=message,field=field),code))

    def task_action(task_id,key):
        g.demo_action_key=key
        if request.args.get("actions")!="demo":
            return action_error("Демонстрационные действия не включены.",code=409)
        case=request.args.get("action_case","")
        if case in ("403","404"):
            return action_error("Доступ к задаче изменился.",code=int(case))
        with action_lock:
            context=action_detail(task_id)
            if not context:
                abort(404)
            task=context["task"]
            allowed={action["key"]:action for action in context["task_actions"]}
            if context["may_contribute"]:
                allowed.update(comment=dict(assignee=False,deadline=False),upload=dict(assignee=False,deadline=False))
            if key not in allowed:
                return action_error("Это действие недоступно.",code=403)
            data=request.form
            allowed_fields={"csrf_token","revision","comment"}|({"assignee_ids"} if allowed[key]["assignee"] else set())|({"due_at"} if allowed[key]["deadline"] else set())
            if set(data)-allowed_fields or any(len(data.getlist(name))!=1 for name in data if name!="assignee_ids") or set(request.files)-{"attachment"}:
                return action_error("Форма содержит неподдерживаемые или повторяющиеся поля.")
            if data.get("csrf_token")!="frontend-preview-only-not-an-auth-token":
                return action_error("Не удалось проверить форму.")
            if case=="409" and task.revision==1:
                identity=(g.preview_identity.role,request.args.get("probe","default"))
                action_states.setdefault(identity,{})[task_id]={"revision":2}
                return action_error("Версия задачи изменилась. Отправка не выполнена.",code=409)
            if data.get("revision")!=str(task.revision):
                return action_error("Версия задачи изменилась. Отправка не выполнена.",code=409)
            if case=="unavailable":
                return action_error("Обработчик ещё не подключён в этом демонстрационном сценарии.",code=409)
            if not data.get("comment","").strip() or case=="400":
                return action_error("Добавьте непустой комментарий к действию.","comment")
            if case=="413":
                return action_error("Превышен размер запроса 21 МБ. Выберите файлы повторно.","attachment",413)
            identity=(g.preview_identity.role,request.args.get("probe","default"))
            state=dict(action_states.get(identity,{}).get(task_id,{}))
            state.setdefault("demo_base_history",list(context["history_entries"]))
            state["revision"]=task.revision+1
            if allowed[key]["assignee"]:
                raw=data.getlist("assignee_ids")
                available={str(p.id):p for p in context["assignee_candidates"]}
                if not raw or len(set(raw))!=len(raw) or any(i not in available for i in raw):
                    return action_error("Выберите разрешённый непустой состав исполнителей.","assignee_ids")
                members=[SimpleNamespace(id=available[i].id,full_name=available[i].full_name,is_active=True,
                    role=available[i].role,department_id=task.recipient_department_id) for i in raw]
                state.update(assignees=members,assignee_ids=[u.id for u in members])
            if allowed[key]["deadline"]:
                try:
                    date=datetime.strptime(data.get("due_at",""),"%Y-%m-%dT%H:%M").replace(tzinfo=DEMO_ZONE)
                    if date.strftime("%Y-%m-%dT%H:%M")!=data["due_at"] or date==task.due_at:
                        raise ValueError
                except (ValueError,KeyError):
                    return action_error("Укажите новый срок, отличный от прежнего.","due_at")
                state["due_at"]=date
            if key in DEMO_ACTIONS and key!="deadline":
                state["status"]=DEMO_ACTIONS[key][1]
            if key=="claim":
                member=SimpleNamespace(id=g.user.id,full_name=g.user.full_name,is_active=True,
                    role=g.user.role,department_id=task.recipient_department_id)
                members=task.assignees+[member]
                state.update(assignees=members,assignee_ids=[u.id for u in members])
            if key=="release":
                members=[u for u in task.assignees if u.id!=g.user.id]
                state.update(assignees=members,assignee_ids=[u.id for u in members],status="in_progress" if members else "new")
            if key=="return":
                members=[u for u in task.assignees if u.is_active]
                state.update(status="in_progress",is_rework=True,assignees=members,assignee_ids=[u.id for u in members],
                             rework_cycle=task.rework_cycle+1,closed_at=None,completed_at=None)
            if key in ("complete","cancel","reject"):
                state.update(closed_at=DEMO_NOW,completed_at=DEMO_NOW if key=="complete" else None,is_rework=False)
            comment_id=task.id*1000+task.revision
            new_comment=SimpleNamespace(id=comment_id,author=g.user,created_at=DEMO_NOW,body=data["comment"].strip())
            files=list(getattr(task,"demo_files",[]))
            new_files=[]
            for upload in request.files.getlist("attachment"):
                if not upload.filename:
                    continue
                ext=upload.filename.rsplit(".",1)[-1].lower()
                if ext not in context["upload_policy"].allowed_extensions:
                    return action_error("Недопустимый формат файла. Выберите файлы повторно.","attachment")
                content=upload.read(20*1024*1024+1)
                if len(content)>20*1024*1024:
                    return action_error("Файл превышает 20 МБ. Выберите файлы повторно.","attachment")
                new_files.append(SimpleNamespace(id=task.id*1000+100+len(files)+len(new_files),original_filename=upload.filename,
                    size_bytes=len(content),comment_id=comment_id,data=content,mimetype={"pdf":"application/pdf","png":"image/png","jpg":"image/jpeg"}.get(ext,"application/octet-stream")))
            if key=="upload" and not new_files:
                return action_error("Выберите хотя бы один файл.","attachment")
            state["demo_comments"]=list(getattr(task,"demo_comments",[]))+[new_comment]
            state["demo_files"]=files+new_files
            state["demo_history"]=list(getattr(task,"demo_history",[]))+[SimpleNamespace(event=SimpleNamespace(created_at=DEMO_NOW),
                label=DEMO_ACTIONS[key][0] if key in DEMO_ACTIONS else "Добавлен комментарий",actor=g.user.full_name,
                comment=data["comment"].strip(),filename=None,changes=[("Версия",task.revision,state["revision"])])]
            action_states.setdefault(identity,{})[task_id]=state
        if case=="slow":
            sleep(0.01 if testing else 2)
        if case=="uncertain":
            return action_error("Не удалось подтвердить результат отправки. Перечитайте задачу.",code=500)
        return private_response(redirect(url_for("tasks.task_detail",task_id=task_id),code=303))

    for key in (*DEMO_ACTIONS,"comment","upload"):
        path={"comment":"comments","upload":"attachments"}.get(key,key)
        app.add_url_rule("/tasks/<int:task_id>/"+path,endpoint="tasks.task_"+key,
            view_func=lambda task_id,key=key:task_action(task_id,key),methods=["POST"])

    def people_state():
        case=request.args.get("people_case","")
        if case not in ("","empty","long","archived","conflict","slow","error"):
            abort(400)
        key=(g.preview_identity.role,request.args.get("probe","default"),case)
        if key not in people_states:
            rows,departments=people_fixture(case)
            people_states[key]=dict(rows=rows,departments=departments,notices={})
        return people_states[key]

    def people_page(staff=False,user_id=None,operation=None):
        # This harness only models UI responses; it never imports account services.
        expected_role="head" if staff else "admin"
        if g.preview_identity.role!=expected_role:
            return private_response(make_response(render_template_string(SHELL,title="Управление недоступно",
                message="У этой роли нет доступа к разделу. Откройте доступный пункт основного меню.",
                catalog=False,components=False),403))
        with people_lock:
            state=people_state()
            rows,departments=state["rows"],state["departments"]
            current=next((row for row in rows if row["id"]==user_id),None)
            if user_id is not None and (current is None or staff and (current["role"]!="employee" or current["department_id"]!=1)):
                return private_response(make_response(render_template_string(SHELL,title="Учётная запись недоступна",
                    message="Запись не найдена или недоступна. Начальник управляет только сотрудниками своего отдела.",
                    catalog=False,components=False),404))
            errors,general={},None
            form_mode=operation=="form"
            creating=form_mode and current is None
            def render(code=200):
                departments_by_id={d["id"]:SimpleNamespace(**d) for d in departments}
                people=[SimpleNamespace(**row,department=departments_by_id.get(row["department_id"])) for row in rows]
                selected=next((person for person in people if person.id==user_id),None)
                context=dict(error=general,form_errors=errors,account_notice=state["notices"].get(request.args.get("notice"),""))
                if staff:
                    context.update(department=departments_by_id[1],employee=selected,
                        employees=[person for person in people if person.role=="employee" and person.department_id==1],
                        active_assignments={row["id"]:row["assignments"] for row in rows},
                        may_create=not departments_by_id[1].is_archived)
                else:
                    context.update(user=selected,users=people,
                        roles={"admin":ROLE_LABELS["admin"]} if selected and selected.role=="admin" else {key:ROLE_LABELS[key] for key in ("head","employee")},
                        departments=[d for d in departments_by_id.values() if not d.is_archived or selected and selected.department_id==d.id])
                    if not form_mode:
                        context["roles"]=ROLE_LABELS
                template=("staff/form.html" if staff else "admin_user_form.html") if form_mode else ("staff/list.html" if staff else "admin_users.html")
                return private_response(make_response(render_template(template,**context),code))
            if creating and staff and departments[0]["is_archived"]:
                general="В архивный отдел нельзя добавлять сотрудников."
                form_mode=False
                return render(400)
            if request.method=="GET":
                return render()
            allowed={"csrf_token"}
            if form_mode:
                allowed.update(("full_name","username","password"))
                if not staff:allowed.update(("role","department_id"))
            data=request.form
            if data.get("csrf_token")!="frontend-preview-only-not-an-auth-token" or set(data)-allowed or request.files or any(len(data.getlist(k))!=1 for k in data):
                general="Форма недействительна или содержит неподдерживаемые поля. Проверьте данные."
                return render(400)
            if request.args.get("people_case")=="error":
                general="Не удалось сохранить изменения. Форма сохранена для проверки; повторите отправку после проверки данных."
                return render(500)
            if form_mode:
                values={name:data.get(name,"").strip() for name in ("full_name","username")}
                for name,limit,label in (("full_name",160,"полное имя"),("username",80,"логин")):
                    if not values[name] or len(values[name])>limit:errors[name]="Укажите "+label+" до "+str(limit)+" символов."
                if creating and not data.get("password"):errors["password"]="Укажите пароль для новой учётной записи."
                if any(row["username"]==values["username"] and row is not current for row in rows):
                    errors["username"]="Пользователь с таким логином уже существует."
                values.update(role="employee" if staff else data.get("role"),department_id=1 if staff else None)
                if not staff:
                    if current and current["role"]=="admin":
                        if data.get("role")!="admin" or data.get("department_id") not in ("",None):
                            general="Нельзя лишить роли единственного администратора; отдел ему не назначается."
                            return render(400)
                    else:
                        if values["role"] not in ("employee","head"):errors["role"]="Выберите роль сотрудника или начальника."
                        destination=next((d for d in departments if str(d["id"])==data.get("department_id")),None)
                        if destination is None or destination["is_archived"] and (current is None or current["department_id"]!=destination["id"]):
                            errors["department_id"]="Выберите действующий отдел."
                        else:values["department_id"]=destination["id"]
                if errors:
                    return render(400)
                if current and current["department_id"]!=values["department_id"] and (current["assignments"] or request.args.get("people_case")=="conflict"):
                    general="Сначала переназначьте активные задачи сотрудника, затем переведите его."
                    return render(409)
                if current:
                    current.update(values)
                else:
                    rows.append(dict(id=max([r["id"] for r in rows]+[100])+1,is_blocked=False,assignments=0,**values))
                notice="Учётная запись сохранена."
            else:
                if current["role"]=="admin":
                    general="Нельзя отключить единственного администратора."
                    return render(400)
                if operation!="unblock" and current["assignments"]:
                    general="Сначала переназначьте активные задачи сотрудника, затем отключите его."
                    return render(409)
                current["is_blocked"]=operation!="unblock"
                notice="Доступ восстановлен." if operation=="unblock" else "Учётная запись отключена. Данные и история сохранены."
            receipt=token_hex(12);state["notices"][receipt]=notice
        if request.args.get("people_case")=="slow":sleep(0.01 if testing else 2)
        return private_response(redirect(url_for("staff.staff_list" if staff else "admin.admin_users",notice=receipt),code=303))

    for prefix,staff in (("/staff",True),("/admin/users",False)):
        namespace="staff.staff_" if staff else "admin.admin_user_"
        app.add_url_rule(prefix,endpoint="staff.staff_list" if staff else "admin.admin_users",
            view_func=lambda staff=staff:people_page(staff),methods=["GET"])
        app.add_url_rule(prefix+"/new",endpoint=namespace+"new",
            view_func=lambda staff=staff:people_page(staff,operation="form"),methods=["GET","POST"])
        app.add_url_rule(prefix+"/<int:user_id>/edit",endpoint=namespace+"edit",
            view_func=lambda user_id,staff=staff:people_page(staff,user_id,"form"),methods=["GET","POST"])
        for operation in (("disable",) if staff else ("block","unblock")):
            app.add_url_rule(prefix+"/<int:user_id>/"+operation,endpoint=namespace+operation,
                view_func=lambda user_id,staff=staff,operation=operation:people_page(staff,user_id,operation),methods=["POST"])
    # Older preview links remain local GET redirects, never production aliases.
    app.add_url_rule("/__preview__/staff",endpoint="preview_staff_list",view_func=lambda:redirect(url_for("staff.staff_list")))
    app.add_url_rule("/__preview__/staff/new",endpoint="preview_staff_form",view_func=lambda:redirect(url_for("staff.staff_new")))

    def department_page(department_id=None,operation=None):
        if g.preview_identity.role!="admin":
            return private_response(make_response(render_template_string(SHELL,title="Управление недоступно",
                message="Управление отделами доступно администратору. Откройте доступный пункт основного меню.",
                catalog=False,components=False),403))
        case=request.args.get("dept_case","")
        if case not in ("","empty","long","linked","error","slow"):
            abort(400)
        key=(request.args.get("probe","default"),case)
        with department_lock:
            state=department_states.setdefault(key,dict(rows=department_fixture(case),notices={}))
            rows=state["rows"]
            current=next((row for row in rows if row["id"]==department_id),None)
            if department_id is not None and current is None:
                return private_response(make_response(render_template_string(SHELL,title="Отдел не найден",
                    message="Отдел не найден или уже удалён. Вернитесь к списку отделов.",
                    catalog=False,components=False),404))
            errors,general={},None
            form_mode=operation=="form"
            def render(code=200):
                notice=state["notices"].get(request.args.get("notice"),{})
                context=dict(department=SimpleNamespace(**current) if current else None,
                    departments=[SimpleNamespace(**row) for row in sorted(rows,key=lambda r:(r["is_archived"],r["name"],r["id"]))],
                    member_counts={row["id"]:row["members"] for row in rows},
                    task_counts={row["id"]:row["tasks"] for row in rows},
                    error=general,form_errors=errors,department_notice=notice.get("message",""),
                    department_error=notice.get("error",""))
                return private_response(make_response(render_template("admin/department_form.html" if form_mode else "admin/departments.html",**context),code))
            if request.method=="GET":
                return render()
            allowed={"csrf_token","name"} if form_mode else {"csrf_token"}
            if request.form.get("csrf_token")!="frontend-preview-only-not-an-auth-token" or set(request.form)-allowed or request.files or any(len(request.form.getlist(name))!=1 for name in request.form):
                general="Форма недействительна или содержит неподдерживаемые поля. Обновите страницу и проверьте данные."
                if not form_mode:
                    return private_response(make_response(render_template_string(SHELL,title="Действие не выполнено",
                        message=general,catalog=False,components=False),400))
                return render(400)
            if form_mode:
                if case=="error":
                    general="Не удалось сохранить отдел. Введённое название сохранено в форме."
                    return render(500)
                name=request.form.get("name","").strip()
                if not name or len(name)>100:
                    errors["name"]="Укажите название от 1 до 100 символов."
                elif any(row is not current and row["name"].casefold()==name.casefold() for row in rows):
                    errors["name"]="Отдел с таким названием уже существует."
                if errors:
                    return render(400)
                if current:
                    current["name"]=name;message="Название отдела изменено."
                else:
                    rows.append(dict(id=max([r["id"] for r in rows]+[100])+1,name=name,is_archived=False,members=0,tasks=0))
                    message="Отдел создан."
                notice={"message":message}
            elif case=="error":
                notice={"error":"Не удалось изменить отдел. Изменения не выполнены; проверьте список перед новой попыткой."}
            elif operation=="delete":
                if case=="linked" and not current["members"] and not current["tasks"]:
                    # Simulated external change between displayed counts and POST.
                    current["tasks"]=1
                if current["members"] or current["tasks"]:
                    notice={"error":"Нельзя удалить отдел с пользователями или задачами. Используйте архивирование."}
                else:
                    rows.remove(current);notice={"message":"Пустой отдел удалён."}
            else:
                current["is_archived"]=operation=="archive"
                notice={"message":"Отдел архивирован." if operation=="archive" else "Отдел восстановлен."}
            receipt=token_hex(12);state["notices"][receipt]=notice
        if case=="slow":sleep(0.01 if testing else 2)
        return private_response(redirect(url_for("admin.admin_departments",notice=receipt),code=303))

    app.add_url_rule("/admin/departments",endpoint="admin.admin_departments",view_func=department_page,methods=["GET"])
    app.add_url_rule("/admin/departments/new",endpoint="admin.admin_department_new",
        view_func=lambda:department_page(operation="form"),methods=["GET","POST"])
    app.add_url_rule("/admin/departments/<int:department_id>/edit",endpoint="admin.admin_department_edit",
        view_func=lambda department_id:department_page(department_id,"form"),methods=["GET","POST"])
    for operation in ("archive","restore","delete"):
        app.add_url_rule("/admin/departments/<int:department_id>/"+operation,endpoint="admin.admin_department_"+operation,
            view_func=lambda department_id,operation=operation:department_page(department_id,operation),methods=["POST"])

    def notification_count():
        if not has_request_context() or not getattr(g,"user",None) or getattr(g,"notification_revoked",False):
            return 0
        with notification_lock:
            _,state=notification_state()
            if state["revoked"]:return 0
            return sum(not row["is_read"] for row in state["rows"])

    def notification_page(notification_id=None,mark_all=False):
        with notification_lock:
            key,state=notification_state()
            rows=state["rows"]
            if state["revoked"]:
                g.notification_revoked=True
                if request.args.get("notify_case")=="session":
                    return private_response(redirect(url_for("auth.login")))
                return notification_error("Доступ к уведомлениям изменился. Прежние сведения не показаны.",403)
            if request.method=="POST":
                case=request.args.get("notify_case","")
                if case in ("403","session"):
                    g.notification_revoked=True
                    state["revoked"]=True
                    if case=="session":
                        return private_response(redirect(url_for("auth.login")))
                    return private_response(make_response(render_template_string(SHELL,title="Уведомление недоступно",
                        message="Доступ изменился. Содержимое уведомления и задачи не показано.",catalog=False,components=False),403))
                allowed={"csrf_token","boundary"} if mark_all else {"csrf_token"}
                if request.form.get("csrf_token")!="frontend-preview-only-not-an-auth-token" or set(request.form)-allowed or request.files or any(len(request.form.getlist(name))!=1 for name in request.form):
                    return notification_error("Форма недействительна. Обновите страницу уведомлений.",400)
                if case=="error":
                    return notification_error("Не удалось сохранить отметку прочтения. Обновите уведомления перед новой попыткой.",500)
                if mark_all:
                    snapshot=notification_snapshots.get(request.form.get("boundary"))
                    if snapshot is None:
                        return notification_error("Граница прочтения недействительна. Откройте уведомления заново.",400)
                    if snapshot["key"]!=key:
                        g.notification_revoked=True
                        return notification_error("Этот набор уведомлений недоступен. Откройте страницу заново.",403)
                    if case=="newer" and not state["injected"]:
                        for ident in (900,7):
                            rows.append(dict(id=ident,event_type="comment_added",message="Новое событие после открытия страницы: "+str(ident),
                                is_read=False,task_id=1001,created_at=DEMO_NOW+timedelta(minutes=1)))
                        state["injected"]=True
                    selected=set(snapshot["ids"])
                    for row in rows:
                        if row["id"] in selected:row["is_read"]=True
                else:
                    selected=next((row for row in rows if row["id"]==notification_id),None)
                    if selected is None:return notification_error("Уведомление не найдено или недоступно.",404)
                    selected["is_read"]=True
                receipt=token_hex(12)
                state["notices"][receipt]="Уведомления из открытого набора отмечены прочитанными. Более новые события останутся непрочитанными." if mark_all else "Уведомление отмечено прочитанным."
            else:
                try:page=int(request.args.get("page","1"))
                except ValueError:return notification_error("Некорректная страница уведомлений.",400)
                if page<1:return notification_error("Некорректная страница уведомлений.",400)
                unread_only=request.args.get("unread")=="1"
                displayed=sorted((row for row in rows if not unread_only or not row["is_read"]),key=lambda row:(row["created_at"],row["id"]),reverse=True)
                pages=(len(displayed)+24)//25
                items=[SimpleNamespace(**row) for row in displayed[(page-1)*25:page*25]]
                boundary=token_hex(24)
                notification_snapshots[boundary]=dict(key=key,ids=[row["id"] for row in rows if not row["is_read"]])
                pagination=SimpleNamespace(page=page,pages=pages,has_prev=page>1,has_next=page<pages,prev_num=page-1,next_num=page+1,total=len(displayed))
                return private_response(make_response(render_template("tasks/notifications.html",notifications=items,
                    notification_texts={row.id:row.message for row in items},pagination=pagination,read_boundary=boundary,
                    unread_only=unread_only,notification_notice=state["notices"].get(request.args.get("notice"),""))))
        if request.args.get("notify_case")=="slow":sleep(0.01 if testing else 2)
        return private_response(redirect(url_for("tasks.notification_center",notice=receipt),code=303))

    def notification_error(message,status):
        # Only a fresh error page is rendered; never reuse private task HTML.
        return private_response(make_response(render_template_string(SHELL,title="Отметка не сохранена",
            message=message,catalog=False,components=False),status))

    app.add_url_rule("/tasks/notifications",endpoint="tasks.notification_center",view_func=notification_page,methods=["GET"])
    app.add_url_rule("/tasks/notifications/<int:notification_id>/read",endpoint="tasks.notification_mark_read",
        view_func=lambda notification_id:notification_page(notification_id),methods=["POST"])
    app.add_url_rule("/tasks/notifications/read-all",endpoint="tasks.notification_mark_all_read",
        view_func=lambda:notification_page(mark_all=True),methods=["POST"])

    def report_page(global_report=False):
        expected="admin" if global_report else "head"
        if g.preview_identity.role!=expected:
            return private_response(make_response(render_template_string(SHELL,title="Отчёт недоступен",
                message="Этот отчёт недоступен для вашей роли. Начальник видит свой отдел, администратор — общую статистику.",
                catalog=False,components=False),403))
        if not global_report:
            values=request.args.getlist("department_id")
            if len(values)>1 or values and (not values[0].isascii() or not values[0].isdigit() or len(values[0])>10):
                return private_response(make_response(render_template_string(SHELL,title="Некорректный запрос",
                    message="Укажите один корректный отдел.",catalog=False,components=False),400))
            if values and int(values[0])!=g.user.department_id:
                return private_response(make_response(render_template_string(SHELL,title="Отчёт недоступен",
                    message="Доступен только отчёт своего отдела.",catalog=False,components=False),403))
        case=request.args.get("report_case","")
        if case not in ("","empty","no-average","zero-average","archived","long","rework","recompleted","error"):
            abort(400)
        if case=="error":
            return private_response(make_response(render_template_string(SHELL,title="Отчёт временно недоступен",
                message="Не удалось получить показатели. Обновите страницу позже; неизвестные значения не заменены нулями.",
                catalog=False,components=False),503))
        own,overall=report_fixture(case)
        context=overall if global_report else own
        return private_response(make_response(render_template("admin/statistics.html" if global_report else "reports/department.html",
            report=context,status_labels=STATUS_LABELS)))

    app.add_url_rule("/reports/department",endpoint="reports.own_department_report",view_func=report_page,methods=["GET"])
    app.add_url_rule("/admin/statistics",endpoint="admin.admin_statistics",
        view_func=lambda:report_page(True),methods=["GET"])

    @app.get("/admin/audit",endpoint="admin.admin_audit")
    def audit_page():
        if g.preview_identity.role!="admin":
            return private_response(make_response(render_template_string(SHELL,title="Журнал недоступен",
                message="Журнал действий доступен только администратору.",catalog=False,components=False),403))
        case=request.args.get("audit_case","")
        if case not in ("","empty","pages","long","boundary","error"):abort(400)
        if case=="error":
            return private_response(make_response(render_template_string(SHELL,title="Журнал временно недоступен",
                message="Не удалось загрузить историю. Обновите страницу позже.",catalog=False,components=False),503))
        filters={key:request.args.get(key,"") for key in ("user_id","date_from","date_to")}
        try:
            for key in (*filters,"page"):
                if len(request.args.getlist(key))>1:
                    raise AuditPreviewFilterError("Передайте одно значение для каждого поля фильтра.",key)
            journal=audit_fixture(case,**filters,page=request.args.get("page","1"))
        except AuditPreviewFilterError as error:
            return private_response(make_response(render_template("admin/audit.html",journal=None,filters=filters,
                error=str(error),error_field=error.field),400))
        return private_response(make_response(render_template("admin/audit.html",journal=journal,
            filters=journal["filters"],error=None)))

    for page in PAGES:
        if not page.path.startswith("/") or urlsplit(page.path).netloc:
            raise ValueError("Only local preview destinations are allowed")
        app.add_url_rule(page.path, endpoint=page.endpoint,
                         view_func=lambda page=page: pending_page(page), methods=["GET"])

    return app


# Fixed workspace boundary: every real-app preview owns a new disposable subfolder.
INTEGRATION_RUNTIME_ROOT = Path(r"D:/Коды/Ccu_platform/spec-kit-main/ai/specs/2026.10.28_11-33_platform-frontend/artifacts/runtime")
SYNTHETIC_PASSWORD = "Caspian-demo-2026!"


def create_integrated_app():
    """Real handlers/auth/CSRF, with only a fresh synthetic database and uploads.

    No default application bootstrap, .env, local config, inherited credentials or
    production database URL is permitted. This function is opt-in, never demo mode.
    """
    import os
    import tempfile
    unsafe = ("DATABASE_URL", "TASK_UPLOAD_DIR", "SECRET_KEY", "ADMIN_PASSWORD",
              "ADMIN_USERNAME", "ADMIN_FULL_NAME", "FLASK_APP")
    if any(os.environ.get(key) for key in unsafe):
        raise RuntimeError("Integrated preview refuses inherited application configuration; use a clean child environment")
    if ROOT.name != "frontend-integration-20261029" or not (ROOT / "app" / "assignment_schema.py").is_file():
        raise RuntimeError("Integrated preview requires the isolated schema-09 integration checkout")
    allowed = INTEGRATION_RUNTIME_ROOT.resolve()
    allowed.mkdir(parents=True, exist_ok=True)
    owned_runtime = tempfile.TemporaryDirectory(prefix="t014-newbase-", dir=allowed)
    runtime = Path(owned_runtime.name).resolve()
    if runtime.parent != allowed:
        raise RuntimeError("Invalid synthetic runtime boundary")
    uploads = runtime / "uploads"
    uploads.mkdir()
    database = runtime / "synthetic.sqlite3"
    # Imports happen only after isolation is validated. No normal runner is used.
    from app import create_app
    from app.extensions import db
    from app.models import Department, Task, User
    from datetime import timezone
    app = create_app(dict(TESTING=True, DATABASE=str(database),
        SQLALCHEMY_DATABASE_URI="sqlite:///" + database.as_posix(), TASK_UPLOAD_DIR=str(uploads),
        SECRET_KEY=token_hex(32), ADMIN_USERNAME="demo.admin", ADMIN_PASSWORD=SYNTHETIC_PASSWORD,
        ADMIN_FULL_NAME="Администратор (тест)", WTF_CSRF_ENABLED=True,
        SESSION_COOKIE_NAME="caspian_integration_newbase"))
    app.config.update(TESTING=False, WTF_CSRF_ENABLED=True, TEMPLATES_AUTO_RELOAD=True)
    app.jinja_env.undefined = StrictUndefined
    app.jinja_env.auto_reload = True
    with app.app_context():
        departments = {d.slug: d for d in Department.query.all()}
        specs = (("demo.head", "Начальник маркетинга (тест)", "head", "marketing"),
                 ("demo.employee", "Анна Соколова (тест)", "employee", "marketing"),
                 ("demo.colleague", "Данияр Омаров (тест)", "employee", "marketing"),
                 ("demo.sender", "Начальник практики (тест)", "head", "practice"),
                 ("demo.outsider", "Сотрудник IT (тест)", "employee", "it"))
        password_hash = User.query.filter_by(username="demo.admin").one().password_hash
        for username, name, role, slug in specs:
            db.session.add(User(username=username, full_name=name, role=role,
                department_id=departments[slug].id, password_hash=password_hash))
        db.session.flush()
        people = {u.username: u for u in User.query.all()}
        now = datetime.now(timezone.utc)
        for index, title in enumerate(("Подготовить зал к встрече", "Собрать заявки на практику",
                "Организовать встречу", "Подготовить рекомендации", "Подготовить план консультаций")):
            row = Task(title=title, description="Синтетическая задача интеграционного стенда. Работа настоящих обработчиков, без рабочих данных.",
                author_id=people["demo.sender"].id, sender_department_id=departments["practice"].id,
                recipient_department_id=departments["marketing"].id, status="in_progress" if index in (1,3) else "new",
                priority=("urgent","high","normal","low","normal")[index],
                due_at=now+timedelta(days=index+2), created_at=now, sent_at=now, revision=1)
            if row.status == "in_progress":
                row.assignee_ids = [people["demo.employee"].id, people["demo.colleague"].id]
            db.session.add(row)
        db.session.commit()
        ids = {u.username: u.id for u in people.values()}
    app.extensions["frontend_integration"] = dict(runtime=runtime, database=database,
        uploads=uploads, identities=ids, owned_runtime=owned_runtime)

    @app.context_processor
    def integrated_banner():
        return dict(integrated_preview_mode=True)

    @app.after_request
    def private_integrated_response(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frontend-Preview"] = "integrated"
        return response

    return app


def cleanup_integrated_app(app):
    """Close only this app's validated, disposable synthetic runtime."""
    info = app.extensions.get("frontend_integration")
    if not info or info.get("closed"):
        return
    runtime = info["runtime"].resolve()
    allowed = INTEGRATION_RUNTIME_ROOT.resolve()
    if runtime.parent != allowed or not runtime.name.startswith("t014-newbase-"):
        raise RuntimeError("Refusing to clean an unowned runtime")
    owner = info.get("owned_runtime")
    if owner is None or Path(owner.name).resolve() != runtime:
        raise RuntimeError("Synthetic runtime ownership mismatch")
    from app.extensions import db
    with app.app_context():
        db.session.remove()
        db.engine.dispose()
    owner.cleanup()
    info["closed"] = True


def main() -> None:
    parser = argparse.ArgumentParser(description="Изолированный preview фронтенда Caspian College")
    parser.add_argument("--mode", choices=("demo", "integrated"), default="demo")
    parser.add_argument("--host", choices=("127.0.0.1", "::1"), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5091)
    arguments = parser.parse_args()
    if not 1024 <= arguments.port <= 65535 or arguments.port == 5000:
        parser.error("Выберите собственный preview-порт 1024–65535, кроме рабочего 5000.")
    if arguments.mode == "integrated" and arguments.port not in (5092, 5093):
        parser.error("Integrated preview uses dedicated loopback port 5092 or the separate new-base port 5093.")
    app = create_integrated_app() if arguments.mode == "integrated" else create_preview_app()
    try:
        app.run(host=arguments.host, port=arguments.port, debug=False,
                use_reloader=False, load_dotenv=False)
    finally:
        if arguments.mode == "integrated":
            cleanup_integrated_app(app)


if __name__ == "__main__":
    main()
