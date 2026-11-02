"""Versioned, token-only server-to-server access for a trusted college CRM."""
from datetime import datetime, timezone
from hashlib import sha256
from hmac import compare_digest
import json
import re
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, g, jsonify, request, send_file, url_for
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import selectinload, undefer
from werkzeug.exceptions import HTTPException

from .audit import _safe
from .crm_events import CrmEventError, ingest_deadline_event, ingest_task_creation
from .extensions import csrf, db
from .models import (AdminAuditEvent, CrmEvent, Department, Task, TaskAttachment,
    TaskComment, TaskEvent, TaskNotification, User, TASK_PRIORITIES, TASK_STATUSES)
from .permissions import ACTIVE, dispatched_clause, is_task_dispatched
from .reminders import as_utc
from .storage import UploadError, attachment_path
from .workflow import WorkflowError, WorkflowConflict

api = Blueprint("crm_api", __name__, url_prefix="/api/v1")
MAX_LIMIT = 200
MAX_JSON_BYTES = 16 * 1024


class ApiError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def _iso(value):
    return as_utc(value).isoformat().replace("+00:00", "Z") if value is not None else None


def _json_value(value):
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _now():
    return datetime.now(timezone.utc)


def _response(data, *, status=200, **extra):
    return jsonify(data=_json_value(data), **_json_value(extra)), status


def _int(value, field, *, minimum=1, maximum=2**31-1):
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value):
        value = int(value)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ApiError("invalid_parameter", f"Некорректное значение поля {field}")
    return value


def _datetime(value, field):
    try:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise ApiError("invalid_parameter", f"{field}: укажите ISO 8601 с часовым поясом") from None


def _query(allowed):
    if set(request.args) - set(allowed) or any(len(request.args.getlist(key)) != 1 for key in request.args):
        raise ApiError("invalid_parameter", "Неподдерживаемые или повторяющиеся параметры запроса")


def _token_scope():
    read = current_app.config.get("CRM_API_READ_TOKEN") or ""
    write = current_app.config.get("CRM_API_WRITE_TOKEN") or ""
    if not read and not write:
        raise ApiError("api_disabled", "API интеграции не настроен", 503)
    if any(not isinstance(token, str) or not 32 <= len(token) <= 512 for token in (read,write) if token) or read and write and read == write:
        raise ApiError("api_configuration", "Ключи API настроены некорректно", 503)
    scheme, _, credential = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not credential or len(credential) > 512:
        raise ApiError("unauthorized", "Требуется Authorization: Bearer <token>", 401)
    digest = sha256(credential.encode()).digest()
    # Hash both inputs to fixed length. Never log or echo credentials.
    matches_read = bool(read) and compare_digest(digest, sha256(read.encode()).digest())
    matches_write = bool(write) and compare_digest(digest, sha256(write.encode()).digest())
    if not matches_read and not matches_write:
        raise ApiError("unauthorized", "Неверный ключ API", 401)
    if request.method not in ("GET", "HEAD", "OPTIONS") and not matches_write:
        raise ApiError("forbidden", "Для POST требуется ключ записи", 403)
    return "write" if matches_write else "read"


def user_data(row):
    from .accounts import is_account_deleted
    return dict(id=row.id, full_name=row.full_name, username=row.username, role=row.role,
        department_id=row.department_id, is_active=row.is_active, language=row.language,
        is_deleted=is_account_deleted(row))


def department_data(row):
    return dict(id=row.id, name=row.name, slug=row.slug, is_archived=row.is_archived)


def task_data(row):
    moment = getattr(g, "crm_read_at", None) or _now()
    local = as_utc(row.due_at).astimezone(ZoneInfo(current_app.config["TASK_TIMEZONE"])) if row.due_at else None
    result = {key: getattr(row,key) for key in ("id", "title", "description", "status", "priority", "author_id",
        "sender_department_id", "recipient_department_id", "revision", "is_rework", "rework_cycle",
        "category", "contact_person")}
    result.update({key: _iso(getattr(row,key)) for key in ("due_at", "created_at", "updated_at", "sent_at", "completed_at", "closed_at")})
    result.update(assignee_ids=row.assignee_ids, due_date=local.date().isoformat() if local else None,
        due_time=local.strftime("%H:%M") if local else None, timezone=current_app.config["TASK_TIMEZONE"],
        is_overdue=bool(row.status in ACTIVE and is_task_dispatched(row) and row.sent_at and as_utc(row.sent_at) <= moment
            and row.due_at and as_utc(row.due_at) < moment),
        links=dict(comments=url_for("crm_api.list_resource", resource="comments", task_id=row.id),
            attachments=url_for("crm_api.list_resource", resource="attachments", task_id=row.id),
            history=url_for("crm_api.list_resource", resource="task-events", task_id=row.id)))
    return result


