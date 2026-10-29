"""Retired management URLs preserve directory data, history and fixed upload policy."""
from pathlib import Path
from tempfile import TemporaryDirectory
from sqlalchemy import select
from app.extensions import db
from app.models import AdminAuditEvent,SystemSettings,Task,TaskAttachment,TaskCategory,User
from app.settings import (SettingsError,SUPPORTED_EXTENSIONS,active_categories,create_category,rename_category,archive_category,
    get_upload_policy,update_upload_policy,validate_upload_policy)
from support.portal_fixtures import PortalCase


class SystemSettingsTests(PortalCase):
    def setUp(self):super().setUp();self.login()

    def category(self,name="Оборудование"):
        with self.app.app_context():
            row=create_category(actor=db.session.get(User,self.ids["root"]),name=name);db.session.commit();return row.id

    def snapshot(self):
        with self.app.app_context():
            return {m.__tablename__:db.session.execute(select(m.__table__).order_by(m.id)).all() for m in (TaskCategory,SystemSettings,AdminAuditEvent,Task,TaskAttachment)}

    def retired(self,ident=9999):
        return (("GET","/admin/categories"),("POST","/admin/categories/new"),("POST",f"/admin/categories/{ident}/edit"),
            ("POST",f"/admin/categories/{ident}/archive"),("GET","/admin/settings"),("POST","/admin/settings"))

    def test_retired_admin_get_and_post_are_410_without_changes(self):
        ident=self.category();self.task(category_id=ident,category="Оборудование");before=self.snapshot()
        for method,path in self.retired(ident):
            response=self.client.open(path,method=method,data=dict(name="Подмена",max_upload_mb="1",allowed_extensions="pdf"))
            self.assertEqual(410,response.status_code);self.assertIn("больше не используется",response.get_data(as_text=True));self.assertEqual(before,self.snapshot())

    def test_role_and_anonymous_checks_remain_before_retirement(self):
        for actor in ("head","employee","outsider",None):
            client=self.app.test_client()
            if actor:self.login(actor,client=client)
            for method,path in self.retired():
                response=client.open(path,method=method,data={"name":"Обход"})
                self.assertEqual(403 if actor else 302,response.status_code)
        self.assertEqual([],self.snapshot()["task_categories"])

    def test_retired_posts_keep_csrf_and_do_not_enable_other_verbs(self):
        self.app.config["WTF_CSRF_ENABLED"]=True
        for method,path in self.retired():
            if method=="POST":self.assertEqual(400,self.client.post(path).status_code)
            self.assertEqual(405,self.client.put(path).status_code)

    def test_default_policy_has_fixed_limit_without_creating_settings(self):
        before=self.snapshot()
        with self.app.app_context():
            self.assertEqual(0,SystemSettings.query.count());self.assertEqual(20*1024*1024,get_upload_policy().max_file_bytes)
            self.assertEqual(SUPPORTED_EXTENSIONS,get_upload_policy().allowed_extensions)
        self.assertEqual(21*1024*1024,self.app.config["MAX_CONTENT_LENGTH"]);self.assertEqual(before,self.snapshot())

    def test_saved_legacy_policy_is_preserved_but_cannot_reduce_or_expand_current_formats(self):
        with self.app.app_context():
            row=SystemSettings(id=1,max_upload_mb=1,allowed_extensions=["pdf"]);db.session.add(row);db.session.commit()
            policy=get_upload_policy();self.assertEqual(20,policy.max_upload_mb);self.assertEqual(SUPPORTED_EXTENSIONS,policy.allowed_extensions)
        before=self.snapshot();self.assertEqual(410,self.client.post("/admin/settings",data=dict(max_upload_mb="20",allowed_extensions=["exe"])).status_code)
        self.assertEqual(before,self.snapshot())

    def test_archived_categories_not_offered_but_existing_category_stays_optional(self):
        visible=self.category();archived=self.category("Архивная")
        with self.app.app_context():archive_category(actor=db.session.get(User,self.ids["root"]),category_id=archived);db.session.commit()
        client=self.app.test_client();self.login("employee",client=client)
        page=client.get("/tasks/new").get_data(as_text=True)
        self.assertIn('name="category_id"',page);self.assertIn("Без категории",page);self.assertIn("Оборудование",page);self.assertNotIn("Архивная",page)
        for category in (str(visible),""):
            response=client.post("/tasks/new",data=self.creation_form(client=client,category_id=category));self.assertEqual(302,response.status_code)

    def test_unknown_or_archived_category_cannot_be_submitted(self):
        archived=self.category()
        with self.app.app_context():archive_category(actor=db.session.get(User,self.ids["root"]),category_id=archived);db.session.commit()
        client=self.app.test_client();self.login("employee",client=client)
        for category in (str(archived),"99999"):
            response=client.post("/tasks/new",data=self.creation_form(client=client,category_id=category));self.assertEqual(400,response.status_code)
        with self.app.app_context():self.assertEqual(0,Task.query.count())

    def test_legacy_category_name_snapshot_and_history_survive_retirement(self):
        ident=self.category();task_id=self.task(category_id=ident,category="Оборудование")
        with self.app.app_context():rename_category(actor=db.session.get(User,self.ids["root"]),category_id=ident,name="Новое название");db.session.commit()
        before=self.snapshot();client=self.app.test_client();self.login("employee",client=client)
        page=client.get(f"/tasks/{task_id}").get_data(as_text=True);self.assertIn("Оборудование",page)
        self.assertEqual(before,self.snapshot())
        audit=self.client.get("/admin/audit").get_data(as_text=True);self.assertIn("Новое название",audit);self.assertIn("Оборудование",audit)

    def test_old_files_remain_downloadable_when_legacy_policy_is_restrictive(self):
        task_id=self.task()
        with TemporaryDirectory(prefix="portal_t012_file_") as folder:
            self.app.config["TASK_UPLOAD_DIR"]=folder;key="a"*32;payload=b"synthetic existing file";Path(folder,key).write_bytes(payload)
            with self.app.app_context():
                db.session.add(SystemSettings(id=1,max_upload_mb=1,allowed_extensions=[]))
                attachment=TaskAttachment(task_id=task_id,uploaded_by_id=self.ids["employee"],original_filename="old.xlsx",storage_key=key,content_type="application/octet-stream",size_bytes=len(payload));db.session.add(attachment);db.session.commit();aid=attachment.id
            before=self.snapshot();self.assertEqual(410,self.client.post("/admin/settings",data=dict(max_upload_mb="1")).status_code)
            with self.client.get(f"/tasks/attachments/{aid}/download") as response:self.assertEqual(payload,response.data)
            self.assertEqual(payload,Path(folder,key).read_bytes());self.assertEqual(before,self.snapshot())

    def test_internal_legacy_services_keep_admin_guard_and_name_validation(self):
        ident=self.category("Straße")
        with self.app.app_context():
            admin=db.session.get(User,self.ids["root"]);archive_category(actor=admin,category_id=ident);db.session.commit()
            for name in ("STRASSE","", " ","x"*81):
                with self.assertRaises(SettingsError):create_category(actor=admin,name=name)
            for name in ("employee","head","outsider"):
                with self.assertRaises(PermissionError):create_category(actor=db.session.get(User,self.ids[name]),name="Недоступно")
            db.session.rollback();self.assertEqual([],active_categories())

    def test_legacy_services_still_keep_existing_audit_data(self):
        ident=self.category()
        with self.app.app_context():
            admin=db.session.get(User,self.ids["root"])
            for _ in range(2):rename_category(actor=admin,category_id=ident,name="Переименовано");archive_category(actor=admin,category_id=ident)
            update_upload_policy(actor=admin,max_upload_mb="10",allowed_extensions=["pdf"]);db.session.commit()
            self.assertEqual(["category_created","category_renamed","category_archived","upload_policy_updated"],[e.action for e in AdminAuditEvent.query.order_by(AdminAuditEvent.id)])
            self.assertEqual(20,get_upload_policy().max_upload_mb)

    def test_invalid_legacy_policy_values_do_not_change_storage(self):
        before=self.snapshot()
        for limit,formats in ((0,["pdf"]),(21,["pdf"]),("bad",["png"]),(10,["exe"])):
            with self.assertRaises(SettingsError):validate_upload_policy(limit,formats)
        self.assertEqual(before,self.snapshot())
