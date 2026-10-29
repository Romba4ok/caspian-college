"""Administrator account management with shared guards and soft deactivation."""
from functools import wraps

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from . import ROLES
from .accounts import AccountError, AccountConflict, create_account, update_account, set_account_blocked
from .permissions import db_get

from .extensions import db
from .models import Department, Task, User
from .departments import (DepartmentError, archive_department, create_department, delete_department,
                          department_counts, rename_department, restore_department)

admin_bp = Blueprint("admin", __name__)
EDITABLE_ROLES = {key: ROLES[key] for key in ("employee", "head")}


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.role != "admin" or not current_user.is_active:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@admin_bp.route("/admin/users")
@admin_required
def admin_users():
    return render_template("admin_users.html", users=User.query.order_by(User.id).all(), roles=ROLES)


def _user_form_context(user=None):
    query = Department.query.filter(or_(Department.is_archived.is_(False),
        Department.id == user.department_id if user is not None else db.false()))
    return {"user": user, "roles": {"admin": ROLES["admin"]} if user and user.role == "admin" else EDITABLE_ROLES,
            "departments": query.order_by(Department.name).all()}


def _account_form_fields():
    allowed={"csrf_token","full_name","username","password","role","department_id"}
    _account_form_check(allowed)
    return {key:request.form.get(key,"") for key in ("full_name","username","password","role","department_id")}


def _account_form_check(allowed):
    if set(request.form)-allowed or request.files or any(len(request.form.getlist(key))!=1 for key in request.form):
        raise AccountError("Форма содержит неподдерживаемые или повторяющиеся поля")


def _form_error(message, user=None, status=400):
    db.session.rollback()
    flash(message,"error")
    return render_template("admin_user_form.html",**_user_form_context(user)),status


def _account_save(user_id=None):
    user=db_get(User,user_id) if user_id is not None else None
    if request.method=="GET":
        return render_template("admin_user_form.html",**_user_form_context(user))
    try:
        fields=_account_form_fields()
        if user_id is None:create_account(actor=current_user,**fields)
        else:update_account(actor=current_user,user_id=user_id,**fields)
        db.session.commit()
    except PermissionError:
        db.session.rollback();abort(403)
    except LookupError:
        db.session.rollback();abort(404)
    except AccountError as error:
        return _form_error(str(error),user,409 if isinstance(error,AccountConflict) else 400)
    except IntegrityError:
        return _form_error("Пользователь с таким логином уже существует или данные изменились. Проверьте отдел и роль.",user)
    except SQLAlchemyError:
        return _form_error("Не удалось сохранить учётную запись",user,500)
    flash("Учётная запись сохранена","success")
    return redirect(url_for("admin.admin_users"))


@admin_bp.route("/admin/users/new",methods=["GET","POST"])
@admin_required
def admin_user_new():
    return _account_save()


@admin_bp.route("/admin/users/<int:user_id>/edit",methods=["GET","POST"])
@admin_required
def admin_user_edit(user_id):
    return _account_save(user_id)


def _block_change(user_id,blocked):
    try:
        _account_form_check({"csrf_token"})
        set_account_blocked(actor=current_user,user_id=user_id,blocked=blocked)
        db.session.commit()
    except PermissionError:
        db.session.rollback();abort(403)
    except LookupError:
        db.session.rollback();abort(404)
    except AccountError as error:
        db.session.rollback();flash(str(error),"error")
        return render_template("admin_users.html",users=User.query.order_by(User.id).all(),roles=ROLES),409 if isinstance(error,AccountConflict) else 400
    except SQLAlchemyError:
        db.session.rollback();flash("Не удалось изменить доступ пользователя","error")
        return render_template("admin_users.html",users=User.query.order_by(User.id).all(),roles=ROLES),500
    flash("Учётная запись отключена" if blocked else "Доступ восстановлен","success")
    return redirect(url_for("admin.admin_users"))


@admin_bp.route("/admin/users/<int:user_id>/block",methods=["POST"])
@admin_required
def admin_user_block(user_id):
    return _block_change(user_id,True)


@admin_bp.route("/admin/users/<int:user_id>/unblock",methods=["POST"])
@admin_required
def admin_user_unblock(user_id):
    return _block_change(user_id,False)


@admin_bp.route("/admin/users/<int:user_id>/delete",methods=["POST"])
@admin_required
def admin_user_delete(user_id):
    # Compatibility URL: account identity/history are never physically deleted.
    return _block_change(user_id,True)


@admin_bp.route("/admin/departments")
@admin_required
def admin_departments():
    members, tasks = department_counts()
    return render_template("admin/departments.html",
        departments=Department.query.order_by(Department.is_archived, Department.name, Department.id).all(),
        member_counts=members, task_counts=tasks)


