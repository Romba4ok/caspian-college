"""Build revision-04 evidence using SQL, never the current ORM models."""
from datetime import datetime, timezone
import sqlalchemy as sa

STAMP = datetime(2026, 1, 10, 9, 0, tzinfo=timezone.utc)
DUE = datetime(2026, 1, 20, 12, 0, tzinfo=timezone.utc)
OLD_STATUSES = ("new", "accepted", "in_progress", "awaiting_clarification", "ready", "completed", "cancelled")


def fill_legacy(bind):
    m = sa.MetaData()
    names = ("users", "departments", "groups", "group_students", "tasks", "task_comments", "task_attachments",
             "task_events", "task_notifications", "task_translations")
    t = {n: sa.Table(n, m, autoload_with=bind) for n in names}
    depts = bind.execute(sa.select(t["departments"].c.id).order_by(t["departments"].c.id)).scalars().all()
    ids = {}
    for username, role, subrole, dept, blocked in (
        ("author", "teacher", "employee", depts[0], False),
        ("boss", "teacher", "head", depts[1], False),
        ("blocked", "teacher", "head", depts[1], True),
        ("student", "student", None, None, False),
        ("unclear", "teacher", None, depts[0], False),
    ):
        ids[username] = bind.execute(t["users"].insert().values(full_name="Имя " + username, username=username,
            password_hash="preserved-" + username, role=role, language="kk", department_role=subrole,
            department_id=dept, is_blocked=blocked)).inserted_primary_key[0]
    gid = bind.execute(t["groups"].insert().values(title="Историческая группа", teacher_id=ids["author"])).inserted_primary_key[0]
    bind.execute(t["group_students"].insert().values(group_id=gid, student_id=ids["student"]))
    for i, status in enumerate(OLD_STATUSES, 1):
        bind.execute(t["tasks"].insert().values(id=i, title="Задача " + status, description="Текст Қазақша English",
            author_id=ids["author"], sender_department_id=depts[0], recipient_department_id=depts[1],
            assignee_id=None if status == "new" else ids["boss"], due_at=None if i % 2 else DUE,
            status=status, category=" Оборудование " if i % 2 else "ОБОРУДОВАНИЕ", created_at=STAMP))
        if i != 4:
            bind.execute(t["task_events"].insert().values(task_id=i, actor_id=ids["author"], event_type="created",
                details={"original": i}, created_at=STAMP))
        if status == "completed":
            bind.execute(t["task_events"].insert().values(task_id=i, actor_id=ids["author"], event_type="completed",
                details={"original": "confirmation"}, created_at=DUE))
    comment_id = bind.execute(t["task_comments"].insert().values(task_id=1, author_id=ids["author"], body="Старый комментарий")).inserted_primary_key[0]
    bind.execute(t["task_attachments"].insert().values(task_id=1, uploaded_by_id=ids["author"],
        original_filename="Материал.pdf", storage_key="preserved-private-key", content_type="application/pdf", size_bytes=123))
    bind.execute(t["task_notifications"].insert().values(task_id=1, user_id=ids["author"], event_type="task_created",
        message="Старое уведомление", deduplication_key="old-dedupe"))
    bind.execute(t["task_translations"].insert().values(source_type="task", source_id=1, source_hash="original-hash",
        target_language="en", translated_text="Preserved historical translation"))
    return {"users": ids, "departments": depts, "group": gid, "comment": comment_id}
