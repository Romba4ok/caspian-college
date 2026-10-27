"""Administrator account management with transactional audit and safe deletion."""
from functools import wraps

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from . import ROLES
from .audit import account_snapshot, actor_snapshot, record_admin_event
from .extensions import db
from .models import Department, Group, Task, TaskAttachment, TaskCategory, TaskComment, TaskEvent, TaskNotification, User, group_students
from .departments import (DepartmentError, archive_department, create_department, delete_department,
                          department_counts, rename_department, restore_department)
from .settings import (SettingsError, SUPPORTED_EXTENSIONS, UploadPolicy, archive_category, create_category,
                       get_upload_policy, rename_category, update_upload_policy)

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


def _lock_accounts(user_id=None):
    ids = {current_user.id}
    if user_id is not None:
        ids.add(user_id)
    # Keep authority changes exclusive without blocking concurrent notification FK checks.
    users = db.session.execute(select(User).where(User.id.in_(ids)).order_by(User.id)
        .with_for_update(key_share=True).execution_options(populate_existing=True)).scalars().all()
    accounts = {user.id: user for user in users}
    actor = accounts.get(current_user.id)
    if actor is None or not actor.is_active or actor.role != "admin":
        abort(403)
    if user_id is not None and user_id not in accounts:
        abort(404)
    return accounts.get(user_id)


@admin_bp.route("/admin/users")
@admin_required
def admin_users():
    return render_template("admin_users.html", users=User.query.order_by(User.id).all(), roles=ROLES)


def _user_form_context(user=None):
    query = Department.query.filter(or_(Department.is_archived.is_(False),
        Department.id == user.department_id if user is not None else db.false()))
    return {"user": user, "roles": {"admin": ROLES["admin"]} if user and user.role == "admin" else EDITABLE_ROLES,
            "departments": query.order_by(Department.name).all()}


