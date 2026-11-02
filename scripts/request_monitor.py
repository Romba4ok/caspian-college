"""Readable committed user actions; optional HTTP diagnostics. Standard library only."""
import argparse
from collections import OrderedDict
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CONFIG = Path.home() / ".caspian-college-monitor" / "config.json"
RESOURCES = {
    "tasks": "задач", "deadlines": "сроков задач", "users": "пользователей",
    "departments": "отделов", "comments": "комментариев", "attachments": "вложений",
    "task-events": "истории задач", "notifications": "уведомлений",
    "audit-events": "журнала действий", "events": "принятых событий CRM", "reports": "отчётности",
}
PAGES = {
    "/": "Главная страница", "/login": "Вход в платформу", "/logout": "Выход из платформы",
    "/tasks/received": "Доска полученных задач", "/tasks/sent": "Доска отправленных задач",
    "/tasks/completed": "Завершённые задачи", "/tasks/new": "Создание задачи на сайте",
    "/tasks/notifications": "Уведомления", "/tasks/notifications/read-all": "Отметка всех уведомлений прочитанными",
    "/admin/users": "Пользователи", "/admin/users/new": "Создание пользователя",
    "/admin/departments": "Отделы", "/admin/departments/new": "Создание отдела",
    "/admin/audit": "Журнал действий", "/admin/statistics": "Общая аналитика",
    "/reports/department": "Отчёт отдела", "/staff": "Сотрудники отдела",
    "/staff/new": "Создание сотрудника",
}
TASK_ACTIONS = {
    "assign": "Назначение исполнителей", "reassign": "Изменение исполнителей",
    "claim": "Сотрудник берёт задачу", "release": "Снятие себя с задачи",
    "complete": "Завершение задачи", "return": "Возврат на доработку",
    "reject": "Отклонение задачи", "cancel": "Отмена задачи", "deadline": "Изменение срока",
    "comments": "Отправка комментария", "attachments": "Прикрепление материалов",
    "edit": "Редактирование задачи", "panel": "Просмотр задачи",
    "status": "Изменение статуса", "confirm": "Подтверждение результата",
    "clarification": "Уточнение задачи",
}
ACCOUNT_ACTIONS = {
    "edit": "Редактирование", "block": "Блокировка", "unblock": "Разблокировка",
    "disable": "Отключение", "delete": "Удаление", "archive": "Архивирование", "restore": "Восстановление",
}


def route_description(method, path):
    if path in ("/api/v1", "/api/v1/"):
        return "Описание API", "api"
    if path == "/api/v1/tasks" and method == "POST":
        return "Создание задачи из CRM", "create"
    if path == "/api/v1/events" and method == "POST":
        return "Напоминание о дедлайне из CRM", "reminder"
    match = re.fullmatch(r"/api/v1/([a-z-]+)(?:/([0-9]{1,10})(/download)?)?", path)
    if match and match[1] in RESOURCES:
        subject = RESOURCES[match[1]]
        if match[3] and match[1] == "attachments":
            return f"Скачивание вложения №{int(match[2])}", "read"
        suffix = f", запись №{int(match[2])}" if match[2] else ""
        return f"API: запрос {subject}{suffix}", "read"
    if path in PAGES:
        return PAGES[path], "read" if method in ("GET", "HEAD") else "form"
    match = re.fullmatch(r"/tasks/([0-9]{1,10})(?:/(?:execution/)?([a-z_]+))?", path)
    if match:
        action = TASK_ACTIONS.get(match[2], "Действие с задачей") if match[2] else "Просмотр задачи"
        return f"{action} №{int(match[1])}", "read" if method in ("GET", "HEAD") else "form"
    match = re.fullmatch(r"/tasks/notifications/([0-9]{1,10})/read", path)
    if match:
        return f"Прочтение уведомления №{int(match[1])}", "form"
    match = re.fullmatch(r"/tasks/attachments/([0-9]{1,10})/download", path)
    if match:
        return f"Скачивание вложения №{int(match[1])}", "read"
    match = re.fullmatch(r"/(admin/(users|departments)|staff)/([0-9]{1,10})/([a-z]+)", path)
    if match and match[4] in ACCOUNT_ACTIONS:
        entity = "отдела" if match[2] == "departments" else "сотрудника" if match[1] == "staff" else "пользователя"
        return f"{ACCOUNT_ACTIONS[match[4]]} {entity} №{int(match[3])}", "read" if method in ("GET", "HEAD") else "form"
    if path.startswith("/static/") or path == "/favicon.ico":
        return "Загрузка оформления или изображения", "asset"
    # Do not echo unknown paths: they may contain private identifiers or secrets.
    return "Другой маршрут сайта", "unknown"


