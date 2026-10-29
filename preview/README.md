# Демонстрационный фронтенд Caspian College

Стек: Flask/Jinja, CSS, обычный JavaScript; сборщик и новые зависимости не нужны.
T014 этой копии подключила frontend `2cc6a7c7b1dfcb170695bdbe1cb967bb69e8c88d`
к backend `720c4b8cc5e9d04a62709c1789f3c1a450f67744` (схема 09).
Рабочая копия: `D:/Коды/Ccu_platform/frontend-integration-20261029`.
Demo для визуальных сценариев и integrated с настоящими обработчиками — разные
режимы. Совместная регрессия T015 этой копии выполнена; отдельная итоговая verify ещё не запускалась.
Точные результаты, статусы и PNG — в Spec Kit workspace
`D:/Коды/Ccu_platform/spec-kit-main/ai/specs/2026.10.28_11-33_platform-frontend/`.

## Запуск

Из корня frontend worktree:

```powershell
python -B -X utf8 -m preview.server --host 127.0.0.1 --port 5094
```

Открыть [карту страниц](http://127.0.0.1:5094/__preview__/?role=head).
Разрешены только loopback-адреса; рабочий порт 5000 не используется.
Если порт занят чужим процессом, выбрать свободный собственный через --port.
Ctrl+C завершает свой процесс; рабочие службы не перезапускать.
Python reloader/debug выключены. Jinja/CSS/JS перечитываются без перезапуска,
изменение Python требует заново запустить только этот preview.

## Карта страниц

Все пути ниже относительно http://127.0.0.1:5094.
Роль задаётся `role=employee`, `role=head` или `role=admin`;
это переключение синтетического контекста, не аутентификация.

| Страница | Путь | Demo-роль |
|---|---|---|
| Вход | /login | Без меню роли; отправка отвечает 409 |
| Нашему отделу | /tasks/received | Все |
| Другим отделам | /tasks/sent | Все; у admin общий просмотр |
| Завершённые | /tasks/completed | Все |
| Создание | /tasks/new | employee, head |
| Полный просмотр | /tasks/1001 | Все |
| Открытие карточки | Название и «Открыть» ведут прямо на полную страницу | Все |
| Уведомления | /tasks/notifications | Все |
| Сотрудники | /staff | head |
| Добавление / изменение сотрудника | /staff/new, /staff/20/edit | head |
| Пользователи | /admin/users | admin |
| Добавление / изменение пользователя | /admin/users/new, /admin/users/22/edit | admin |
| Отделы | /admin/departments | admin |
| Создание / переименование отдела | /admin/departments/new, /admin/departments/1/edit | admin |
| Общая статистика | /admin/statistics | admin |
| Отчёт своего отдела | /reports/department | head |
| Журнал | /admin/audit | admin |

У сотрудника меню «Задачи → Уведомления»; у начальника добавлены сотрудники
и свой отчёт; у администратора пользователи, отделы, журнал и общая отчётность.
Категорий, системных настроек, учебных страниц, регистрации и выбора языка нет.
[Компоненты](http://127.0.0.1:5094/__preview__/components?role=head) — служебная
демонстрация, а не дополнительная страница продукта.

## Сценарии

- Доска/список: `view=board|list`, `size=0|1|6|36|300`.
  Завершённые: `scope=received|sent` (admin видит общий набор).
  Список — 25 на страницу, `page=2`; доска показывает весь набор.
  Две активные или три закрытые колонки, строго одна карточка в ряд колонки.
- Формы действий: `actions=demo`; `actions=empty` явно запрещает действия.
  `probe=<уникальное-имя>` изолирует синтетические квитанции/состояния текущего процесса.
  После перезапуска всё demo-состояние теряется.
- Legacy read-only фрагмент (из интерфейса не вызывается): `panel_case=slow|stale|retry|error|401|403|404|user|missing-user|id|login|form|unsafe`.
  Ошибки, чужая identity/ID и небезопасные фрагменты не должны показывать старую задачу.
- Создание: `create_case=slow|uncertain|file-error|too-large|conflict|archived|unavailable`.
- Действия/обсуждение: `action_case=400|409|413|403|404|unavailable|uncertain|slow|refresh-error|refresh-user|refresh-invalid|read403`.
  Сбой после подтверждённого сохранения требует GET-перечитывания, не повторного POST.
- Люди: `people_case=empty|long|archived|conflict|slow|error`.
- Отделы: `dept_case=empty|long|linked|slow|error`.
- Уведомления: `notify_case=empty|pages|long|newer|error|403|session|slow`.
  Native поля boundary и csrf_token передаются серверу; массовая отметка сохраняет
  точную границу просмотра и не затрагивает более новые записи.
- Отчёты: `report_case=empty|no-average|zero-average|archived|long|rework|recompleted|error`.
  Только готовые агрегаты DTO. Нет данных и действительные нулевые часы различаются.
- Журнал: `audit_case=empty|pages|long|boundary|error`; фильтры
  `user_id/date_from/date_to/page`. Обе даты включены по Asia/Qyzylorda.
  ID 99 — исторический инициатор; pages содержит 64 события с размером страницы 20.
  boundary проверяет включённые местные 00:00/23:59 и исключение соседних моментов.
- Ошибки входа: `/login?scenario=invalid-login`. Логин/пароль preview не сохраняет.

Негативные ответы существуют только здесь. Это воспроизведение транспорта и
отображения, не копия production permission/transaction services и не проверка SQL.
Данные и исторические имена синтетические, демонстрационные даты фиксированы:
28.10.2026 11:00 Asia/Qyzylorda. Файлы — только генерируемые demo PDF.

## Проверка без скриптов и движения

`nojs=1` устанавливает на ответе preview CSP `script-src 'none'`.
Браузер действительно блокирует все скрипты страницы. Native ссылки, required-формы,
подтверждения через details и полная страница с native action forms остаются доступны.
Это проверка страницы с заблокированными скриптами, не изменение настроек браузера
или ослабление production-защиты. Переключатель передаётся только внутри demo.
У журнала native GET сохраняет этот служебный параметр скрытым полем;
production context этих полей не содержит.

`motion=reduce-css` добавляет только в HTML preview CSS-эквивалент существующих
правил reduced-motion: animation/transition none, scroll-behavior auto.
Это **CSS-эмуляция**, не системная настройка; matchMedia и JS-ветви предпочтения
не меняются и не объявляются проверенными таким способом. В обычной странице
продолжает действовать настоящий prefers-reduced-motion из stylesheet.

## Проверки

```powershell
python -B -X utf8 -m unittest discover -s tests/frontend -p "test_*.py" -v
node --check static/ui.js
node --check static/tasks.js
node --check static/task-panel.js
git diff --check
```

Subprocess блокирует production app/ORM, network и чтение runtime config, затем
рендерит страницы и выполняет синтетические запросы. StrictUndefined обнаруживает
пропущенные контексты. Браузерная приёмка использует реальные viewport
375×812, 768×1000, 1024×1000, 1366×1000, 1440×1000, три роли и предусмотренные
состояния; точные результаты и ограничения записываются в tasks.md, артефакты —
в artifacts/. Отдельные успешные тесты не заменяют совместную приёмку.

## Изоляция и дальнейшая интеграция

Preview не импортирует app.create_app/models, не подключает PostgreSQL/ORM,
не читает .env/config.json, рабочие пароли, uploads или пользовательские данные.
DATABASE_URL среды не используется. Сеанс авторизации не создаётся.
Все запросы/ссылки/actions same-origin на собственный loopback.
CSRF здесь — явная заглушка для проверки формы; это не настоящий CSRF и не способ
обхода авторизации. Production шаблоны используют серверные поля и permissions.

T014 на новой базе добавила отдельный режим настоящего приложения:

```powershell
cd D:\Коды\Ccu_platform\frontend-integration-20261029
python -B -X utf8 -m preview.server --mode integrated --host 127.0.0.1 --port 5093
```

Режим demo по умолчанию не импортирует backend. Integrated разрешён только явно,
в этой integration-копии и на loopback:5092/5093. Для этой копии используйте 5093;
5092 принадлежит параллельному чату. Порт 5000 запрещён. При унаследованных
DATABASE_URL, TASK_UPLOAD_DIR, SECRET_KEY, ADMIN_PASSWORD/USERNAME/FULL_NAME или
FLASK_APP запуск отклоняется: используйте чистый дочерний процесс, не меняя
пользовательскую среду и не загружая .env.

Каждый запуск создаёт собственную TemporaryDirectory t014-newbase-* только в
frontend Spec Kit artifacts/runtime: synthetic.sqlite3 и uploads. Модельная схема
создаётся тестовой фабрикой, миграции рабочей БД не вызываются. Используются
настоящие login/session, permissions и CSRF, revision/creation_key/action.url.
Cookie caspian_integration_newbase изолирует этот стенд от других localhost-портов.
Баннер обозначает синтетическую среду; role-query не обходит авторизацию.

Логины: demo.head, demo.employee, demo.colleague (Маркетинг), demo.sender
(начальник Практики), demo.outsider (IT), demo.admin. Пароль только этого стенда:
Caspian-demo-2026!. Пять стартовых задач, две с командой из двух участников.
Никакие рабочие данные, пароли и uploads не копируются. Ctrl+C останавливает
только свою консоль; finally вызывает cleanup_integrated_app: закрывает соединения
и удаляет только свою проверенную TemporaryDirectory. Тесты используют ту же
очистку. Чужие runtime-папки и процессы не удалять.

Фронтенд читает assignees/assignee_ids, отправляет повторяемое assignee_ids и полный
итоговый состав; claim/release не отправляют состав. 400/409 сохраняют весь выбор
и комментарий; конфликт требует нового явного действия. Деталь открывается
полной страницей, sidebar shell и task-panel.js в неё/доску не подключаются.
Категория/контакт не выводятся и не отправляются; backend отклоняет legacy поля.
Неизвестная completed_at не заменяется closed_at. Шапка использует настоящий
staff.staff_list; при ширине до 1359px существующее меню сворачивается, чтобы
длинная навигация администратора не перекрывала логотип.

Проверки 29.10.2026: 11 integrated tests (22.091s) и 126 frontend tests (41.211s),
все OK без ResourceWarning/SAWarning/DeprecationWarning. Команды:

```powershell
python -B -X utf8 -m unittest discover -s tests -p test_frontend_integration.py -v
python -B -X utf8 -m unittest discover -s tests/frontend -v
```

В браузере на собственном 5093 проверены реальные входы/CSRF и полный цикл команды:
назначение двоих → снятие одного → присоединение → выполнение → возврат прежней
команде; администратор read-only и переход начальника к сотрудникам. Собственный
стенд после проверки остановлен, его БД/uploads удалены. SQLite и этот smoke не
заменяют полную регрессию, PostgreSQL, responsive/keyboard/reduced-motion в T015.
Не изменялись main checkout, app/migrations, серверные тесты, рабочая БД, порты
5000/5091/5092 и хостинг. Коммит и push T014 не выполнялись. Подробнее —
docs/integration-status.md и artifacts/t014-newbase-verification.md в Spec Kit.


## Палитра и интервалы шапки — 29.10.2026

По отдельному поручению добавлен static/caspian-palette.css, подключённый после style.css в base.html. При объединении с backend сохранить оба stylesheet. Существующий style.css и JS этой правкой не менялись. Итог: яркий фирменный оранжевый и белые поверхности, мятный/зелёный/янтарный/розовый для состояний; голубой исключён. Интервал пунктов шапки 18px desktop, внутри пункта 10px; mobile 10px 12px. Из readonly удалены категория/контактное лицо в соответствии с новой backend-формой; старые данные остаются в модели/истории.

126 frontend checks OK; просмотр 1440/390 без горизонтального overflow, mobile menu и admin-nav проверены. Эти прежние проверки относятся к demo. Реальное сопряжение проверяется в отдельном integrated режиме T014; оно не заменяет общую приёмку T015. Backend-изменения полей находятся отдельно в основном checkout; после merge проверять актуальный docs/frontend-contract.md, plural assignee_ids и новый набор разрешённых create fields. Commit/push не выполнены.


## T015 новой копии — 29.10.2026

Общая команда python -B -X utf8 -m unittest discover -s tests -v выполнена
на объединённом коде с PORTAL_TEST_DATABASE_URL только собственного временного
PostgreSQL17.6:804 OK,909.957s,без skips.158 native-only случаев включены,
остальные SQLite/frontend случаи также выполнены. Отдельно frontend126 OK
(48.139s); после последней сверки усилены assertions приватности существующего
интеграционного теста и повторены все13 integrated cases:OK,22.362s. Это повторы
входящих в804 проверок, не дополнительные930 уникальных тестов. Предупреждений0.

Исправлены реальные несовместимости: поля/ошибки/контекст/счётчики из параллельной
копии, дата отправки для обеих сторон; app/tasks.py теперь передаёт read-only
card_counts списка двумя scoped агрегатами. При36 и300 задачах и board, и list
делают13 запросов вместо прежних61 у списка. Права/переходы/схема не менялись.

Встроенный браузер:215 сочетаний43 страниц/ролей с ширинами375/768/1024/1366/1440,
без document overflow/дублей ID/scroll колонок/sidebar. Пройден реальный цикл
отправки/назначения команды/снятия/присоединения/выполнения/возврата/замены состава/
повторного выполнения, отказ/отмена, signed чтение уведомлений,409 двух вкладок,
меню/диалог с клавиатуры, DnD без мутации при отмене, приватный PDF и404 постороннему.

No-JS проверен CSP script-src 'none' и настоящим native POST. Reduced-motion —
ранее согласованная CSS-эмуляция существующих правил:transition/animation0s,
scroll auto; system matchMedia при этом false, системная/JS-ветка не объявляется
проверенной. Эти режимы находятся только в отдельном диагностическом runner из
Spec Kit artifacts, не в production templates. Обычный integrated CLI их не
активирует query-флагами. Проверка не добавляла QA-ветку в рабочее приложение.

Свои browser/PG процессы остановлены, DB/uploads/cluster удалены, viewport reset,
свой скачанный PDF убран из Downloads. Основные checkout чистые;5000/5091/5092,
рабочая БД/пароли/Render не менялись, staging/commit/push нет. Последние изменения
другого чата пересверены: одинаковая дата отправки сохранена, assertions видимости
счётчиков объединены, QA реализован отдельно. Подробнее docs/integration-status.md
и frontend Spec Kit artifacts/t015-newbase-verification.md.


Последняя CSS-сверка:сохранены margin-top:0 истории и tabular-nums номеров задач
другого чата. После неё повторены30 затронутых сочетаний карточек/списка/доски на
пяти ширинах:без overflow/sidebar/дублей ID;history margin0px. Frontend126 повторно
OK,48.139s. Это локальная CSS-сверка,не новый бизнес-процесс;серверные/HTML/JS
исходники после полного804 не менялись. Второй свой стенд также остановлен и очищен.