def comment_data(row):
    return dict(id=row.id, task_id=row.task_id, author_id=row.author_id, body=row.body, created_at=_iso(row.created_at))


def attachment_data(row):
    return dict(id=row.id, task_id=row.task_id, uploaded_by_id=row.uploaded_by_id, comment_id=row.comment_id,
        filename=row.original_filename, content_type=row.content_type, size_bytes=row.size_bytes,
        created_at=_iso(row.created_at), download_url=url_for("crm_api.download_attachment", attachment_id=row.id))


def task_event_data(row):
    return dict(id=row.id, task_id=row.task_id, actor_id=row.actor_id, event_type=row.event_type,
        comment_id=row.comment_id, actor_snapshot=_safe(row.actor_snapshot), details=_safe(row.details), created_at=_iso(row.created_at))


def notification_data(row):
    return dict(id=row.id, task_id=row.task_id, user_id=row.user_id, event_type=row.event_type,
        message=row.message, is_read=row.is_read, created_at=_iso(row.created_at))


def audit_data(row):
    return dict(id=row.id, actor_id=row.actor_id, actor_snapshot=_safe(row.actor_snapshot), action=row.action,
        object_type=row.object_type, object_id=row.object_id, details=_safe(row.details), created_at=_iso(row.created_at))


def crm_event_data(row):
    return dict(id=row.id, source=row.source, event_id=row.event_id, event_type=row.event_type,
        task_id=row.task_id, due_at=_iso(row.due_at), rework_cycle=row.rework_cycle,
        notification_count=row.notification_count, received_at=_iso(row.received_at))


# Explicit public projections. No generic model/SQL serialization or secret columns.
RESOURCES = {
    "tasks": (Task, task_data), "deadlines": (Task, task_data),
    "departments": (Department, department_data), "users": (User, user_data),
    "comments": (TaskComment, comment_data), "attachments": (TaskAttachment, attachment_data),
    "task-events": (TaskEvent, task_event_data), "notifications": (TaskNotification, notification_data),
    "audit-events": (AdminAuditEvent, audit_data), "events": (CrmEvent, crm_event_data),
}


def _resource(name):
    if name not in RESOURCES:
        raise ApiError("not_found", "Ресурс API не найден", 404)
    model, serializer = RESOURCES[name]
    query = model.query
    if model is Task:
        query = query.options(undefer("*"), selectinload(Task.assignments))
    elif model in (Department, TaskAttachment, TaskEvent):
        query = query.options(undefer("*"))
    return model, serializer, query


def _filters(name, model, query):
    allowed = {"limit", "after_id"}
    integers = {}
    if model is Task:
        allowed |= {"status", "priority", "sender_department_id", "recipient_department_id", "assignee_id",
            "due_from", "due_to", "updated_since", "updated_until"}
        integers = {"sender_department_id": model.sender_department_id, "recipient_department_id": model.recipient_department_id}
    elif model in (TaskComment, TaskAttachment, TaskEvent, TaskNotification, CrmEvent):
        integers = {"task_id": model.task_id}
    if model is User:
        integers = {"department_id": User.department_id}
    if model is TaskNotification:
        integers["user_id"] = TaskNotification.user_id
        allowed.add("is_read")
    if model is CrmEvent:
        allowed.add("source")
    allowed |= set(integers)
    _query(allowed)
    for field,column in integers.items():
        if field in request.args:
            query = query.filter(column == _int(request.args[field], field))
    if name == "deadlines":
        query = query.filter(Task.status.in_(ACTIVE), dispatched_clause(), Task.due_at.is_not(None),
            Task.sent_at <= g.crm_read_at, Task.created_at <= g.crm_read_at)
    if model is Task:
        for field, values in (("status", TASK_STATUSES), ("priority", TASK_PRIORITIES)):
            if field in request.args:
                if request.args[field] not in values:
                    raise ApiError("invalid_parameter", f"Неизвестное значение {field}")
                query = query.filter(getattr(Task,field) == request.args[field])
        if "assignee_id" in request.args:
            ident = _int(request.args["assignee_id"], "assignee_id")
            query = query.filter(Task.assignments.any(user_id=ident))
        for low, high, column in (("due_from", "due_to", Task.due_at), ("updated_since", "updated_until", Task.updated_at)):
            lower = _datetime(request.args[low], low) if low in request.args else None
            upper = _datetime(request.args[high], high) if high in request.args else None
            if lower and upper and lower > upper:
                raise ApiError("invalid_parameter", f"{low} не может быть позже {high}")
            if lower:
                query = query.filter(column >= lower)
            if upper:
                query = query.filter(column <= upper)
    if model is TaskNotification and "is_read" in request.args:
        if request.args["is_read"] not in ("true", "false"):
            raise ApiError("invalid_parameter", "is_read: ожидается true или false")
        query = query.filter(TaskNotification.is_read.is_(request.args["is_read"] == "true"))
    if model is CrmEvent and "source" in request.args:
        query = query.filter(CrmEvent.source == request.args["source"])
    return query