def explain(status, kind, method):
    if status >= 500:
        return "ОШИБКА", "Сервер не выполнил запрос. Нужна проверка работы приложения."
    failures = {
        400: "Запрос отклонён. Проверьте поля, формат данных или актуальность формы.",
        401: "Вход или ключ доступа не принят. Для API проверьте Bearer-ключ.",
        403: "Запрос запрещён: недостаточно прав или действие недоступно этому автору.",
        404: "Запрошенная страница, задача или другая запись не найдена.",
        405: "Этот адрес не поддерживает выбранный способ обращения GET/POST.",
        409: "Конфликт данных. Запись изменилась или ID события использован с другим содержимым.",
        410: "Этот этап работы больше не используется.",
        413: "Запрос или вложение превышает допустимый размер.",
        415: "Формат данных не подходит. Для API нужен application/json.",
        422: "Сервер не поддерживает переданные данные или тип события.",
        429: "Слишком много запросов. Следует подождать перед повтором.",
    }
    if status >= 400:
        return "ОТКАЗ", failures.get(status, "Сервер отклонил запрос клиента.")
    if status == 304:
        return "КЭШ", "Содержимое не изменилось; браузер использует сохранённую копию."
    if status >= 300:
        return "ПЕРЕХОД", "Сервер перенаправил на другую страницу. Результат формы смотрите на ней."
    if kind == "create" and status == 201:
        return "УСПЕХ", "Задача создана и отправлена отделу; сохранены история и штатные уведомления."
    if kind == "create" and status == 200:
        return "ПОВТОР", "Возвращена ранее созданная задача. Повторная задача не создавалась."
    if kind == "reminder" and status == 201:
        return "УСПЕХ", "Событие CRM принято. Напоминания обработаны с защитой от повторов."
    if kind == "reminder" and status == 200:
        return "ПОВТОР", "Событие уже обработано; возвращена прежняя квитанция."
    if method in ("GET", "HEAD"):
        return "УСПЕХ", "Сервер вернул запрошенную страницу или данные."
    return "ОТВЕТ", "Сервер ответил на отправленные данные. Подробный результат смотрите на сайте или в ответе API."


