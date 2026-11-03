"""Рабочая панель: сводка по поручениям отдела (только чтение)."""
from datetime import timedelta

from flask import Blueprint, render_template
from flask_login import current_user
from sqlalchemy import and_, true
from sqlalchemy.orm import joinedload

from .extensions import db
from .models import Department, Task
from .permissions import ACTIVE, dispatched_clause, task_visibility_clause
from .reporting import _metrics, utc_now
from .tasks import _deadline_state, task_user_required

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")

SOON_DAYS = 7


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
    return dict(
        generated_at=now, department=department, total=total, percent=round(100 * done / total) if total else 0,
        done=done, in_progress=statuses["in_progress"], new=statuses["new"], overdue=incoming["overdue"],
        closed_other=statuses["cancelled"] + statuses["rejected"], soon=soon_query.count(), soon_days=SOON_DAYS,
        outgoing_active=(outgoing["statuses"]["new"] + outgoing["statuses"]["in_progress"]) if outgoing else None,
        outgoing_total=outgoing["total"] if outgoing else None, upcoming=upcoming, overdue_tasks=overdue,
        deadline_states={task.id: _deadline_state(task, now) for task in upcoming + overdue})


def db_department(user):
    if user.role == "admin" or user.department_id is None:
        return None
    return db.session.get(Department, user.department_id)


@dashboard_bp.route("")
@task_user_required
def dashboard():
    return render_template("dashboard.html", data=dashboard_data(current_user))
