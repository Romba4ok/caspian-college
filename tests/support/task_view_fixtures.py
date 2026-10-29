"""Synthetic current workflow cards and HTTP read-model helpers."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
import json
import re

from flask import template_rendered
from sqlalchemy import select
from app.accounts import authentication_stamp
from app.extensions import db
from app.models import Department, Task, TaskAttachment, TaskComment, TaskEvent, TaskNotification, User
from support.portal_fixtures import PortalCase

NOW = datetime(2030, 1, 10, 10, tzinfo=timezone.utc)


class Markup(HTMLParser):
    def __init__(self, source):
        super().__init__(); self.links=[]; self.forms=[]; self.feed(source)
    def handle_starttag(self, tag, attrs):
        if tag == "a": self.links.append(dict(attrs))
        if tag == "form": self.forms.append(dict(attrs))


class ViewCase(PortalCase):
    def setUp(self):
        super().setUp()
        for name, role, blocked in (("worker", "employee", False), ("peer", "employee", False),
                ("receiver-head", "head", False), ("receiver-head2", "head", False), ("blocked-worker", "employee", True)):
            self.add_member(name, role=role, department_id=self.other_department_id, blocked=blocked)
        with self.app.app_context():
            self.fourth = Department.query.filter(~Department.id.in_((self.department_id,self.other_department_id,self.third_department_id))).first().id

    def client_for(self, name="employee"):
        client=self.app.test_client()
        with self.app.app_context(): stamp=authentication_stamp(db.session.get(User,self.ids[name]))
        with client.session_transaction() as session:
            session["_user_id"]=str(self.ids[name]);session["_fresh"]=True;session["_auth_stamp"]=stamp
        return client

    def card(self, status="new", **changes):
        return self.task(**(dict(status=status, title="Текущее поручение", description="Описание поручения",
            assignee_id=None if status=="new" else self.ids["worker"], sent_at=NOW-timedelta(days=5),
            created_at=NOW-timedelta(days=6), due_at=NOW+timedelta(days=1),
            completed_at=NOW if status=="completed" else None,
            closed_at=NOW if status in ("completed","cancelled","rejected") else None)|changes))

    def many(self, count, statuses=("new","in_progress"), **changes):
        with self.app.app_context():
            rows=[]
            for i in range(count):
                status=statuses[i%len(statuses)]
                rows.append(Task(**(dict(title=f"Карточка {i:03}",description="Синтетические данные", status=status,
                    author_id=self.ids["employee"],sender_department_id=self.department_id,recipient_department_id=self.other_department_id,
                    assignee_id=None if status=="new" else self.ids["worker"], sent_at=NOW-timedelta(days=5),
                    created_at=NOW-timedelta(days=6),due_at=NOW+timedelta(days=1),
                    completed_at=NOW if status=="completed" else None,
                    closed_at=NOW if status in ("completed","cancelled","rejected") else None)|changes)))
            db.session.add_all(rows);db.session.flush();ids=[r.id for r in rows];db.session.commit();return ids

    def foreign(self, **changes):
        return self.card(sender_department_id=self.third_department_id,recipient_department_id=self.fourth,
            author_id=self.ids["outsider"],title="FOREIGN-PRIVATE-CARD",**changes)

    def render(self, direction="sent", actor="employee", view="board", **params):
        contexts=[]
        def capture(sender,template,context,**kw):contexts.append(context)
        query=dict(params)
        if view is not None:query["view"]=view
        with template_rendered.connected_to(capture,self.app):
            response=self.client_for(actor).get(f"/tasks/{direction}",query_string=query)
        self.assertEqual(200,response.status_code,response.get_data(as_text=True)[:200])
        return response,contexts[-1]

    def cards(self, response):
        text=response.get_data(as_text=True) if hasattr(response,"get_data") else response
        return {d["id"]:d for d in [json.loads(unescape(raw)) for raw in re.findall(r'data-board-task="([^"]+)"',text)]}

    def snapshot(self):
        with self.app.app_context():
            return {m.__tablename__:deepcopy(db.session.execute(select(m.__table__).order_by(m.id)).all())
                for m in (Task,TaskComment,TaskAttachment,TaskEvent,TaskNotification)}