def format_event(row, *, show_all=False):
    """Return only approved fields, never raw logs, URLs, IPs, bodies or tokens."""
    if not isinstance(row, dict):
        return None
    method, status = row.get("method"), row.get("httpStatus")
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        return None
    if type(status) is not int or not 100 <= status <= 599:
        return None
    raw_path = row.get("path")
    if not isinstance(raw_path, str) or len(raw_path) > 8192:
        return None
    try:
        path = urlsplit(raw_path).path.rstrip("/") or "/"
    except ValueError:
        return None
    action, kind = route_description(method, path)
    health = str(row.get("clientUa", "")).startswith("RailwayHealthCheck")
    if not show_all and (kind == "asset" or health) and status < 400:
        return None
    if health:
        action = "Автоматическая проверка доступности Railway"
    try:
        stamp = datetime.fromisoformat(row.get("timestamp", "").replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError
        clock = stamp.astimezone(ZoneInfo("Asia/Qyzylorda")).strftime("%d.%m %H:%M:%S")
    except (ValueError, TypeError, AttributeError):
        clock = "время неизвестно"
    duration = row.get("totalDuration")
    elapsed = f"{round(duration)} мс" if type(duration) in (int, float) and 0 <= duration <= 86400000 else "время ответа неизвестно"
    label, outcome = explain(status, kind, method)
    return f"{clock} | {label} | {action}\n  Что произошло: {outcome}\n  Технически: {method} · HTTP {status} · {elapsed}\n"


def activity_from_row(row):
    if not isinstance(row,dict): return None
    if "caspian_activity" not in row:
        message=row.get("message")
        if not isinstance(message,str) or len(message)>16384: return None
        try: row=json.loads(message)
        except (ValueError,RecursionError): return None
    if not isinstance(row,dict): return None
    activity=row.get("caspian_activity")
    if not isinstance(activity,dict) or activity.get("version")!=1: return None
    if not re.fullmatch(r"(?:task|admin|crm):[0-9]{1,10}",str(activity.get("id",""))): return None
    if not isinstance(activity.get("actor"),dict) or not isinstance(activity.get("message"),str): return None
    return activity


def safe_display(value,limit=400):
    if not isinstance(value,str): return ""
    value=re.sub(r"(?i)\bBearer\s+[^\s,;]+","Bearer [скрыто]",value)
    value=re.sub(r"(?i)(\b(?:password|password_hash|secret|secret_key|token|api_key|пароль|токен)\b\s*[:=]\s*)[^\s,;]+",r"\1[скрыто]",value)
    return re.sub(r"[\x00-\x1f\x7f]"," ",value).strip()[:limit]


def format_activity(activity):
    if activity is None: return None
    try:
        stamp=datetime.fromisoformat(activity.get("timestamp","").replace("Z","+00:00"))
        if stamp.tzinfo is None: raise ValueError
        clock=stamp.astimezone(ZoneInfo("Asia/Qyzylorda")).strftime("%d.%m %H:%M:%S")
    except (ValueError,TypeError,AttributeError): clock="время неизвестно"
    actor=activity["actor"]
    name=safe_display(actor.get("name"),120) or "Неизвестный участник"
    saved_role=actor.get("role")
    role={"admin":"администратор","head":"начальник отдела","employee":"сотрудник","system":"система"}.get(saved_role,"участник") if isinstance(saved_role,str) else "участник"
    ident=actor.get("id")
    identity=f" · ID {ident}" if type(ident) is int and 0<ident<=2147483647 else ""
    return f"{clock} | {name} · {role}{identity}\n  {safe_display(activity['message'])}\n"


class RecentRequests:
    def __init__(self, capacity=4096):
        self.capacity = capacity
        self.seen = OrderedDict()

    def repeated(self, row):
        key = row.get("requestId")
        if not isinstance(key, str) or not key:
            return False
        if key in self.seen:
            return True
        self.seen[key] = None
        if len(self.seen) > self.capacity:
            self.seen.popitem(last=False)
        return False


def find_cli(explicit):
    candidate = explicit or shutil.which("railway")
    if not candidate:
        raise ValueError("Railway CLI не найден. Установите официальный CLI: npm install -g @railway/cli")
    path = Path(candidate).expanduser().resolve()
    if os.name == "nt" and path.suffix.lower() in (".cmd", ".bat"):
        # Resolve npm's wrapper to the official native executable for clean shutdown.
        choices = (path.parent / "node_modules/@railway/cli/bin/railway.exe",
                   path.parent.parent / "@railway/cli/bin/railway.exe")
        path = next((item for item in choices if item.is_file()), path)
        if path.suffix.lower() != ".exe":
            raise ValueError("Укажите путь к официальному railway.exe через --railway.")
    if not path.is_file():
        raise ValueError("Railway CLI по сохранённому пути не найден. Проверьте --railway или настройки монитора.")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Понятный журнал запросов Caspian College в реальном времени")
    parser.add_argument("--project", help="ID проекта Railway")
    parser.add_argument("--service", help="Имя сервиса (по умолчанию caspian-college)")
    parser.add_argument("--environment", help="Окружение (по умолчанию production)")
    parser.add_argument("--railway", help="Путь к официальному Railway CLI")
    parser.add_argument("--once", action="store_true", help="Показать последние события и выйти")
    parser.add_argument("--lines", type=int, default=15, choices=range(1,201), metavar="1..200", help="Количество последних событий для --once")
    parser.add_argument("--all", action="store_true", help="Также показывать оформление и проверки доступности")
    parser.add_argument("--http", action="store_true", help="Технические HTTP-запросы вместо действий пользователей")
    args = parser.parse_args(argv)
    try:
        config = json.loads(CONFIG.read_text(encoding="utf-8-sig")) if CONFIG.exists() else {}
    except (ValueError, OSError, UnicodeError):
        print("Не удалось прочитать настройки монитора. Проверьте JSON-файл: ~/.caspian-college-monitor/config.json")
        return 2
    if not isinstance(config, dict) or any(not isinstance(v, str) for v in config.values()):
        print("Настройки монитора должны быть JSON-объектом со строковыми значениями.")
        return 2
    try:
        cli = find_cli(args.railway or config.get("railway"))
        project = args.project or config.get("project")
        if not project or not re.fullmatch(r"[A-Za-z0-9-]{1,80}",project):
            raise ValueError("Укажите ID проекта через --project или в настройках монитора.")
        service = args.service or config.get("service", "caspian-college")
        environment = args.environment or config.get("environment", "production")
    except ValueError as error:
        # These messages are authored above; config values are never echoed.
        print(str(error))
        print("Инструкция: docs/request-monitor.md")
        return 2
    except OSError:
        print("Не удалось открыть Railway CLI. Проверьте установку и параметр --railway.")
        return 2
    try:
        ZoneInfo("Asia/Qyzylorda")
    except ZoneInfoNotFoundError:
        print("Нет данных часового пояса. Выполните: python -m pip install tzdata")
        return 2
    command = [str(cli), "logs", "--json", "--project", project,
               "--service", service, "--environment", environment]
    if args.http: command.append("--http")
    if args.once:
        command += ["--lines",str(args.lines)]
    print("Caspian College — "+("ЖИВОЙ ЖУРНАЛ ЗАПРОСОВ" if args.http else "ДЕЙСТВИЯ СОТРУДНИКОВ"))
    print("Время: Asia/Qyzylorda. Ctrl+C — остановить просмотр.\n")
    if args.http:
        print("HTTP показывает ответы сервера; результат обычных форм проверяйте на сайте.")
    else:
        print("Кто и что сделал: задачи, комментарии, исполнители, сотрудники и смена паролей.")
        print("Только сохранённые изменения. Открытия страниц, входы и выходы скрыты.")
    print("Ожидаю новые действия…\n",flush=True)
    recent = RecentRequests()
    shown = 0
    delay = 3
    process = None
    try:
        while True:
            auth_problem = False
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            for line in process.stdout:
                try:
                    row = json.loads(line)
                except (ValueError, RecursionError):
                    if any(term in line.lower() for term in ("unauthorized", "not logged", "railway login", "saved login")):
                        auth_problem = True
                    continue
                if isinstance(row, dict) and "method" not in row:
                    diagnostic = " ".join(str(row.get(key,"")) for key in ("code","error","message")).lower()
                    if any(term in diagnostic for term in ("unauthorized", "not logged", "railway login", "saved login")):
                        auth_problem = True
                activity=activity_from_row(row) if not args.http else None
                message = format_event(row, show_all=args.all) if args.http else format_activity(activity)
                dedup_row=row if args.http else {"requestId":activity["id"]} if activity else {}
                if message and not recent.repeated(dedup_row):
                    print(message,flush=True)
                    shown += 1
                    delay = 3
            code = process.wait()
            process.stdout.close()
            process = None
            if auth_problem:
                print("Нужно снова войти в Railway через официальный CLI: railway login. Затем запустите монитор ещё раз.")
                return 2
            if args.once:
                if code:
                    print("Railway не вернул журнал. Проверьте вход в аккаунт, доступ к проекту и подключение к сети.")
                    return 2
                print(f"Показано событий: {shown}. Для непрерывного просмотра запустите без --once.")
                return 0
            if code == 0:
                # Some Railway CLI versions return an HTTP snapshot rather than
                # a long-lived stream. That is normal: refresh without warnings.
                time.sleep(5)
                continue
            print(f"Связь с журналом прервалась. Повторное подключение через {delay} с…",flush=True)
            time.sleep(delay)
            delay = min(delay*2,30)
    except KeyboardInterrupt:
        print(f"\nПросмотр остановлен. Показано событий: {shown}. Сайт продолжает работать.")
        return 0
    except OSError:
        print("Не удалось запустить Railway CLI или прочитать журнал. Проверьте установку и подключение.")
        return 2
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
            if process.stdout:
                process.stdout.close()


if __name__ == "__main__":
    raise SystemExit(main())
