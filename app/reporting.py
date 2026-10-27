"""Read-only SQL aggregates, scoped to the current account and department."""
from datetime import datetime, timezone

from flask import Blueprint, abort, render_template, request
from flask_login import current_user, login_required
from sqlalchemy import and_, case, func, select, true

from .extensions import db
from .models import Department, Task, TASK_STATUSES, User
from .permissions import TERMINAL, dispatched_clause, is_platform_user, task_visibility_clause

reports_bp = Blueprint("reports", __name__, url_prefix="/reports")


def utc_now():
    return datetime.now(timezone.utc)


def _actor(actor, role):
    ident = getattr(actor, "id", None)
    if type(ident) is not int:
        raise PermissionError("Требуется действующая учётная запись")
    with db.session.no_autoflush:
        fresh = db.session.get(User, ident, populate_existing=True)
    if not is_platform_user(fresh) or fresh.role != role:
        raise PermissionError("Отчёт недоступен для вашей роли")
    return fresh


def _empty():
    return dict(total=0, statuses={key: 0 for key in TASK_STATUSES}, overdue=0,
        average_hours=None, included=0, excluded=0)


def _metrics(scope, now, *, group=None):
    """One grouped query per direction; no task texts or account rows leave SQL."""
    valid = and_(Task.status == "completed", dispatched_clause(), Task.sent_at.is_not(None),
        Task.completed_at.is_not(None), Task.completed_at >= Task.sent_at)
    overdue = and_(dispatched_clause(), ~Task.status.in_(TERMINAL), Task.due_at.is_not(None), Task.due_at < now)
    if db.session.get_bind().dialect.name == "postgresql":
        hours = func.extract("epoch", Task.completed_at - Task.sent_at) / 3600.0
    else:
        hours = (func.julianday(Task.completed_at) - func.julianday(Task.sent_at)) * 24.0
    count = lambda condition: func.coalesce(func.sum(case((condition, 1), else_=0)), 0)
    columns = [func.count(Task.id).label("total"), count(overdue).label("overdue"),
        count(valid).label("included"), func.avg(case((valid, hours), else_=None)).label("average_hours")]
    columns += [count(Task.status == status).label("status_" + status) for status in TASK_STATUSES]
    statement = select(*columns).where(scope)
    if group is not None:
        statement = statement.add_columns(group.label("department_id")).group_by(group)
    result = {}
    for row in db.session.execute(statement).mappings():
        counts = {key: int(row["status_" + key]) for key in TASK_STATUSES}
        included = int(row["included"])
        result[row["department_id"] if group is not None else None] = dict(
            total=int(row["total"]), statuses=counts, overdue=int(row["overdue"]), included=included,
            excluded=counts["completed"] - included,
            average_hours=float(row["average_hours"]) if row["average_hours"] is not None else None)
    return result if group is not None else result.get(None, _empty())


def _moment(now):
    value = now or utc_now()
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def global_statistics(actor, *, now=None):
    _actor(actor, "admin")
    moment = _moment(now)
    incoming = _metrics(dispatched_clause(), moment, group=Task.recipient_department_id)
    outgoing = _metrics(true(), moment, group=Task.sender_department_id)
    departments = db.session.execute(select(Department.id, Department.name, Department.is_archived)
        .order_by(Department.name, Department.id)).mappings().all()
    return dict(overall=_metrics(true(), moment), generated_at=moment,
        departments=[dict(id=d["id"], name=d["name"], archived=d["is_archived"],
            incoming=incoming.get(d["id"], _empty()), outgoing=outgoing.get(d["id"], _empty())) for d in departments])


def department_report(actor, *, department_id=None, now=None):
    actor = _actor(actor, "head")
    if department_id is not None and (type(department_id) is not int or department_id != actor.department_id):
        raise PermissionError("Доступен только отчёт своего отдела")
    department = db.session.execute(select(Department.id, Department.name, Department.is_archived)
        .where(Department.id == actor.department_id)).mappings().one_or_none()
    if department is None:
        raise PermissionError("Отдел недоступен")
    moment = _moment(now)
    visible = task_visibility_clause(actor)
    return dict(department=dict(id=department["id"], name=department["name"], archived=department["is_archived"]),
        generated_at=moment, overall=_metrics(visible, moment),
        incoming=_metrics(and_(visible, dispatched_clause(), Task.recipient_department_id == actor.department_id), moment),
        outgoing=_metrics(and_(visible, Task.sender_department_id == actor.department_id), moment))


@reports_bp.app_template_filter("report_hours")
def report_hours(value):
    return "Нет данных" if value is None else f"{value:.2f}".replace(".", ",") + " ч"


@reports_bp.route("/department")
@login_required
def own_department_report():
    if current_user.role != "head":
        abort(403)
    raw = request.args.getlist("department_id")
    if len(raw) > 1:
        abort(400)
    if raw and (not raw[0].isascii() or not raw[0].isdigit() or len(raw[0]) > 10):
        abort(400)
    try:
        report = department_report(current_user, department_id=int(raw[0]) if raw else None)
    except PermissionError:
        abort(403)
    from .tasks import STATUS_LABELS
    return render_template("reports/department.html", report=report, status_labels=STATUS_LABELS)
