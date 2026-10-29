"""Preserved category directory and fixed policy for all new uploads."""
from dataclasses import dataclass

from sqlalchemy import select

from .audit import record_admin_event
from .extensions import db
from .models import SystemSettings, TaskCategory, User

SUPPORTED_EXTENSIONS = ("pdf", "docx", "xlsx", "pptx", "jpg", "png")


class SettingsError(ValueError):
    """Invalid directory entry or upload policy."""
    def __init__(self, message, field=None):
        super().__init__(message)
        self.field = field


@dataclass(frozen=True)
class UploadPolicy:
    max_upload_mb: int = 20
    allowed_extensions: tuple[str, ...] = SUPPORTED_EXTENSIONS

    @property
    def max_file_bytes(self):
        return self.max_upload_mb * 1024 * 1024

    def as_dict(self):
        return {"max_upload_mb": self.max_upload_mb, "allowed_extensions": list(self.allowed_extensions)}


def validate_upload_policy(max_upload_mb, allowed_extensions):
    if (isinstance(max_upload_mb, bool) or not (isinstance(max_upload_mb, int) or
            isinstance(max_upload_mb, str) and max_upload_mb.strip().isascii() and max_upload_mb.strip().isdigit())):
        raise SettingsError("Размер файла должен быть целым числом от 1 до 20 МБ", "max_upload_mb")
    try:
        limit = int(max_upload_mb)
    except ValueError:
        raise SettingsError("Размер файла должен быть целым числом от 1 до 20 МБ", "max_upload_mb") from None
    if not 1 <= limit <= 20:
        raise SettingsError("Размер файла должен быть целым числом от 1 до 20 МБ", "max_upload_mb")
    if not isinstance(allowed_extensions, (list, tuple)) or any(not isinstance(ext, str) for ext in allowed_extensions):
        raise SettingsError("Выберите форматы из поддерживаемого списка", "allowed_extensions")
    extensions = {ext.strip().lower() for ext in allowed_extensions}
    if not extensions.issubset(SUPPORTED_EXTENSIONS):
        raise SettingsError("Допустимы только PDF, DOCX, XLSX, PPTX, JPG и PNG", "allowed_extensions")
    return UploadPolicy(limit, tuple(ext for ext in SUPPORTED_EXTENSIONS if ext in extensions))


def get_upload_policy():
    """R32: saved legacy settings never change the effective 20 MB / six formats."""
    return UploadPolicy()


def _lock_admin(actor):
    if actor is None or not getattr(actor, "is_active", False):
        raise PermissionError("Требуется действующий администратор")
    admin = db.session.execute(select(User).where(User.id == actor.id).with_for_update()
        .execution_options(populate_existing=True)).scalar_one_or_none()
    if admin is None or not admin.is_active or admin.role != "admin":
        raise PermissionError("Настройки доступны только администратору")
    return admin


def update_upload_policy(*, actor, max_upload_mb, allowed_extensions):
    admin = _lock_admin(actor)
    policy = validate_upload_policy(max_upload_mb, allowed_extensions)
    row = db.session.get(SystemSettings, 1, with_for_update=True, populate_existing=True)
    before = {"max_upload_mb": row.max_upload_mb, "allowed_extensions": row.allowed_extensions} if row else UploadPolicy().as_dict()
    if before == policy.as_dict():
        return policy
    if row is None:
        row = SystemSettings(id=1)
        db.session.add(row)
    row.max_upload_mb = policy.max_upload_mb
    row.allowed_extensions = list(policy.allowed_extensions)
    record_admin_event("upload_policy_updated", "system_settings", 1,
                       before=before, after=policy.as_dict(), actor=admin)
    return policy


def active_categories():
    return TaskCategory.query.filter_by(is_archived=False).order_by(TaskCategory.name, TaskCategory.id).all()


def _category_name(value, category_id=None):
    name = (value or "").strip()
    if not name or len(name) > 80:
        raise SettingsError("Название категории должно содержать от 1 до 80 символов")
    normalized = name.casefold()
    query = select(TaskCategory.id).where(TaskCategory.normalized_name == normalized)
    if category_id is not None:
        query = query.where(TaskCategory.id != category_id)
    if db.session.execute(query).first() is not None:
        raise SettingsError("Категория с таким названием уже существует, включая архив")
    return name, normalized


def _lock_category(category_id):
    category = db.session.execute(select(TaskCategory).where(TaskCategory.id == category_id).with_for_update()
        .execution_options(populate_existing=True)).scalar_one_or_none()
    if category is None:
        raise LookupError("Категория не найдена")
    return category


def _snapshot(category):
    return {"name": category.name, "is_archived": category.is_archived}


def create_category(*, actor, name):
    admin = _lock_admin(actor)
    display, normalized = _category_name(name)
    category = TaskCategory(name=display, normalized_name=normalized)
    db.session.add(category)
    db.session.flush()
    record_admin_event("category_created", "category", category.id, after=_snapshot(category), actor=admin)
    return category


def rename_category(*, actor, category_id, name):
    admin = _lock_admin(actor)
    category = _lock_category(category_id)
    display, normalized = _category_name(name, category.id)
    if display != category.name:
        before = _snapshot(category)
        category.name, category.normalized_name = display, normalized
        record_admin_event("category_renamed", "category", category.id,
                           before=before, after=_snapshot(category), actor=admin)
    return category


def archive_category(*, actor, category_id):
    admin = _lock_admin(actor)
    category = _lock_category(category_id)
    if not category.is_archived:
        before = _snapshot(category)
        category.is_archived = True
        record_admin_event("category_archived", "category", category.id,
                           before=before, after=_snapshot(category), actor=admin)
    return category
