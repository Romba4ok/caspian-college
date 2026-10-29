"""Complete, scoped board data without per-card SQL."""
import os
from unittest import TestCase,skipUnless
from sqlalchemy import event
from app.extensions import db
from app.models import Department,Task,TaskComment,TaskAttachment,User
from support.task_view_fixtures import ViewCase


class BoardProjectionTests(ViewCase):
    def test_every_active_status_is_grouped_once_for_both_departments(self):
        ids=self.many(36)
        for direction,actor in (("sent","employee"),("received","worker")):
            response,ctx=self.render(direction,actor)
            self.assertEqual(["new","in_progress"],[g["key"] for g in ctx["board_groups"]])
            self.assertEqual(["Новые","В работе"],[g["label"] for g in ctx["board_groups"]])
            self.assertEqual([18,18],[g["count"] for g in ctx["board_groups"]])
            self.assertEqual(set(ids),set(self.cards(response)))
            self.assertEqual(36,sum(len(g["tasks"]) for g in ctx["board_groups"]))

    def test_300_cards_are_complete_while_list_keeps_25(self):
        ids=self.many(300)
        for direction,actor in (("sent","employee"),("received","receiver-head")):
            for page in (1,9):
                response,ctx=self.render(direction,actor,page=page)
                self.assertEqual(set(ids),set(self.cards(response)));self.assertEqual(300,ctx["pagination"].total)
            _,ctx=self.render(direction,actor,view="list",page=2)
            self.assertEqual(25,len(ctx["tasks"]));self.assertEqual(12,ctx["pagination"].pages)

    def test_empty_active_boards_have_two_groups_and_no_candidates(self):
        response,ctx=self.render()
        self.assertEqual([0,0],[g["count"] for g in ctx["board_groups"]]);self.assertEqual({},self.cards(response))

    def test_counts_do_not_multiply_comments_and_files(self):
        ident=self.card()
        with self.app.app_context():
            comments=[TaskComment(task_id=ident,author_id=self.ids["employee"],body=str(i)) for i in range(3)]
            db.session.add_all(comments);db.session.flush()
            for i in range(4):db.session.add(TaskAttachment(task_id=ident,uploaded_by_id=self.ids["employee"],
                comment_id=None if i==0 else comments[0].id,original_filename=f"file{i}.pdf",storage_key=str(i)*32,content_type="application/pdf",size_bytes=10))
            db.session.commit()
        response,_=self.render();data=self.cards(response)[ident]
        self.assertEqual((3,4),(data["comment_count"],data["attachment_count"]))

    def test_candidates_are_only_active_recipient_members_and_only_on_authorized_cards(self):
        new=self.card();work=self.card("in_progress")
        response,_=self.render("received","receiver-head");cards=self.cards(response)
        self.assertEqual({self.ids[x] for x in ("worker","peer","receiver-head","receiver-head2")},{u["id"] for u in cards[new]["candidates"]})
        self.assertIn(self.ids["worker"],{u["id"] for u in cards[work]["candidates"]})
        response,_=self.render("received","peer")
        self.assertTrue(all(not c["candidates"] for c in self.cards(response).values()))

    def test_foreign_cards_counts_and_candidates_never_leak(self):
        ident=self.card();foreign=self.foreign()
        for direction,actor in (("sent","employee"),("received","worker")):
            response,ctx=self.render(direction,actor)
            self.assertEqual({ident},set(self.cards(response)));self.assertNotIn(b"FOREIGN-PRIVATE-CARD",response.data)
            self.assertEqual(1,ctx["counts"][direction]);self.assertNotIn(foreign,ctx["board_data"])
        response,_=self.render(actor="root");self.assertEqual({ident,foreign},set(self.cards(response)))
        self.assertTrue(all(not c["actions"] and not c["candidates"] for c in self.cards(response).values()))

    def test_sql_count_stays_bounded_from_36_to_300_for_board_and_list(self):
        self.many(36)
        with self.app.app_context():engine=db.engine
        def measured(view):
            queries=[]
            def capture(*args):queries.append(args[2])
            event.listen(engine,"before_cursor_execute",capture)
            try:response,_=self.render("received","receiver-head",view=view)
            finally:event.remove(engine,"before_cursor_execute",capture)
            self.assertEqual(200,response.status_code);return len(queries)
        small={v:measured(v) for v in ("board","list")};self.many(264)
        for view in ("board","list"):
            large=measured(view);self.assertLessEqual(large,small[view]+2);self.assertLessEqual(large,20)

    def test_reading_projection_and_list_does_not_write_workflow(self):
        self.many(36);before=self.snapshot()
        self.render();self.render("received","receiver-head");self.render(view="list")
        self.assertEqual(before,self.snapshot())


