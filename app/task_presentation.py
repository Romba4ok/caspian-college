"""Explain task direction and result handoffs without changing workflow rules."""

from .permissions import is_task_dispatched


def task_progress(task, user):
    sender, recipient = task.sender_department.name, task.recipient_department.name
    closed = task.status in ("completed", "cancelled", "rejected")
    dispatched = is_task_dispatched(task)
    if user.role == "admin":
        relationship = f"Заказчик: {sender} · Исполняющий отдел: {recipient}"
    elif user.department_id == task.sender_department_id:
        relationship = "Наш отдел поручал" if task.status == "completed" else ("Поручение нашего отдела" if closed else "Наш отдел — заказчик")
        if task.status == "cancelled" and not dispatched:
            relationship = "Заявка нашего отдела"
    else:
        relationship = "Наш отдел выполнял" if task.status == "completed" else ("Поручение нашему отделу" if closed else "Наш отдел — исполнитель")
    stages = {
        "new": (f"Поручение на доработке. В отделе «{recipient}» нужно назначить исполнителя." if task.is_rework else f"Поручение поступило отделу «{recipient}». Исполнитель ещё не назначен."),
        "in_progress": (f"Поручение возвращено отделу «{recipient}» на доработку." if task.is_rework else f"Отдел «{recipient}» выполняет поручение."),
        "completed": f"Отдел «{recipient}» отметил выполнение поручения. Отдельная приёмка не требуется.",
        "cancelled": f"Отдел «{sender}» отменил поручение." if dispatched else "Заявка отменена до отправки другому отделу.",
        "rejected": f"Отдел «{recipient}» отклонил поручение.",
    }
    return dict(relationship=relationship, stage=stages.get(task.status, ""))