def _read_fields(user=None):
    name = request.form.get("full_name", "").strip()
    username = request.form.get("username", "").strip()
    role = request.form.get("role", "")
    allowed = {"admin"} if user and user.role == "admin" else EDITABLE_ROLES
    if not name or len(name) > 160 or not username or len(username) > 80 or role not in allowed:
        raise ValueError("Укажите имя до 160 символов, логин до 80 символов и допустимую роль")
    raw_department = request.form.get("department_id", "").strip()
    if role == "admin":
        if raw_department:
            raise ValueError("Администратору отдел не назначается")
        return name, username, role, None
    try:
        department_id = int(raw_department)
    except ValueError:
        raise ValueError("Выберите существующий отдел") from None
    department = db.session.execute(select(Department).where(Department.id == department_id)
        .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if department is None:
        raise ValueError("Выбранный отдел не найден")
    if department.is_archived and (user is None or user.department_id != department.id):
        raise ValueError("Нельзя назначить пользователя в архивный отдел")
    return name, username, role, department.id


def _form_error(message, user=None):
    db.session.rollback()
    flash(message, "error")
    return render_template("admin_user_form.html", **_user_form_context(user)), 400


@admin_bp.route("/admin/users/new", methods=["GET", "POST"])
@admin_required
def admin_user_new():
    if request.method == "POST":
        _lock_accounts()
        try:
            name, username, role, department_id = _read_fields()
            password = request.form.get("password", "")
            if not password:
                raise ValueError("Пароль обязателен при создании учётной записи")
        except ValueError as error:
            return _form_error(str(error))
        user = User(full_name=name, username=username, role=role, department_id=department_id)
        user.set_password(password)
        try:
            db.session.add(user)
            db.session.flush()
            record_admin_event("account_created", "user", user.id, after=dict(account_snapshot(user), password_changed=True))
            db.session.commit()
        except IntegrityError:
            return _form_error("Пользователь с таким логином уже существует или данные изменились. Проверьте отдел и роль.")
        flash("Учётная запись создана", "success")
        return redirect(url_for("admin.admin_users"))
    return render_template("admin_user_form.html", **_user_form_context())


@admin_bp.route("/admin/users/<int:user_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_user_edit(user_id):
    user = _lock_accounts(user_id) if request.method == "POST" else db.get_or_404(User, user_id)
    if request.method == "POST":
        try:
            name, username, role, department_id = _read_fields(user)
        except ValueError as error:
            return _form_error(str(error), user)
        before, snapshot = account_snapshot(user), actor_snapshot(current_user)
        user.full_name, user.username, user.role, user.department_id = name, username, role, department_id
        password = request.form.get("password", "")
        if password:
            user.set_password(password)
        try:
            record_admin_event("account_updated", "user", user.id, before=before,
                after=dict(account_snapshot(user), password_changed=bool(password)), snapshot=snapshot)
            db.session.commit()
        except IntegrityError:
            return _form_error("Пользователь с таким логином уже существует или данные изменились. Проверьте отдел и роль.", user)
        flash("Учётная запись обновлена", "success")
        return redirect(url_for("admin.admin_users"))
    return render_template("admin_user_form.html", **_user_form_context(user))


def _block_change(user_id, blocked):
    user = _lock_accounts(user_id)
    if user.role == "admin":
        flash("Нельзя заблокировать последнего администратора" if blocked else "Администратор уже активен", "error" if blocked else "success")
        return redirect(url_for("admin.admin_users"))
    if not blocked and (user.role not in EDITABLE_ROLES or user.department_id is None):
        flash("Перед разблокировкой назначьте пользователю отдел и роль", "error")
        return redirect(url_for("admin.admin_users"))
    if user.is_blocked != blocked:
        before = account_snapshot(user)
        user.is_blocked = blocked
        record_admin_event("account_blocked" if blocked else "account_unblocked", "user", user.id,
                           before=before, after=account_snapshot(user))
        db.session.commit()
    flash("Учётная запись заблокирована" if blocked else "Учётная запись разблокирована", "success")
    return redirect(url_for("admin.admin_users"))


@admin_bp.route("/admin/users/<int:user_id>/block", methods=["POST"])
@admin_required
def admin_user_block(user_id):
    return _block_change(user_id, True)


@admin_bp.route("/admin/users/<int:user_id>/unblock", methods=["POST"])
@admin_required
def admin_user_unblock(user_id):
    return _block_change(user_id, False)


def _has_task_links(user_id):
    links = [Task.query.filter(or_(Task.author_id == user_id, Task.assignee_id == user_id)).exists(),
        TaskComment.query.filter_by(author_id=user_id).exists(), TaskAttachment.query.filter_by(uploaded_by_id=user_id).exists(),
        TaskEvent.query.filter_by(actor_id=user_id).exists(), TaskNotification.query.filter_by(user_id=user_id).exists()]
    return db.session.query(or_(*links)).scalar()


def _has_legacy_links(user_id):
    return db.session.query(or_(Group.query.filter_by(teacher_id=user_id).exists(),
        db.session.query(group_students).filter(group_students.c.student_id == user_id).exists())).scalar()


@admin_bp.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_user_delete(user_id):
    user = _lock_accounts(user_id)
    if user.role == "admin":
        flash("Нельзя удалить последнего администратора", "error")
        return redirect(url_for("admin.admin_users"))
    if _has_task_links(user.id):
        flash("Нельзя удалить учётную запись, связанную с задачами. Вместо удаления заблокируйте её.", "error")
        return redirect(url_for("admin.admin_users"))
    if _has_legacy_links(user.id):
        flash("Нельзя удалить учётную запись с историческими данными групп. Вместо удаления заблокируйте её.", "error")
        return redirect(url_for("admin.admin_users"))
    try:
        record_admin_event("account_deleted", "user", user.id, before=account_snapshot(user))
        db.session.delete(user)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Нельзя удалить учётную запись со связанными данными. Вместо удаления заблокируйте её.", "error")
        return redirect(url_for("admin.admin_users"))
    flash("Учётная запись удалена", "success")
    return redirect(url_for("admin.admin_users"))


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


def _categories_response(*, error=None, editing_id=None):
    counts = dict(db.session.execute(select(Task.category_id, func.count(Task.id))
        .where(Task.category_id.is_not(None)).group_by(Task.category_id)).all())
    return render_template("admin/categories.html", categories=TaskCategory.query.order_by(
        TaskCategory.is_archived, TaskCategory.name, TaskCategory.id).all(),
        task_counts=counts, error=error, editing_id=editing_id), 400 if error else 200


@admin_bp.route("/admin/categories")
@admin_required
def admin_categories():
    return _categories_response()


@admin_bp.route("/admin/categories/new", methods=["POST"])
@admin_required
def admin_category_new():
    try:
        create_category(actor=current_user, name=request.form.get("name", ""))
        db.session.commit()
    except PermissionError:
        db.session.rollback()
        abort(403)
    except SettingsError as error:
        db.session.rollback()
        return _categories_response(error=str(error))
    except IntegrityError:
        db.session.rollback()
        return _categories_response(error="Название занято или данные изменились. Проверьте категорию.")
    flash("Категория создана", "success")
    return redirect(url_for("admin.admin_categories"))


@admin_bp.route("/admin/categories/<int:category_id>/edit", methods=["POST"])
@admin_required
def admin_category_edit(category_id):
    try:
        rename_category(actor=current_user, category_id=category_id, name=request.form.get("name", ""))
        db.session.commit()
    except PermissionError:
        db.session.rollback()
        abort(403)
    except LookupError:
        db.session.rollback()
        abort(404)
    except SettingsError as error:
        db.session.rollback()
        return _categories_response(error=str(error), editing_id=category_id)
    except IntegrityError:
        db.session.rollback()
        return _categories_response(error="Название занято или данные изменились. Проверьте категорию.", editing_id=category_id)
    flash("Название категории изменено", "success")
    return redirect(url_for("admin.admin_categories"))


@admin_bp.route("/admin/categories/<int:category_id>/archive", methods=["POST"])
@admin_required
def admin_category_archive(category_id):
    try:
        archive_category(actor=current_user, category_id=category_id)
        db.session.commit()
    except PermissionError:
        db.session.rollback()
        abort(403)
    except LookupError:
        db.session.rollback()
        abort(404)
    except IntegrityError:
        db.session.rollback()
        flash("Не удалось архивировать категорию: данные изменились. Повторите действие.", "error")
    else:
        flash("Категория архивирована", "success")
    return redirect(url_for("admin.admin_categories"))


def _settings_response(policy, *, error=None, field=None):
    selected = {value.strip().lower() for value in request.form.getlist("allowed_extensions")} if request.method == "POST" else set(policy.allowed_extensions)
    return render_template("admin/settings.html", policy=policy, extensions=SUPPORTED_EXTENSIONS,
        selected_extensions=selected, error=error, error_field=field), 400 if error else 200


@admin_bp.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        try:
            update_upload_policy(actor=current_user, max_upload_mb=request.form.get("max_upload_mb", ""),
                                 allowed_extensions=request.form.getlist("allowed_extensions"))
            db.session.commit()
        except PermissionError:
            db.session.rollback()
            abort(403)
        except SettingsError as error:
            db.session.rollback()
            return _settings_response(UploadPolicy(), error=str(error), field=error.field)
        except IntegrityError:
            db.session.rollback()
            return _settings_response(UploadPolicy(), error="Настройки не сохранены: данные изменились. Повторите действие.")
        flash("Настройки файлов сохранены", "success")
        return redirect(url_for("admin.admin_settings"))
    try:
        policy = get_upload_policy()
    except SettingsError:
        return _settings_response(UploadPolicy(), error="Сохранённые настройки некорректны. Укажите допустимые значения и сохраните их.")
    return _settings_response(policy)


@admin_bp.route("/admin/statistics")
@admin_required
def admin_statistics():
    from .reporting import global_statistics
    from .tasks import STATUS_LABELS
    try:
        report = global_statistics(current_user)
    except PermissionError:
        abort(403)
    return render_template("admin/statistics.html", report=report, status_labels=STATUS_LABELS)


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
