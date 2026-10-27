"""Shared synthetic accounts; no connection to the working portal database."""
from datetime import datetime, timezone
import unittest

from app import create_app
from app.extensions import db
from app.models import Department, Task, User


class PortalCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "DATABASE": ":memory:", "SECRET_KEY": "synthetic-tests",
            "ADMIN_USERNAME": "root", "ADMIN_PASSWORD": "root-password"})
        self.client = self.app.test_client()
        with self.app.app_context():
            departments = Department.query.order_by(Department.id).all()
            self.department_id, self.other_department_id, self.third_department_id = departments[0].id, departments[1].id, departments[2].id
            template = User(full_name="Сотрудник", username="employee", role="employee", department_id=self.department_id)
            template.set_password("member-password")
            db.session.add(template)
            for name, role, dept in (("head", "head", self.department_id), ("outsider", "employee", self.third_department_id)):
                db.session.add(User(full_name=name, username=name, role=role, department_id=dept, password_hash=template.password_hash))
            db.session.commit()
            self.ids = {u.username: u.id for u in User.query.all()}
            with db.engine.connect() as conn:
                conn.exec_driver_sql("PRAGMA foreign_keys=ON")

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            self.app.extensions["memory_db"].close()
            db.engine.dispose()

    def login(self, username="root", password=None, *, client=None, follow=True):
        return (client or self.client).post("/login", data={"username": username,
            "password": password or ("root-password" if username == "root" else "member-password")}, follow_redirects=follow)

    def form(self, username="created", **changes):
        return {**dict(full_name="Новый сотрудник", username=username, role="employee",
                       department_id=str(self.department_id), password="member-password"), **changes}

    def account(self, username):
        with self.app.app_context():
            u = User.query.filter_by(username=username).one_or_none()
            return None if u is None else {key: getattr(u, key) for key in
                ("id", "full_name", "username", "role", "department_id", "department_role", "is_blocked", "password_hash", "language")}

    def task(self, **changes):
        values = dict(title="Сохранить задачу", description="Исторический текст", author_id=self.ids["employee"],
            sender_department_id=self.department_id, recipient_department_id=self.other_department_id,
            due_at=datetime(2030, 1, 1, tzinfo=timezone.utc), status="draft")
        with self.app.app_context():
            task = Task(**(values | changes))
            db.session.add(task)
            db.session.commit()
            return task.id

    def draft_form(self, **changes):
        return dict(title="Подготовить документы", description="Нужны документы к встрече",
            recipient_department_id=str(self.other_department_id), due_at="2030-06-01T12:00",
            priority="normal", category_id="", contact_person="") | changes

    def add_member(self, username, *, role="employee", department_id=None, blocked=False):
        with self.app.app_context():
            user = User(full_name=username, username=username, role=role,
                department_id=department_id if department_id is not None else self.department_id,
                is_blocked=blocked, password_hash=db.session.get(User, self.ids["employee"]).password_hash)
            db.session.add(user)
            db.session.commit()
            self.ids[username] = user.id
            return user.id

    def task_batch(self, count, *, statuses=("draft",), **changes):
        """Create many synthetic cards in one transaction, never in the portal DB."""
        with self.app.app_context():
            rows = []
            for index in range(count):
                status = statuses[index % len(statuses)]
                values = dict(title=f"Поручение {index + 1:03d}", description="Синтетический пример",
                    author_id=self.ids["employee"], sender_department_id=self.department_id,
                    recipient_department_id=self.other_department_id,
                    due_at=datetime(2030, 1, 1, tzinfo=timezone.utc), status=status,
                    sent_at=None if status in ("draft", "pending_approval") else datetime(2029, 12, 1, tzinfo=timezone.utc))
                rows.append(Task(**(values | changes)))
            db.session.add_all(rows)
            db.session.flush()
            ids = [row.id for row in rows]
            db.session.commit()
            return ids
