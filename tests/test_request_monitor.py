"""Monitor evidence must remain understandable, accurate and free of secrets."""
import importlib.util
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location("request_monitor",Path(__file__).resolve().parents[1]/"scripts/request_monitor.py")
monitor=importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


class RequestMonitorTests(unittest.TestCase):
    def row(self,**changes):
        row=dict(timestamp="2026-11-02T05:20:00.123456789Z",method="GET",path="/tasks/received",
                 httpStatus=200,totalDuration=42,requestId="test-request")
        row.update(changes)
        return row

    def test_real_http_fields_get_local_time_and_understandable_route(self):
        text=monitor.format_event(self.row())
        self.assertIn("02.11 10:20:00",text)
        self.assertIn("Доска полученных задач",text)
        self.assertIn("42 мс",text)

    def test_cannot_leak_query_bodies_headers_ips_or_arbitrary_paths(self):
        for path in ("/api/v1/tasks?token=TOP-SECRET", "/reset/TOP-SECRET", "/tasks/3?password=TOP-SECRET"):
            output=monitor.format_event(self.row(path=path,srcIp="TOP-SECRET",headers={"Authorization":"TOP-SECRET"},message="TOP-SECRET",body="TOP-SECRET"))
            self.assertNotIn("TOP-SECRET",output)
        self.assertIn("Другой маршрут",monitor.format_event(self.row(path="/reset/TOP-SECRET")))

    def test_assets_and_healthchecks_hidden_but_their_failures_are_visible(self):
        for changes in (dict(path="/static/ui.js",httpStatus=304),dict(clientUa="RailwayHealthCheck/1.0")):
            self.assertIsNone(monitor.format_event(self.row(**changes)))
            self.assertIsNotNone(monitor.format_event(self.row(**changes),show_all=True))
        self.assertIn("ОШИБКА",monitor.format_event(self.row(path="/static/ui.js",httpStatus=500)))

    def test_creation_and_retries_are_distinguished_from_reminders(self):
        created=monitor.format_event(self.row(method="POST",path="/api/v1/tasks",httpStatus=201))
        self.assertIn("Задача создана",created)
        retry=monitor.format_event(self.row(method="POST",path="/api/v1/tasks",httpStatus=200))
        self.assertIn("ПОВТОР",retry)
        reminder=monitor.format_event(self.row(method="POST",path="/api/v1/events",httpStatus=201))
        self.assertIn("Событие CRM принято",reminder)
        self.assertNotIn("уведомления отправлены",reminder)

    def test_redirect_and_form_reply_do_not_falsely_claim_mutation_success(self):
        for path,status in (("/tasks/3/complete",302),("/login",200)):
            text=monitor.format_event(self.row(method="POST",path=path,httpStatus=status))
            self.assertNotIn("УСПЕХ",text)
            self.assertNotIn("Задача выполнена",text)

    def test_failures_are_explained_without_pretending_to_know_exact_validation_reason(self):
        for status,phrase in ((400,"Проверьте поля"),(401,"ключ доступа"),(403,"недостаточно прав"),
                              (409,"Конфликт данных"),(503,"Сервер не выполнил")):
            self.assertIn(phrase,monitor.format_event(self.row(httpStatus=status)))

    def test_malformed_records_never_show_raw_messages(self):
        for row in (None,[],{},dict(message="TOP-SECRET"),self.row(httpStatus="200"),self.row(method="TOP-SECRET"),self.row(path=None)):
            self.assertIsNone(monitor.format_event(row))
        output=monitor.format_event(self.row(timestamp="bad",totalDuration=float('nan')))
        self.assertIn("время неизвестно",output)
        self.assertIn("время ответа неизвестно",output)

    def test_reconnect_deduplication_is_bounded(self):
        recent=monitor.RecentRequests(capacity=2)
        self.assertFalse(recent.repeated({"requestId":"a"}))
        self.assertTrue(recent.repeated({"requestId":"a"}))
        self.assertFalse(recent.repeated({"requestId":"b"}))
        self.assertFalse(recent.repeated({"requestId":"c"}))
        self.assertFalse(recent.repeated({"requestId":"a"}))

    def run_snapshots(self,snapshots,codes,sleep_effect=None,http=True):
        class Process:
            def __init__(self,rows,code):
                self.stdout=io.StringIO("".join(json.dumps(row)+"\n" for row in rows))
                self.code=code
            def wait(self,timeout=None):return self.code
        processes=[Process(rows,code) for rows,code in zip(snapshots,codes)]
        output=io.StringIO()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(monitor,"CONFIG",Path(directory)/"absent.json"), \
             patch.object(monitor,"find_cli",return_value=Path("railway.exe")), \
             patch.object(monitor.subprocess,"Popen",side_effect=processes), \
             patch.object(monitor.time,"sleep",side_effect=sleep_effect), redirect_stdout(output):
            result=monitor.main(["--project","test-project"]+(["--http"] if http else []))
        return result,output.getvalue()

    def test_normal_http_snapshots_refresh_quietly_and_only_show_new_requests(self):
        first=self.row(requestId="first")
        second=self.row(requestId="second",method="POST",path="/api/v1/tasks",httpStatus=201)
        code,output=self.run_snapshots([[first],[first,second]],[0,0],sleep_effect=[None,KeyboardInterrupt])
        self.assertEqual(0,code)
        self.assertEqual(1,output.count("Доска полученных задач"))
        self.assertIn("Задача создана",output)
        self.assertNotIn("Связь с журналом прервалась",output)
        self.assertIn("Показано событий: 2",output)

    def test_structured_cli_auth_failure_gives_safe_help_without_echoing_error(self):
        code,output=self.run_snapshots([[{"code":"UNAUTHORIZED","error":"TOP-SECRET"}]],[1])
        self.assertEqual(2,code)
        self.assertIn("снова войти в Railway",output)
        self.assertNotIn("TOP-SECRET",output)

    def test_default_mode_shows_named_actions_and_ignores_login_get_and_logout(self):
        activity={"version":1,"id":"task:15","timestamp":"2026-11-02T05:00:00Z",
            "actor":{"id":5,"name":"Иван Петров","role":"employee"},"message":"Создал задачу №15"}
        rows=[self.row(path="/login"),self.row(method="POST",path="/logout"),
              {"message":json.dumps({"caspian_activity":activity})}]
        code,output=self.run_snapshots([rows],[0],sleep_effect=KeyboardInterrupt,http=False)
        self.assertEqual(0,code)
        self.assertIn("Иван Петров · сотрудник · ID 5",output)
        self.assertIn("Создал задачу №15",output)
        self.assertNotIn("HTTP 200",output)
        self.assertNotIn("Вход в платформу",output)

    def test_activity_text_is_sanitized_and_other_application_logs_hidden(self):
        self.assertIsNone(monitor.activity_from_row({"message":"password=TOP-SECRET"}))
        activity={"version":1,"id":"admin:1","timestamp":"2026-11-02T05:00:00Z",
            "actor":{"id":1,"name":"Admin\npassword=TOP-SECRET","role":"admin"},"message":"Bearer TOP-SECRET"}
        output=monitor.format_activity(monitor.activity_from_row({"caspian_activity":activity}))
        self.assertNotIn("TOP-SECRET",output)
        self.assertNotIn("Admin\n",output)
        activity["actor"]["role"]={"unexpected":"data"}
        self.assertIn("участник",monitor.format_activity(activity))


if __name__=="__main__":
    unittest.main()
