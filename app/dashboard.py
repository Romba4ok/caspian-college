"""Рабочая панель: сводка по поручениям отдела (только чтение)."""
from datetime import timedelta

from flask import Blueprint, render_template
from flask_login import current_user
from sqlalchemy import and_, case
from sqlalchemy.orm import contains_eager, joinedload

from . import analytics
from .extensions import db
from .models import Department, Task, TaskAssignee, TaskNotification
from .notifications import notification_text, visible_notifications_query
from .permissions import ACTIVE, dispatched_clause, task_visibility_clause
from .reporting import _metrics, utc_now
from .tasks import _deadline_state, task_user_required

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")

SOON_DAYS = 7
TREND_DAYS = 14
LIST_LIMIT = 5
# Срочные выше: тот же порядок, что и на доске задач.
PRIORITY_RANK = case(("urgent", 0), ("high", 1), ("normal", 2), ("low", 3), value=Task.priority, else_=4)


def _scopes(user):
    """Входящие поручения отдела (или все — для администратора) и исходящие отдела."""
    visible = task_visibility_clause(user)
    if user.role == "admin":
        return dispatched_clause(), None
    incoming = and_(visible, dispatched_clause(), Task.recipient_department_id == user.department_id)
    outgoing = and_(visible, Task.sender_department_id == user.department_id)
    return incoming, outgoing


def _active(scope):
    return Task.query.options(joinedload(Task.sender_department), joinedload(Task.recipient_department)) \
        .filter(scope, ~Task.is_deleted, Task.status.in_(ACTIVE), Task.due_at.is_not(None))


def dashboard_data(user, now=None):
    now = now or utc_now()
    incoming_scope, outgoing_scope = _scopes(user)
    incoming = _metrics(incoming_scope, now)
    outgoing = _metrics(outgoing_scope, now) if outgoing_scope is not None else None
    statuses = incoming["statuses"]
    total = incoming["total"]
    done = statuses["completed"]
    soon_limit = now + timedelta(days=SOON_DAYS)
    soon_query = _active(incoming_scope).filter(Task.due_at >= now, Task.due_at <= soon_limit)
    overdue_query = _active(incoming_scope).filter(Task.due_at < now, Task.sent_at <= now)
    upcoming = soon_query.order_by(Task.due_at.asc(), Task.id.asc()).limit(6).all()
    overdue = overdue_query.order_by(Task.due_at.asc(), Task.id.asc()).limit(6).all()
    department = db_department(user)
    extra = _extra(user, incoming_scope, now)
    shown = upcoming + overdue + extra["mine"] + extra["free"]
    return dict(
        generated_at=now, department=department, total=total, percent=round(100 * done / total) if total else 0,
        done=done, in_progress=statuses["in_progress"], new=statuses["new"], overdue=incoming["overdue"],
        closed_other=statuses["cancelled"] + statuses["rejected"], soon=soon_query.count(), soon_days=SOON_DAYS,
        outgoing_active=(outgoing["statuses"]["new"] + outgoing["statuses"]["in_progress"]) if outgoing else None,
        outgoing_total=outgoing["total"] if outgoing else None, upcoming=upcoming, overdue_tasks=overdue,
        deadline_states={task.id: _deadline_state(task, now) for task in shown}, extra=extra)


def _extra(user, incoming_scope, now):
    """Личные и аналитические блоки: мои задачи, свободные, доработка, уведомления, графики."""
    employee = user.role != "admin"
    ordered = (PRIORITY_RANK, Task.due_at.asc(), Task.id.asc())
    mine = []
    if employee:
        mine = (Task.query.options(joinedload(Task.sender_department), joinedload(Task.recipient_department))
            .join(TaskAssignee, TaskAssignee.task_id == Task.id)
            .filter(TaskAssignee.user_id == user.id, ~Task.is_deleted, Task.status.in_(ACTIVE), task_visibility_clause(user), dispatched_clause())
            .order_by(*ordered).limit(LIST_LIMIT).all())
    free_query = _active(incoming_scope).filter(Task.status == "new")
    unread = visible_notifications_query(user).filter(TaskNotification.is_read.is_(False)).count()
    # visible_notifications_query already outer-joins Task (many-to-one, no row duplication); reuse that join for the title.
    latest = (visible_notifications_query(user).options(contains_eager(TaskNotification.task))
        .order_by(TaskNotification.created_at.desc(), TaskNotification.id.desc()).limit(LIST_LIMIT).all())
    return dict(mine=mine, free=[] if user.role == "admin" else free_query.order_by(*ordered).limit(LIST_LIMIT).all(), free_total=free_query.count(),
        rework=_active(incoming_scope).filter(Task.status == "in_progress", Task.rework_cycle > 0).count(),
        unread=unread, notifications=[dict(id=n.id, task_id=n.task_id, text=notification_text(n), created_at=n.created_at, is_read=n.is_read) for n in latest],
        priorities=analytics.priorities(incoming_scope), punctuality=analytics.punctuality(incoming_scope, now),
        trend=analytics.trend(incoming_scope, incoming_scope, now, TREND_DAYS),
        ranking=analytics.department_ranking(now) if user.role == "admin" else None)


def db_department(user):
    if user.role == "admin" or user.department_id is None:
        return None
    return db.session.get(Department, user.department_id)


@dashboard_bp.route("")
@task_user_required
def dashboard():
    return render_template("dashboard.html", data=dashboard_data(current_user))
