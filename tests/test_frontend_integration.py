"""Real Flask endpoints with the merged design, real CSRF and disposable data."""
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
import re
import unittest
from unittest.mock import patch
from werkzeug.datastructures import MultiDict
from preview.server import create_integrated_app, cleanup_integrated_app, INTEGRATION_RUNTIME_ROOT, SYNTHETIC_PASSWORD
from preview.fixtures import preview_pdf


class Markup(HTMLParser):
    def __init__(self, text):
        super().__init__(); self.tags=[]; self.feed(text)
    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class FrontendIntegrationCase(unittest.TestCase):
    def setUp(self):
        self.app=create_integrated_app(); self.app.testing=True
        self.info=self.app.extensions['frontend_integration']
        self.ids=self.info['identities']; self.clients={}
    def tearDown(self):
        cleanup_integrated_app(self.app)
    def token(self, client, path='/tasks/received'):
        response=client.get(path)
        self.assertEqual(response.status_code,200,response.text[:200])
        tokens=[a['value'] for t,a in Markup(response.text).tags if a.get('name')=='csrf_token']
        self.assertTrue(tokens)
        return tokens[0]
    def login(self, name='demo.head'):
        if name in self.clients:return self.clients[name]
        client=self.app.test_client()
        token=self.token(client,'/login')
        response=client.post('/login',data=dict(username=name,password=SYNTHETIC_PASSWORD,csrf_token=token),follow_redirects=True)
        self.assertEqual(response.status_code,200,response.text[:200])
        self.assertNotIn('Неверный логин',response.text)
        self.clients[name]=client;return client
    def snapshot(self, ident):
        from app.extensions import db
        from app.models import Task
        with self.app.app_context():
            db.session.remove(); task=db.session.get(Task,ident)
            return dict(status=task.status,revision=task.revision,ids=list(task.assignee_ids),
                comments=len(task.comments),files=len(task.attachments),cycle=task.rework_cycle)
    def post(self, client, path, data):
        values=MultiDict(data)
        values.setlist('csrf_token',[self.token(client)])
        response=client.post(path,data=values)
        response.request.environ['wsgi.input'].close()
        return response
    def action(self, ident, key, name='demo.head', **fields):
        client=self.login(name)
        values=MultiDict(dict(revision=str(self.snapshot(ident)['revision']),comment='Синтетическое пояснение действия'))
        for key_,value in fields.items():values.setlist(key_,value if isinstance(value,list) else [value])
        return self.post(client,f'/tasks/{ident}/{key}',values)
    def assertRedirect(self, response):
        self.assertEqual(response.status_code,302,response.text[:200])
    def create(self):
        client=self.login('demo.sender'); response=client.get('/tasks/new')
        fields=[a for t,a in Markup(response.text).tags if t in ('input','textarea','select')]
        self.assertFalse({'category_id','contact_person'} & {a.get('name') for a in fields})
        key=next(a['value'] for a in fields if a.get('name')=='creation_key')
        values=dict(creation_key=key,recipient_department_id=str(self.department('demo.head')),
            title='Проверить командную работу',description='Результат интеграционной проверки',
            due_at='2030-06-01T12:00',priority='high')
        response=self.post(client,'/tasks/new',values)
        self.assertRedirect(response)
        ident=int(response.location.split('/')[-1])
        return ident,values
    def department(self,name):
        from app.extensions import db
        from app.models import User
        with self.app.app_context():return db.session.get(User,self.ids[name]).department_id

    def test_real_login_csrf_isolation_and_complete_page_map(self):
        self.assertEqual(self.info['runtime'].parent,INTEGRATION_RUNTIME_ROOT.resolve())
        self.assertTrue(self.info['database'].is_file())
        self.assertTrue(self.app.config['WTF_CSRF_ENABLED'])
        self.assertEqual(self.app.config['SESSION_COOKIE_NAME'],'caspian_integration_newbase')
        anonymous=self.app.test_client()
        self.assertEqual(anonymous.post('/login',data={'username':'demo.head','password':SYNTHETIC_PASSWORD}).status_code,400)
        self.assertEqual(anonymous.get('/tasks/1').status_code,302)
        for name,paths in [('demo.head',['/tasks/received','/tasks/sent','/tasks/completed','/tasks/new','/tasks/1','/tasks/2','/staff','/staff/new','/reports/department','/tasks/notifications']),
                ('demo.employee',['/tasks/received','/tasks/sent','/tasks/completed','/tasks/2','/tasks/notifications']),
                ('demo.admin',['/tasks/received','/tasks/completed','/tasks/2','/admin/users','/admin/users/new','/admin/departments','/admin/departments/new','/admin/statistics','/admin/audit','/tasks/notifications'])]:
            client=self.login(name)
            for path in paths:
                with self.subTest(name=name,path=path):
                    result=client.get(path);self.assertEqual(result.status_code,200)
                    self.assertIn('Интеграционный стенд',result.text)
                    self.assertEqual(result.headers['Cache-Control'],'no-store')
                    self.assertIn('caspian-palette.css',result.text)
        head=self.login(); full=head.get('/tasks/2').text
        self.assertIn('Анна Соколова',full);self.assertIn('Данияр Омаров',full)
        self.assertIn('Исполнители (2)',full)
        self.assertIn('Исполнители (2)',head.get('/tasks/received').text)
        self.assertIn('href="/staff"',head.get('/tasks/received').text)
        labels=[a for t,a in Markup(full).tags if a.get('name')=='assignee_ids']
        self.assertTrue(labels);self.assertTrue(all('multiple' in a for a in labels))
        self.assertNotIn('name="assignee_id"',full)
        self.assertNotIn('<span>Категория</span>',full);self.assertNotIn('Контактное лицо',full)
        self.assertNotIn('task-panel.js',head.get('/tasks/received').text)

    def test_unknown_completion_date_never_falls_back_to_closed_at(self):
        from datetime import datetime, timezone
        from app.extensions import db
        from app.models import Task
        with self.app.app_context():
            task=db.session.get(Task,2)
            task.status='completed';task.completed_at=None
            task.closed_at=datetime(2035,6,1,12,tzinfo=timezone.utc)
            db.session.commit()
        client=self.login()
        for path in ('/tasks/2','/tasks/completed?view=board','/tasks/completed?view=list'):
            response=client.get(path)
            self.assertEqual(response.status_code,200)
            self.assertIn('дата и время неизвестны',response.text.lower())
            self.assertNotIn('2035',response.text)

    def test_invalid_login_is_inline_preserves_username_and_never_password(self):
        client=self.app.test_client(); token=self.token(client,'/login')
        response=client.post('/login',data=dict(csrf_token=token,username='demo.head',password='synthetic-wrong-password'))
        self.assertEqual(response.status_code,200)
        nodes=Markup(response.text).tags
        password=next(a for t,a in nodes if a.get('name')=='password')
        username=next(a for t,a in nodes if a.get('name')=='username')
        self.assertEqual(password.get('value'),'')
        self.assertEqual(password.get('aria-describedby'),'field-password-error')
        self.assertEqual(password.get('aria-invalid'),'true')
        self.assertEqual(username.get('value'),'demo.head')
        self.assertIn('Неверный логин или пароль',response.text)
        self.assertNotIn('synthetic-wrong-password',response.text)
        self.assertNotIn('data-error-summary',response.text)
        self.assertEqual(client.get('/tasks/received').status_code,302)

    def test_team_lifecycle_real_handlers_and_single_count(self):
        ident,_=self.create(); a=self.ids['demo.employee'];b=self.ids['demo.colleague']
        self.assertRedirect(self.action(ident,'assign',assignee_ids=[str(a),str(b)]))
        self.assertEqual(set(self.snapshot(ident)['ids']),{a,b})
        self.assertRedirect(self.action(ident,'release','demo.employee'))
        self.assertEqual(self.snapshot(ident)['ids'],[b]);self.assertEqual(self.snapshot(ident)['status'],'in_progress')
        self.assertRedirect(self.action(ident,'claim','demo.employee'))
        self.assertEqual(set(self.snapshot(ident)['ids']),{a,b})
        self.assertRedirect(self.action(ident,'complete','demo.employee'))
        self.assertEqual(self.snapshot(ident)['status'],'completed')
        closed=self.login('demo.employee').get(f'/tasks/{ident}')
        self.assertNotIn('id="board-dialog"',closed.text)
        self.assertRedirect(self.action(ident,'return','demo.sender'))
        self.assertEqual(set(self.snapshot(ident)['ids']),{a,b})
        self.assertEqual(self.snapshot(ident)['cycle'],1)
        self.assertRedirect(self.action(ident,'reassign',assignee_ids=[str(b)]))
        self.assertEqual(self.snapshot(ident)['ids'],[b])
        self.assertRedirect(self.action(ident,'complete','demo.colleague'))
        from app.reporting import department_report
        from app.models import User
        from app.extensions import db
        with self.app.app_context():
            report=department_report(db.session.get(User,self.ids['demo.head']))
            self.assertEqual(report['incoming']['total'],6)
            self.assertEqual(report['incoming']['statuses']['completed'],1)

    def test_validation_and_conflict_keep_entire_selected_team_and_comment(self):
        a=self.ids['demo.employee'];b=self.ids['demo.colleague']
        before=self.snapshot(2)
        response=self.action(2,'reassign',assignee_ids=[str(a),str(b)],comment='Сохраняем обоих')
        self.assertEqual(response.status_code,400)
        self.assertEqual(self.snapshot(2),before)
        selected=[attrs['value'] for tag,attrs in Markup(response.text).tags if tag=='option' and 'selected' in attrs]
        self.assertTrue({str(a),str(b)}.issubset(selected));self.assertIn('Сохраняем обоих',response.text)
        self.assertRedirect(self.action(2,'deadline','demo.sender',due_at='2030-06-02T13:00'))
        response=self.action(2,'reassign',revision=str(before['revision']),assignee_ids=[str(a),str(b)],comment='Сохранить при конфликте')
        self.assertEqual(response.status_code,409)
        selected=[attrs['value'] for tag,attrs in Markup(response.text).tags if tag=='option' and 'selected' in attrs]
        self.assertTrue({str(a),str(b)}.issubset(selected));self.assertIn('Сохранить при конфликте',response.text)
        self.assertEqual(self.snapshot(2)['revision'],before['revision']+1)
        self.assertIn('name="revision" value="'+str(before['revision']+1)+'"',response.text)
        for ids in [[],[str(a),str(a)],[str(a),'999999']]:
            self.assertEqual(self.action(1,'assign',assignee_ids=ids).status_code,400)
        self.assertEqual(self.action(1,'assign',assignee_id=str(a)).status_code,400)
        self.assertEqual(self.snapshot(1)['ids'],[])

    def test_last_release_new_and_terminal_deadline_and_retired_routes(self):
        a=self.ids['demo.employee'];b=self.ids['demo.colleague']
        self.assertRedirect(self.action(1,'assign',assignee_ids=[str(a)]))
        self.assertRedirect(self.action(1,'release','demo.employee'))
        self.assertEqual(self.snapshot(1)['status'],'new');self.assertEqual(self.snapshot(1)['ids'],[])
        self.assertRedirect(self.action(1,'deadline','demo.sender',due_at='2030-06-02T13:00'))
        self.assertRedirect(self.action(1,'cancel','demo.sender'))
        self.assertEqual(self.snapshot(1)['status'],'cancelled')
        self.assertRedirect(self.action(3,'reject'))
        self.assertEqual(self.snapshot(3)['status'],'rejected')
        self.assertEqual(self.action(3,'return','demo.sender').status_code,409)
        client=self.login()
        for path in ['/tasks/2/confirm','/tasks/2/review/complete','/tasks/2/edit']:
            self.assertEqual(self.post(client,path,{'revision':'1','comment':'Проверка удалённого пути'}).status_code,410)

    def test_comments_files_are_atomic_private_and_bound_to_comment(self):
        client=self.login();before=self.snapshot(1)
        values=MultiDict(dict(revision=str(before['revision']),comment='Материалы для встречи'))
        values.add('attachment',(BytesIO(preview_pdf(1)),'materials.pdf'))
        response=self.post(client,'/tasks/1/comments',values);self.assertRedirect(response)
        after=self.snapshot(1);self.assertEqual(after['comments'],before['comments']+1);self.assertEqual(after['files'],1)
        self.assertEqual(after['status'],before['status'])
        from app.models import TaskAttachment
        with self.app.app_context():
            attachment=TaskAttachment.query.filter_by(task_id=1).one()
            self.assertIsNotNone(attachment.comment_id);file_id=attachment.id
        download=client.get(f'/tasks/attachments/{file_id}/download')
        try:self.assertEqual(download.status_code,200);self.assertTrue(download.data.startswith(b'%PDF-'))
        finally:download.close()
        self.assertEqual(self.login('demo.outsider').get(f'/tasks/attachments/{file_id}/download').status_code,404)
        before=self.snapshot(1)
        invalid=MultiDict(dict(revision=str(before['revision']),comment='Невалидный файл'))
        invalid.add('attachment',(BytesIO(b'not a PDF'),'broken.pdf'))
        response=self.post(client,'/tasks/1/attachments',invalid)
        self.assertEqual(response.status_code,400);self.assertEqual(self.snapshot(1),before)
        oversized=MultiDict(dict(revision=str(before['revision']),comment='Слишком большой запрос'))
        oversized.add('attachment',(BytesIO(b'x'*(21*1024*1024)),'large.pdf'))
        response=self.post(client,'/tasks/1/attachments',oversized)
        self.assertEqual(response.status_code,413);self.assertEqual(self.snapshot(1),before)

    def test_creation_key_retry_validation_and_native_return_context(self):
        ident,values=self.create();client=self.login('demo.sender')
        response=self.post(client,'/tasks/new',values);self.assertRedirect(response)
        self.assertEqual(response.location,f'/tasks/{ident}')
        for legacy in ('category_id','contact_person'):
            response=self.post(client,'/tasks/new',dict(values,**{legacy:''}))
            self.assertEqual(response.status_code,400)
        page=client.get('/tasks/new')
        key=next(a['value'] for t,a in Markup(page.text).tags if a.get('name')=='creation_key')
        response=self.post(client,'/tasks/new',dict(values,creation_key=key,title=''))
        self.assertEqual(response.status_code,400);self.assertIn(key,response.text)
        self.assertIn(values['description'],response.text)
        result=self.login().get('/tasks/2?origin_section=completed&origin_view=list&origin_scope=sent&origin_page=2')
        links=[a['href'] for t,a in Markup(result.text).tags if 'reading-back' in a.get('class','')]
        self.assertEqual(len(links),1)
        self.assertIn('/tasks/completed?',links[0]);self.assertIn('view=list',links[0]);self.assertIn('scope=sent',links[0]);self.assertIn('page=2',links[0])
        self.assertNotIn('https://evil.test',self.login().get('/tasks/2?origin_section=https://evil.test').text)

    def test_admin_readonly_role_boundaries_and_staff_assignment_counts(self):
        admin=self.login('demo.admin');head=self.login();employee=self.login('demo.employee')
        self.assertNotIn('id="board-dialog"',admin.get('/tasks/2').text)
        before=self.snapshot(2)
        self.assertEqual(self.action(2,'complete','demo.admin').status_code,403)
        self.assertEqual(self.snapshot(2),before)
        for client,path in [(admin,'/staff'),(employee,'/staff'),(employee,'/reports/department'),(head,'/admin/users'),(head,'/admin/statistics'),(head,'/admin/audit')]:
            self.assertEqual(client.get(path).status_code,403)
        disabled=self.post(head,f'/staff/{self.ids["demo.employee"]}/disable',{})
        self.assertIn(disabled.status_code,(400,409));self.assertIn('Анна Соколова',disabled.text)
        self.assertEqual(self.action(2,'claim','demo.employee',assignee_ids=[str(self.ids['demo.colleague'])]).status_code,400)
        self.assertEqual(self.login('demo.outsider').get('/tasks/1').status_code,404)

    def test_people_departments_notifications_reports_and_audit(self):
        head=self.login();admin=self.login('demo.admin')
        response=self.post(head,'/staff/new',dict(full_name='Новый сотрудник (тест)',username='demo.created',password=SYNTHETIC_PASSWORD))
        self.assertRedirect(response);self.assertIn('Новый сотрудник',head.get('/staff').text)
        from app.models import User,Department,TaskNotification
        with self.app.app_context():
            created=User.query.filter_by(username='demo.created').one().id
        self.assertRedirect(self.post(head,f'/staff/{created}/edit',dict(full_name='Изменённый сотрудник',username='demo.created',password='')))
        self.assertRedirect(self.post(head,f'/staff/{created}/disable',{}))
        self.assertRedirect(self.post(admin,f'/admin/users/{created}/unblock',{}))
        self.assertRedirect(self.post(admin,'/admin/departments/new',dict(name='Тестовый отдел')))
        with self.app.app_context():dept=Department.query.filter_by(name='Тестовый отдел').one().id
        self.assertRedirect(self.post(admin,f'/admin/departments/{dept}/archive',{}))
        self.assertRedirect(self.post(admin,f'/admin/departments/{dept}/restore',{}))
        self.assertRedirect(self.post(admin,f'/admin/departments/{dept}/delete',{}))
        with self.app.app_context():self.assertIsNone(Department.query.filter_by(id=dept).one_or_none())
        self.assertRedirect(self.post(admin,'/admin/users/new',dict(full_name='Создан администратором',
            username='demo.admin-created',password=SYNTHETIC_PASSWORD,role='employee',department_id=str(self.department('demo.head')))))
        self.assertRedirect(self.action(1,'assign',assignee_ids=[str(self.ids['demo.employee']),str(self.ids['demo.colleague'])]))
        client=self.login('demo.sender');notes=client.get('/tasks/notifications')
        boundary=next(a['value'] for t,a in Markup(notes.text).tags if a.get('name')=='boundary')
        with self.app.app_context():
            note_id=TaskNotification.query.filter_by(user_id=self.ids['demo.sender'],is_read=False).first().id
        self.assertRedirect(self.post(client,f'/tasks/notifications/{note_id}/read',{}))
        self.assertRedirect(self.post(client,'/tasks/notifications/read-all',dict(boundary=boundary)))
        self.assertEqual(client.get('/tasks/notifications?unread=1').status_code,200)
        for path in ['/reports/department','/tasks/notifications']:
            self.assertEqual(head.get(path).status_code,200)
        journal=admin.get('/admin/audit');self.assertEqual(journal.status_code,200)
        self.assertIn('Изменённый сотрудник',journal.text)
        self.assertEqual(admin.get('/admin/audit?date_from=invalid').status_code,400)
        self.assertEqual(admin.get('/admin/statistics').status_code,200)


    def test_stale_sender_discussion_keeps_draft_when_only_return_is_available(self):
        before=self.snapshot(2)
        self.assertRedirect(self.action(2,'complete','demo.employee'))
        completed=self.snapshot(2)
        response=self.action(2,'comments','demo.sender',revision=str(before['revision']),comment='UNSAVED-SENDER-DISCUSSION')
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.snapshot(2),completed)
        self.assertIn('UNSAVED-SENDER-DISCUSSION',response.text)
        self.assertIn('id="unsaved-action-comment" readonly',response.text)
        forms=[a.get('action') for t,a in Markup(response.text).tags if t=='form']
        self.assertNotIn('/tasks/2/comments',forms)
        self.assertIn('/tasks/2/return',forms)


    def test_list_counts_are_scoped_and_do_not_multiply_comments_and_files(self):
        client=self.login()
        for filenames in [('one.pdf','two.pdf'),('three.pdf',)]:
            values=MultiDict(dict(revision=str(self.snapshot(2)['revision']),comment='Реальные материалы списка'))
            for filename in filenames:
                values.add('attachment',(BytesIO(preview_pdf(1)),filename))
            self.assertRedirect(self.post(client,'/tasks/2/comments',values))
        self.assertRedirect(self.action(1,'comments',comment='Отдельное обсуждение'))
        # Preserve the parallel chat's stronger visibility/read-only assertions.
        from flask import template_rendered
        outsider=self.login('demo.outsider')
        creation=outsider.get('/tasks/new')
        key=next(a['value'] for t,a in Markup(creation.text).tags if a.get('name')=='creation_key')
        foreign=self.post(outsider,'/tasks/new',dict(creation_key=key,
            recipient_department_id=str(self.department('demo.sender')),
            title='PRIVATE-OTHER-DEPARTMENT',description='Synthetic private task',
            priority='normal',due_at='2030-06-01T12:00'))
        self.assertRedirect(foreign)
        foreign_id=int(foreign.location.split('/')[-1])
        before={ident:self.snapshot(ident) for ident in (1,2)}
        contexts=[]
        def capture(sender,template,context,**extra):contexts.append(context)
        template_rendered.connect(capture,self.app)
        try:response=client.get('/tasks/received?view=list')
        finally:template_rendered.disconnect(capture,self.app)
        self.assertEqual(response.status_code,200)
        counts=contexts[-1]['card_counts']
        self.assertEqual(set(counts),{task.id for task in contexts[-1]['tasks']})
        self.assertNotIn(foreign_id,counts)
        self.assertNotIn('PRIVATE-OTHER-DEPARTMENT',response.text)
        for ident,snapshot in before.items():
            self.assertEqual(counts[ident],dict(comments=snapshot['comments'],attachments=snapshot['files']))
            self.assertEqual(self.snapshot(ident),snapshot)
        card=response.text.split('data-task-id="2"',1)[1].split('</article>',1)[0]
        self.assertIn('Комментарии: <strong>2</strong>',card)
        self.assertIn('Вложения: <strong>3</strong>',card)



class IsolationBoundaryCase(unittest.TestCase):
    def test_inherited_working_configuration_is_rejected(self):
        for name in ['DATABASE_URL','TASK_UPLOAD_DIR','SECRET_KEY','ADMIN_PASSWORD','FLASK_APP']:
            with self.subTest(name=name),patch.dict('os.environ',{name:'synthetic-forbidden-value'}):
                with self.assertRaisesRegex(RuntimeError,'refuses inherited'):
                    create_integrated_app()


if __name__=='__main__':unittest.main()
