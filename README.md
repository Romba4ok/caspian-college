# Caspian College

Платформа поручений между отделами колледжа: задачи, исполнители, сроки, файлы,
обсуждения, уведомления и отчётность. Русский интерфейс, доска задач, роли и права доступа.

**Сайт:** [caspian-college-production.up.railway.app](https://caspian-college-production.up.railway.app/login)

**Документация:** [CRM API](docs/crm-api.md) · [OpenAPI / Postman](docs/crm-openapi.json) · [Монитор действий](docs/request-monitor.md) · [Railway](docs/railway.md)

## Возможности и роли

Задача сразу отправляется другому отделу. Основной процесс:
**Новая → В работе → Выполнено**. Поддерживаются несколько исполнителей,
переназначение, отмена, отклонение и возврат на доработку. Изменения сохраняются
в истории; комментарии и приватные файлы доступны участвующим отделам и администратору.
Срок задаётся отдельными полями даты и времени в часовом поясе `Asia/Qyzylorda`.

| Роль | Возможности |
|---|---|
| Администратор | Пользователи, отделы, общая аналитика и журнал. Не выполняет задачи за отделы. |
| Начальник отдела | Задачи своего отдела, назначения, свои сотрудники и отчёт отдела. |
| Сотрудник | Отправка задач другим отделам, работа над задачами своего отдела, комментарии и файлы. |

Публичной регистрации нет: администратор создаёт начальников и сотрудников с отделами.

**Удаление сотрудников.** Кнопка «Удалить» скрывает сотрудника из списка и закрывает
вход; задачи, авторство и история сохраняются. Активные назначения сначала нужно
переназначить. Начальник удаляет только сотрудников своего отдела, администратор —
любого пользователя, кроме единственного администратора.

Восстановление: **Пользователи → Удалённые → Вернуть в список**. После этого доступ
ещё отключён; администратор включает его отдельно. Кнопка «Отключить» просто
закрывает вход, оставляя сотрудника в общем списке.

## Локальный запуск

Нужны **Python 3.12–3.14** и собственная **PostgreSQL-база**. Стек: Flask, Jinja,
SQLAlchemy, Alembic, CSS/JavaScript. Отдельной frontend-сборки нет.

Для новой установки в PowerShell:

```powershell
git clone https://github.com/Romba4ok/caspian-college.git
cd caspian-college
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Создайте отдельную PostgreSQL-базу и задайте настройки в текущем терминале.
Замените примеры пользователя, пароля БД и пароля администратора своими значениями:

```powershell
$env:DATABASE_URL = "postgresql+psycopg://USER:PASSWORD@localhost:5432/caspian_college"
$env:SECRET_KEY = & .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
$env:ADMIN_USERNAME = "admin"
$env:ADMIN_PASSWORD = "YOUR_STRONG_ADMIN_PASSWORD"
$env:TASK_UPLOAD_DIR = Join-Path $env:LOCALAPPDATA "CaspianCollege\private-uploads"
.\.venv\Scripts\python.exe -m flask --app app:create_app db upgrade
.\.venv\Scripts\python.exe -m flask --app app:create_app run --host 127.0.0.1 --port 5000
```

Откройте [локальный сайт](http://127.0.0.1:5000/login). Первый вход — с заданными
`ADMIN_USERNAME`/`ADMIN_PASSWORD`. Дальше сохраняйте ту же базу, постоянный
`SECRET_KEY` и каталог файлов. Пароль существующего администратора меняется
через интерфейс, а не переменной окружения.

Все переменные перечислены в [.env.example](.env.example). Они читаются из
окружения процесса; копирование файла в `.env` само по себе их не загружает.
Для уже настроенного Windows-портала есть `python scripts/local_portal.py start-site`.
Запускайте helper из checkout, указанного в его конфигурации; для обычного
перезапуска не выполняйте повторный `setup`.

## API: подключение другого проекта или CRM

GET получает **актуальные данные из БД**. POST создаёт задачи или обрабатывает
напоминания, PATCH изменяет задачи, DELETE удаляет их из рабочих списков. Базовый адрес:

```text
https://caspian-college-production.up.railway.app/api/v1
```

1. На сервере платформы задайте два разных случайных ключа длиной 32–512 символов:
   `CRM_API_READ_TOKEN` для чтения и `CRM_API_WRITE_TOKEN` для чтения, POST, PATCH и DELETE.
   Без настроенных ключей API выключен; на опубликованном Railway они уже настроены.
2. Передайте нужный ключ доверенному серверу другого проекта через его защищённое окружение.
3. Отправляйте заголовок `Authorization: Bearer <ключ>` при каждом запросе.

Ключи дают доступ ко всем отделам; не помещайте их в браузерный JavaScript или Git.
Вход на сайт по паролю не заменяет Bearer-ключ.

| Запрос | Что возвращает или делает |
|---|---|
| `GET /tasks`, `GET /deadlines` | Задачи и активные сроки |
| `GET /users`, `GET /departments` | Пользователи без паролей и отделы |
| `GET /comments`, `GET /attachments` | Комментарии и метаданные файлов |
| `GET /task-events`, `GET /audit-events` | История задач и административных действий |
| `GET /notifications`, `GET /events`, `GET /reports` | Уведомления, квитанции CRM и отчётность |
| `POST /tasks` | Создать и сразу отправить задачу другому отделу |
| `POST /events` | Обработать напоминание о дедлайне |
| `PATCH /tasks/{id}` | Изменить поля задачи или выполнить рабочее действие |
| `DELETE /tasks/{id}` | Удалить задачу из рабочих списков с сохранением истории |

### GET в PowerShell

Предварительно задайте `CRM_API_READ_TOKEN` в терминале:

```powershell
$base = "https://caspian-college-production.up.railway.app/api/v1"
$read = @{Authorization = "Bearer $($env:CRM_API_READ_TOKEN)"}
Invoke-RestMethod "$base/tasks?limit=100" -Headers $read | ConvertTo-Json -Depth 20
```

Ответ содержит `data` и `pagination`. Для всех строк переходите по `pagination.next`,
пока он не станет `null`. Этот адрес относителен корню сайта, а не `/api/v1`.
Даты — ISO 8601 UTC. Другая система сама опрашивает API
и обновляет свои записи по ID; автоматическая синхронизация одним GET не включается.
Удалённые сотрудники остаются в API с `is_deleted=true`, `is_active=false` для сверки данных.

### POST: создать задачу

Задайте `CRM_API_WRITE_TOKEN`. Через GET `/users` и `/departments` узнайте ID,
затем замените примеры `7` и `5` действительными значениями:

```powershell
$write = @{Authorization = "Bearer $($env:CRM_API_WRITE_TOKEN)"}
$body = @{source="my-crm"; event_id="create-task-001"; author_id=7; recipient_department_id=5; title="Подготовить материалы"; description="Подготовить презентацию"; due_at="2026-11-05T15:30:00+05:00"; priority="normal"} | ConvertTo-Json
Invoke-RestMethod "$base/tasks" -Method Post -Headers $write -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($body)) | ConvertTo-Json -Depth 20
```

Автор — действующий сотрудник или начальник с отделом, администратор не подходит.
Отдел отправителя определяется по автору; получатель должен быть другим отделом.
Для новой задачи нужен новый `event_id`. Повтор неизменённого запроса с прежними
`source + event_id` возвращает ту же задачу без дубля.

Первое создание — HTTP `201`, повтор — `200`; `400` — неверные поля, `401` — ключ
не принят, `403` — нет прав, `409` — конфликт. Фильтры, полный JSON, скачивание файлов
и POST напоминаний: [подробная инструкция API](docs/crm-api.md).

### PATCH и DELETE: управление задачами

Оба метода требуют `CRM_API_WRITE_TOKEN` и JSON с `source`, уникальным `event_id`,
`actor_id`, текущей `revision` и пояснением `comment`. Инициатор должен иметь
соответствующие права в платформе. ID/версию задачи получают через GET.

Пример изменения названия (замените ID `123` и `actor_id=7`):

```powershell
$task = (Invoke-RestMethod "$base/tasks/123" -Headers $read).data
$body = @{source="my-crm"; event_id="edit-task-123-001"; actor_id=7; revision=$task.revision; comment="Уточнили поручение в CRM"; changes=@{title="Подготовить итоговую презентацию"}} | ConvertTo-Json -Depth 5
Invoke-RestMethod "$base/tasks/123" -Method Patch -Headers $write -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($body)) | ConvertTo-Json -Depth 20
```

DELETE после получения свежей версии:

```powershell
$task = (Invoke-RestMethod "$base/tasks/123" -Headers $read).data
$body = @{source="my-crm"; event_id="delete-task-123-001"; actor_id=7; revision=$task.revision; comment="Поручение больше не требуется"} | ConvertTo-Json
Invoke-RestMethod "$base/tasks/123" -Method Delete -Headers $write -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($body)) | ConvertTo-Json -Depth 20
```

PATCH по умолчанию меняет `title`, `description`, `priority`, `due_at` активной
задачи. Поля редактирует автор или начальник отправителя; срок — только начальник
отправителя. Через `action` доступны `assign`, `reassign`, `claim`, `release`,
`complete`, `return`, `cancel`, `reject`, `deadline`, `comment` по обычным правилам ролей.
Автор, отделы и служебные даты напрямую не перезаписываются.

DELETE доступен автору/начальнику отправителя. Активная задача закрывается,
помечается `is_deleted=true` и исчезает с доски и из отчётов. ID, файлы, история
и квитанции остаются; GET `/tasks` возвращает отметку для синхронизации. Участники
получают уведомление об удалении. Это не физическое стирание базы.

Для новой команды используйте новый `event_id`; сетевой повтор отправляйте с
прежним телом/ID. Устаревшая версия даёт `409`: перечитайте задачу перед новой
командой. Удалённая задача — `410` для новых изменений; точный повтор ранее
принятой команды возвращает `200` с `meta.duplicate=true`.

## Монитор: кто и что сделал

Откройте **`Start-Monitor.cmd`** или выполните из корня проекта:

```powershell
python -X utf8 scripts/request_monitor.py
```

Показываются имя, роль и сохранённое действие: задачи, комментарии, назначения,
удаление сотрудников и смена пароля. Входы, выходы и переходы по страницам скрыты.
Сам пароль не выводится. Ctrl+C останавливает просмотр, сайт продолжает работать.

Нужен официальный Railway CLI с авторизацией владельца проекта. На текущем
компьютере настройки уже сохранены. Другой компьютер и технический режим `--http`:
[инструкция монитора](docs/request-monitor.md).

## Размещение и обновление

Railway использует `Dockerfile`: миграции, затем Gunicorn. Нужны PostgreSQL и
постоянный volume вложений. Настройки: `DATABASE_URL`, постоянный `SECRET_KEY`,
`TASK_UPLOAD_DIR`; при первой установке — `ADMIN_USERNAME`/`ADMIN_PASSWORD`.
Для CRM добавьте оба API-ключа. Подробности: [Railway](docs/railway.md).

Текущая схема — **`20261102_10`**. Обновление выполняется штатным `flask db upgrade`.
Удаление сотрудников и монитор не требуют очистки базы. Старые миграции 08/09
могут отказать при несовместимых рабочих данных: не обходите отказ через `stamp`
или удаление таблиц. Перед обновлением сохраните резервную копию БД и файлов.

Railway Free может усыплять сервисы и останавливать их после исчерпания кредитов.
CRM нужны тайм-аут 60–90 секунд и повторы того же события. Текущая публикация
Railway выполняется через CLI; push в GitHub сам по себе её не обновляет.

## Код и проверки

`app/` — API и бизнес-логика; `templates/` и `static/` — интерфейс;
`migrations/` — схема; `scripts/` и `deploy/` — служебные команды;
`tests/` — проверки; `docs/` — подробные инструкции.

Пример проверки монитора:

```powershell
python -m unittest discover -s tests -p test_request_monitor.py -v
```

PostgreSQL-тесты используют `PORTAL_TEST_DATABASE_URL` и изолированные схемы.
Не направляйте их на рабочую БД. Пароли, API-ключи, дампы, пользовательские файлы
и локальная конфигурация в репозиторий не включаются.
