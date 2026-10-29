"""Department workflow models, preserving historical teaching records."""

from uuid import uuid4

from flask_login import UserMixin
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint, text
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db

group_students = db.Table(
    "group_students",
    db.Column("group_id", db.Integer, db.ForeignKey("groups.id"), primary_key=True),
    db.Column("student_id", db.Integer, db.ForeignKey("users.id"), primary_key=True),
)


class User(UserMixin, db.Model):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'head', 'employee')", name="ck_users_role"),
        CheckConstraint("role <> 'admin' OR (NOT is_blocked AND department_id IS NULL)", name="ck_users_admin_active"),
        CheckConstraint("role = 'admin' OR is_blocked OR department_id IS NOT NULL", name="ck_users_department"),
        Index("uq_users_single_admin", "role", unique=True,
              sqlite_where=text("role = 'admin'"), postgresql_where=text("role = 'admin'")),
    )

    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(160), nullable=False)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(24), nullable=False, default="employee")
    language = db.Column(db.String(8), nullable=False, default="ru", server_default="ru")
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id", ondelete="SET NULL"), nullable=True)
    department_role = db.Column(db.String(24), nullable=True)
    is_blocked = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())

    taught_groups = db.relationship("Group", back_populates="teacher", cascade="all, delete-orphan")
    student_groups = db.relationship("Group", secondary=group_students, back_populates="students")
    department = db.relationship("Department", back_populates="members")

    @property
    def is_active(self):
        return not self.is_blocked

    @property
    def is_authenticated(self):
        return self.is_active

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def __getitem__(self, key):
        """Keep the mapping-style template contract used by the original portal."""
        return getattr(self, key)


class Group(db.Model):
    __tablename__ = "groups"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(160), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    teacher = db.relationship("User", back_populates="taught_groups")
    students = db.relationship("User", secondary=group_students, back_populates="student_groups")

    def __getitem__(self, key):
        return getattr(self, key)


class Department(db.Model):
    __tablename__ = "departments"
    __table_args__ = (CheckConstraint("length(trim(normalized_name)) > 0", name="ck_departments_name"),)

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(40), unique=True, nullable=False)
    name = db.Column(db.String(100), unique=True, nullable=False)
    normalized_name = db.deferred(db.Column(db.String(300), unique=True, nullable=False,
                                 default=lambda ctx: ctx.get_current_parameters()["name"].strip().casefold()))
    is_archived = db.deferred(db.Column(db.Boolean, nullable=False, default=False, server_default=db.false()))

    members = db.relationship("User", back_populates="department")


TASK_STATUSES = ("new", "in_progress", "completed", "cancelled", "rejected")
TASK_PRIORITIES = ("low", "normal", "high", "urgent")


