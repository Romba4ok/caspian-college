"""Shared account operations. Callers commit the account and its audit together."""
import hmac
from hashlib import sha256

from flask import current_app
from sqlalchemy import func, select

from .audit import account_snapshot, actor_snapshot, record_admin_event
from .extensions import db
from .models import AdminAuditEvent, Department, Task, User
from .permissions import ACTIVE, can_manage_employee, is_platform_user


class AccountError(ValueError):
    def __init__(self, message, field=None):
        super().__init__(message)
        self.field = field


class AccountConflict(AccountError):
    """An active assignment prevents changing access or department."""


def _id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise AccountError("Выберите существующий отдел", "department_id")
    if isinstance(value, str) and (len(value)>10 or not value.isascii() or not value.isdecimal()):
        raise AccountError("Выберите существующий отдел", "department_id")
    value = int(value)
    if not 0 < value <= 2**31 - 1:
        raise AccountError("Выберите существующий отдел", "department_id")
    return value


def _locked_accounts(actor, user_id=None):
    if actor is None or getattr(actor, "id", None) is None:
        raise PermissionError("Требуется действующий пользователь")
    if user_id is not None:
        try:
            user_id = _id(user_id)
        except AccountError:
            raise LookupError("Сотрудник не найден") from None
    with db.session.no_autoflush:
        ids = {actor.id} | ({user_id} if user_id is not None else set())
        users = db.session.execute(select(User).where(User.id.in_(ids)).order_by(User.id)
            .with_for_update(key_share=True).execution_options(populate_existing=True)).scalars().all()
        accounts = {user.id: user for user in users}
        actor = accounts.get(actor.id)
        if not is_platform_user(actor) or actor.role not in ("admin", "head"):
            raise PermissionError("Управление сотрудниками недоступно")
        target = accounts.get(user_id)
        if user_id is not None and (target is None or actor.role == "head" and not can_manage_employee(actor, target)):
            raise LookupError("Сотрудник не найден")
        return actor, target


def _departments(actor, target=None, destination=None):
    ids = {actor.department_id, target.department_id if target else None, destination} - {None}
    return {dept.id: dept for dept in db.session.execute(select(Department).where(Department.id.in_(ids))
        .order_by(Department.id).with_for_update().execution_options(populate_existing=True)).scalars()}


def _fields(actor, target, full_name, username, role, department_id):
    full_name = full_name.strip() if isinstance(full_name, str) else ""
    username = username.strip() if isinstance(username, str) else ""
    if not full_name or len(full_name) > 160:
        raise AccountError("Укажите полное имя до 160 символов", "full_name")
    if not username or len(username) > 80:
        raise AccountError("Укажите логин до 80 символов", "username")
    if actor.role == "head":
        if role not in (None, "employee") or department_id not in (None, actor.department_id, str(actor.department_id)):
            raise AccountError("Начальник создаёт и редактирует только сотрудников своего отдела")
        role, department_id = "employee", actor.department_id
    elif target is not None and target.role == "admin":
        if role != "admin" or department_id not in (None, ""):
            raise AccountError("Нельзя лишить роли единственного администратора; отдел ему не назначается")
        department_id = None
    else:
        if role not in ("head", "employee"):
            raise AccountError("Допустимы роли сотрудника и начальника отдела", "role")
        department_id = _id(department_id)
    departments = _departments(actor, target, department_id)
    if role != "admin":
        department = departments.get(department_id)
        if department is None:
            raise AccountError("Выбранный отдел не найден", "department_id")
        if department.is_archived and (target is None or target.department_id != department.id):
            raise AccountError("Нельзя назначить пользователя в архивный отдел", "department_id")
    return dict(full_name=full_name, username=username, role=role, department_id=department_id)


def _require_no_active_assignments(user):
    # The same NO KEY UPDATE user lock is held by assign/claim/reassign/return.
    if db.session.execute(select(Task.id).where(Task.assignments.any(user_id=user.id), Task.status.in_(ACTIVE)).limit(1)).first():
        raise AccountConflict("Сначала переназначьте активные задачи сотрудника, затем отключите или переведите его")


def create_account(*, actor, full_name, username, password, role=None, department_id=None):
    actor, _ = _locked_accounts(actor)
    values = _fields(actor, None, full_name, username, role, department_id)
    if not isinstance(password, str) or not password:
        raise AccountError("Пароль обязателен при создании учётной записи", "password")
    user = User(**values)
    user.set_password(password)
    db.session.add(user); db.session.flush()
    record_admin_event("account_created", "user", user.id,
        after=dict(account_snapshot(user), password_changed=True), actor=actor)
    return user


def update_account(*, actor, user_id, full_name, username, password="", role=None, department_id=None):
    if user_id is None:
        raise LookupError("Сотрудник не найден")
    actor, user = _locked_accounts(actor, user_id)
    values = _fields(actor, user, full_name, username, role, department_id)
    if not isinstance(password, str):
        raise AccountError("Некорректный пароль", "password")
    if values["department_id"] != user.department_id:
        _require_no_active_assignments(user)
    before, snapshot = account_snapshot(user), actor_snapshot(actor)
    if before == dict(before, **values) and not password:
        return user
    for key, value in values.items():
        setattr(user, key, value)
    if password:
        user.set_password(password)
    db.session.flush()
    record_admin_event("account_updated", "user", user.id, before=before,
        after=dict(account_snapshot(user), password_changed=bool(password)), snapshot=snapshot, actor=actor)
    return user


def set_account_blocked(*, actor, user_id, blocked):
    if user_id is None:
        raise LookupError("Сотрудник не найден")
    actor, user = _locked_accounts(actor, user_id)
    if type(blocked) is not bool:
        raise AccountError("Некорректное действие с учётной записью")
    if not blocked and actor.role != "admin":
        raise PermissionError("Восстановление доступа доступно администратору")
    if user.role == "admin":
        if blocked: raise AccountError("Нельзя отключить или удалить единственного администратора")
        return user
    departments = _departments(actor, user)
    if not blocked and (user.role not in ("employee", "head") or user.department_id not in departments):
        raise AccountError("Перед разблокировкой назначьте пользователю отдел и роль")
    if user.is_blocked == blocked:
        return user
    if blocked:
        _require_no_active_assignments(user)
    before = account_snapshot(user)
    user.is_blocked = blocked
    record_admin_event("account_blocked" if blocked else "account_unblocked", "user", user.id,
        before=before, after=account_snapshot(user), actor=actor)
    db.session.flush()
    return user


def authentication_stamp(user):
    """Invalidate signed sessions after password reset or any disable/unblock cycle."""
    latest = db.session.execute(select(func.max(AdminAuditEvent.id)).where(AdminAuditEvent.object_type == "user",
        AdminAuditEvent.object_id == user.id, AdminAuditEvent.action == "account_blocked")).scalar() or 0
    secret = current_app.secret_key
    if isinstance(secret, str): secret = secret.encode("utf-8")
    return hmac.new(secret, f"account-auth:v1|{user.id}|{user.password_hash}|{latest}".encode(), sha256).hexdigest()


def session_is_current(user, stamp):
    if stamp is None:
        # Existing signed ID-only cookies remain usable until a security edit.
        # They cannot be converted into fresh sessions after disabling/resetting.
        return not db.session.execute(select(AdminAuditEvent.id).where(AdminAuditEvent.object_type == "user",
            AdminAuditEvent.object_id == user.id, AdminAuditEvent.action.in_(("account_blocked", "account_updated"))).limit(1)).first()
    return isinstance(stamp, str) and len(stamp)==64 and stamp.isascii() and hmac.compare_digest(stamp, authentication_stamp(user))