def _department_form_error(message, department=None):
    db.session.rollback()
    return render_template("admin/department_form.html", department=department, error=message), 400


@admin_bp.route("/admin/departments/new", methods=["GET", "POST"])
@admin_required
def admin_department_new():
    if request.method == "POST":
        try:
            create_department(actor=current_user, name=request.form.get("name", ""))
            db.session.commit()
        except PermissionError:
            db.session.rollback()
            abort(403)
        except DepartmentError as error:
            return _department_form_error(str(error))
        except IntegrityError:
            return _department_form_error("Название занято или данные изменились. Проверьте название отдела.")
        flash("Отдел создан", "success")
        return redirect(url_for("admin.admin_departments"))
    return render_template("admin/department_form.html", department=None, error=None)


@admin_bp.route("/admin/departments/<int:department_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_department_edit(department_id):
    if request.method == "POST":
        try:
            rename_department(actor=current_user, department_id=department_id, name=request.form.get("name", ""))
            db.session.commit()
        except PermissionError:
            db.session.rollback()
            abort(403)
        except LookupError:
            db.session.rollback()
            abort(404)
        except DepartmentError as error:
            department = db.get_or_404(Department, department_id)
            return _department_form_error(str(error), department)
        except IntegrityError:
            db.session.rollback()
            return _department_form_error("Название занято или данные изменились. Проверьте название отдела.",
                                          db.get_or_404(Department, department_id))
        flash("Название отдела изменено", "success")
        return redirect(url_for("admin.admin_departments"))
    return render_template("admin/department_form.html", department=db.get_or_404(Department, department_id), error=None)


def _department_action(department_id, action, message):
    try:
        action(actor=current_user, department_id=department_id)
        db.session.commit()
    except PermissionError:
        db.session.rollback()
        abort(403)
    except LookupError:
        db.session.rollback()
        abort(404)
    except DepartmentError as error:
        db.session.rollback()
        flash(str(error), "error")
    except IntegrityError:
        db.session.rollback()
        flash("Отдел связан с данными или данные изменились. Операция отменена; используйте архивирование.", "error")
    else:
        flash(message, "success")
    return redirect(url_for("admin.admin_departments"))


@admin_bp.route("/admin/departments/<int:department_id>/archive", methods=["POST"])
@admin_required
def admin_department_archive(department_id):
    return _department_action(department_id, archive_department, "Отдел архивирован")


@admin_bp.route("/admin/departments/<int:department_id>/restore", methods=["POST"])
@admin_required
def admin_department_restore(department_id):
    return _department_action(department_id, restore_department, "Отдел восстановлен")


@admin_bp.route("/admin/departments/<int:department_id>/delete", methods=["POST"])
@admin_required
def admin_department_delete(department_id):
    return _department_action(department_id, delete_department, "Пустой отдел удалён")


@admin_bp.route("/admin/categories")
@admin_required
def admin_categories():
    abort(410, description="Управление категориями больше не используется")


@admin_bp.route("/admin/categories/new", methods=["POST"])
@admin_required
def admin_category_new():
    abort(410, description="Управление категориями больше не используется")


@admin_bp.route("/admin/categories/<int:category_id>/edit", methods=["POST"])
@admin_required
def admin_category_edit(category_id):
    abort(410, description="Управление категориями больше не используется")


@admin_bp.route("/admin/categories/<int:category_id>/archive", methods=["POST"])
@admin_required
def admin_category_archive(category_id):
    abort(410, description="Управление категориями больше не используется")


@admin_bp.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    abort(410, description="Страница настроек больше не используется")


@admin_bp.route("/admin/statistics")
@admin_required
def admin_statistics():
    from .reporting import global_statistics, report_status_labels
    try:
        report = global_statistics(current_user)
    except PermissionError:
        abort(403)
    return render_template("admin/statistics.html", report=report, status_labels=report_status_labels())


@admin_bp.route("/admin/audit")
@admin_required
def admin_audit():
    from .audit import AuditFilterError, audit_page
    filters = {key: request.args.get(key, "") for key in ("user_id", "date_from", "date_to")}
    try:
        for key in (*filters, "page"):
            if len(request.args.getlist(key)) > 1:
                raise AuditFilterError("Передайте одно значение для каждого поля фильтра.", key)
        journal = audit_page(current_user, **filters, page=request.args.get("page", "1"))
    except PermissionError:
        abort(403)
    except AuditFilterError as error:
        return render_template("admin/audit.html", journal=None, filters=filters,
            error=str(error), error_field=error.field), 400
    return render_template("admin/audit.html", journal=journal, filters=journal["filters"], error=None)
