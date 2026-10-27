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
        "draft": f"Поручение ещё не отправлено в отдел «{recipient}».",
        "pending_approval": f"Начальник отдела «{sender}» согласует отправку поручения.",
        "awaiting_assignment": f"Поручение передано отделу «{recipient}». Ожидается назначение исполнителя.",
        "assigned": f"В отделе «{recipient}» назначен исполнитель поручения.",
        "in_progress": f"Отдел «{recipient}» выполняет поручение.",
        "awaiting_clarification": f"Отдел «{recipient}» ожидает уточнения от отдела «{sender}».",
        "recipient_review": f"Готовый результат проверяет начальник отдела «{recipient}». Заказчику результат ещё не передан.",
        "sender_acceptance": f"Готовый результат передан отделу «{sender}» на окончательную приёмку.",
        "completed": f"Отдел «{sender}» принял результат. Поручение завершено.",
        "cancelled": f"Отдел «{sender}» отменил поручение." if dispatched else "Заявка отменена до отправки другому отделу.",
        "rejected": f"Отдел «{recipient}» отклонил поручение.",
    }
    return dict(relationship=relationship, stage=stages.get(task.status, ""))
