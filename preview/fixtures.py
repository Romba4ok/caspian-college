"""Synthetic presentation fixtures, not a second authorization implementation."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import re
from types import SimpleNamespace
from zoneinfo import ZoneInfo


DEMO_ZONE = ZoneInfo("Asia/Qyzylorda")
DEMO_NOW = datetime(2026, 10, 28, 11, 0, tzinfo=DEMO_ZONE)
ROLE_LABELS = {
    "employee": "Сотрудник отдела",
    "head": "Начальник отдела",
    "admin": "Администратор",
}


@dataclass(frozen=True)
class Identity:
    role: str
    name: str
    department: str | None


IDENTITIES = {
    "employee": Identity("employee", "Сотрудник (демо)", "Маркетинг (демо)"),
    "head": Identity("head", "Начальник (демо)", "Маркетинг (демо)"),
    "admin": Identity("admin", "Администратор (демо)", None),
}


@dataclass(frozen=True)
class Page:
    endpoint: str
    path: str
    title: str
    roles: tuple[str, ...]


ALL_ROLES = tuple(ROLE_LABELS)
ALL_PAGES = (
    Page("tasks.received_tasks", "/tasks/received", "Нашему отделу", ALL_ROLES),
    Page("tasks.sent_tasks", "/tasks/sent", "Другим отделам", ALL_ROLES),
    Page("tasks.completed_tasks", "/tasks/completed", "Завершённые", ALL_ROLES),
    Page("tasks.task_new", "/tasks/new", "Новая задача", ("employee", "head")),
    Page("tasks.notification_center", "/tasks/notifications", "Уведомления", ALL_ROLES),
    Page("staff.staff_list", "/staff", "Сотрудники", ("head",)),
    Page("staff.staff_new", "/staff/new", "Добавление сотрудника", ("head",)),
    Page("reports.own_department_report", "/reports/department", "Отчёт отдела", ("head",)),
    Page("admin.admin_users", "/admin/users", "Пользователи", ("admin",)),
    Page("admin.admin_user_new", "/admin/users/new", "Добавление пользователя", ("admin",)),
    Page("admin.admin_departments", "/admin/departments", "Отделы", ("admin",)),
    Page("admin.admin_department_new", "/admin/departments/new", "Создание отдела", ("admin",)),
    Page("admin.admin_audit", "/admin/audit", "Журнал действий", ("admin",)),
    Page("admin.admin_statistics", "/admin/statistics", "Общая отчётность", ("admin",)),
)

# Ready pages and remaining placeholders have separate route handlers.
# Unprepared PAGES retain the honest 503 contract.
PEOPLE_ENDPOINTS={"staff.staff_list","staff.staff_new","admin.admin_users","admin.admin_user_new"}
DEPARTMENT_ENDPOINTS={"admin.admin_departments","admin.admin_department_new"}
NOTIFICATION_ENDPOINTS={"tasks.notification_center"}
REPORT_ENDPOINTS={"reports.own_department_report","admin.admin_statistics"}
AUDIT_ENDPOINTS={"admin.admin_audit"}
TASK_PAGES=ALL_PAGES[:3]
READY_PAGES=tuple(page for page in ALL_PAGES if page in ALL_PAGES[:4] or page.endpoint in PEOPLE_ENDPOINTS|DEPARTMENT_ENDPOINTS|NOTIFICATION_ENDPOINTS|REPORT_ENDPOINTS|AUDIT_ENDPOINTS)
PAGES=tuple(page for page in ALL_PAGES if page not in READY_PAGES)
TASK_ENDPOINTS = {"received": "tasks.received_tasks", "sent": "tasks.sent_tasks",
                  "completed": "tasks.completed_tasks"}
TASK_LABELS = {"received": "Нашему отделу", "sent": "Другим отделам", "completed": "Завершённые"}
STATUS_LABELS = {"new": "Новая", "in_progress": "В работе", "completed": "Выполнено",
                 "cancelled": "Отменена", "rejected": "Отклонена"}
PRIORITY_LABELS = {"urgent": "Срочный", "high": "Высокий", "normal": "Обычный", "low": "Низкий"}
SCENARIO_SIZES = (0, 1, 6, 36, 300)


def _tasks(size, side, *, closed=False, mixed=False):
    """Deterministic synthetic records; never consult the application or storage."""
    marketing = SimpleNamespace(id=1, name="Маркетинг (демо)", is_archived=False)
    practice = SimpleNamespace(id=2, name="Практика (демо)", is_archived=False)
    long_department = SimpleNamespace(id=3, name="Отдел координации производственной практики и взаимодействия с партнёрами (демо)", is_archived=False)
    titles = ("Подготовить зал к встрече", "Собрать заявки на практику",
              "Собрать материалы для отчёта", "Подготовить рекомендации",
              "Организовать встречу", "Подготовить план консультаций")
    result = []
    for index in range(size):
        current_side = ("received" if index % 2 == 0 else "sent") if mixed else side
        counterpart = long_department if index % 11 == 10 else practice
        sender, recipient = (counterpart, marketing) if current_side == "received" else (marketing, counterpart)
        status = ("completed", "cancelled", "rejected")[index % 3] if closed else ("new", "in_progress")[index % 2]
        base_id = (3000 if closed else 1000) + (1000 if side == "sent" else 0)
        assignee = None if status == "new" else SimpleNamespace(id=20, full_name="Сотрудник (демо)", is_active=True)
        rework = index % 6 == 3
        if index % 12 == 5 and not closed:
            assignee = SimpleNamespace(id=21, full_name="Прежний сотрудник (отключён, демо)", is_active=False)
            rework = True
        if assignee:
            assignee.role = 'employee'
            assignee.department_id = recipient.id
        due = None if index % 13 == 12 else DEMO_NOW + timedelta(hours=(-26 + index * 12) if not closed else -48 + index % 5 * 8)
        finished = None if index % 17 == 16 else DEMO_NOW - timedelta(hours=index * 3 + (0 if side == "received" else 1))
        title = titles[index % len(titles)]
        if index % 11 == 10:
            title = "Подготовить и согласовать организационные материалы для совместной встречи представителей колледжа и предприятий — партнёров по производственной практике"
        result.append(SimpleNamespace(
            id=base_id + index, title=title, status=status, priority=tuple(PRIORITY_LABELS)[index % 4],
            description="Организовать работу отдела и подготовить необходимые материалы. Синтетический пример для проверки интерфейса.",
            revision=1, sender_department=sender, recipient_department=recipient,
            author=SimpleNamespace(full_name="Автор поручения (демо)"), assignee=assignee,
            assignee_id=assignee.id if assignee else None, assignees=[assignee] if assignee else [], assignee_ids=[assignee.id] if assignee else [], recipient_department_id=recipient.id, sender_department_id=sender.id, is_rework=rework and not closed, rework_cycle=1 if rework else 0,
            created_at=DEMO_NOW - timedelta(days=60 + index), due_at=due,
            completed_at=finished if status == "completed" and index % 19 != 18 else None,
            closed_at=finished if closed else None,
            comments=tuple(range(index % 5)), attachments=tuple(range(index % 3))))
    return result


def closure_date(task):
    return task.completed_at if task.status == "completed" else task.closed_at


def ordered_tasks(tasks, *, closed=False):
    """Preview mirrors the documented server order before pagination/grouping."""
    def key(task):
        tie = (-task.created_at.timestamp(), -task.id)
        if closed:
            date = closure_date(task)
            return (date is None, -date.timestamp() if date else 0, *tie)
        return (tuple(PRIORITY_LABELS).index(task.priority), task.due_at is None,
                task.due_at.timestamp() if task.due_at else 0, *tie)
    return sorted(tasks, key=key)


def deadline_state(task):
    if task.status in ("completed", "cancelled", "rejected"):
        late = task.status == "completed" and task.completed_at and task.due_at and task.completed_at > task.due_at
        return {"overdue": False, "text": "Завершена позже срока" if late else "Задача закрыта"}
    if task.due_at is None:
        return {"overdue": False, "text": "Срок неизвестен — историческая задача"}
    seconds = (task.due_at - DEMO_NOW).total_seconds()
    minutes = int(abs(seconds) // 60)
    days, hours = minutes // 1440, minutes // 60 % 24
    duration = f"{days} д. {hours} ч." if days else (f"{hours} ч. {minutes % 60} мин." if hours else (f"{minutes} мин." if minutes else "менее минуты"))
    return {"overdue": seconds < 0, "text": ("Просрочена на " if seconds < 0 else "Осталось ") + duration if seconds else "Срок наступил"}


def task_context(role, direction, *, size=6, view="board", scope="received", page=1, actions=False, overrides=None):
    """Presentation adapter, not production visibility or action authorization."""
    is_admin = role == "admin"
    active_received = _tasks(size, "received", mixed=is_admin)
    active_sent = active_received if is_admin else _tasks(size, "sent")
    closed_received = _tasks(size, "received", closed=True, mixed=is_admin)
    closed_sent = [] if is_admin else _tasks(size, "sent", closed=True)
    if actions:
        def project(records):
            for task in records:
                if role == "employee" and task.recipient_department.id == 1 and task.assignee and task.assignee.is_active:
                    task.assignee = SimpleNamespace(id=1,full_name="Сотрудник (демо)",is_active=True,role="employee",department_id=task.recipient_department_id)
                    task.assignees = [task.assignee]
                    task.assignee_ids = [1]
                    task.assignee_id = 1
                state = (overrides or {}).get(task.id,{})
                for name,value in state.items():
                    setattr(task,name,value)
            return records
        incoming = project(active_received+closed_received)
        outgoing = incoming if is_admin else project(active_sent+closed_sent)
        active_received = [task for task in incoming if task.status in ("new","in_progress")]
        closed_received = [task for task in incoming if task.status not in ("new","in_progress")]
        active_sent = [task for task in outgoing if task.status in ("new","in_progress")]
        closed_sent = [] if is_admin else [task for task in outgoing if task.status not in ("new","in_progress")]
    counts = {"received": len(active_received), "sent": len(active_sent),
              "completed": len(closed_received) + len(closed_sent)}
    completed_scope = scope if direction == "completed" and not is_admin else None
    selected = (closed_received if scope == "received" or is_admin else closed_sent) if direction == "completed" else (active_received if direction == "received" else active_sent)
    ordered = ordered_tasks(selected, closed=direction == "completed")
    total = len(ordered)
    pages = max(1, (total + 24) // 25) if view == "list" else 1
    page = min(max(1, page), pages) if view == "list" else 1
    visible = ordered[(page - 1) * 25:page * 25] if view == "list" else ordered
    statuses = ("completed", "cancelled", "rejected") if direction == "completed" else ("new", "in_progress")
    labels = {"new": "Новые", "in_progress": "В работе", "completed": "Выполнено", "cancelled": "Отменены", "rejected": "Отклонены"}
    groups = [SimpleNamespace(key=status, label=labels[status], statuses=[status],
                              tasks=[task for task in ordered if task.status == status],
                              count=sum(task.status == status for task in ordered)) for status in statuses]
    board_data = {task.id: dict(id=task.id, title=task.title, revision=task.revision, status=task.status,
                  actions=demo_actions(role,task) if actions and direction!="completed" else [], detail_url="", assignee_ids=task.assignee_ids, assignees=[dict(id=u.id,full_name=u.full_name,is_available=u.is_active) for u in task.assignees], group=task.status,
                  comment_count=len(task.comments)+len(getattr(task,"demo_comments",[])), attachment_count=len(task.attachments)+len(getattr(task,"demo_files",[])),
                  due_at=task_datetime(task.due_at, "%Y-%m-%dT%H:%M") if task.due_at else "", candidates=demo_candidates(task,role) if actions else [])
                  for task in visible}
    attention = {task.id: ("Нужно переназначить исполнителя" if role == "head" and direction == "received" and task.assignees and not any(u.is_active for u in task.assignees) else
                          "Назначьте исполнителя" if role == "head" and direction == "received" and task.status == "new" else None) for task in visible}
    return dict(tasks=visible, direction=direction, view=view, counts=counts, completed_scope=completed_scope,
                list_labels=TASK_LABELS, list_endpoints=TASK_ENDPOINTS, board_groups=groups, board_data=board_data,
                pagination=SimpleNamespace(total=total, page=page, pages=pages, has_prev=page > 1, has_next=page < pages,
                                           prev_num=page - 1, next_num=page + 1),
                deadline_states={task.id: deadline_state(task) for task in visible}, attention=attention,
                may_create=not is_admin, status_labels=STATUS_LABELS, priority_labels=PRIORITY_LABELS,
                upload_policy=creation_context("")["upload_policy"] if actions else None)


DEMO_ACTIONS = {
    "assign":("Назначить исполнителей","in_progress"),"claim":("Взять задачу","in_progress"),
    "reassign":("Изменить состав исполнителей","in_progress"),"release":("Снять задачу с себя","new"),
    "complete":("Выполнено","completed"),"return":("Вернуть на доработку","in_progress"),
    "cancel":("Отменить задачу","cancelled"),"reject":("Отклонить задачу","rejected"),
    "deadline":("Изменить срок",None),
}


def demo_actions(role,task):
    """Small synthetic scenario profiles. Never used for production authorization."""
    if role=="admin":
        return []
    outgoing=task.sender_department.id==1
    if task.status in ("cancelled","rejected"):
        keys=[]
    elif task.status=="completed":
        keys=["return"] if outgoing else []
    elif outgoing:
        keys=["cancel","deadline"] if role=="head" else ["cancel"]
    elif task.status=="new":
        keys=["assign","claim","reject"] if role=="head" else ["claim"]
    elif role=="head":
        keys=["reassign","reject"]
        if any(u.is_active for u in task.assignees):
            keys.insert(1,"complete")
        if 2 in task.assignee_ids:
            keys.insert(0,"release")
    else:
        keys=["release","complete","reject"] if 1 in task.assignee_ids else ["claim"]
    return [dict(key=key,label=("Присоединиться" if key=="claim" and task.status=="in_progress" else DEMO_ACTIONS[key][0]),target=("in_progress" if key=="release" and len(task.assignee_ids)>1 else DEMO_ACTIONS[key][1]) or task.status,
                 assignee=key in ("assign","reassign"),deadline=key=="deadline",
                 terminal=key in ("complete","cancel","reject"),url="") for key in keys]


def demo_candidates(task,role):
    if not any(action["assignee"] for action in demo_actions(role,task)):
        return []
    return [dict(id=number,name=name,role=label) for number,name,label in
            ((20,"Сотрудник (демо)","Сотрудник"),(22,"Другой сотрудник (демо)","Сотрудник"),(2,"Начальник (демо)","Начальник"))
            ]


def creation_context(key, *, error=None, field=None, receipt=None):
    """Presentation-only form catalogue; no production workflow or persistence."""
    return dict(task=None, creation_key=key, form_error=error, error_field=field,
                departments=[SimpleNamespace(id=2,name="Практика (демо)"),
                             SimpleNamespace(id=3,name="Информационные технологии (демо)"),
                             SimpleNamespace(id=4,name="Учебный отдел (демо)")],
                categories=[SimpleNamespace(id=1,name="Документы"),SimpleNamespace(id=2,name="Мероприятия")],
                priority_labels=PRIORITY_LABELS,
                upload_policy=SimpleNamespace(allowed_extensions=("pdf","docx","xlsx","pptx","jpg","png"),max_upload_mb=20),
                creation_receipt=receipt)


def identity_for(role: str) -> Identity:
    """Select a demonstration label; this grants no production identity."""
    return IDENTITIES[role]


def preview_pdf(attachment_id):
    """Small valid, immutable PDF for the isolated download fixture."""
    stream = f"BT /F1 14 Tf 40 760 Td (Caspian College - SYNTHETIC PREVIEW - file {attachment_id}) Tj ET".encode("ascii")
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"]
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, item in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode() + item + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(data)


def detail_context(role, task_id, *, size=6, actions=False, overrides=None):
    """Fresh read-only projection. Viewing never updates a task or its history."""
    found = None
    for direction, scope in (("received", "received"), ("sent", "sent"),
                             ("completed", "received"), ("completed", "sent")):
        context = task_context(role, direction, size=size, scope=scope,actions=actions,overrides=overrides)
        found = next((task for task in context["tasks"] if task.id == task_id), None)
        if found:
            break
    if found is None:
        return None
    task = found
    comment_count, file_count = len(task.comments), len(task.attachments)
    task.sent_at = task.created_at + timedelta(minutes=15)
    task.category = "Организация работы"
    task.contact_person = "Координатор отдела (демо)"
    task.comments = [SimpleNamespace(id=task.id * 10 + i, author=SimpleNamespace(full_name=("Координатор (демо)" if i % 2 == 0 else "Сотрудник (демо)")),
                     created_at=task.sent_at + timedelta(hours=i + 1),
                     body=("Материалы подготовлены. Прикладываю документ к этому комментарию." if i == 0 else "Уточнили состав участников и организационные детали. Материалы доступны в задаче."))
                     for i in range(comment_count)]
    task.attachments = [SimpleNamespace(id=task.id * 10 + i + 1, original_filename=("План встречи.pdf" if i == 0 else "Организационные материалы.pdf"),
                        size_bytes=len(preview_pdf(task.id * 10 + i + 1)),
                        comment_id=task.comments[min(i, comment_count - 1)].id if comment_count else None)
                        for i in range(file_count)]
    comment_files = {comment.id: [file for file in task.attachments if file.comment_id == comment.id] for comment in task.comments}
    history = [SimpleNamespace(event=SimpleNamespace(created_at=task.sent_at), label="Задача отправлена", actor=task.author.full_name,
                               comment="Поручение передано исполняющему отделу.", filename=None,
                               changes=[("Статус", "Не создана", "Новая")])]
    if task.assignees:
        history.append(SimpleNamespace(event=SimpleNamespace(created_at=task.sent_at + timedelta(minutes=30)), label="Исполнитель назначен",
                       actor="Начальник отдела (демо)", comment="Назначен ответственный сотрудник.", filename=None,
                       changes=[("Исполнитель", "Не назначен", '; '.join(u.full_name for u in task.assignees)), ("Статус", "Новая", "В работе")]))
    if task.is_rework:
        history.append(SimpleNamespace(event=SimpleNamespace(created_at=DEMO_NOW - timedelta(hours=3)), label="Возвращена на доработку",
                       actor="Автор поручения (демо)", comment="Дополнить материалы по замечаниям отдела-заказчика.", filename=None,
                       changes=[("Статус", "Выполнено", "В работе")]))
    if task.status in ("completed", "cancelled", "rejected"):
        history.append(SimpleNamespace(event=SimpleNamespace(created_at=closure_date(task)), label=STATUS_LABELS[task.status],
                       actor="Участник отдела (демо)", comment="Результат и материалы сохранены в истории задачи.", filename=None,
                       changes=[("Статус", "В работе", STATUS_LABELS[task.status])]))
    task.comments.extend(getattr(task,"demo_comments",[]))
    task.attachments.extend(getattr(task,"demo_files",[]))
    for comment in getattr(task,"demo_comments",[]):
        comment_files[comment.id]=[file for file in task.attachments if file.comment_id==comment.id]
    history=list(getattr(task,"demo_base_history",history))
    history.extend(getattr(task,"demo_history",[]))
    allowed=demo_actions(role,task) if actions else []
    candidates=[SimpleNamespace(id=item["id"],full_name=item["name"],role="head" if item["id"]==2 else "employee")
                for item in demo_candidates(task,role)] if actions else []
    contribute=bool(actions and role!="admin" and task.status in ("new","in_progress"))
    return dict(task=task, status_labels=STATUS_LABELS, priority_labels=PRIORITY_LABELS, task_actions=allowed, action_urls={},
                deadline_state=deadline_state(task), attention_label=None, history_entries=history, comment_files=comment_files,
                upload_policy=creation_context("")["upload_policy"] if actions else None, back_endpoint=TASK_ENDPOINTS[direction], back_scope=scope if direction == "completed" and role != "admin" else None,
                action_error=None, failed_action=None, unavailable_assignee=task.status == "in_progress" and not any(u.is_active for u in task.assignees),
                may_edit=False, may_contribute=contribute, comment_url=None,upload_url=None,
                assignee_candidates=candidates,was_sent=True)


def presentation_user(role: str):
    """Match template attributes with synthetic values, without an auth session."""
    identity = identity_for(role)
    department = SimpleNamespace(id=1, name=identity.department, is_archived=False) if identity.department else None
    return SimpleNamespace(id=tuple(IDENTITIES).index(role) + 1, role=role,
                           full_name=identity.name, department=department,
                           department_id=department.id if department else None)


def task_datetime(value: datetime | None, pattern: str = "%d.%m.%Y %H:%M") -> str:
    if value is None:
        return "Не указано"
    if value.tzinfo is None:
        raise ValueError("Preview dates must have an explicit timezone")
    return value.astimezone(DEMO_ZONE).strftime(pattern)


def report_hours(value):
    return "Нет данных" if value is None else f"{value:.2f}".replace(".",",")+" ч"


def report_fixture(case=""):
    """Prepared display aggregates. No task queries, events or production math."""
    def metrics(counts,overdue=0,average=None,included=0,excluded=0):
        return dict(total=sum(counts),statuses=dict(zip(STATUS_LABELS,counts)),overdue=overdue,
            average_hours=average,included=included,excluded=excluded)
    incoming=metrics((3,4,5,2,1),2,18.5,4,1)
    outgoing=metrics((1,2,3,1,1),1,20.0,2,1)
    overall=metrics((4,6,8,3,2),3,19.0,6,2)
    global_overall=metrics((6,8,11,4,3),5,158/9,9,2)
    departments=[
        dict(id=1,name="Маркетинг (демо)",archived=case=="archived",incoming=incoming,outgoing=outgoing),
        dict(id=2,name="Практика (демо)",archived=False,incoming=metrics((2,2,3,1,1),2,28.0,3,0),outgoing=metrics((4,5,5,2,2),3,20.0,5,0)),
        dict(id=3,name="Учебный отдел (демо)",archived=True,incoming=metrics((1,2,3,1,1),1,0.0,2,1),outgoing=metrics((1,1,3,1,0),1,9.0,2,1)),
        dict(id=4,name="Отдел без задач (демо)",archived=False,incoming=metrics((0,0,0,0,0)),outgoing=metrics((0,0,0,0,0)))]
    if case in ("rework","recompleted"):
        incoming=metrics((3,5,4,2,1),2,20.0,3,1) if case=="rework" else metrics((3,4,5,2,1),2,21.0,4,1)
        overall=metrics((4,7,7,3,2),3,20.0,5,2) if case=="rework" else metrics((4,6,8,3,2),3,124/6,6,2)
        global_overall=metrics((6,9,10,4,3),5,18.0,8,2) if case=="rework" else metrics((6,8,11,4,3),5,168/9,9,2)
        departments[0]["incoming"]=incoming
        departments[1]["outgoing"]=metrics((4,6,4,2,2),3,21.5,4,0) if case=="rework" else metrics((4,5,5,2,2),3,22.0,5,0)
    all_metrics=[overall,global_overall]+[group for d in departments for group in (d["incoming"],d["outgoing"])]
    if case=="empty":
        for group in all_metrics:group.update(metrics((0,0,0,0,0)))
    elif case=="no-average":
        for group in all_metrics:group.update(average_hours=None,included=0,excluded=group["statuses"]["completed"])
    elif case=="zero-average":
        for group in all_metrics:
            if group["included"]:group["average_hours"]=0.0
    if case=="long":
        departments[0]["name"]="Отдел координации образовательных программ и взаимодействия с предприятиями — партнёрами колледжа"
        departments[1]["name"]="Отдел сопровождения совместных проектов колледжа и организаций-партнёров по производственной практике"
    generated=DEMO_NOW.astimezone(ZoneInfo("UTC"))
    own=dict(department={key:departments[0][key] for key in ("id","name","archived")},
        overall=overall,incoming=incoming,outgoing=outgoing,generated_at=generated)
    global_report=dict(overall=global_overall,departments=departments,generated_at=generated)
    return own,global_report


def notification_fixture(case=""):
    """Only own synthetic display messages, never real delivery or a task audit."""
    texts=[
        ("sent","Поручение отправлено: Подготовить зал к встрече",1000),
        ("completed","Задача выполнена: Подготовить рекомендации",3003),
        ("comment_added","Добавлен комментарий: Собрать материалы для отчёта",1002),
        ("deadline_changed","Изменён срок задачи: Подготовить рекомендации",1003),
        ("rejected","Задача отклонена получателем: Собрать материалы для отчёта. Причина: требуется участие другого отдела.",3002),
        ("returned_for_revision","Задача возвращена на доработку: Подготовить рекомендации",1003),
        ("reminder_overdue","Задача просрочена: Собрать заявки на практику",1001),
        ("comment_added","Сообщение о ранее доступной задаче без ссылки на материалы",None)]
    rows=[]
    for index in range(57 if case=="pages" else 8):
        event,message,task_id=texts[index%len(texts)]
        if case=="long" and index==0:
            event,task_id="rejected",3002
            message="Задача отклонена получателем: Собрать материалы для отчёта. Причина: "+("Требуется согласовать состав участников и расписание консультаций. "*8)
        rows.append(dict(id=100+index,event_type=event,message=message,is_read=index%(2 if case=="pages" else 3)!=0,
            task_id=task_id,created_at=DEMO_NOW-timedelta(hours=index)))
    return [] if case=="empty" else rows


def department_fixture(case=""):
    """Display rows and counts only; no users, tasks or audit records are stored."""
    rows=[
        dict(id=1,name="Маркетинг (демо)",is_archived=False,members=4,tasks=12),
        dict(id=2,name="Практика (демо)",is_archived=False,members=3,tasks=9),
        dict(id=3,name="Учебный отдел (демо)",is_archived=True,members=2,tasks=8),
        dict(id=4,name="Новый отдел (демо)",is_archived=False,members=0,tasks=0)]
    if case=="long":
        rows[1]["name"]="Отдел координации образовательных программ и взаимодействия с предприятиями — партнёрами колледжа"
    return [] if case=="empty" else rows


def people_fixture(case=""):
    """Presentation-only accounts; passwords and production identities are absent."""
    departments=[dict(id=1,name="Маркетинг (демо)",is_archived=case=="archived"),
        dict(id=2,name="Практика (демо)",is_archived=False),
        dict(id=3,name="Архивный отдел (демо)",is_archived=True)]
    rows=[dict(id=3,full_name="Администратор (демо)",username="admin.demo",role="admin",department_id=None,is_blocked=False,assignments=0),
        dict(id=2,full_name="Начальник (демо)",username="head.demo",role="head",department_id=1,is_blocked=False,assignments=0),
        dict(id=20,full_name="Алия Садыкова (демо)",username="aliya.demo",role="employee",department_id=1,is_blocked=False,assignments=2),
        dict(id=22,full_name="Данияр Ахметов (демо)",username="daniyar.demo",role="employee",department_id=1,is_blocked=False,assignments=0),
        dict(id=23,full_name="Мария Волкова (демо)",username="maria.demo",role="employee",department_id=1,is_blocked=True,assignments=0),
        dict(id=30,full_name="Сотрудник практики (демо)",username="practice.demo",role="employee",department_id=2,is_blocked=False,assignments=0),
        dict(id=31,full_name="Сотрудник архивного отдела (демо)",username="archive.demo",role="employee",department_id=3,is_blocked=True,assignments=0)]
    if case=="long":
        rows[3]["full_name"]="Александра-Мария Константинопольская — координатор совместных образовательных программ колледжа и предприятий-партнёров (демо)"
        rows[3]["username"]="long.demonstration.account.with.a.very.long.login.for.layout"
    if case=="empty":
        rows=[]
    return rows,departments


class AuditPreviewFilterError(ValueError):
    """Presentation harness errors; no production audit import or database."""
    def __init__(self, message, field):
        super().__init__(message)
        self.field = field


def audit_fixture(case="", *, user_id="", date_from="", date_to="", page="1"):
    """Synthetic, already formatted event DTOs; filter only this demo dataset."""
    def positive(value, field):
        if not re.fullmatch(r"[1-9][0-9]{0,9}", str(value), flags=re.ASCII) or int(value)>2147483647:
            raise AuditPreviewFilterError("Укажите положительный числовой идентификатор или номер страницы.",field)
        return int(value)
    selected=positive(user_id,"user_id") if user_id else None
    number=positive(page,"page")
    days=[]
    for value,field in ((date_from,"date_from"),(date_to,"date_to")):
        try:
            if value and not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}",value):raise ValueError
            days.append(date.fromisoformat(value) if value else None)
        except ValueError:
            raise AuditPreviewFilterError("Укажите существующую дату в формате ГГГГ-ММ-ДД.",field) from None
    if all(days) and days[0]>days[1]:
        raise AuditPreviewFilterError("Начало периода не может быть позже его окончания.","date_to")
    try:
        start=datetime.combine(days[0],datetime.min.time(),DEMO_ZONE) if days[0] else None
        end=datetime.combine(days[1]+timedelta(days=1),datetime.min.time(),DEMO_ZONE) if days[1] else None
    except (ValueError,OverflowError):
        raise AuditPreviewFilterError("Эта граница периода не поддерживается. Выберите другую дату.","date_to") from None
    def entry(source,ident,actor,name,role,department,action,obj,object_id,comment,changes,minute):
        return dict(source=source,id=ident,created_at=(DEMO_NOW-timedelta(minutes=minute)).astimezone(timezone.utc),
            actor_key=str(actor) if actor else None,actor_name=name,actor_role=role,actor_department=department,
            action=action,object_label=obj,object_id=object_id,task_id=object_id if source=="task" else None,
            comment=comment,changes=changes)
    rows=[
        entry("task",41,20,"Алия Садыкова (демо)","Сотрудник отдела","Маркетинг (демо)","Выполнено",
            "Задача",1001,"Материалы подготовлены и переданы отделу практики.",
            [("Статус","В работе","Выполнено"),("Дата выполнения","Не указано","28.10.2026 10:40")],20),
        entry("admin",41,3,"Администратор (демо)","Администратор",None,"Отдел переименован",
            "Отдел",4,"Системное пояснение: Отдел переименован",
            [("Название","Отдел сопровождения","Отдел сопровождения студентов (демо)")],20),
        entry("task",40,2,"Начальник (демо)","Начальник отдела","Маркетинг (демо)","Исполнитель назначен",
            "Задача",1000,"Назначаю ответственного за подготовку встречи.",
            [("Исполнитель","Не назначен","Алия Садыкова (демо)"),("Статус","Новая","В работе")],45),
        entry("admin",40,2,"Начальник (демо)","Начальник отдела","Маркетинг (демо)","Учётная запись изменена",
            "Пользователь",22,"Системное пояснение: Учётная запись изменена",
            [("Имя","Данияр А. (демо)","Данияр Ахметов (демо)")],70),
        entry("task",39,2,"Начальник (демо)","Начальник отдела","Маркетинг (демо)","Возвращено на доработку",
            "Задача",1001,"Добавьте расписание консультаций и уточните аудитории.",
            [("Статус","Выполнено","В работе"),("На доработке","Нет","Да")],95),
        entry("admin",39,99,"Дана — исторический снимок (демо)","Начальник отдела","Прежний отдел сопровождения (демо)",
            "Учётная запись отключена","Пользователь",44,"Системное пояснение: Учётная запись отключена",
            [("Заблокирован","Нет","Да")],1500),
        entry("admin",38,None,"Системный процесс (демо)","Система",None,"Отдел архивирован","Отдел",3,
            "Системное пояснение: Отдел архивирован",[("В архиве","Нет","Да")],1600),
        entry("task",38,None,"Имя инициатора неизвестно","Историческая роль неизвестна",
            "Отдел на момент действия неизвестен","Историческое событие","Задача",1002,
            "Пояснение исторического события не сохранено",[],3000)]
    if case=="pages":
        base=rows
        rows=[dict(row,id=row["id"]+100*copy,created_at=row["created_at"]-timedelta(days=copy))
              for copy in range(8) for row in base]
    if case=="long":
        rows[0]["actor_name"]="Александра-Мария Константинопольская — координатор совместных образовательных проектов (демо)"
        rows[0]["actor_department"]="Отдел информационного сопровождения студентов и взаимодействия с предприятиями-партнёрами (демо)"
        rows[0]["comment"]=("Подготовлены материалы для совместной встречи.\nУточнены аудитории и время консультаций. "*8).strip()
        rows[0]["changes"]=[("Описание","Предыдущая редакция описания. "*12,
            "Обновлённое описание встречи и распределения ответственных. "*12),
            ("Контактное лицо","Не указано","ОченьДлинноеЗначениеБезПробеловДляПроверкиПереноса"*8)]
    if case=="boundary":
        local=datetime(2026,10,27,tzinfo=DEMO_ZONE)
        moments=[local-timedelta(microseconds=1),local,local+timedelta(days=1,microseconds=-1),local+timedelta(days=1)]
        rows=[dict(rows[0],id=501+index,actor_key="2",actor_name="Начальник (демо)",actor_role="Начальник отдела",
                   created_at=moment.astimezone(timezone.utc)) for index,moment in enumerate(moments)]
    if case=="empty":rows=[]
    choices={int(row["actor_key"]):dict(id=int(row["actor_key"]),name=row["actor_name"])
             for row in reversed(sorted(rows,key=lambda row:row["created_at"],reverse=True)) if row["actor_key"]}
    if selected and selected not in choices:
        choices[selected]=dict(id=selected,name="Инициатор без записей")
    rows=sorted((row for row in rows if (not selected or row["actor_key"]==str(selected))
                 and (not start or row["created_at"]>=start) and (not end or row["created_at"]<end)),
                key=lambda row:(-row["created_at"].timestamp(),row["source"],-row["id"]))
    total=len(rows);pages=max(1,(total+19)//20);number=min(number,pages)
    return dict(entries=rows[(number-1)*20:number*20],total=total,page=number,pages=pages,
        users=sorted(choices.values(),key=lambda user:(user["name"].casefold(),user["id"])),
        filters=dict(user_id=str(selected) if selected else "",date_from=date_from,date_to=date_to))


def preview_progress(task, user):
    """Read-only sample wording matching the public five-state presentation."""
    sender, recipient = task.sender_department.name, task.recipient_department.name
    if user.role == 'admin':
        relationship = f'Заказчик: {sender} · Исполняющий отдел: {recipient}'
    elif user.department_id == task.sender_department.id:
        relationship = 'Наш отдел поручал' if task.status == 'completed' else 'Наш отдел — заказчик'
    else:
        relationship = 'Наш отдел выполнял' if task.status == 'completed' else 'Наш отдел — исполнитель'
    stage = {'new':f'Поручение поступило отделу «{recipient}». Исполнитель ещё не назначен.',
        'in_progress':f'Отдел «{recipient}» выполняет поручение.',
        'completed':f'Отдел «{recipient}» отметил выполнение поручения. Отдельная приёмка не требуется.',
        'cancelled':f'Отдел «{sender}» отменил поручение.',
        'rejected':f'Отдел «{recipient}» отклонил поручение.'}.get(task.status,'')
    return dict(relationship=relationship,stage=stage)
