"""Teaching routes are gone; historical groups and membership remain intact."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.extensions import db
from app.models import Group, User
from support.portal_fixtures import PortalCase


class HistoricalGroupTests(PortalCase):
    def setUp(self):
        super().setUp()
        with self.app.app_context():
            group = Group(title="Скрытая история группы", teacher_id=self.ids["head"])
            group.students.append(db.session.get(User, self.ids["employee"]))
            db.session.add(group)
            db.session.commit()
            self.group_id = group.id

    def test_old_urls_return_no_data_for_any_role(self):
        for role in ("root", "head", "employee"):
            client = self.app.test_client()
            self.login(role, client=client)
            for url in ("/teacher/groups", "/teacher/groups/new", f"/teacher/groups/{self.group_id}", f"/teacher/groups/{self.group_id}/members"):
                self.assertEqual(404, client.get(url).status_code)
                self.assertEqual(404, client.post(url, data={"title":"new","student_id":self.ids["outsider"]}).status_code)
            self.assertNotIn("Мои группы".encode(), client.get("/").data)
        with self.app.app_context():
            group = db.session.get(Group, self.group_id)
            self.assertEqual("Скрытая история группы", group.title)
            self.assertEqual([self.ids["employee"]], [u.id for u in group.students])
            self.assertEqual(1, Group.query.count())

    def test_legacy_teacher_and_member_cannot_be_deleted_with_cascades(self):
        self.login()
        for name in ("head", "employee"):
            response = self.client.post(f'/admin/users/{self.ids[name]}/delete', follow_redirects=True)
            self.assertIn("историческими данными групп".encode(), response.data)
        with self.app.app_context():
            group = db.session.get(Group, self.group_id)
            self.assertEqual(self.ids["head"], group.teacher_id)
            self.assertEqual([self.ids["employee"]], [u.id for u in group.students])