class Task(db.Model):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("priority IN ('low', 'normal', 'high', 'urgent')", name="ck_tasks_priority"),
        CheckConstraint(
            "status IN ('new', 'in_progress', 'completed', 'cancelled', 'rejected')",
            name="ck_tasks_status",
        ),
        CheckConstraint("sender_department_id <> recipient_department_id", name="ck_tasks_distinct_departments"),
        CheckConstraint("due_at IS NOT NULL", name="ck_tasks_due_required"),
        CheckConstraint("rework_cycle >= 0", name="ck_tasks_rework_cycle"),
        CheckConstraint("length(trim(creation_key)) >= 32 AND length(creation_key) <= 64 "
                        "AND length(trim(creation_key)) = length(creation_key)", name="ck_tasks_creation_key"),
        CheckConstraint("assignee_id IS NULL", name="ck_tasks_legacy_assignee_unused"),
        UniqueConstraint("creation_key", name="uq_tasks_creation_key"),
        CheckConstraint("revision >= 0", name="ck_tasks_revision"),
    )

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False)
    author_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    sender_department_id = db.Column(
        db.Integer, db.ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    recipient_department_id = db.Column(
        db.Integer, db.ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Only the request-free old-schema scheduler reads this deferred column.
    # Schema 09 requires NULL; current assignments have exactly one source below.
    legacy_assignee_id = db.deferred(db.Column("assignee_id", db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True))
    due_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    priority = db.Column(db.String(16), nullable=False, default="normal", server_default="normal")
    category = db.Column(db.String(80), nullable=True)
    contact_person = db.Column(db.String(160), nullable=True)
    status = db.Column(db.String(32), nullable=False, default="new", server_default="new", index=True)
    # Keep additions deferred so the existing reminder reader can operate on the
    # old working schema until its separately approved upgrade. No read fallback
    # invents values; new services explicitly access these after migration.
    sent_at = db.deferred(db.Column(db.DateTime(timezone=True), nullable=False))
    # Deferred so historical revision-07 readers do not select revision-08 fields.
    # This is read compatibility only, not permission to write the old workflow.
    creation_key = db.deferred(db.Column(db.String(64), nullable=False, default=lambda: uuid4().hex))
    is_rework = db.deferred(db.Column(db.Boolean, nullable=False, default=False, server_default=db.false()))
    rework_cycle = db.deferred(db.Column(db.Integer, nullable=False, default=0, server_default="0"))
    completed_at = db.deferred(db.Column(db.DateTime(timezone=True), nullable=True))
    closed_at = db.deferred(db.Column(db.DateTime(timezone=True), nullable=True))
    revision = db.deferred(db.Column(db.Integer, nullable=False, default=0, server_default="0"))
    legacy_missing_due = db.deferred(db.Column(db.Boolean, nullable=False, default=False, server_default=db.false()))
    is_legacy_dispatched = db.deferred(db.Column(db.Boolean, nullable=False, default=False, server_default=db.false()))
    category_id = db.deferred(db.Column(db.Integer, db.ForeignKey("task_categories.id", ondelete="RESTRICT"), nullable=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.func.now(), onupdate=db.func.now())

    author = db.relationship("User", foreign_keys=[author_id], backref="authored_tasks")
    sender_department = db.relationship("Department", foreign_keys=[sender_department_id], backref="sent_tasks")
    recipient_department = db.relationship("Department", foreign_keys=[recipient_department_id], backref="received_tasks")
    assignments = db.relationship("TaskAssignee", back_populates="task", cascade="all, delete-orphan",
        order_by="TaskAssignee.user_id")

    @property
    def assignee_ids(self):
        return sorted(member.user_id if member.user_id is not None else member.user.id for member in self.assignments)

    @assignee_ids.setter
    def assignee_ids(self, ids):
        previous = {member.user_id: member for member in self.assignments}
        self.assignments = [previous.get(ident) or TaskAssignee(user_id=ident) for ident in sorted(ids)]

    @property
    def assignees(self):
        return [member.user for member in self.assignments]

    # Internal single-member construction/read compatibility for existing callers.
    # Never selects a first member of a team and never reads the old SQL column.
    @property
    def assignee(self):
        if len(self.assignments) > 1:
            raise ValueError("Use assignees for a task with multiple participants")
        return self.assignments[0].user if self.assignments else None

    @assignee.setter
    def assignee(self, user):
        self.assignments = [] if user is None else [TaskAssignee(user=user)]

    @property
    def assignee_id(self):
        if len(self.assignments) > 1:
            raise ValueError("Use assignee_ids for a task with multiple participants")
        return self.assignee_ids[0] if self.assignments else None

    @assignee_id.setter
    def assignee_id(self, ident):
        self.assignee_ids = [] if ident is None else [ident]
    comments = db.relationship("TaskComment", back_populates="task", cascade="all, delete-orphan", order_by="(TaskComment.created_at, TaskComment.id)")
    attachments = db.relationship("TaskAttachment", back_populates="task", cascade="all, delete-orphan")
    events = db.relationship("TaskEvent", back_populates="task", cascade="all, delete-orphan", order_by="(TaskEvent.created_at, TaskEvent.id)")
    notifications = db.relationship("TaskNotification", back_populates="task", cascade="all, delete-orphan")


class TaskAssignee(db.Model):
    __tablename__ = "task_assignees"
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True, index=True)
    task = db.relationship("Task", back_populates="assignments")
    user = db.relationship("User")


class TaskComment(db.Model):
    __tablename__ = "task_comments"
    __table_args__ = (UniqueConstraint("id", "task_id", name="uq_task_comments_id_task"),)

    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    author_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.func.now())

    task = db.relationship("Task", back_populates="comments")
    author = db.relationship("User", backref="task_comments")


class TaskAttachment(db.Model):
    __tablename__ = "task_attachments"
    __table_args__ = (
        ForeignKeyConstraint(["comment_id", "task_id"], ["task_comments.id", "task_comments.task_id"],
                             name="fk_task_attachments_comment_task", ondelete="RESTRICT"),
    )

    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    original_filename = db.Column(db.String(255), nullable=False)
    storage_key = db.Column(db.String(255), unique=True, nullable=False)
    content_type = db.Column(db.String(120), nullable=False)
    size_bytes = db.Column(db.BigInteger, nullable=False)
    comment_id = db.deferred(db.Column(db.Integer, nullable=True, index=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.func.now())

    task = db.relationship("Task", back_populates="attachments", foreign_keys=[task_id])
    comment = db.relationship("TaskComment", foreign_keys=[comment_id, task_id], viewonly=True)
    uploaded_by = db.relationship("User", backref="task_attachments")


class TaskEvent(db.Model):
    __tablename__ = "task_events"
    __table_args__ = (
        ForeignKeyConstraint(["comment_id", "task_id"], ["task_comments.id", "task_comments.task_id"],
                             name="fk_task_events_comment_task", ondelete="RESTRICT"),
    )

    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    event_type = db.Column(db.String(48), nullable=False, index=True)
    details = db.Column(db.JSON, nullable=False, default=dict)
    actor_snapshot = db.deferred(db.Column(db.JSON, nullable=True))
    comment_id = db.deferred(db.Column(db.Integer, nullable=True, index=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.func.now())

    task = db.relationship("Task", back_populates="events", foreign_keys=[task_id])
    comment = db.relationship("TaskComment", foreign_keys=[comment_id, task_id], viewonly=True)
    actor = db.relationship("User", backref="task_events")


class TaskNotification(db.Model):
    __tablename__ = "task_notifications"

    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = db.Column(db.String(48), nullable=False)
    message = db.Column(db.String(500), nullable=False)
    deduplication_key = db.Column(db.String(64), nullable=True, unique=True, index=True)
    is_read = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.func.now())

    task = db.relationship("Task", back_populates="notifications")
    user = db.relationship("User", backref="task_notifications")


class TaskTranslation(db.Model):
    __tablename__ = "task_translations"
    __table_args__ = (
        UniqueConstraint("source_type", "source_id", "source_hash", "target_language", name="uq_task_translation_version"),
    )

    id = db.Column(db.Integer, primary_key=True)
    source_type = db.Column(db.String(24), nullable=False)
    source_id = db.Column(db.Integer, nullable=False)
    source_hash = db.Column(db.String(64), nullable=False)
    target_language = db.Column(db.String(8), nullable=False)
    translated_text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.func.now())


class TaskCategory(db.Model):
    __tablename__ = "task_categories"
    __table_args__ = (CheckConstraint("length(trim(normalized_name)) > 0", name="ck_task_categories_name"),)
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    normalized_name = db.Column(db.String(240), unique=True, nullable=False,
                               default=lambda ctx: ctx.get_current_parameters()["name"].strip().casefold())
    is_archived = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())


class SystemSettings(db.Model):
    __tablename__ = "system_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_system_settings_singleton"),
        CheckConstraint("max_upload_mb BETWEEN 1 AND 20", name="ck_system_settings_upload_limit"),
    )
    id = db.Column(db.Integer, primary_key=True, default=1)
    max_upload_mb = db.Column(db.Integer, nullable=False, default=20, server_default="20")
    allowed_extensions = db.Column(db.JSON, nullable=False,
                                  default=lambda: ["pdf", "docx", "xlsx", "pptx", "jpg", "png"])


class AdminAuditEvent(db.Model):
    __tablename__ = "admin_audit_events"
    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    actor_snapshot = db.Column(db.JSON, nullable=False)
    action = db.Column(db.String(80), nullable=False)
    object_type = db.Column(db.String(40), nullable=False)
    object_id = db.Column(db.Integer, nullable=True)
    details = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.func.now(), index=True)


# create_all is only used by synthetic fixtures; production uses migration 09.
from sqlalchemy import event as _schema_event
from .assignment_schema import install_guards as _install_assignment_guards
_schema_event.listen(TaskAssignee.__table__, "after_create",
    lambda table, connection, **kw: _install_assignment_guards(connection))