@api.get("")
@api.get("/")
def index():
    _query(set())
    return _response(dict(version="1", timezone=current_app.config["TASK_TIMEZONE"], max_limit=MAX_LIMIT,
        resources={name: url_for("crm_api.list_resource", resource=name) for name in RESOURCES},
        reports=url_for("crm_api.reports"),
        create_task=dict(method="POST", path=url_for("crm_api.create_crm_task")),
        receive_event=dict(method="POST", path=url_for("crm_api.receive_event"), event_type="deadline_reminder")))


@api.get("/<resource>")
def list_resource(resource):
    model, serializer, query = _resource(resource)
    g.crm_read_at = _now()
    query = _filters(resource, model, query)
    limit = _int(request.args.get("limit", "50"), "limit", maximum=MAX_LIMIT)
    after = _int(request.args.get("after_id", "0"), "after_id", minimum=0)
    rows = query.filter(model.id > after).order_by(model.id).limit(limit + 1).all()
    more, rows = len(rows) > limit, rows[:limit]
    next_id = rows[-1].id if more else None
    args = dict(request.args)
    args["after_id"] = next_id
    return _response([serializer(row) for row in rows], pagination=dict(limit=limit, has_more=more,
        next_after_id=next_id, next=request.path + "?" + urlencode(args) if more else None),
        meta=dict(generated_at=_iso(g.crm_read_at), timezone=current_app.config["TASK_TIMEZONE"]))


@api.get("/<resource>/<int:identifier>")
def resource_detail(resource, identifier):
    _query(set())
    identifier = _int(identifier, "id")
    model, serializer, query = _resource(resource)
    row = query.filter(model.id == identifier).first()
    if row is None:
        raise ApiError("not_found", "Запись не найдена", 404)
    if resource == "deadlines" and (row.status not in ACTIVE or not is_task_dispatched(row) or not row.due_at
            or as_utc(row.sent_at) > _now() or as_utc(row.created_at) > _now()):
        raise ApiError("not_found", "Активный срок не найден", 404)
    return _response(serializer(row))


@api.get("/reports")
def reports():
    _query(set())
    from .reporting import global_statistics
    admin = User.query.filter_by(role="admin", is_blocked=False).first()
    if admin is None:
        raise ApiError("not_ready", "Администратор платформы не настроен", 503)
    return _response(global_statistics(admin))


@api.get("/attachments/<int:attachment_id>/download")
def download_attachment(attachment_id):
    _query(set())
    attachment_id = _int(attachment_id, "id")
    row = db.session.get(TaskAttachment, attachment_id)
    if row is None:
        raise ApiError("not_found", "Вложение не найдено", 404)
    try:
        path = attachment_path(row.storage_key)
    except UploadError:
        raise ApiError("not_found", "Файл не найден", 404) from None
    if not path.is_file():
        raise ApiError("not_found", "Файл не найден", 404)
    return send_file(path, as_attachment=True, download_name=row.original_filename,
        mimetype=row.content_type, conditional=False, etag=False, max_age=0)