from support.migration_fixtures import MigrationDatabase
from support.task_view_fixtures import NOW


@skipUnless(os.environ.get("PORTAL_TEST_DATABASE_URL"),"Требуется безопасный runner PostgreSQL")
class PostgreSQLBoardProjectionTests(MigrationDatabase,TestCase):
    postgres=True
    client_for=ViewCase.client_for
    cards=ViewCase.cards

    def render(self,*args,**kwargs):
        # The migration fixture holds an app context; each browser request must
        # get its own g/current_user cache, just as a real request does.
        with self.app.app_context():
            return ViewCase.render(self,*args,**kwargs)

    def test_native_36_and_300_cards_with_scoped_aggregates_bounded_queries_and_list(self):
        self.upgrade();departments=Department.query.order_by(Department.id).all();source,recipient=departments[:2]
        users=[User(username=name,full_name=name,password_hash="synthetic",role=role,department_id=did)
            for name,role,did in (("employee","employee",source.id),("worker","employee",recipient.id),("receiver-head","head",recipient.id))]
        db.session.add_all(users);db.session.flush();self.ids={u.username:u.id for u in users};source_id,recipient_id=source.id,recipient.id;db.session.commit()
        ids=[]
        def add(count):
            rows=[Task(title=f"Native {len(ids)+i}",description="Синтетический пример",author_id=self.ids["employee"],sender_department_id=source_id,
                recipient_department_id=recipient_id,status="new" if i%2==0 else "in_progress",assignee_id=None if i%2==0 else self.ids["worker"],due_at=NOW,sent_at=NOW,created_at=NOW) for i in range(count)]
            db.session.add_all(rows);db.session.flush();ids.extend(t.id for t in rows);db.session.commit();db.session.remove()
        engine=db.engine
        def read(view):
            queries=[]
            def capture(*args):queries.append(args[2])
            event.listen(engine,"before_cursor_execute",capture)
            try:response,ctx=self.render("received","receiver-head",view=view)
            finally:event.remove(engine,"before_cursor_execute",capture)
            self.assertEqual(len(ids),ctx["pagination"].total)
            self.assertEqual(25 if view=="list" else len(ids),len(ctx["tasks"]))
            if view=="board":self.assertEqual(set(ids),set(self.cards(response)));self.assertEqual(len(ids),sum(g["count"] for g in ctx["board_groups"]))
            return len(queries)
        add(36);small={v:read(v) for v in ("board","list")};add(264)
        task_id=ids[0];comment=TaskComment(task_id=task_id,author_id=self.ids["employee"],body="Обсуждение");db.session.add(comment);db.session.flush()
        db.session.add(TaskAttachment(task_id=task_id,comment_id=comment.id,uploaded_by_id=self.ids["employee"],original_filename="example.pdf",storage_key="synthetic-projection.pdf",content_type="application/pdf",size_bytes=20));db.session.commit();db.session.remove()
        for view in ("board","list"):
            large=read(view);self.assertLessEqual(large,small[view]+2);self.assertLessEqual(large,20)
        response,_=self.render("received","receiver-head");card=self.cards(response)[task_id];self.assertEqual((1,1),(card["comment_count"],card["attachment_count"]))
