"""Unified, immutable administrator journal on synthetic SQLite/PostgreSQL data."""
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from html import unescape
import os
import re
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from sqlalchemy import create_engine, event, select, text

from app import create_app
from app.audit import (AuditFilterError, actor_snapshot, audit_page, audit_text, record_admin_event)
from app.extensions import db
from app.models import AdminAuditEvent, Department, Task, TaskCategory, TaskComment, TaskEvent, TaskNotification, User
from support.portal_fixtures import PortalCase

NOW = datetime(2030, 1, 10, 10, tzinfo=timezone.utc)
START = datetime(2030, 1, 9, 19, tzinfo=timezone.utc)  # Jan 10, 00:00 Asia/Qyzylorda


class AuditTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.task_id = self.task(title="Проверить журнал")

    def login(self, username="root", password=None, **kwargs):
        # Login does not switch an already authenticated session; isolate role checks.
        self.client = self.app.test_client()
        return super().login(username, password, **kwargs)

    def entry(self, source="task", *, who="employee", at=NOW, snapshot=None, details=None, **values):
        with self.app.app_context():
            actor = db.session.get(User, self.ids[who])
            common = dict(actor_id=actor.id, actor_snapshot=actor_snapshot(actor) if snapshot is None else snapshot,
                details=details if details is not None else {"comment":"Пояснение действия"}, created_at=at)
            row = (TaskEvent(**(dict(task_id=self.task_id, event_type="comment_added") | common | values)) if source == "task"
                else AdminAuditEvent(**(dict(action="account_updated", object_type="user", object_id=actor.id) | common | values)))
            db.session.add(row); db.session.commit()
            return row.id

    def journal(self, who="root", **filters):
        with self.app.app_context():
            return audit_page(db.session.get(User,self.ids[who]), **filters)

    def test_union_preserves_equal_ids_and_timestamps_with_stable_pages(self):
        for source in ("task","admin"):
            for _ in range(3): self.entry(source)
        expected=[("admin",3),("admin",2),("admin",1),("task",3),("task",2),("task",1)]
        pages=[self.journal(page=n,per_page=2) for n in (1,2,3)]
        self.assertEqual(expected,[(e["source"],e["id"]) for p in pages for e in p["entries"]])
        self.assertEqual(6,pages[0]["total"]);self.assertEqual(3,pages[0]["pages"])
        self.assertEqual(pages[1],self.journal(page=2,per_page=2))

    def test_newest_first_across_both_sources(self):
        self.entry("admin",at=NOW-timedelta(days=1));self.entry(at=NOW+timedelta(days=1));self.entry("admin",at=NOW)
        self.assertEqual(["task","admin","admin"],[e["source"] for e in self.journal()["entries"]])

    def test_date_boundaries_include_the_whole_last_local_day(self):
        for moment in (START-timedelta(microseconds=1),START,START+timedelta(days=1,microseconds=-1),START+timedelta(days=1)):
            self.entry(at=moment)
        self.assertEqual(2,self.journal(date_from="2030-01-10",date_to="2030-01-10")["total"])
        self.assertEqual(3,self.journal(date_from="2030-01-10")["total"])
        self.assertEqual(3,self.journal(date_to="2030-01-10")["total"])

    def test_initiator_and_dates_work_separately_and_together(self):
        self.entry();self.entry("admin",who="head");self.entry(who="head",at=START-timedelta(days=1))
        self.assertEqual(2,self.journal(user_id=self.ids["head"])["total"])
        self.assertEqual(2,self.journal(date_from="2030-01-10")["total"])
        result=self.journal(user_id=self.ids["head"],date_from="2030-01-10",date_to="2030-01-10")
        self.assertEqual(1,result["total"]);self.assertEqual("admin",result["entries"][0]["source"])

    def test_deleted_initiator_remains_filterable_by_saved_id(self):
        self.entry(actor_id=None,snapshot={"id":7654321,"name":"Удалённый инициатор","role":"employee","department_name":"Прежний отдел"})
        result=self.journal(user_id=7654321)
        self.assertEqual(1,result["total"])
        self.assertIn({"id":7654321,"name":"Удалённый инициатор"},result["users"])

    def test_identity_changes_do_not_rewrite_saved_event_or_filter_history(self):
        self.entry(snapshot={"id":self.ids["employee"],"name":"Прежнее имя","role":"employee","department_name":"Прежний отдел"})
        with self.app.app_context():
            user=db.session.get(User,self.ids["employee"]);user.full_name="Новое имя";user.role="head";user.department_id=self.third_department_id
            db.session.commit()
        row=self.journal()["entries"][0]
        self.assertEqual(("Прежнее имя","Сотрудник отдела","Прежний отдел"),(row["actor_name"],row["actor_role"],row["actor_department"]))
        self.assertIn({"id":self.ids["employee"],"name":"Прежнее имя"},self.journal()["users"])

    def test_missing_historical_snapshot_does_not_invent_current_name_or_role(self):
        self.entry(snapshot={})
        with self.app.app_context():
            row=TaskEvent.query.one();row.actor_snapshot=None;db.session.commit()
        row=self.journal()["entries"][0]
        self.assertEqual("Имя инициатора неизвестно",row["actor_name"])
        self.assertEqual("Историческая роль неизвестна",row["actor_role"])
        self.assertEqual("Отдел на момент действия неизвестен",row["actor_department"])

    def test_partial_snapshot_keeps_known_name_without_guessing_role(self):
        self.entry(snapshot={"name":"Старое имя"})
        row=self.journal()["entries"][0]
        self.assertEqual("Старое имя",row["actor_name"]);self.assertEqual("Историческая роль неизвестна",row["actor_role"])

    def test_malformed_filter_name_is_not_serialized_and_time_has_utc_offset(self):
        self.entry(snapshot={"id":self.ids["employee"],"name":{"private":"do-not-display"}})
        result=self.journal()
        self.assertEqual("Имя неизвестно",result["users"][0]["name"])
        self.assertEqual(timedelta(0),result["entries"][0]["created_at"].utcoffset())
        self.login();body=self.client.get("/admin/audit").get_data(as_text=True)
        self.assertNotIn("do-not-display",body)
        self.assertIn('datetime="2030-01-10T10:00:00+00:00"',body)

    def test_secret_fields_and_credential_text_are_not_exposed(self):
        secrets=("hidden-password-value","hidden-token-value","hidden-hash-value","hidden-session-value","hidden-csrf-value","private-db-password")
        self.entry("admin",details={"before":{"password":"hidden-password-value","access_token":"hidden-token-value"},
            "after":{"password_hash":"hidden-hash-value","session_cookie":"hidden-session-value","password_changed":True,
                "full_name":"Безопасное имя"},"comment":"password=hidden-password-value token=hidden-token-value csrf_token=hidden-csrf-value session_id=hidden-session-value postgresql://user:private-db-password@localhost/db"})
        self.login();response=self.client.get("/admin/audit");self.assertEqual(200,response.status_code)
        body=response.get_data(as_text=True)
        for secret in secrets:self.assertNotIn(secret,body)
        self.assertNotIn("postgresql://",body);self.assertIn("Пароль изменён",body);self.assertIn("Безопасное имя",body)
        self.assertIn("[скрыто]",body)

    def test_session_cookie_auth_stamp_and_authorization_are_hidden_without_rewriting_history(self):
        comment="Пояснение до реквизитов\nCookie: session=cookie-secret; custom_auth=another-secret\nAuthorization: Bearer bearer-secret\n_auth_stamp=stamp-secret apiKey=api-secret session=plain-session-secret\nОбычное пояснение после реквизитов"
        ident=self.entry(details={"comment":comment})
        self.login();page=self.client.get("/admin/audit").get_data(as_text=True)
        for value in ("cookie-secret","another-secret","bearer-secret","stamp-secret","api-secret","plain-session-secret"):
            self.assertNotIn(value,page)
        self.assertIn("Обычное пояснение после реквизитов",page)
        with self.app.app_context():self.assertEqual(comment,db.session.get(TaskEvent,ident).details["comment"])

    def test_new_admin_metadata_never_persists_credential_keys(self):
        from app.audit import record_admin_event
        with self.app.app_context():
            row=record_admin_event("account_updated","user",self.ids["employee"],actor=db.session.get(User,self.ids["root"]),
                before={"nested":{"session":"session-secret","_auth_stamp":"stamp-secret","apiKey":"api-secret","Cookie":"cookie-secret"}},
                after={"password_changed":True,"full_name":"Допустимое имя","authorization":"authorization-secret"})
            db.session.commit()
            stored=json.dumps(row.details,ensure_ascii=False)
            for secret in ("session-secret","stamp-secret","api-secret","cookie-secret","authorization-secret"):
                self.assertNotIn(secret,stored)
            self.assertTrue(row.details["after"]["password_changed"])
            self.assertEqual("Допустимое имя",row.details["after"]["full_name"])

    def test_head_staff_operations_are_audited_with_saved_actor_and_system_explanation(self):
        from app.accounts import create_account,update_account,set_account_blocked
        with self.app.app_context():
            head=db.session.get(User,self.ids["head"])
            original_name=head.full_name;original_department=head.department.name
            user=create_account(actor=head,full_name="Сотрудник",username="staff-audit",password="staff-secret")
            db.session.commit();ident=user.id
            update_account(actor=head,user_id=ident,full_name="Новое имя сотрудника",username="staff-audit",password="")
            db.session.commit()
            set_account_blocked(actor=head,user_id=ident,blocked=True);db.session.commit()
            head.full_name="Новое имя начальника";head.department.name="Новое имя отдела";db.session.commit()
        rows=self.journal(user_id=self.ids["head"])["entries"]
        self.assertEqual(3,len(rows))
        self.assertEqual({"Учётная запись создана","Учётная запись изменена","Учётная запись отключена"},{row["action"] for row in rows})
        for row in rows:
            self.assertEqual((original_name,"Начальник отдела",original_department),(row["actor_name"],row["actor_role"],row["actor_department"]))
            self.assertIn("Системное пояснение",row["comment"])
            self.assertEqual(ident,row["object_id"])
        self.login();page=self.client.get("/admin/audit",query_string={"user_id":self.ids["head"]}).get_data(as_text=True)
        self.assertNotIn("staff-secret",page)

    def test_current_claim_release_and_rework_fields_have_explicit_labels(self):
        self.entry(event_type="claimed",details={"comment":"Взял", "before":{"status":"new","assignee_ids":None},"after":{"status":"in_progress","assignee_ids":self.ids["employee"]}})
        self.entry(event_type="released",details={"comment":"Снял", "before":{"status":"in_progress"},"after":{"status":"new"}})
        self.entry(event_type="returned_for_revision",details={"comment":"Замечания", "before":{"rework_cycle":1,"is_rework":False},"after":{"rework_cycle":2,"is_rework":True}})
        rows=self.journal()["entries"]
        self.assertEqual({"Сотрудник взял задачу","Исполнитель снял задачу с себя","Задача возвращена на доработку"},{row["action"] for row in rows})
        rework=next(row for row in rows if row["action"]=="Задача возвращена на доработку")
        self.assertIn(("Цикл доработки","1","2"),rework["changes"])
        self.assertIn(("На доработке","Нет","Да"),rework["changes"])

    def test_earlier_workflow_status_values_remain_readable_without_becoming_current_statuses(self):
        self.entry(event_type="status_changed",details={"comment":"Историческая запись", "before":{"status":"accepted"},"after":{"status":"ready"}})
        self.assertIn(("Статус","Принята (прежний процесс)","Готова (прежний процесс)"),self.journal()["entries"][0]["changes"])

    def test_hash_and_bearer_redaction_does_not_destroy_regular_comment(self):
        for value in ("scrypt:32768:8:1$secret$hash", "pbkdf2:sha256:1000$secret$hash", "Bearer abc.def.ghi", "https://name:password@example.test/path"):
            cleaned=audit_text("Документ принят; "+value)
            self.assertIn("Документ принят",cleaned);self.assertNotIn(value,cleaned)

    def test_unknown_legacy_actions_objects_and_malformed_snapshots_are_safe(self):
        self.entry("admin",action="unknown-secret-action",object_type="unknown-secret-object",snapshot={"id":"token=hidden-secret","name":{"password":"hidden-secret"},"role":[]},
            actor_id=None,details={"before":[],"after":[],"comment":{}})
        self.login();response=self.client.get("/admin/audit");self.assertEqual(200,response.status_code)
        body=response.get_data(as_text=True);self.assertIn("Историческое событие",body)
        self.assertNotIn("hidden-secret",body);self.assertNotIn("unknown-secret",body)

    def test_escaped_names_comments_and_changes_no_raw_html(self):
        self.entry(snapshot={"name":"<script>actor()</script>","role":"employee"},details={"comment":"<img src=x onerror=alert(1)>","before":{},"after":{"title":"<script>title()</script>"}})
        self.login();body=self.client.get("/admin/audit").get_data(as_text=True)
        self.assertIn("&lt;script&gt;actor()",body);self.assertNotIn("<script>",body);self.assertNotIn("<img src=x",body)

    def test_readonly_service_and_get_preserve_tasks_events_and_notifications(self):
        self.entry();self.entry("admin")
        def snapshot():
            with self.app.app_context():
                return ([(t.id,t.status,t.revision,t.due_at,t.sent_at,t.completed_at) for t in Task.query.all()],
                    [(e.id,deepcopy(e.details),deepcopy(e.actor_snapshot),e.created_at) for e in TaskEvent.query.all()],
                    [(e.id,deepcopy(e.details),e.created_at) for e in AdminAuditEvent.query.all()],
                    TaskComment.query.count(),TaskNotification.query.count())
        before=snapshot();self.journal();self.login();self.client.get("/admin/audit")
        self.assertEqual(before,snapshot())

    def test_permissions_anonymous_employee_head_and_read_only_routes(self):
        self.entry()
        self.assertEqual(302,self.client.get("/admin/audit").status_code)
        for who in ("employee","head","outsider"):
            self.login(who);response=self.client.get("/admin/audit")
            self.assertEqual(403,response.status_code);self.assertNotIn("Пояснение действия",response.get_data(as_text=True))
            with self.assertRaises(PermissionError):self.journal(who)
        self.login();self.assertEqual(200,self.client.get("/admin/audit").status_code)
        self.assertEqual(405,self.client.post("/admin/audit",data={"delete":"all"}).status_code)
        for method in ("put","patch","delete"):self.assertEqual(405,getattr(self.client,method)("/admin/audit").status_code)

    def test_service_rechecks_stale_role_blocking_and_invalid_actor(self):
        self.login();self.entry()
        with self.app.app_context():
            root=db.session.get(User,self.ids["root"])
            with self.assertRaises(PermissionError):audit_page(SimpleNamespace(id=True))
            with self.assertRaises(PermissionError):audit_page(SimpleNamespace())
            # External administrative changes must invalidate a previously loaded actor.
            db.session.execute(text("UPDATE users SET role='employee', department_id=:dept, is_blocked=1 WHERE id=:id"),
                {"id":root.id,"dept":self.department_id});db.session.commit()
            with self.assertRaises(PermissionError):audit_page(root)
        self.assertNotEqual(200,self.client.get("/admin/audit").status_code)

    def test_invalid_filters_are_clear_400_without_traceback(self):
        self.login()
        for query in ({"user_id":"-1"},{"user_id":"1 OR 1=1"},{"user_id":"9"*80},{"page":"0"},{"page":"1.5"},
            {"date_from":"2030-02-30"},{"date_from":"20300110"},{"date_to":"9999-12-31"},
            {"date_from":"2030-01-11","date_to":"2030-01-10"},[("user_id","1"),("user_id","2")],[("date_to","2030-01-10"),("date_to","2030-01-11")]):
            response=self.client.get("/admin/audit",query_string=query)
            self.assertEqual(400,response.status_code,query);self.assertIn('role="alert"',response.get_data(as_text=True));self.assertNotIn("Traceback",response.get_data(as_text=True))
        for values in ({"per_page":0},{"per_page":101},{"page":True}):
            with self.assertRaises(AuditFilterError):self.journal(**values)

    def test_empty_result_and_out_of_range_page_are_usable(self):
        self.login();body=self.client.get("/admin/audit?user_id=99999&page=999999").get_data(as_text=True)
        self.assertIn("Событий не найдено",body);self.assertIn("Страница 1 из 1",body);self.assertIn("Сбросить",body)

    def test_pagination_preserves_filters_and_reset_is_clean(self):
        for _ in range(21):self.entry()
        self.login();response=self.client.get("/admin/audit",query_string={"user_id":self.ids["employee"],"date_from":"2030-01-10","date_to":"2030-01-10"})
        body=response.get_data(as_text=True);self.assertEqual(200,response.status_code)
        self.assertEqual(20,body.count('class="card audit-entry"'))
        link=unescape(re.search(r'rel="next" href="([^"]+)"',body)[1]);query=parse_qs(urlsplit(link).query)
        self.assertEqual(["2"],query["page"]);self.assertEqual(["2030-01-10"],query["date_to"])
        self.assertEqual([str(self.ids["employee"])],query["user_id"])
        second=self.client.get(link).get_data(as_text=True);self.assertEqual(1,second.count('class="card audit-entry"'))
        self.assertIn('href="/admin/audit">Сбросить',body)

    def test_navigation_has_journal_only_for_admin(self):
        for who,visible in (("root",True),("head",False),("employee",False)):
            self.login(who);body=self.client.get("/tasks/received").get_data(as_text=True)
            self.assertEqual(visible,'href="/admin/audit"' in body)

    def test_all_administrative_changes_have_one_transactional_record(self):
        self.login()
        def post(path,data,action):
            with self.app.app_context(): before=AdminAuditEvent.query.count()
            response=self.client.post(path,data=data);self.assertEqual(302,response.status_code,(path,response.get_data(as_text=True)))
            with self.app.app_context():
                self.assertEqual(before+1,AdminAuditEvent.query.count(),path)
                row=AdminAuditEvent.query.order_by(AdminAuditEvent.id.desc()).first()
                self.assertEqual(action,row.action);self.assertEqual(self.ids["root"],row.actor_id)
                self.assertEqual("admin",row.actor_snapshot["role"]);self.assertNotIn("password_hash",str(row.details))
        post("/admin/users/new",self.form("audit-user"),"account_created")
        ident=self.account("audit-user")["id"]
        post(f"/admin/users/{ident}/edit",self.form("audit-user",full_name="Другое имя",password="new-synthetic-password"),"account_updated")
        post(f"/admin/users/{ident}/block",{},"account_blocked")
        post(f"/admin/users/{ident}/unblock",{},"account_unblocked")
        post(f"/admin/users/{ident}/delete",{},"account_blocked")
        post("/admin/departments/new",{"name":"Аудит отдела"},"department_created")
        with self.app.app_context(): ident=Department.query.filter_by(name="Аудит отдела").one().id
        post(f"/admin/departments/{ident}/edit",{"name":"Аудит переименован"},"department_renamed")
        post(f"/admin/departments/{ident}/archive",{},"department_archived")
        post(f"/admin/departments/{ident}/restore",{},"department_restored")
        post(f"/admin/departments/{ident}/delete",{},"department_deleted")
        # T012 retires management routes; their historical events stay readable.
        with self.app.app_context():
            from app.settings import create_category,rename_category,archive_category,update_upload_policy
            admin=db.session.get(User,self.ids["root"])
            category=create_category(actor=admin,name="Аудит категории")
            rename_category(actor=admin,category_id=category.id,name="Другая категория")
            archive_category(actor=admin,category_id=category.id)
            update_upload_policy(actor=admin,max_upload_mb="10",allowed_extensions=["pdf","png"])
            db.session.commit()
        self.assertEqual(14,self.journal()["total"])

    def test_record_is_rolled_back_with_the_administrative_change(self):
        with self.app.app_context():
            actor=db.session.get(User,self.ids["root"]);user=db.session.get(User,self.ids["employee"])
            old=user.full_name;user.full_name="Не сохранять"
            record_admin_event("account_updated","user",user.id,actor=actor,before={"full_name":old},after={"full_name":user.full_name})
            db.session.flush();db.session.rollback()
            self.assertEqual(old,db.session.get(User,self.ids["employee"]).full_name)
            self.assertEqual(0,AdminAuditEvent.query.count())

    def test_query_count_is_bounded_and_payload_is_paged(self):
        for _ in range(24):self.entry()
        with self.app.app_context():
            statements=[]
            def capture(conn,cursor,statement,parameters,context,executemany):statements.append(statement)
            event.listen(db.engine,"before_cursor_execute",capture)
            try:
                result=audit_page(db.session.get(User,self.ids["root"]),per_page=2)
                self.assertEqual(2,len(result["entries"]));self.assertEqual(24,result["total"])
                self.assertLessEqual(len(statements),6)
            finally:event.remove(db.engine,"before_cursor_execute",capture)