def _json_payload():
    _query(set())
    if request.mimetype != "application/json":
        raise ApiError("unsupported_media_type", "Используйте Content-Type: application/json", 415)
    raw = request.stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ApiError("request_too_large", "Запрос превышает 16 КиБ", 413)
    def unique_pairs(pairs):
        result = {}
        for key,value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result
    def reject_constant(value):
        raise ValueError(value)
    try:
        payload = json.loads(raw, object_pairs_hook=unique_pairs, parse_constant=reject_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ApiError("invalid_json", "Некорректный JSON или повторяющиеся поля", 400) from None
    if not isinstance(payload, dict):
        raise ApiError("invalid_payload", "Ожидается JSON-объект")
    return payload


def _source_event(payload):
    for field,pattern in (("source", r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"), ("event_id", r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,159}")):
        if not isinstance(payload[field],str) or not re.fullmatch(pattern, payload[field]):
            raise ApiError("invalid_event", f"Некорректное поле {field}")


def _event_payload():
    payload = _json_payload()
    required = {"source", "event_id", "event_type", "task_id", "due_at", "rework_cycle"}
    if set(payload) != required:
        raise ApiError("invalid_event", "Обязательные поля: " + ", ".join(sorted(required)))
    _source_event(payload)
    if payload["event_type"] != "deadline_reminder":
        raise ApiError("unsupported_event", "Поддерживается event_type=deadline_reminder", 422)
    # JSON integers only: bool and strings do not identify a task/cycle.
    for field,minimum in (("task_id",1), ("rework_cycle",0)):
        if type(payload[field]) is not int:
            raise ApiError("invalid_event", f"{field}: ожидается целое число")
        payload[field] = _int(payload[field], field, minimum=minimum)
    payload["due_at"] = _datetime(payload["due_at"], "due_at")
    return payload


@api.post("/tasks")
def create_crm_task():
    payload = _json_payload()
    required = {"source", "event_id", "author_id", "recipient_department_id", "title", "description", "due_at"}
    if required - set(payload) or set(payload) - required - {"priority"}:
        raise ApiError("invalid_task", "Обязательные поля: " + ", ".join(sorted(required)) + "; необязательное: priority")
    _source_event(payload)
    for field in ("author_id", "recipient_department_id"):
        if type(payload[field]) is not int:
            raise ApiError("invalid_task", f"{field}: ожидается целое число")
        payload[field] = _int(payload[field], field, maximum=2**31-2)
    for field in ("title", "description"):
        if not isinstance(payload[field], str) or not payload[field].strip():
            raise ApiError("invalid_task", f"{field}: требуется непустая строка")
        payload[field] = payload[field].strip()
        try:
            payload[field].encode("utf-8")
        except UnicodeEncodeError:
            raise ApiError("invalid_task", f"{field}: некорректный Unicode") from None
        if "\x00" in payload[field]:
            raise ApiError("invalid_task", f"{field}: недопустимый нулевой символ")
    if len(payload["title"]) > 200:
        raise ApiError("invalid_task", "title: не более 200 символов")
    payload["priority"] = payload.get("priority", "normal")
    if not isinstance(payload["priority"], str) or payload["priority"] not in TASK_PRIORITIES:
        raise ApiError("invalid_task", "Неизвестный priority")
    payload["due_at"] = _datetime(payload["due_at"], "due_at")
    try:
        task, receipt, duplicate = ingest_task_creation(payload)
        result = task_data(task)
        meta = dict(source=receipt.source, event_id=receipt.event_id, duplicate=duplicate)
        location = url_for("crm_api.resource_detail", resource="tasks", identifier=task.id)
        db.session.commit()
    except CrmEventError as error:
        raise ApiError(error.code, str(error), error.status) from None
    except PermissionError as error:
        raise ApiError("author_unavailable", str(error), 403) from None
    except WorkflowConflict as error:
        raise ApiError("task_conflict", str(error), 409) from None
    except WorkflowError as error:
        raise ApiError("invalid_task", str(error), 400) from None
    response, status = _response(result, status=200 if duplicate else 201, meta=meta)
    response.headers["Location"] = location
    return response, status


@api.post("/events")
def receive_event():
    payload = _event_payload()
    try:
        receipt, duplicate = ingest_deadline_event(payload)
        result = crm_event_data(receipt)
        db.session.commit()
    except CrmEventError as error:
        raise ApiError(error.code, str(error), error.status) from None
    result["duplicate"] = duplicate
    return _response(result, status=200 if duplicate else 201)


@api.errorhandler(400)
@api.errorhandler(413)
@api.errorhandler(HTTPException)
def api_http_error(error):
    # Includes HTTP-level body limits, before our small JSON parser can run.
    code = "request_too_large" if error.code == 413 else "invalid_request"
    message = "Тело запроса слишком большое" if error.code == 413 else "Запрос API отклонён"
    return jsonify(error=dict(code=code, message=message)), error.code


def init_app(app):
    app.register_blueprint(api)
    # Bearer authentication is mandatory. Ordinary HTML forms keep CSRF protection.
    csrf.exempt(api)

    def is_api_request():
        return request.path == "/api/v1" or request.path.startswith("/api/v1/")

    @app.before_request
    def protect_crm_api():
        if is_api_request():
            g.crm_scope = _token_scope()
            if request.routing_exception is not None:
                status = request.routing_exception.code or 404
                raise ApiError("method_not_allowed" if status == 405 else "not_found", "Маршрут API недоступен", status)

    @app.after_request
    def protect_crm_response(response):
        if is_api_request():
            response.headers["Cache-Control"] = "private, no-store, max-age=0"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.vary.add("Authorization")
            if response.status_code == 401:
                response.headers["WWW-Authenticate"] = 'Bearer realm="crm-api"'
        return response

    @app.errorhandler(ApiError)
    def api_error(error):
        db.session.rollback()
        return jsonify(error=dict(code=error.code, message=str(error))), error.status

    @app.errorhandler(SQLAlchemyError)
    def api_database_error(error):
        if is_api_request():
            db.session.rollback()
            # Do not send SQL parameters, credentials or exception details to callers.
            return jsonify(error=dict(code="temporarily_unavailable", message="API временно недоступен; повторите запрос")), 503
        raise error
