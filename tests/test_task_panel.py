"""Private read-only preview uses the detail page's data and permissions."""
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory

from app.audit import record_task_event
from app.extensions import db
from app.models import Department, Task, TaskAttachment, TaskComment, TaskEvent, TaskNotification, User
from support.portal_fixtures import PortalCase


class PanelMarkup(HTMLParser):
    def __init__(self, html):
        super().__init__(); self.tags=[]; self.links=[]; self.feed(html)
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs); self.tags.append((tag,attrs))
        if tag=='a': self.links.append(attrs.get('href',''))


class TaskPanelTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.add_member('worker',department_id=self.other_department_id)
        self.add_member('receiver-head',role='head',department_id=self.other_department_id)

    def as_user(self,name='employee'):
        self.client=self.app.test_client(); self.login(name)

    def panel(self, task_id, expected=200):
        response=self.client.get(f'/tasks/{task_id}/panel')
        self.assertEqual(expected,response.status_code)
        self.assertIn('no-store',response.headers.get('Cache-Control',''))
        self.assertIn('Cookie',response.headers.get('Vary',''))
        return response

    def sent(self, **changes):
        return self.task(status='in_progress',sent_at=datetime(2029,1,1,tzinfo=timezone.utc),
            assignee_id=self.ids['worker'],**changes)

    def test_shared_description_participants_dates_and_history(self):
        task_id=self.sent(title='Проверить весь комплект',description='Полное описание\nВторая строка',
            category='Отчётность',contact_person='Контактное лицо отдела')
        with self.app.app_context():
            task=db.session.get(Task,task_id)
            record_task_event(task=task,actor=db.session.get(User,self.ids['head']),event_type='work_started',comment='Проверяем комплектность')
            db.session.commit()
        self.as_user('receiver-head')
        panel=self.panel(task_id).get_data(as_text=True)
        full=self.client.get(f'/tasks/{task_id}').get_data(as_text=True)
        for text in ['Проверить весь комплект','Полное описание\nВторая строка','Контактное лицо отдела',
            'Отчётность','Проверяем комплектность','Работа начата','Сотрудник','worker','Создана','Поручение отправлено другому отделу:']:
            self.assertIn(text,panel); self.assertIn(text,full)
        self.assertIn(f'href="/tasks/{task_id}#task-permitted-actions"',panel)
        self.assertIn('id="task-permitted-actions"',full)
        self.assertEqual(str(task_id),self.panel(task_id).headers['X-Task-Panel'])
        self.assertFalse(any(t in ('form','script','html','input','textarea') for t,a in PanelMarkup(panel).tags))

    def test_comments_files_use_the_original_private_download_and_same_links(self):
        task_id=self.sent()
        with TemporaryDirectory(prefix='portal_case_panel_') as folder:
            key='a'*32;Path(folder,key).write_bytes(b'%PDF-1.4\n%%EOF')
            self.app.config['TASK_UPLOAD_DIR']=folder
            with self.app.app_context():
                comment=TaskComment(task_id=task_id,author_id=self.ids['worker'],body='Сверены исходные материалы')
                db.session.add(comment);db.session.flush()
                attachment=TaskAttachment(task_id=task_id,comment_id=comment.id,uploaded_by_id=self.ids['worker'],
                    original_filename='Исходные материалы.pdf',storage_key=key,content_type='application/pdf',size_bytes=15)
                db.session.add(attachment);db.session.commit();aid=attachment.id
            self.as_user()
            panel=self.panel(task_id).get_data(as_text=True);full=self.client.get(f'/tasks/{task_id}').get_data(as_text=True)
            for html in (panel,full):
                self.assertIn('Сверены исходные материалы',html)
                self.assertEqual(2,PanelMarkup(html).links.count(f'/tasks/attachments/{aid}/download'))
                self.assertNotIn(key,html)
            with self.client.get(f'/tasks/attachments/{aid}/download') as download:
                self.assertEqual(200,download.status_code)
            self.as_user('outsider');self.panel(task_id,404)
            self.assertEqual(404,self.client.get(f'/tasks/attachments/{aid}/download').status_code)

    def test_sender_recipient_and_admin_can_read_dispatched_task(self):
        task_id=self.sent()
        for role in ('employee','head','worker','receiver-head','root'):
            self.as_user(role);self.panel(task_id)

    def test_shared_panel_identifies_current_user_after_account_switch(self):
        shared = self.sent()
        hidden = self.task(title='Private foreign task', sender_department_id=self.third_department_id, recipient_department_id=self.department_id)
        self.as_user('employee')
        original = self.panel(shared)
        self.assertEqual(str(self.ids['employee']), original.headers['X-Task-User'])
        self.as_user('receiver-head')
        current = self.panel(shared)
        self.assertEqual(str(shared), current.headers['X-Task-Panel'])
        self.assertEqual(str(self.ids['receiver-head']), current.headers['X-Task-User'])
        self.assertNotEqual(original.headers['X-Task-User'], current.headers['X-Task-User'])
        denied = self.panel(hidden, 404)
        self.assertNotIn('Private foreign task', denied.get_data(as_text=True))
        self.assertNotIn('X-Task-User', denied.headers)

    def test_other_department_and_unknown_task_return_no_private_data(self):
        task_id=self.sent(title='Строго приватное поручение')
        self.as_user('outsider')
        for identifier in (task_id,999999):
            response=self.panel(identifier,404)
            self.assertNotIn('Строго приватное поручение',response.get_data(as_text=True))
            self.assertNotIn('X-Task-Panel',response.headers)

    def test_all_five_current_states_visible_to_both_participating_departments(self):
        ids=[self.task(status=status) for status in ('new','in_progress','completed','cancelled','rejected')]
        for name in ('receiver-head','head'):
            self.as_user(name)
            for task_id in ids:self.panel(task_id)

    def test_anonymous_is_redirected_without_cacheable_private_fragment(self):
        task_id=self.sent()
        response=self.panel(task_id,302)
        self.assertIn('/login',response.location)
        self.assertNotIn('data-task-panel-content',response.get_data(as_text=True))

    def test_blocked_user_cannot_reuse_session_to_read_panel(self):
        task_id=self.sent();self.as_user('worker')
        with self.app.app_context():
            db.session.get(User,self.ids['worker']).is_blocked=True;db.session.commit()
        response=self.client.get(f'/tasks/{task_id}/panel')
        self.assertIn(response.status_code,(302,403))
        self.assertIn('no-store',response.headers.get('Cache-Control',''))
        self.assertNotIn('data-task-panel-content',response.get_data(as_text=True))

    def test_read_only_endpoint_rejects_post(self):
        task_id=self.sent();self.as_user('worker')
        self.assertEqual(405,self.client.post(f'/tasks/{task_id}/panel').status_code)

    def test_archived_department_still_has_read_access_to_old_task(self):
        task_id=self.sent()
        with self.app.app_context():
            db.session.get(Department,self.other_department_id).is_archived=True;db.session.commit()
        self.as_user('receiver-head');self.panel(task_id)

    def test_admin_and_terminal_previews_have_no_work_actions(self):
        active=self.sent();closed=self.task(status='completed',sent_at=datetime(2029,1,1,tzinfo=timezone.utc),completed_at=datetime(2029,2,1,tzinfo=timezone.utc))
        self.as_user('root')
        html=self.panel(active).get_data(as_text=True)
        self.assertNotIn('#task-permitted-actions',html);self.assertNotIn('#task-discussion',html)
        self.assertIn('Просмотр администратора',html)
        self.as_user('head');html=self.panel(closed).get_data(as_text=True)
        self.assertIn('Завершена:',html);self.assertIn('Вернуть на доработку',html);self.assertNotIn('data-panel-action',html)
        self.assertNotIn('#task-discussion',html)

    def test_user_text_is_escaped_in_description_comments_files_and_history(self):
        payload='<script>alert("private")</script>'
        task_id=self.sent(title=payload,description=payload,contact_person=payload)
        with self.app.app_context():
            record_task_event(task=db.session.get(Task,task_id),actor=db.session.get(User,self.ids['employee']),event_type='comment_added',comment=payload)
            db.session.add(TaskAttachment(task_id=task_id,uploaded_by_id=self.ids['employee'],
                original_filename=payload+'.pdf',storage_key='b'*32,content_type='application/pdf',size_bytes=15))
            db.session.commit()
        self.as_user();html=self.panel(task_id).get_data(as_text=True)
        self.assertIn('&lt;script&gt;',html)
        self.assertNotIn('<script>',html)
        self.assertFalse(any(t=='script' for t,a in PanelMarkup(html).tags))

    def test_repeated_reads_change_no_tasks_comments_events_or_notifications(self):
        task_id=self.sent();self.as_user('worker')
        def snapshot():
            with self.app.app_context():
                task=db.session.get(Task,task_id)
                return task.status,task.revision,TaskComment.query.count(),TaskEvent.query.count(),TaskNotification.query.count()
        before=snapshot()
        for i in range(3):self.panel(task_id)
        self.assertEqual(before,snapshot())

    def test_unknown_completion_date_is_not_fabricated_in_panel_or_full_page(self):
        task_id=self.task(status='completed', completed_at=None);self.as_user('receiver-head')
        for path in (f'/tasks/{task_id}',f'/tasks/{task_id}/panel'):
            html=self.client.get(path).get_data(as_text=True)
            self.assertIn('дата и время неизвестны (историческая запись)',html)

    def test_board_keeps_ordinary_links_and_only_one_preview_shell(self):
        task_id=self.sent();self.as_user('receiver-head')
        response=self.client.get('/tasks/received?view=board');html=response.get_data(as_text=True)
        self.assertIn(f'href="/tasks/{task_id}"',html)
        self.assertEqual(1,html.count('id="task-panel-dialog"'))
        self.assertIn('task-panel.js',html)
        self.assertNotIn('Полное описание',html)
        listing=self.client.get('/tasks/received?view=list').get_data(as_text=True)
        self.assertNotIn('task-panel-dialog',listing)
