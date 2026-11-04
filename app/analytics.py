"""Дополнительная аналитика только для чтения: динамика, приоритеты, своевременность, нагрузка.

Все цифры считаются из существующих полей задач; ничего не оценивается и не прогнозируется.
Область (scope) всегда приходит снаружи, поэтому права доступа определяет вызывающий код.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import current_app
from sqlalchemy import case, func, select

from .accounts import deleted_account_clause
from .extensions import db
from .models import Department, Task, TaskAssignee, TASK_PRIORITIES, User
from .permissions import ACTIVE, dispatched_clause


def _utc(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _count(condition):
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)


def trend(sent_scope, done_scope, now, days=30):
    """Поступило и выполнено по календарным дням (Asia/Qyzylorda), последние `days` дней."""
    zone = ZoneInfo(current_app.config.get("TASK_TIMEZONE", "Asia/Qyzylorda"))
    first = now.astimezone(zone).date() - timedelta(days=days - 1)
    # SQLite хранит время без пояса: границу сравниваем в UTC, как и остальные отчёты.
    start = datetime.combine(first, datetime.min.time(), zone).astimezone(timezone.utc)
    sent, done = Counter(), Counter()
    for (moment,) in db.session.execute(select(Task.sent_at).where(sent_scope, ~Task.is_deleted, dispatched_clause(),
            Task.sent_at >= start, Task.sent_at <= now)):
        sent[_utc(moment).astimezone(zone).date()] += 1
    for (moment,) in db.session.execute(select(Task.completed_at).where(done_scope, ~Task.is_deleted, dispatched_clause(),
            Task.status == "completed", Task.completed_at.is_not(None), Task.completed_at >= start, Task.completed_at <= now)):
        done[_utc(moment).astimezone(zone).date()] += 1
    series = [dict(day=first + timedelta(days=i), sent=sent[first + timedelta(days=i)], completed=done[first + timedelta(days=i)])
        for i in range(days)]
    return dict(days=series, maximum=max([1] + [max(d["sent"], d["completed"]) for d in series]),
        sent_total=sum(d["sent"] for d in series), completed_total=sum(d["completed"] for d in series), length=days)


def priorities(scope):
    """Активные задачи по приоритетам (всегда все четыре ключа)."""
    rows = db.session.execute(select(Task.priority, func.count(Task.id)).where(scope, ~Task.is_deleted, dispatched_clause(),
        Task.status.in_(ACTIVE)).group_by(Task.priority)).all()
    found = {key: int(count) for key, count in rows}
    result = {key: found.get(key, 0) for key in TASK_PRIORITIES}
    return dict(counts=result, total=sum(result.values()))


def punctuality(scope, now):
    """Выполнено в срок или позже срока; процент считается только при наличии выполненных задач."""
    row = db.session.execute(select(_count(Task.completed_at <= Task.due_at), _count(Task.completed_at > Task.due_at))
        .where(scope, ~Task.is_deleted, dispatched_clause(), Task.status == "completed", Task.completed_at.is_not(None),
            Task.completed_at <= now)).one()
    on_time, late = int(row[0]), int(row[1])
    total = on_time + late
    return dict(on_time=on_time, late=late, total=total, percent=round(100 * on_time / total) if total else None)


def workload(department_id, now):
    """Нагрузка действующих участников отдела по входящим задачам. Задача с несколькими исполнителями видна каждому."""
    members = db.session.execute(select(User.id, User.full_name, User.role).where(User.department_id == department_id,
        ~User.is_blocked, ~deleted_account_clause(User.id)).order_by(User.full_name, User.id)).all()
    overdue = (Task.status.in_(ACTIVE)) & (Task.due_at < now)
    stats = {row[0]: row for row in db.session.execute(select(TaskAssignee.user_id, _count(Task.status.in_(ACTIVE)),
        _count(overdue), _count(Task.status == "completed")).join(Task, Task.id == TaskAssignee.task_id)
        .where(Task.recipient_department_id == department_id, ~Task.is_deleted, dispatched_clause())
        .group_by(TaskAssignee.user_id))}
    rows = []
    for ident, name, role in members:
        _, active, late, done = stats.get(ident, (ident, 0, 0, 0))
        rows.append(dict(id=ident, name=name, role=role, active=int(active), overdue=int(late), completed=int(done)))
    free = db.session.scalar(select(func.count(Task.id)).where(Task.recipient_department_id == department_id, ~Task.is_deleted,
        dispatched_clause(), Task.status == "new")) or 0
    return dict(rows=rows, free=int(free), maximum=max([1] + [r["active"] + r["completed"] for r in rows]))


def counterparts(department_id):
    """С какими отделами идёт обмен поручениями: сколько получено от них и отправлено им."""
    incoming = dict(db.session.execute(select(Task.sender_department_id, func.count(Task.id)).where(
        Task.recipient_department_id == department_id, ~Task.is_deleted, dispatched_clause()).group_by(Task.sender_department_id)).all())
    outgoing = dict(db.session.execute(select(Task.recipient_department_id, func.count(Task.id)).where(
        Task.sender_department_id == department_id, ~Task.is_deleted, dispatched_clause()).group_by(Task.recipient_department_id)).all())
    ids = (set(incoming) | set(outgoing)) - {None, department_id}
    names = dict(db.session.execute(select(Department.id, Department.name).where(Department.id.in_(ids))).all()) if ids else {}
    rows = sorted((dict(name=names.get(i, "Отдел"), incoming=int(incoming.get(i, 0)), outgoing=int(outgoing.get(i, 0))) for i in ids),
        key=lambda r: (-(r["incoming"] + r["outgoing"]), r["name"]))
    return dict(rows=rows[:8], maximum=max([1] + [max(r["incoming"], r["outgoing"]) for r in rows]))


def department_ranking(now, limit=6):
    """Входящая нагрузка отделов для администратора: сначала где больше всего просрочек и активных задач."""
    active = _count(Task.status.in_(ACTIVE))
    overdue = _count(Task.status.in_(ACTIVE) & (Task.due_at < now))
    done = _count(Task.status == "completed")
    rows = db.session.execute(select(Department.id, Department.name, Department.is_archived, active, overdue, done)
        .join(Task, Task.recipient_department_id == Department.id)
        .where(~Task.is_deleted, dispatched_clause()).group_by(Department.id, Department.name, Department.is_archived)).all()
    items = sorted((dict(id=r[0], name=r[1], archived=r[2], active=int(r[3]), overdue=int(r[4]), completed=int(r[5])) for r in rows),
        key=lambda r: (-r["overdue"], -r["active"], r["name"]))[:limit]
    return dict(rows=items, maximum=max([1] + [r["active"] + r["completed"] for r in items]))


def extras_for_scope(scope, now, days=30):
    """Общий набор для отчётов: динамика, приоритеты, своевременность по одной области."""
    return dict(trend=trend(scope, scope, now, days), priorities=priorities(scope), punctuality=punctuality(scope, now))