@skipUnless(os.environ.get("PORTAL_TEST_DATABASE_URL"),"Требуется безопасный runner PostgreSQL")
class PostgreSQLAuditTests(TestCase):
    def setUp(self):
        self.schema="portal_case_"+uuid4().hex;self.control=create_engine(os.environ["PORTAL_TEST_DATABASE_URL"])
        with self.control.begin() as conn:conn.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.app=create_app({"TESTING":True,"DATABASE":os.environ["PORTAL_TEST_DATABASE_URL"],
            "SQLALCHEMY_ENGINE_OPTIONS":{"connect_args":{"options":f"-csearch_path={self.schema}"}},
            "SECRET_KEY":"synthetic-audit","ADMIN_PASSWORD":"synthetic-password"})
        self.ctx=self.app.app_context();self.ctx.push();self.root=User.query.filter_by(role="admin").one()
        departments=Department.query.order_by(Department.id).all()
        task=Task(title="Аудит PostgreSQL",description="Проверка",author_id=self.root.id,sender_department_id=departments[0].id,
                  recipient_department_id=departments[1].id,status="new",due_at=NOW,sent_at=NOW)
        db.session.add(task);db.session.flush();self.task_id=task.id;db.session.commit()

    def tearDown(self):
        db.session.remove();db.engine.dispose();self.ctx.pop()
        with self.control.begin() as conn:conn.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        self.control.dispose()

    def test_native_union_json_identity_ties_and_local_date_bounds(self):
        for moment in (START-timedelta(microseconds=1),START,START+timedelta(days=1,microseconds=-1),START+timedelta(days=1)):
            for model in (TaskEvent,AdminAuditEvent):
                common=dict(actor_id=None,actor_snapshot={"id":7654321,"name":"Исторический инициатор","role":"head"},created_at=moment,details={"comment":"Объяснение"})
                db.session.add(TaskEvent(task_id=self.task_id,event_type="comment_added",**common) if model is TaskEvent
                    else AdminAuditEvent(action="department_renamed",object_type="department",object_id=1,**common))
        db.session.commit()
        first=audit_page(self.root,user_id=7654321,date_from="2030-01-10",date_to="2030-01-10",per_page=2)
        second=audit_page(self.root,user_id=7654321,date_from="2030-01-10",date_to="2030-01-10",per_page=2,page=2)
        self.assertEqual(4,first["total"]);self.assertEqual(2,first["pages"])
        self.assertEqual(["admin","task"],[r["source"] for r in first["entries"]])
        self.assertEqual(["admin","task"],[r["source"] for r in second["entries"]])
        self.assertTrue(all(r["actor_name"]=="Исторический инициатор" for r in first["entries"]+second["entries"]))
        self.assertEqual([{"id":7654321,"name":"Исторический инициатор"}],first["users"])
