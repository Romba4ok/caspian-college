"""Read-after-write board fragments obey the original privacy/workflow boundary."""
from datetime import datetime, timezone
from html import unescape
import json
import re

from app.extensions import db
from app.models import Task, TaskComment, TaskEvent, TaskNotification, User
from support.portal_fixtures import PortalCase
from test_task_board import BoardMarkup


class BoardRefreshTests(PortalCase):
    def setUp(self):
        super().setUp()
        self.add_member('worker',department_id=self.other_department_id)
        self.add_member('receiver-head',role='head',department_id=self.other_department_id)

    def as_user(self,name='employee'):
        self.client=self.app.test_client();self.login(name)

    def fragment(self,direction='sent',status=200):
        response=self.client.get(f'/tasks/{direction}?view=board&fragment=board&page=999')
        self.assertEqual(status,response.status_code)
        self.assertIn('no-store',response.headers.get('Cache-Control',''))
        return response

    def cards(self,response):
        return {c['id']:c for c in (json.loads(unescape(v)) for v in re.findall(r'data-board-task="([^"]+)"',response.get_data(as_text=True)))}

    def sent(self,**changes):
        return self.task(**(dict(status='in_progress',sent_at=datetime.now(timezone.utc),assignee_id=self.ids['worker'])|changes))

    def test_complete_fragment_matches_initial_board_and_all_counts(self):
        ids=self.task_batch(36,statuses=('awaiting_assignment','assigned','in_progress','awaiting_clarification','recipient_review','sender_acceptance'),assignee_id=self.ids['worker'])
        self.as_user()
        fresh=self.fragment();html=fresh.get_data(as_text=True)
        initial=self.client.get('/tasks/sent?view=board')
        self.assertEqual(self.cards(initial),self.cards(fresh))
        self.assertEqual(set(ids),set(self.cards(fresh)))
        markup=BoardMarkup(html);self.assertEqual(36,markup.counts['sent'])
        self.assertEqual(36,sum(len(g['ids']) for g in markup.columns.values()))
        self.assertEqual(0,markup.dialog_count)
        for forbidden in ('<script','<form','<html','task-panel-dialog','board-dialog'):
            self.assertNotIn(forbidden,html)
        self.assertEqual('sent',fresh.headers['X-Task-Board'])
        self.assertEqual(str(self.ids['employee']),fresh.headers['X-Task-User'])

    def test_receiver_fragment_excludes_presend_and_foreign_tasks(self):
        active=self.sent();hidden=self.task(title='Не показывать получателю')
        foreign=self.task(sender_department_id=self.third_department_id,recipient_department_id=self.department_id)
        self.as_user('receiver-head');response=self.fragment('received')
        self.assertEqual({active},set(self.cards(response)))
        self.assertNotIn('Не показывать получателю',response.get_data(as_text=True))
        self.assertNotIn(hidden,self.cards(response));self.assertNotIn(foreign,self.cards(response))

    def test_other_department_has_an_empty_private_fragment(self):
        self.sent(title='Конфиденциальное поручение');self.as_user('outsider')
        for direction in ('received','sent'):
            response=self.fragment(direction)
            self.assertEqual({},self.cards(response))
            self.assertNotIn('Конфиденциальное поручение',response.get_data(as_text=True))

    def test_anonymous_and_blocked_do_not_get_a_private_fragment(self):
        self.sent();self.fragment(status=302)
        self.as_user('employee')
        with self.app.app_context():
            db.session.get(User,self.ids['employee']).is_blocked=True;db.session.commit()
        response=self.fragment(status=302)
        self.assertNotIn('data-board-root',response.get_data(as_text=True))

    def test_user_header_changes_when_session_changes(self):
        self.sent();self.as_user();one=self.fragment().headers['X-Task-User']
        self.as_user('root');two=self.fragment().headers['X-Task-User']
        self.assertNotEqual(one,two)

    def test_fragment_flag_cannot_turn_explicit_list_into_a_board(self):
        self.as_user()
        for path in ('/tasks/completed?view=list&fragment=board','/tasks/sent?view=list&fragment=board','/tasks/sent?view=board&fragment=unknown'):
            response=self.client.get(path)
            self.assertEqual(400,response.status_code)
            self.assertIn('no-store',response.headers['Cache-Control'])

    def test_assignment_refreshes_exact_status_candidates_and_revision(self):
        task_id=self.sent(status='awaiting_assignment',assignee_id=None)
        self.as_user('receiver-head')
        response=self.client.post(f'/tasks/{task_id}/execution/assign',data={'revision':'0','assignee_id':str(self.ids['worker']),'comment':'Назначаю ответственного'})
        self.assertEqual(302,response.status_code)
        card=self.cards(self.fragment('received'))[task_id]
        self.assertEqual(('assigned','to_do',1,self.ids['worker']),(card['status'],card['group'],card['revision'],card['assignee_id']))
        self.assertNotIn('assign',{a['key'] for a in card['actions']})
        self.assertIn('start',{a['key'] for a in card['actions']})
        self.assertEqual(1,card['comment_count'])

    def test_conflict_then_read_has_new_revision_without_duplicate_event(self):
        task_id=self.sent(status='assigned');self.as_user('receiver-head')
        path=f'/tasks/{task_id}/execution/start'
        self.assertEqual(302,self.client.post(path,data={'revision':'0','comment':'Первый клиент'}).status_code)
        response=self.client.post(path,data={'revision':'0','comment':'Комментарий второго клиента'})
        self.assertEqual(409,response.status_code)
        with self.app.app_context():
            self.assertIsNone(TaskComment.query.filter_by(task_id=task_id,body='Комментарий второго клиента').first())
        card=self.cards(self.fragment('received'))[task_id]
        self.assertEqual(('in_progress','working',1),(card['status'],card['group'],card['revision']))
        with self.app.app_context():self.assertEqual(1,TaskEvent.query.filter_by(task_id=task_id).count())

    def test_completion_removes_card_and_updates_all_tab_counts(self):
        task_id=self.sent(status='sender_acceptance');self.as_user('head')
        self.assertEqual(302,self.client.post(f'/tasks/{task_id}/review/complete',data={'revision':'0','comment':'Результат принят'}).status_code)
        response=self.fragment();self.assertNotIn(task_id,self.cards(response))
        self.assertEqual({'sent':0,'received':0,'completed':1},BoardMarkup(response.get_data(as_text=True)).counts)

    def test_fragment_reads_write_nothing_and_escape_user_text(self):
        self.sent(title='<img src=x onerror=alert(1)>');self.as_user()
        def snapshot():
            with self.app.app_context():return [(t.id,t.revision,t.status) for t in Task.query.all()],TaskEvent.query.count(),TaskComment.query.count(),TaskNotification.query.count()
        before=snapshot()
        for i in range(3):
            html=self.fragment().get_data(as_text=True);self.assertNotIn('<img src=x',html);self.assertIn('&lt;img',html)
        self.assertEqual(before,snapshot())
