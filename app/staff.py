"""Head-only department staff; no administrative routes are reused."""
from functools import wraps

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .accounts import AccountConflict, AccountError, create_account, update_account, set_account_blocked
from .extensions import db
from .models import Task, TaskAssignee, User
from .permissions import ACTIVE, can_manage_staff, can_manage_employee, db_get

staff_bp = Blueprint("staff", __name__, url_prefix="/staff")


def head_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not can_manage_staff(current_user): abort(403)
        return view(*args, **kwargs)
    return wrapped


def _employee(user_id):
    user = db_get(User, user_id)
    if not can_manage_employee(current_user, user): abort(404)
    return user


def _fields(allowed):
    if set(request.form) - allowed or request.files or any(len(request.form.getlist(k)) != 1 for k in request.form):
        raise AccountError("Форма содержит неподдерживаемые или повторяющиеся поля")


def _list_context(error=None):
    members = User.query.filter_by(role="employee", department_id=current_user.department_id).order_by(User.full_name, User.id).all()
    counts = dict(db.session.execute(select(TaskAssignee.user_id, func.count()).join(Task, Task.id == TaskAssignee.task_id).where(Task.status.in_(ACTIVE),
        TaskAssignee.user_id.in_([u.id for u in members])).group_by(TaskAssignee.user_id)).all())
    return dict(employees=members, active_assignments=counts, department=current_user.department,
        may_create=not current_user.department.is_archived, error=error)


@staff_bp.route("")
@head_required
def staff_list():
    return render_template("staff/list.html", **_list_context())


def _form(user_id=None):
    employee = _employee(user_id) if user_id is not None else None
    if request.method == "GET":
        if employee is None and current_user.department.is_archived:
            return render_template("staff/list.html", **_list_context("В архивный отдел нельзя добавлять сотрудников")), 400
        return render_template("staff/form.html", employee=employee, department=current_user.department, error=None)
    try:
        _fields({"csrf_token", "full_name", "username", "password"})
        values = {key: request.form.get(key, "") for key in ("full_name", "username", "password")}
        if employee is None: create_account(actor=current_user, **values)
        else: update_account(actor=current_user, user_id=user_id, **values)
        db.session.commit()
    except PermissionError:
        db.session.rollback(); abort(403)
    except LookupError:
        db.session.rollback(); abort(404)
    except (AccountError, SQLAlchemyError) as failure:
        db.session.rollback()
        status = 409 if isinstance(failure, AccountConflict) else 400
        if isinstance(failure, IntegrityError): message = "Пользователь с таким логином уже существует"
        elif isinstance(failure, SQLAlchemyError): message, status = "Не удалось сохранить сотрудника", 500
        else: message = str(failure)
        employee = _employee(user_id) if user_id is not None else None
        return render_template("staff/form.html", employee=employee, department=current_user.department,
            error=message), status
    flash("Сотрудник сохранён", "success")
    return redirect(url_for("staff.staff_list"))


@staff_bp.route("/new", methods=["GET", "POST"])
@head_required
def staff_new():
    return _form()


@staff_bp.route("/<int:user_id>/edit", methods=["GET", "POST"])
@head_required
def staff_edit(user_id):
    return _form(user_id)


@staff_bp.route("/<int:user_id>/disable", methods=["POST"])
@head_required
def staff_disable(user_id):
    _employee(user_id)
    try:
        _fields({"csrf_token"})
        set_account_blocked(actor=current_user, user_id=user_id, blocked=True)
        db.session.commit()
    except PermissionError:
        db.session.rollback(); abort(403)
    except LookupError:
        db.session.rollback(); abort(404)
    except (AccountError, SQLAlchemyError) as failure:
        db.session.rollback()
        status = 409 if isinstance(failure, AccountConflict) else 400
        message = str(failure)
        if isinstance(failure, SQLAlchemyError): message, status = "Не удалось отключить сотрудника", 500
        return render_template("staff/list.html", **_list_context(message)), status
    flash("Учётная запись отключена. Данные и история сохранены", "success")
    return redirect(url_for("staff.staff_list"))
