"""Private task lists, detail presentation and validated workflow routes."""

from datetime import datetime, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, current_app, g, flash, redirect, render_template, request, send_file, url_for, make_response
from flask_login import current_user, login_required

from sqlalchemy import case, func
from sqlalchemy.orm import undefer
from sqlalchemy.exc import SQLAlchemyError

from .extensions import db
from .models import Department, TASK_PRIORITIES, TASK_STATUSES, Task, TaskAttachment, TaskNotification, User
from .notifications import (notification_text, visible_notifications_query,
    notification_read_boundary, mark_all_notifications_read)
from .i18n import t
from .permissions import (
    PRE_DISPATCH, TERMINAL, dispatched_clause, task_visibility_clause, can_create_task, can_edit_draft, can_submit_for_approval,
    can_withdraw_approval, can_return_approval, can_approve_task, can_send_task,
    can_assign_task, can_start_task, can_request_clarification, has_available_assignee,
    can_submit_result, can_review_recipient_result, can_return_result, can_reject_task, is_task_dispatched,
    can_answer_clarification, can_cancel_task, can_change_working_status, can_claim_task,
    can_reassign_task, can_review_result, can_change_deadline, can_contribute_task,
    can_release_task, can_complete_task, is_platform_user, is_recipient_employee, is_recipient_head,
    get_visible_attachment_or_404, get_visible_notification_or_404, get_visible_task_or_404,
)
from .workflow import (
    WorkflowConflict, transition_approval, transition_execution, transition_review, transition_completion, update_draft,
    WORKING_TRANSITIONS, WorkflowError, change_working_status, claim_task, create_task,
    reassign_task, update_task_before_acceptance, answer_task_clarification, make_creation_key,
    cancel_task, confirm_task_result, return_task_for_revision,
    add_task_attachment, add_task_comment, change_task_deadline,
)
from .storage import UploadBatch, UploadError, attachment_path, discard_upload, save_upload
from .task_board import load_board_projection, task_card_options
from .task_presentation import task_progress


tasks_bp = Blueprint("tasks", __name__, url_prefix="/tasks")


@tasks_bp.before_request
@login_required
def guard_unimplemented_mutations():
    """Retired stages cannot become aliases of current business actions."""
    endpoint = request.endpoint.rsplit(".", 1)[-1]
    retired = {"task_approval", "task_review", "task_working_status", "task_confirm",
               "task_answer_clarification", "task_edit"}
    if endpoint not in retired | {"task_execution"} or request.method == "GET" and endpoint != "task_edit":
        return None
    if not is_platform_user(current_user):
        abort(403)
    get_visible_task_or_404((request.view_args or {}).get("task_id"), current_user)
    if current_user.role == "admin":
        abort(403)
    if endpoint in retired:
        abort(410, description="Этот этап удалён из процесса работы задач")
    action = (request.view_args or {}).get("action")
    if action in ("start", "request_clarification", "answer_clarification"):
        abort(410, description="Этот этап удалён из процесса работы задач")
    if action not in ("assign", "reassign"):
        abort(400)
    return None


@tasks_bp.app_template_filter("task_progress")
def describe_task_progress(task):
    return task_progress(task, current_user)


STATUS_LABELS = {
    "new": "Новая",
    "draft": "Черновик",
    "pending_approval": "На согласовании",
    "awaiting_assignment": "Ожидает назначения",
    "assigned": "Назначена",
    "in_progress": "В работе",
    "awaiting_clarification": "Ожидает уточнения",
    "recipient_review": "На проверке отдела-получателя",
    "sender_acceptance": "На приёмке отдела-отправителя",
    "completed": "Выполнено",
    "cancelled": "Отменена",
    "rejected": "Отклонена",
}
PRIORITY_LABELS = {"low": "Низкий", "normal": "Обычный", "high": "Высокий", "urgent": "Срочный"}


def task_user_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.role != "admin" and (
            current_user.role not in ("head", "employee") or current_user.department_id is None
        ):
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def _departments_for_form():
    return Department.query.filter(Department.id != current_user.department_id, Department.is_archived.is_(False)).order_by(Department.name).all()


def _parse_form_deadline(raw_value):
    if not raw_value:
        return None
    try:
        value = datetime.fromisoformat(raw_value)
        if value.tzinfo is None:
            value = value.replace(tzinfo=ZoneInfo(current_app.config["TASK_TIMEZONE"]))
        return value.astimezone(timezone.utc)
    except (TypeError, ValueError):
        raise WorkflowError("Укажите срок в корректном формате", "due_at") from None


@tasks_bp.app_template_filter("task_datetime")
def format_task_datetime(value, pattern="%d.%m.%Y %H:%M"):
    if value is None:
        return t("Не задан")
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo(current_app.config["TASK_TIMEZONE"])).strftime(pattern)


