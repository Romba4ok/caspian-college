"""Private comment/action attachments, fixed policy and request-wide rollback."""
from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import tempfile
from unittest.mock import patch
import re

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.datastructures import FileStorage

from app.extensions import db
from app.models import Task, TaskAttachment, TaskComment, TaskEvent, TaskNotification, User, SystemSettings
from app.storage import UploadBatch, PreparedUpload, UploadError, attachment_path
from app.workflow import add_task_attachment, _save_action_files, WorkflowError
from support.portal_fixtures import PortalCase
from test_tasks import attachment_bytes


class CommentAttachmentTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.directory=tempfile.TemporaryDirectory(prefix="portal_case_")
        self.app.config["TASK_UPLOAD_DIR"]=self.directory.name
        self.add_member("receiver",department_id=self.other_department_id)
        self.add_member("receiver-head",role="head",department_id=self.other_department_id)
        self.fresh=self.task()
        self.sent=self.task(status="in_progress",sent_at=datetime.now(timezone.utc),
            assignee_id=self.ids["receiver"])

    def tearDown(self):
        super().tearDown()
        self.directory.cleanup()

    def login(self,username="employee",password=None,**kwargs):
        self.client=self.app.test_client()
        with self.client.session_transaction() as session:
            session["_user_id"]=str(self.ids[username]);session["_fresh"]=True

    def revision(self,task_id):
        with self.app.app_context():return db.session.get(Task,task_id).revision

    def snapshot(self):
        with self.app.app_context():
            return {m.__tablename__:deepcopy(db.session.execute(select(m.__table__).order_by(m.id)).all())
                for m in (Task,TaskComment,TaskAttachment,TaskEvent,TaskNotification)}

    def files(self):
        return {str(p.relative_to(self.directory.name)):p.read_bytes() for p in Path(self.directory.name).rglob("*") if p.is_file()}

    def upload(self,extension=".pdf",name=None,data=None):
        return (BytesIO(attachment_bytes(extension) if data is None else data),name or "report"+extension)

    def post(self,task_id=None,actor="employee",path=None,comment="Материалы к обсуждению",files=None,extra=None):
        task_id=task_id or self.sent
        self.login(actor)
        data={"revision":str(self.revision(task_id)),"comment":comment}
        if files is not None:data["attachment"]=files
        data.update(extra or {})
        response = self.client.post(path or f"/tasks/{task_id}/comments",data=data)
        response.request.environ["wsgi.input"].close()
        return response

    def policy(self,formats,limit=20):
        with self.app.app_context():
            row=db.session.get(SystemSettings,1)
            if row is None:row=SystemSettings(id=1);db.session.add(row)
            row.max_upload_mb=limit;row.allowed_extensions=formats
            db.session.commit()

    def test_six_real_formats_are_linked_to_one_comment_and_one_event(self):
        formats=(".pdf",".docx",".xlsx",".pptx",".jpg",".png")
        response=self.post(files=[self.upload(ext) for ext in formats])
        self.assertEqual(302,response.status_code,response.get_data(as_text=True))
        with self.app.app_context():
            c=TaskComment.query.filter_by(task_id=self.sent).one()
            event=TaskEvent.query.filter_by(task_id=self.sent).one()
            rows=TaskAttachment.query.filter_by(task_id=self.sent).all()
            self.assertEqual(6,len(rows))
            self.assertEqual({c.id},{f.comment_id for f in rows})
            self.assertEqual(c.id,event.comment_id)
            self.assertEqual(6,len(event.details["attachments"]))
            self.assertEqual("in_progress",db.session.get(Task,self.sent).status)
            ids=[(f.id,f.original_filename) for f in rows]
        for ident,name in ids:
            response=self.client.get(f"/tasks/attachments/{ident}/download")
            self.assertEqual(200,response.status_code)
            self.assertIn("private, no-store",response.headers["Cache-Control"])
            response.close()
        page=self.client.get(f"/tasks/{self.sent}").get_data(as_text=True)
        self.assertIn('aria-label="Файлы комментария"',page)
        for _,name in ids:self.assertIn(name,page)
        self.assertFalse((Path(self.directory.name)/".pending").exists())

    def test_plain_comment_does_not_change_active_status_or_executor(self):
        for ident in (self.fresh,self.sent):
            with self.app.app_context():
                task=db.session.get(Task,ident);before=(task.status,task.assignee_id,task.due_at)
            self.assertEqual(302,self.post(task_id=ident,actor="receiver").status_code)
            with self.app.app_context():
                task=db.session.get(Task,ident)
                self.assertEqual(before,(task.status,task.assignee_id,task.due_at))
                self.assertEqual(0,TaskAttachment.query.count())

    def test_initial_multiple_files_share_description_without_duplicate_comments(self):
        self.login()
        form=self.creation_form(attachment=[self.upload(".pdf"),self.upload(".png")])
        response=self.client.post("/tasks/new",data=form)
        self.assertEqual(302,response.status_code,response.get_data(as_text=True))
        ident=int(response.headers["Location"].rsplit("/",1)[1])
        with self.app.app_context():
            c=TaskComment.query.filter_by(task_id=ident).one()
            event=TaskEvent.query.filter_by(task_id=ident).one()
            self.assertEqual(form["description"],c.body)
            self.assertEqual(2,TaskAttachment.query.filter_by(task_id=ident,comment_id=c.id).count())
            self.assertEqual(c.id,event.comment_id)
            self.assertEqual("new",db.session.get(Task,ident).status)

    def test_removed_edit_approval_and_review_cannot_attach_files(self):
        before=self.snapshot()
        for suffix in ("edit","approval/submit","approval/approve","review/submit_result","review/complete"):
            self.assertEqual(410,self.post(path=f"/tasks/{self.fresh}/{suffix}",files=self.upload()).status_code)
        self.assertEqual(before,self.snapshot());self.assertEqual({},self.files())

    def test_completion_return_and_repeated_result_files_belong_to_each_action(self):
        for actor,action,files in (("receiver","complete",[self.upload(),self.upload(".docx")]),
                                  ("head","return",self.upload(".png")),("receiver","complete",self.upload())):
            self.assertEqual(302,self.post(actor=actor,path=f"/tasks/{self.sent}/{action}",files=files).status_code)
        with self.app.app_context():
            task=db.session.get(Task,self.sent)
            self.assertEqual(("completed",1),(task.status,task.rework_cycle))
            events=TaskEvent.query.filter_by(task_id=self.sent).order_by(TaskEvent.id).all()
            self.assertEqual([2,1,1],[len(e.details["attachments"]) for e in events])
            for event in events:
                self.assertEqual(len(event.details["attachments"]),TaskAttachment.query.filter_by(comment_id=event.comment_id).count())

    def test_assignment_starts_work_and_keeps_its_comment_file(self):
        self.assertEqual(302,self.post(task_id=self.fresh,actor="receiver-head",path=f"/tasks/{self.fresh}/assign",
            files=self.upload(),extra={"assignee_ids":str(self.ids["receiver"])}).status_code)
        with self.app.app_context():
            self.assertEqual("in_progress",db.session.get(Task,self.fresh).status)
            self.assertEqual(TaskEvent.query.one().comment_id,TaskAttachment.query.one().comment_id)

    def test_invalid_second_file_rolls_back_everything_and_cleans_staging(self):
        before=self.snapshot()
        response=self.post(files=[self.upload(),self.upload(".png",data=b"forged image")])
        self.assertEqual(400,response.status_code)
        self.assertEqual(before,self.snapshot());self.assertEqual({},self.files())
        self.assertEqual([],list(Path(self.directory.name).iterdir()))

    def test_corrupted_or_forged_all_formats_and_bad_names_are_rejected(self):
        for ext in (".pdf",".docx",".xlsx",".pptx",".jpg",".png"):
            before=self.snapshot()
            self.assertEqual(400,self.post(files=self.upload(ext,data=b"forged")).status_code,ext)
            self.assertEqual(before,self.snapshot());self.assertEqual({},self.files())
        self.assertEqual(400,self.post(files=self.upload(name="forbidden.exe")).status_code)
        self.assertEqual(400,self.post(files=self.upload(data=b"")).status_code)

    def test_empty_comment_with_files_is_not_an_action(self):
        before=self.snapshot()
        self.assertEqual(400,self.post(comment=" \n ",files=self.upload()).status_code)
        self.assertEqual(before,self.snapshot());self.assertEqual({},self.files())

    def test_foreign_comment_id_or_task_id_fields_cannot_rebind_files(self):
        before=self.snapshot()
        for fields in ({"comment_id":"999"},{"task_id":str(self.fresh)},{"uploaded_by_id":"1"},
                       {"storage_key":"a"*32}):
            self.assertEqual(400,self.post(files=self.upload(),extra=fields).status_code)
            self.assertEqual(before,self.snapshot());self.assertEqual({},self.files())

    def test_service_refuses_foreign_comment_and_untrusted_metadata(self):
        with self.app.app_context():
            foreign=TaskComment(task_id=self.fresh,author_id=self.ids["employee"],body="Чужая задача")
            db.session.add(foreign);db.session.flush()
            event=TaskEvent(task_id=self.fresh,actor_id=self.ids["employee"],comment_id=foreign.id,event_type="comment_added",details={})
            db.session.add(event);db.session.commit()
            task=db.session.get(Task,self.sent);actor=db.session.get(User,self.ids["employee"])
            with UploadBatch() as batch:
                batch.prepare(FileStorage(stream=BytesIO(attachment_bytes(".pdf")),filename="sample.pdf"))
                with self.assertRaises(WorkflowError):_save_action_files(task,actor,event,batch.prepared)
            with self.assertRaises(WorkflowError):
                add_task_attachment(task=task,actor=actor,metadata={"comment_id":foreign.id})
            db.session.rollback()
        self.assertEqual({},self.files())

    def test_fixed_twenty_mb_boundary_ignores_legacy_settings(self):
        self.policy(["pdf"],1)
        base=attachment_bytes(".png")
        allowed=base+b"\0"*(20*1024*1024-len(base))
        self.assertEqual(302,self.post(files=self.upload(".png",data=allowed)).status_code)
        before=self.snapshot();old=self.files()
        self.assertEqual(413,self.post(files=self.upload(".png",data=allowed+b"x")).status_code)
        self.assertEqual(before,self.snapshot());self.assertEqual(old,self.files())
        self.assertEqual(302,self.post(files=self.upload(".pdf")).status_code)

    def test_whole_request_stays_at_21_mb_and_does_not_create_partial_files(self):
        base=attachment_bytes(".png")
        part=base+b"\0"*(11*1024*1024-len(base))
        before=self.snapshot()
        self.assertEqual(413,self.post(files=[self.upload(".png",data=part),self.upload(".png",data=part)]).status_code)
        self.assertEqual(before,self.snapshot());self.assertEqual({},self.files())
        self.assertEqual(21*1024*1024,self.app.config["MAX_CONTENT_LENGTH"])

    def test_legacy_empty_formats_do_not_disable_files_text_or_existing_downloads(self):
        self.assertEqual(302,self.post(files=self.upload()).status_code)
        with self.app.app_context():ident=TaskAttachment.query.one().id
        self.policy([])
        self.assertEqual(302,self.post(files=self.upload(".png")).status_code)
        self.assertEqual(302,self.post(comment="Только текст").status_code)
        response=self.client.get(f"/tasks/attachments/{ident}/download")
        self.assertEqual(200,response.status_code);response.close()
        with self.app.app_context():self.assertEqual([],db.session.get(SystemSettings,1).allowed_extensions)

    def test_staged_size_or_content_tampering_is_rejected_and_cleaned(self):
        before=self.snapshot()
        for resize in (False,True):
            with self.app.app_context(),self.assertRaises(UploadError):
                with UploadBatch() as batch:
                    batch.prepare(FileStorage(stream=BytesIO(attachment_bytes(".pdf")),filename="sample.pdf"))
                    item=batch.prepared[0]
                    item.path.write_bytes(b"x"*(item.size+(1 if resize else 0)))
                    item.install()
            self.assertEqual(before,self.snapshot());self.assertEqual({},self.files())

    def test_macro_office_documents_cannot_disguise_as_allowed_formats(self):
        from zipfile import ZipFile
        for ext in (".docx", ".xlsx", ".pptx"):
            source=BytesIO(attachment_bytes(ext));target=BytesIO()
            with ZipFile(source) as original,ZipFile(target,"w") as output:
                for name in original.namelist():output.writestr(name,original.read(name))
                output.writestr("vbaProject.bin",b"synthetic macro")
            before=self.snapshot()
            self.assertEqual(400,self.post(files=self.upload(ext,data=target.getvalue())).status_code)
            self.assertEqual(before,self.snapshot());self.assertEqual({},self.files())

    def test_unbound_older_attachment_remains_readable_with_unchanged_bytes(self):
        from app.storage import save_upload
        original=attachment_bytes(".pdf")
        with self.app.app_context():
            metadata=save_upload(FileStorage(stream=BytesIO(original),filename="Прежний.pdf"))
            row=TaskAttachment(task_id=self.sent,uploaded_by_id=self.ids["employee"],comment_id=None,**metadata)
            db.session.add(row);db.session.commit();ident=row.id;key=row.storage_key
        self.policy([])
        self.assertEqual(302,self.post(files=self.upload(".png")).status_code)
        with self.app.app_context():
            row=db.session.get(TaskAttachment,ident)
            self.assertIsNone(row.comment_id);self.assertEqual(key,row.storage_key)
        self.assertEqual(original,Path(self.directory.name,key).read_bytes())
        self.login("receiver")
        response=self.client.get(f"/tasks/attachments/{ident}/download")
        self.assertEqual(200,response.status_code);self.assertEqual(original,response.data);response.close()
        self.assertIn("Прежний.pdf",self.client.get(f"/tasks/{self.sent}").get_data(as_text=True))

    def test_invalid_legacy_extension_setting_neither_expands_nor_breaks_fixed_policy(self):
        self.policy(["exe"],1)
        with self.app.app_context():
            from app.settings import get_upload_policy,SUPPORTED_EXTENSIONS
            self.assertEqual(SUPPORTED_EXTENSIONS,get_upload_policy().allowed_extensions)
            self.assertEqual(20,get_upload_policy().max_upload_mb)
            self.assertEqual(["exe"],db.session.get(SystemSettings,1).allowed_extensions)
        self.assertEqual(400,self.post(files=self.upload(name="file.exe")).status_code)
        self.assertEqual(302,self.post(files=self.upload(".png")).status_code)

    def test_open_form_and_new_upload_use_fixed_policy_after_legacy_settings_change(self):
        self.login()
        self.assertEqual(200,self.client.get(f"/tasks/{self.sent}").status_code)
        self.policy(["png"],1)
        self.assertEqual(302,self.client.post(f"/tasks/{self.sent}/comments",data={
            "revision":"0","comment":"Та же фиксированная политика","attachment":self.upload()}).status_code)
        with self.app.app_context():
            from app.settings import get_upload_policy
            policy=get_upload_policy()
            self.assertEqual(20,policy.max_upload_mb)
            self.assertEqual(6,len(policy.allowed_extensions))

    def test_commit_failure_removes_only_new_files_and_keeps_old_materials(self):
        self.assertEqual(302,self.post(files=self.upload()).status_code)
        before=self.snapshot();old=self.files()
        with patch("app.tasks.db.session.commit",side_effect=SQLAlchemyError("synthetic failure")):
            response=self.post(files=[self.upload(".png"),self.upload(".docx")])
        self.assertEqual(500,response.status_code)
        self.assertEqual(before,self.snapshot());self.assertEqual(old,self.files())

    def test_second_install_failure_rolls_back_result_and_first_file(self):
        original=PreparedUpload.install
        calls=[]
        def failing(item):
            calls.append(item)
            if len(calls)==2:raise UploadError("Сбой второго файла")
            return original(item)
        before=self.snapshot()
        with patch("app.storage.PreparedUpload.install",new=failing):
            response=self.post(actor="receiver",path=f"/tasks/{self.sent}/complete",
                files=[self.upload(),self.upload(".png")])
        self.assertEqual(400,response.status_code)
        self.assertEqual(before,self.snapshot());self.assertEqual({},self.files())

    def test_disk_copy_failure_is_safe_without_traceback_or_partial_file(self):
        before=self.snapshot()
        with patch("shutil.copyfileobj",side_effect=OSError("synthetic disk detail")):
            response=self.post(files=self.upload())
        self.assertEqual(500,response.status_code)
        self.assertNotIn("synthetic disk detail",response.get_data(as_text=True))
        self.assertEqual(before,self.snapshot());self.assertEqual({},self.files())

    def test_attachment_event_failure_rolls_back_new_comment_and_files(self):
        before=self.snapshot()
        with patch("app.workflow.TaskAttachment",side_effect=SQLAlchemyError("synthetic metadata failure")):
            response=self.post(files=self.upload())
        self.assertEqual(500,response.status_code)
        self.assertEqual(before,self.snapshot());self.assertEqual({},self.files())

    def test_new_task_files_are_private_to_both_departments_and_admin(self):
        self.assertEqual(302,self.post(task_id=self.fresh,files=self.upload()).status_code)
        with self.app.app_context():ident=TaskAttachment.query.one().id
        for actor in ("employee","head","receiver","receiver-head","root"):
            self.login(actor)
            response=self.client.get(f"/tasks/attachments/{ident}/download")
            self.assertEqual(200,response.status_code);response.close()
        self.login("outsider")
        self.assertEqual(404,self.client.get(f"/tasks/attachments/{ident}/download").status_code)

    def test_closed_tasks_and_admin_cannot_comment_or_upload_but_can_download(self):
        self.assertEqual(302,self.post(files=self.upload()).status_code)
        with self.app.app_context():
            ident=TaskAttachment.query.one().id
            db.session.get(Task,self.sent).status="completed"
            db.session.commit()
        before=self.snapshot();old=self.files()
        for name in ("employee","receiver","head","root"):
            self.assertEqual(403,self.post(actor=name,files=self.upload()).status_code)
            self.assertEqual(403,self.post(actor=name,path=f"/tasks/{self.sent}/attachments",files=self.upload()).status_code)
        response=self.client.get(f"/tasks/attachments/{ident}/download")
        self.assertEqual(200,response.status_code);response.close()
        self.assertEqual(before,self.snapshot());self.assertEqual(old,self.files())

    def test_stale_comment_never_installs_files_or_duplicates_events(self):
        self.assertEqual(302,self.post().status_code)
        before=self.snapshot()
        self.assertEqual(409,self.post(files=self.upload(),extra={"revision":"0"}).status_code)
        self.assertEqual(before,self.snapshot());self.assertEqual({},self.files())

    def test_filename_is_escaped_and_download_is_safe_from_path_traversal(self):
        self.assertEqual(302,self.post(files=self.upload(name='../<svg-onload>.pdf')).status_code)
        page=self.client.get(f"/tasks/{self.sent}").get_data(as_text=True)
        self.assertIn("&lt;svg-onload&gt;.pdf",page)
        self.assertNotIn("<svg-onload>.pdf",page)
        with self.app.app_context():
            row=TaskAttachment.query.one()
            self.assertRegex(row.storage_key,r"^[a-f0-9]{32}$")
            with self.assertRaises(UploadError):attachment_path("../secret")
            self.assertEqual("<svg-onload>.pdf",row.original_filename)

    def test_upload_alias_requires_comment_and_version_and_csrf(self):
        before=self.snapshot()
        self.assertEqual(400,self.post(path=f"/tasks/{self.sent}/attachments",comment="",files=self.upload()).status_code)
        self.assertEqual(before,self.snapshot())
        self.login();self.app.config["WTF_CSRF_ENABLED"]=True
        page=self.client.get(f"/tasks/{self.sent}").get_data(as_text=True)
        self.assertEqual(400,self.client.post(f"/tasks/{self.sent}/comments",data={
            "revision":"0","comment":"Без CSRF","attachment":self.upload()}).status_code)
        token=re.search(r'name="csrf_token" value="([^"]+)"',page).group(1)
        self.assertEqual(302,self.client.post(f"/tasks/{self.sent}/attachments",data={
            "csrf_token":token,"revision":"0","comment":"Документы","attachment":self.upload()}).status_code)

