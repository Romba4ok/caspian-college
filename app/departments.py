"""Department lifecycle. Callers commit changes and audit together."""
from uuid import uuid4

from sqlalchemy import delete, func, or_, select, union_all

from .audit import record_admin_event
from .extensions import db
from .models import Department, Task, User


class DepartmentError(ValueError):
    """A department change conflicts with the department lifecycle rules."""


def _lock_admin(actor):
    if actor is None or not getattr(actor, "is_active", False):
        raise PermissionError("Требуется действующий администратор")
    admin = db.session.execute(select(User).where(User.id == actor.id).with_for_update()
        .execution_options(populate_existing=True)).scalar_one_or_none()
    if admin is None or not admin.is_active or admin.role != "admin":
        raise PermissionError("Управление отделами доступно только администратору")
    return admin


def _lock_department(department_id):
    department = db.session.execute(select(Department).where(Department.id == department_id)
        .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if department is None:
        raise LookupError("Отдел не найден")
    return department


def _name(value, department_id=None):
    name = (value or "").strip()
    if not name or len(name) > 100:
        raise DepartmentError("Название отдела должно содержать от 1 до 100 символов")
    normalized = name.casefold()
    query = select(Department.id).where(Department.normalized_name == normalized)
    if department_id is not None:
        query = query.where(Department.id != department_id)
    if db.session.execute(query).first() is not None:
        raise DepartmentError("Отдел с таким названием уже существует")
    return name, normalized


def _snapshot(department):
    return {"name": department.name, "slug": department.slug, "is_archived": department.is_archived}


def create_department(*, actor, name):
    admin = _lock_admin(actor)
    display, normalized = _name(name)
    department = Department(name=display, normalized_name=normalized, slug="dept-" + uuid4().hex)
    db.session.add(department)
    db.session.flush()
    record_admin_event("department_created", "department", department.id, after=_snapshot(department), actor=admin)
    return department


def rename_department(*, actor, department_id, name):
    admin = _lock_admin(actor)
    department = _lock_department(department_id)
    display, normalized = _name(name, department.id)
    if department.name != display:
        before = _snapshot(department)
        department.name, department.normalized_name = display, normalized
        record_admin_event("department_renamed", "department", department.id,
                           before=before, after=_snapshot(department), actor=admin)
    return department


def _set_archived(*, actor, department_id, archived):
    admin = _lock_admin(actor)
    department = _lock_department(department_id)
    if department.is_archived != archived:
        before = _snapshot(department)
        department.is_archived = archived
        record_admin_event("department_archived" if archived else "department_restored", "department", department.id,
                           before=before, after=_snapshot(department), actor=admin)
    return department


def archive_department(*, actor, department_id):
    return _set_archived(actor=actor, department_id=department_id, archived=True)


def restore_department(*, actor, department_id):
    return _set_archived(actor=actor, department_id=department_id, archived=False)


def _reference_queries(department_id):
    return (select(User.id).where(User.department_id == department_id).exists(),
            select(Task.id).where(or_(Task.sender_department_id == department_id,
                                      Task.recipient_department_id == department_id)).exists())


def _has_department_links(department_id):
    return db.session.execute(select(or_(*_reference_queries(department_id)))).scalar_one()


def delete_department(*, actor, department_id):
    admin = _lock_admin(actor)
    department = _lock_department(department_id)
    if _has_department_links(department.id):
        raise DepartmentError("Нельзя удалить отдел с пользователями или задачами. Используйте архивирование.")
    before = _snapshot(department)
    # Do not use ORM deletion: Department.members and the legacy SET NULL FK
    # must never silently detach staff. Recheck links in the DELETE itself.
    user_links, task_links = _reference_queries(department.id)
    result = db.session.execute(delete(Department).where(Department.id == department.id,
        ~user_links, ~task_links).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise DepartmentError("В отделе появились связанные данные. Используйте архивирование.")
    record_admin_event("department_deleted", "department", department.id, before=before, actor=admin)


def department_counts():
    members = dict(db.session.execute(select(User.department_id, func.count(User.id))
        .where(User.department_id.is_not(None)).group_by(User.department_id)).all())
    directions = union_all(select(Task.sender_department_id.label("department_id")),
                           select(Task.recipient_department_id.label("department_id"))).subquery()
    tasks = dict(db.session.execute(select(directions.c.department_id, func.count())
        .group_by(directions.c.department_id)).all())
    return members, tasks