def _now():
    return datetime.now(timezone.utc)


def _utc(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _duration(seconds):
    minutes = int(abs(seconds) // 60)
    days, hours = minutes // 1440, minutes // 60 % 24
    if days:
        return f"{days} д. {hours} ч."
    if hours:
        return f"{hours} ч. {minutes % 60} мин."
    return f"{minutes} мин." if minutes else "менее минуты"


def _deadline_state(task, now):
    if task.status in TERMINAL:
        late = task.status == "completed" and task.completed_at and task.due_at and _utc(task.completed_at) > _utc(task.due_at)
        return dict(overdue=False, text="Завершена позже срока" if late else "Задача закрыта")
    if task.due_at is None:
        return dict(overdue=False, text="Срок неизвестен — историческая задача")
    seconds = (_utc(task.due_at) - now).total_seconds()
    if not is_task_dispatched(task) and seconds <= 0:
        return dict(overdue=False, text="Срок наступил; задача ещё не отправлена")
    if seconds < 0:
        return dict(overdue=True, text=f"Просрочена на {_duration(seconds)}")
    return dict(overdue=False, text=f"Осталось {_duration(seconds)}" if seconds else "Срок наступил")


def _is_overdue(task):
    return _deadline_state(task, _now())["overdue"]


LIST_ENDPOINTS = {"received": "tasks.received_tasks", "sent": "tasks.sent_tasks", "completed": "tasks.completed_tasks"}
LIST_LABELS = {"received": "Нашему отделу", "sent": "Другим отделам", "completed": "Завершённые"}


def _visible_task_query(direction):
    query = Task.query.filter(task_visibility_clause(current_user))
    if direction == "completed":
        return query.filter(Task.status.in_(TERMINAL))
    query = query.filter(Task.status.in_(("new", "in_progress")))
    if direction == "received":
        query = query.filter(dispatched_clause())
    if current_user.role != "admin":
        field = Task.recipient_department_id if direction == "received" else Task.sender_department_id
        query = query.filter(field == current_user.department_id)
    return query


def _attention(task):
    if current_user.role != "head":
        return None
    if can_assign_task(current_user, task):
        return "Назначьте исполнителя"
    if can_reassign_task(current_user, task) and not has_available_assignee(task):
        return "Нужно переназначить исполнителя"
    return None


def _task_ordering(direction):
    if direction == "completed":
        finished_at = case((Task.status == "completed", Task.completed_at), else_=Task.closed_at)
        return (finished_at.is_(None), finished_at.desc(), Task.created_at.desc(), Task.id.desc())
    priority = case((Task.priority == "urgent", 0), (Task.priority == "high", 1),
        (Task.priority == "normal", 2), (Task.priority == "low", 3), else_=4)
    return (priority, Task.due_at.is_(None), Task.due_at.asc(), Task.created_at.desc(), Task.id.desc())


def _list_context(direction):
    query = _visible_task_query(direction)
    counts = {key: _visible_task_query(key).count() for key in LIST_ENDPOINTS}
    completed_scope = None
    if direction == "completed" and current_user.role != "admin":
        completed_scope = request.args.get("scope", "received")
        if completed_scope not in ("received", "sent"):
            completed_scope = "received"
        field = Task.recipient_department_id if completed_scope == "received" else Task.sender_department_id
        query = query.filter(field == current_user.department_id)
    list_total = query.count() if completed_scope else counts[direction]
    ordered = query.order_by(*_task_ordering(direction))
    moment = _now()
    view = "list" if request.args.get("view") == "list" else "board"
    board_data = {}
    groups = []
    if view == "board":
        projection = load_board_projection(ordered, direction, _available_task_actions)
        tasks, pagination, groups = projection.tasks, projection.pagination, projection.groups
        # Match the groups and current tab to the same materialized SQL result,
        # even if another request changed a task since the earlier tab counts.
        if not completed_scope:
            counts[direction] = pagination.total
        for task in tasks:
            board_data[task.id] = dict(id=task.id, title=task.title, revision=task.revision,
                is_rework=task.is_rework, rework_cycle=task.rework_cycle,
                unavailable_assignee=task.status == "in_progress" and not has_available_assignee(task),
                status=task.status, detail_url=url_for("tasks.task_detail", task_id=task.id),
                actions=projection.actions[task.id], assignee_ids=task.assignee_ids,
                assignees=[dict(id=u.id, full_name=u.full_name, is_available=is_recipient_employee(u, task)) for u in task.assignees],
                group=projection.group_keys[task.status],
                comment_count=projection.card_counts[task.id]["comments"],
                attachment_count=projection.card_counts[task.id]["attachments"],
                due_at=format_task_datetime(task.due_at, "%Y-%m-%dT%H:%M") if task.due_at else "",
                candidates=[dict(id=u.id, name=u.full_name, role="Начальник отдела" if u.role == "head" else "Сотрудник отдела")
                    for u in projection.candidates[task.id]])
    else:
        pagination = ordered.options(*task_card_options()).paginate(
            page=min(max(1, request.args.get("page", 1, type=int)), max(1, (list_total + 24) // 25)),
            per_page=25, error_out=False)
        tasks = pagination.items
    columns = ["completed", "cancelled", "rejected"] if direction == "completed" else ["new", "in_progress"]
    context = dict(tasks=tasks, direction=direction, pagination=pagination,
        list_labels=({"received": "Полученные поручения", "sent": "Отправленные поручения", "completed": "Завершённые"}
                     if current_user.role == "admin" else LIST_LABELS), list_endpoints=LIST_ENDPOINTS,
        view=view, board_data=board_data, board_columns=columns, board_groups=groups,
        upload_policy=_detail_upload_policy() if view == "board" else None,
        counts=counts, completed_scope=completed_scope,
        may_create=can_create_task(current_user),
        deadline_states={task.id: _deadline_state(task, moment) for task in tasks},
        attention={task.id: _attention(task) for task in tasks},
        unavailable_assignees={task.id: task.status == "in_progress" and not has_available_assignee(task) for task in tasks},
        status_labels={key:STATUS_LABELS[key] for key in TASK_STATUSES}, priority_labels=PRIORITY_LABELS)
    if request.args.get("fragment") is not None:
        if request.args.get("fragment") != "board" or view != "board":
            abort(400)
        response = make_response(render_template("tasks/_board_content.html", **context))
        response.headers["X-Task-Board"] = direction
        response.headers["X-Task-User"] = str(current_user.id)
        return response
    return render_template("tasks/list.html", **context)



@tasks_bp.route("/received")
@task_user_required
def received_tasks():
    return _list_context("received")


@tasks_bp.route("/sent")
@task_user_required
def sent_tasks():
    return _list_context("sent")


@tasks_bp.route("/completed")
@task_user_required
def completed_tasks():
    return _list_context("completed")


@tasks_bp.route("/notifications")
@task_user_required
def notification_center():
    query = visible_notifications_query(current_user)
    unread_only = request.args.get("unread") == "1"
    if unread_only:
        query = query.filter(TaskNotification.is_read.is_(False))
    pagination = query.order_by(TaskNotification.created_at.desc(), TaskNotification.id.desc()).paginate(
        page=request.args.get("page", 1, type=int), per_page=25, error_out=False,
    )
    texts = {note.id: notification_text(note) for note in pagination.items}
    return render_template(
        "tasks/notifications.html", notifications=pagination.items,
        pagination=pagination, unread_only=unread_only,
        notification_texts=texts, read_boundary=notification_read_boundary(current_user),
    )


@tasks_bp.route("/notifications/<int:notification_id>/read", methods=["POST"])
@task_user_required
def notification_mark_read(notification_id):
    notification = get_visible_notification_or_404(notification_id, current_user)
    if notification.user_id != current_user.id:
        abort(403)
    try:
        notification.is_read = True
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        return "Не удалось сохранить отметку прочтения", 500
    return redirect(url_for("tasks.notification_center"))


@tasks_bp.route("/notifications/read-all", methods=["POST"])
@task_user_required
def notification_mark_all_read():
    try:
        allowed = {"csrf_token", "boundary"}
        if set(request.form) - allowed or any(len(request.form.getlist(k)) != 1 for k in request.form):
            abort(400, "Некорректная форма прочтения уведомлений")
        mark_all_notifications_read(current_user, request.form.get("boundary"))
        db.session.commit()
    except ValueError:
        db.session.rollback()
        abort(400, "Обновите страницу уведомлений")
    except PermissionError:
        db.session.rollback()
        abort(404)
    except SQLAlchemyError:
        db.session.rollback()
        return "Не удалось сохранить отметки прочтения", 500
    return redirect(url_for("tasks.notification_center"))


def _uploads_boundary(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        from .settings import UploadPolicy
        policy = UploadPolicy() if request.endpoint in {"tasks.task_new", "tasks.task_execution", "tasks.task_assign",
            "tasks.task_claim", "tasks.task_reassign", "tasks.task_release", "tasks.task_complete",
            "tasks.task_return", "tasks.task_cancel", "tasks.task_reject", "tasks.task_deadline",
            "tasks.task_comment", "tasks.task_upload"} else None
        with UploadBatch(policy=policy) as batch:
            g.task_upload_batch = batch
            try:
                return view(*args, **kwargs)
            except BaseException:
                db.session.rollback()
                raise
            finally:
                g.pop("task_upload_batch", None)
    return wrapped


def _prepare_uploads():
    batch = g.task_upload_batch
    for upload in request.files.getlist("attachment"):
        batch.prepare(upload)
    return tuple(batch.prepared)


def _commit_task_action():
    db.session.commit()
    batch = g.get("task_upload_batch")
    if batch is not None:
        batch.keep()


def _strict_form(allowed, *, upload=False, multiple=()):
    if set(request.form) - set(allowed) or any(key not in multiple and len(request.form.getlist(key)) != 1 for key in request.form):
        raise WorkflowError("Форма содержит неподдерживаемые или повторяющиеся поля")
    if set(request.files) - ({"attachment"} if upload else set()):
        raise WorkflowError("Форма содержит неподдерживаемые вложения")


def _creation_payload():
    return dict(recipient_department_id=request.form.get("recipient_department_id"),
        title=request.form.get("title"), description=request.form.get("description"),
        due_at=_parse_form_deadline(request.form.get("due_at", "")),
        priority=request.form.get("priority", "normal"), category_id=request.form.get("category_id"),
        contact_person=request.form.get("contact_person"), creation_key=request.form.get("creation_key"))


CREATE_FIELDS = {"csrf_token", "title", "description", "recipient_department_id",
                 "due_at", "priority", "category_id", "contact_person", "creation_key"}


def _render_creation(error=None):
    from .settings import active_categories, UploadPolicy
    policy = UploadPolicy()
    key = request.form.get("creation_key") if request.method == "POST" else None
    if key is None or getattr(error, "field", None) == "creation_key":
        key = make_creation_key(current_user)
    return render_template("tasks/form.html", task=None, departments=_departments_for_form(),
        categories=active_categories(), priority_labels=PRIORITY_LABELS, upload_policy=policy,
        creation_key=key, form_error=str(error) if error else None, error_field=_action_error_field(error))


@tasks_bp.route("/new", methods=["GET", "POST"])
@task_user_required
@_uploads_boundary
def task_new():
    if not can_create_task(current_user):
        abort(403)
    if request.method == "GET":
        return _render_creation()
    try:
        _strict_form(CREATE_FIELDS, upload=True)
        payload = _creation_payload()
        files = _prepare_uploads()
        task = create_task(actor=current_user, prepared_files=files, **payload)
        _commit_task_action()
    except PermissionError:
        db.session.rollback()
        abort(403)
    except (WorkflowError, UploadError) as error:
        db.session.rollback()
        return _render_creation(error), 409 if isinstance(error, WorkflowConflict) else 400
    except (SQLAlchemyError, OSError):
        db.session.rollback()
        return _render_creation(WorkflowError("Не удалось отправить задачу. Повторите попытку с той же формой")), 500
    flash("Задача отправлена в другой отдел", "success")
    return redirect(url_for("tasks.task_detail", task_id=task.id))


def _detail_upload_policy():
    from .settings import UploadPolicy
    return UploadPolicy()


def _comment_files(task):
    from sqlalchemy.orm import undefer
    result = {}
    for entry in TaskAttachment.query.filter_by(task_id=task.id).options(undefer(TaskAttachment.comment_id)).all():
        if entry.comment_id is not None:
            result.setdefault(entry.comment_id, []).append(entry)
    return result


HISTORY_LABELS = {
    "claimed": "Сотрудник взял задачу", "released": "Исполнитель снял задачу с себя",
    "created": "Задача создана", "edited": "Поручение изменено", "assigned": "Исполнитель назначен",
    "reassigned": "Состав исполнителей изменён", "status_changed": "Статус изменён", "work_started": "Работа начата",
    "submitted_for_approval": "Заявка подана на согласование", "approval_withdrawn": "Автор отозвал заявку",
    "approval_returned": "Заявка возвращена автору", "approved_and_sent": "Задача одобрена и отправлена",
    "sent": "Задача отправлена", "clarification_requested": "Запрошено уточнение",
    "clarification_answered": "Получен ответ на уточнение", "result_submitted": "Результат передан на внутреннюю проверку",
    "result_forwarded": "Результат передан отправителю", "returned_for_revision": "Задача возвращена на доработку",
    "completed": "Задача выполнена", "cancelled": "Задача отменена", "rejected": "Задача отклонена получателем",
    "deadline_changed": "Срок изменён", "comment_added": "Добавлен комментарий", "attachment_added": "Добавлено вложение",
    "workflow_migrated": "Переход на новый процесс",
}
HISTORY_FIELDS = {"title": "Название", "description": "Описание", "status": "Статус", "priority": "Приоритет",
    "assignee_id": "Исполнитель (прежняя запись)", "assignees": "Состав исполнителей", "author_id": "Автор", "sender_department_id": "Отдел-отправитель",
    "recipient_department_id": "Отдел-получатель", "category": "Категория", "contact_person": "Контактное лицо",
    "due_at": "Срок", "sent_at": "Отправка", "completed_at": "Завершение", "closed_at": "Закрытие", "is_rework": "На доработке", "rework_cycle": "Цикл доработки"}


def _history_value(field, value):
    if value is None or value == "":
        return "Не указано"
    if field == "assignees":
        return ", ".join(str(u.get("name", "Имя не сохранено")) for u in value if isinstance(u, dict)) or "Не назначены"
    if field == "is_rework":
        return "Да" if value else "Нет"
    if field == "status":
        return STATUS_LABELS.get(value, {"new": "Новая (прежний процесс)", "accepted": "Принята (прежний процесс)",
            "ready": "Готова (прежний процесс)"}.get(value, "Историческое состояние"))
    if field == "priority":
        return PRIORITY_LABELS.get(value, "Исторический приоритет")
    if field.endswith("_at"):
        try:
            return format_task_datetime(value)
        except (ValueError, TypeError):
            return "Историческая дата неизвестна"
    if field.endswith("_id"):
        return f"{'Отдел' if 'department' in field else 'Пользователь'} №{value} (имя на момент действия не зафиксировано)"
    return str(value)


def _history_entries(task):
    """Render stored snapshots, never substitute today's account/department data."""
    result = []
    for event in task.events:
        details = event.details or {}
        before, after = details.get("before", {}), details.get("after", {})
        label = HISTORY_LABELS.get(event.event_type, "Историческое событие")
        if "assignee_ids" in after:
            label = {"assigned":"Состав исполнителей назначен", "reassigned":"Состав исполнителей изменён"}.get(event.event_type,label)
        if event.event_type in ("created", "edited") and after.get("status") == "draft":
            label = "Черновик создан" if event.event_type == "created" else "Черновик изменён"
        snapshot = event.actor_snapshot
        if snapshot:
            role = {"head": "Начальник отдела", "employee": "Сотрудник отдела", "admin": "Администратор", "system": "Система"}.get(snapshot.get("role"), "Историческая роль неизвестна")
            actor = f"{snapshot.get('name', 'Имя неизвестно')} · {role}"
            if snapshot.get("role") != "system":
                actor += f" · {snapshot.get('department_name') or 'Отдел неизвестен'}"
        else:
            actor = f"{event.actor.full_name if event.actor else 'Инициатор неизвестен'} · исторический снимок недоступен"
        changes = [(label, _history_value(field, before.get(field)), _history_value(field, after.get(field)))
            for field, label in HISTORY_FIELDS.items() if field in after and before.get(field) != after.get(field)]
        result.append(dict(event=event, label=label, actor=actor, changes=changes,
            comment=details.get("comment") or details.get("reason"), filename=details.get("filename")))
    return result


def _available_task_actions(task):
    """Publish only implemented actions; their canonical URLs are authoritative."""
    definitions = (
        ("assign", "Назначить исполнителей", "in_progress", "execution", can_assign_task),
        ("claim", "Присоединиться" if task.status == "in_progress" else "Взять задачу", "in_progress", "execution", can_claim_task),
        ("reassign", "Изменить состав исполнителей", "in_progress", "execution", can_reassign_task),
        ("release", "Снять задачу с себя", "new" if len(task.assignee_ids) == 1 else "in_progress", "execution", can_release_task),
        ("complete", "Выполнено", "completed", "review", can_complete_task),
        ("return", "Вернуть на доработку", "in_progress", "review", can_return_result),
        ("cancel", "Отменить задачу", "cancelled", "review", can_cancel_task),
        ("reject", "Отклонить задачу", "rejected", "review", can_reject_task),
        ("deadline", "Изменить срок", task.status, "review", can_change_deadline),
    )
    return [dict(key=key, label=label, target=target, group=group,
        url=url_for(f"tasks.task_{key}", task_id=task.id), assignee=key in ("assign", "reassign"),
        deadline=key == "deadline", terminal=target in TERMINAL) for key,label,target,group,predicate in definitions if predicate(current_user,task)]


def _assignee_candidates(task, actions):
    if not any(action["assignee"] for action in actions):
        return []
    query = User.query.filter(User.department_id == task.recipient_department_id,
        User.role.in_(("employee", "head")), User.is_blocked.is_(False))
    return query.order_by(User.full_name, User.id).all()


def _action_error_field(error):
    return "attachment" if isinstance(error, UploadError) else getattr(error, "field", None)


def _detail_context(task, *, action_error=None, failed_action=None, action_error_field=None):
    actions = _available_task_actions(task)
    candidates = _assignee_candidates(task, actions)
    may_contribute = can_contribute_task(current_user, task)
    if action_error and not action_error_field and not request.form.get("comment", "").strip():
        action_error_field = "comment"
    return dict(task=task, status_labels=STATUS_LABELS, task_actions=actions,
        action_urls={action["key"]: action["url"] for action in actions},
        deadline_state=_deadline_state(task, _now()), attention_label=_attention(task),
        history_entries=_history_entries(task),
        back_endpoint="tasks.completed_tasks" if task.status in TERMINAL else (
            "tasks.received_tasks" if current_user.department_id == task.recipient_department_id and is_task_dispatched(task)
            else "tasks.sent_tasks"),
        back_scope=("sent" if current_user.department_id == task.sender_department_id else "received")
            if task.status in TERMINAL and current_user.role != "admin" else None,
        priority_labels=PRIORITY_LABELS, may_edit=can_edit_draft(current_user, task),
        approval_actions=[(a["key"], a["label"]) for a in actions if a["group"] == "approval"],
        execution_actions=[(a["key"], a["label"]) for a in actions if a["group"] == "execution"],
        review_actions=[(a["key"], a["label"]) for a in actions if a["group"] == "review"],
        was_sent=is_task_dispatched(task),
        unavailable_assignee=task.status in ("assigned", "in_progress", "awaiting_clarification",
            "recipient_review", "sender_acceptance") and not has_available_assignee(task),
        action_error=action_error, failed_action=failed_action, action_error_field=action_error_field,
        may_claim=False, may_reassign=False, may_review=False, may_answer=False,
        may_cancel=False, may_change_deadline=can_change_deadline(current_user, task), may_contribute=may_contribute,
        comment_url=url_for("tasks.task_comment", task_id=task.id) if may_contribute else None,
        upload_url=url_for("tasks.task_upload", task_id=task.id) if may_contribute else None,
        upload_policy=_detail_upload_policy(), comment_files=_comment_files(task),
        assignee_candidates=candidates, working_targets=())


def _detail_response(task, *, action_error=None, failed_action=None, action_error_field=None):
    return render_template("tasks/detail.html", **_detail_context(
        task, action_error=action_error, failed_action=failed_action, action_error_field=action_error_field))


@tasks_bp.after_request
def _private_panel_response(response):
    if request.endpoint == "tasks.task_panel" or (request.args.get("fragment") is not None and
            request.endpoint in ("tasks.received_tasks", "tasks.sent_tasks", "tasks.completed_tasks")):
        response.headers["Cache-Control"] = "private, no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.vary.add("Cookie")
    return response


@tasks_bp.route("/<int:task_id>/panel")
@task_user_required
def task_panel(task_id):
    task = get_visible_task_or_404(task_id, current_user)
    response = make_response(render_template("tasks/_task_panel.html", **_detail_context(task)))
    response.headers["X-Task-Panel"] = str(task.id)
    response.headers["X-Task-User"] = str(current_user.id)
    return response


@tasks_bp.route("/<int:task_id>")
@task_user_required
def task_detail(task_id):
    return _detail_response(get_visible_task_or_404(task_id, current_user))


@tasks_bp.route("/<int:task_id>/approval/<action>", methods=["POST"])
@task_user_required
@_uploads_boundary
def task_approval(task_id, action):
    task = get_visible_task_or_404(task_id, current_user)
    if current_user.role == "admin":
        abort(403)
    try:
        _strict_form({"csrf_token", "revision", "comment"}, upload=True)
        task = transition_approval(task=task, actor=current_user, action=action,
            expected_revision=request.form.get("revision"), comment=request.form.get("comment"), prepared_files=_prepare_uploads())
        _commit_task_action()
    except PermissionError:
        db.session.rollback()
        abort(403)
    except (WorkflowError, UploadError) as error:
        db.session.rollback()
        task = get_visible_task_or_404(task_id, current_user)
        return _detail_response(task, action_error=str(error), action_error_field=_action_error_field(error), failed_action=action), (
            409 if isinstance(error, WorkflowConflict) else 400)
    except (SQLAlchemyError, OSError):
        db.session.rollback()
        task = get_visible_task_or_404(task_id, current_user)
        return _detail_response(task, action_error="Не удалось сохранить действие. Повторите попытку",
            failed_action=action), 500
    flash("Действие сохранено", "success")
    return redirect(url_for("tasks.task_detail", task_id=task.id))


def _perform_task_action(task_id, action, **arguments):
    get_visible_task_or_404(task_id, current_user)
    abort(403)


@_uploads_boundary
def _execution_response(task_id, action=None, *, status_alias=False):
    task = get_visible_task_or_404(task_id, current_user)
    if current_user.role == "admin":
        abort(403)
    if not is_recipient_employee(current_user, task) or action in ("assign", "reassign") and not is_recipient_head(current_user, task):
        abort(403)
    try:
        if status_alias:
            action = {"in_progress": "start", "awaiting_clarification": "request_clarification"}.get(request.form.get("status"))
            if action is None:
                raise WorkflowError("Недопустимый переход рабочего статуса")
        allowed = {"csrf_token", "revision", "comment"}
        if action in ("assign", "reassign"):
            allowed.add("assignee_ids")
        if status_alias:
            allowed.add("status")
        _strict_form(allowed, upload=True, multiple=("assignee_ids",))
        task = transition_execution(task=task, actor=current_user, action=action,
            expected_revision=request.form.get("revision"), comment=request.form.get("comment"),
            assignee_ids=request.form.getlist("assignee_ids") if action in ("assign", "reassign") else None, prepared_files=_prepare_uploads())
        _commit_task_action()
    except PermissionError:
        db.session.rollback()
        abort(403)
    except (WorkflowError, UploadError) as error:
        db.session.rollback()
        task = get_visible_task_or_404(task_id, current_user)
        return _detail_response(task, action_error=str(error), action_error_field=_action_error_field(error), failed_action=action), (
            409 if isinstance(error, WorkflowConflict) else 400)
    except (SQLAlchemyError, OSError):
        db.session.rollback()
        task = get_visible_task_or_404(task_id, current_user)
        return _detail_response(task, action_error="Не удалось сохранить действие. Повторите попытку",
            failed_action=action), 500
    flash("Действие сохранено", "success")
    return redirect(url_for("tasks.task_detail", task_id=task.id))


@tasks_bp.route("/<int:task_id>/execution/<action>", methods=["POST"])
@task_user_required
def task_execution(task_id, action):
    return _execution_response(task_id, action)


@tasks_bp.route("/<int:task_id>/assign", methods=["POST"])
@task_user_required
def task_assign(task_id):
    return _execution_response(task_id, "assign")


@_uploads_boundary
def _completion_response(task_id, action):
    task = get_visible_task_or_404(task_id, current_user)
    if current_user.role == "admin":
        abort(403)
    from .permissions import is_sender_employee, is_sender_head
    if action in ("complete", "reject"):
        if not is_recipient_employee(current_user, task):
            abort(403)
    elif not is_sender_employee(current_user, task) or not (is_sender_head(current_user, task) or current_user.id == task.author_id):
        abort(403)
    try:
        _strict_form({"csrf_token", "revision", "comment"}, upload=True)
        task = transition_completion(task=task, actor=current_user, action=action,
            expected_revision=request.form.get("revision"), comment=request.form.get("comment"), prepared_files=_prepare_uploads())
        _commit_task_action()
    except PermissionError:
        db.session.rollback()
        abort(403)
    except (WorkflowError, UploadError) as error:
        db.session.rollback()
        task = get_visible_task_or_404(task_id, current_user)
        return _detail_response(task, action_error=str(error), action_error_field=_action_error_field(error), failed_action=action), 409 if isinstance(error, WorkflowConflict) else 400
    except (SQLAlchemyError, OSError):
        db.session.rollback()
        task = get_visible_task_or_404(task_id, current_user)
        return _detail_response(task, action_error="Не удалось сохранить действие. Повторите попытку", failed_action=action), 500
    flash("Действие сохранено", "success")
    return redirect(url_for("tasks.task_detail", task_id=task.id))


@tasks_bp.route("/<int:task_id>/review/<action>", methods=["POST"])
@task_user_required
def task_review(task_id, action):
    abort(410, description="Этапы проверки результата удалены")


@tasks_bp.route("/<int:task_id>/reject", methods=["POST"])
@task_user_required
def task_reject(task_id):
    return _completion_response(task_id, "reject")


@tasks_bp.route("/<int:task_id>/claim", methods=["POST"])
@task_user_required
def task_claim(task_id):
    return _execution_response(task_id, "claim")


@tasks_bp.route("/<int:task_id>/reassign", methods=["POST"])
@task_user_required
def task_reassign(task_id):
    return _execution_response(task_id, "reassign")


@tasks_bp.route("/<int:task_id>/release", methods=["POST"])
@task_user_required
def task_release(task_id):
    return _execution_response(task_id, "release")


@tasks_bp.route("/<int:task_id>/status", methods=["POST"])
@task_user_required
def task_working_status(task_id):
    return _execution_response(task_id, status_alias=True)


@tasks_bp.route("/<int:task_id>/complete", methods=["POST"])
@task_user_required
def task_complete(task_id):
    return _completion_response(task_id, "complete")


@tasks_bp.route("/<int:task_id>/confirm", methods=["POST"])
@task_user_required
def task_confirm(task_id):
    abort(410, description="Подтверждение отправителем больше не требуется")


@tasks_bp.route("/<int:task_id>/return", methods=["POST"])
@task_user_required
def task_return(task_id):
    return _completion_response(task_id, "return")


@tasks_bp.route("/<int:task_id>/clarification", methods=["POST"])
@task_user_required
def task_answer_clarification(task_id):
    return _execution_response(task_id, "answer_clarification")


@tasks_bp.route("/<int:task_id>/cancel", methods=["POST"])
@task_user_required
def task_cancel(task_id):
    return _completion_response(task_id, "cancel")


@tasks_bp.route("/<int:task_id>/deadline", methods=["POST"])
@task_user_required
@_uploads_boundary
def task_deadline(task_id):
    task = get_visible_task_or_404(task_id, current_user)
    from .permissions import is_sender_head
    if not is_sender_head(current_user, task):
        abort(403)
    try:
        _strict_form({"csrf_token", "revision", "comment", "due_at"}, upload=True)
        task = change_task_deadline(task=task, actor=current_user,
            expected_revision=request.form.get("revision"), comment=request.form.get("comment"),
            due_at=_parse_form_deadline(request.form.get("due_at")), prepared_files=_prepare_uploads())
        _commit_task_action()
    except PermissionError:
        db.session.rollback()
        abort(403)
    except (WorkflowError, UploadError) as error:
        db.session.rollback()
        return _detail_response(get_visible_task_or_404(task_id, current_user),
            action_error=str(error), action_error_field=_action_error_field(error), failed_action="deadline"), 409 if isinstance(error, WorkflowConflict) else 400
    except (SQLAlchemyError, OSError):
        db.session.rollback()
        return _detail_response(get_visible_task_or_404(task_id, current_user),
            action_error="Не удалось сохранить изменение срока и файлы", failed_action="deadline"), 500
    return redirect(url_for("tasks.task_detail", task_id=task.id))


@tasks_bp.route("/<int:task_id>/comments", methods=["POST"])
@task_user_required
@_uploads_boundary
def task_comment(task_id):
    return _comment_response(task_id)


@tasks_bp.route("/<int:task_id>/attachments", methods=["POST"])
@task_user_required
@_uploads_boundary
def task_upload(task_id):
    return _comment_response(task_id, require_file=True)


def _comment_response(task_id, require_file=False):
    task = get_visible_task_or_404(task_id, current_user)
    if current_user.role == "admin":
        abort(403)
    try:
        _strict_form({"csrf_token", "revision", "comment"}, upload=True)
        files = _prepare_uploads()
        if require_file and not files:
            raise WorkflowError("Выберите файл")
        method = add_task_attachment if require_file else add_task_comment
        method(task=task, actor=current_user, comment=request.form.get("comment"),
            expected_revision=request.form.get("revision"), prepared_files=files)
        _commit_task_action()
    except PermissionError:
        db.session.rollback()
        abort(403)
    except (WorkflowError, UploadError) as error:
        db.session.rollback()
        task = get_visible_task_or_404(task_id, current_user)
        return _detail_response(task, action_error=str(error), action_error_field=_action_error_field(error), failed_action="comment"), (
            409 if isinstance(error, WorkflowConflict) else 400)
    except (SQLAlchemyError, OSError):
        db.session.rollback()
        task = get_visible_task_or_404(task_id, current_user)
        return _detail_response(task, action_error="Не удалось сохранить комментарий и файлы",
            failed_action="comment"), 500
    return redirect(url_for("tasks.task_detail", task_id=task.id))


@tasks_bp.route("/attachments/<int:attachment_id>/download")
@task_user_required
def task_download(attachment_id):
    attachment = get_visible_attachment_or_404(attachment_id, current_user)
    try:
        path = attachment_path(attachment.storage_key)
    except UploadError:
        abort(404)
    if not path.is_file():
        abort(404)
    response = send_file(
        path, as_attachment=True, download_name=attachment.original_filename,
        mimetype=attachment.content_type, conditional=False, etag=False, max_age=0,
    )
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@tasks_bp.route("/<int:task_id>/edit", methods=["GET", "POST"])
@task_user_required
@_uploads_boundary
def task_edit(task_id):
    get_visible_task_or_404(task_id, current_user)
    abort(410, description="Редактирование отправленного поручения недоступно")
