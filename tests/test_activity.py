"""Only committed business changes may appear as named employee activity."""
import json
from unittest.mock import patch
from app.activity import logger,activity_payload
from app.accounts import create_account
from app.audit import record_admin_event
from app.extensions import db
from app.models import AdminAuditEvent,User
from app.workflow import create_task,make_creation_key
from datetime import datetime,timezone
from support.portal_fixtures import PortalCase


class ActivityTests(PortalCase):
    def test_legacy_metadata_cannot_put_credentials_in_activity_fields(self):
        payload=activity_payload(AdminAuditEvent,dict(id=1,actor_snapshot={"id":"token=PRIVATE","name":"Bearer PRIVATE"},
            object_id=2,action="secret=PRIVATE",details={"after":{"full_name":"password=PRIVATE"}}))
        self.assertNotIn("PRIVATE",json.dumps(payload))
        self.assertIsNone(payload["actor"]["id"])

    def payloads(self,info):
        return [json.loads(call.args[0])["caspian_activity"] for call in info.call_args_list]

    def test_task_action_waits_for_commit_and_keeps_author_and_crm_origin(self):
        with self.app.app_context(),patch.object(logger,"info") as info:
            author=db.session.get(User,self.ids["employee"])
            task=create_task(actor=author,recipient_department_id=self.other_department_id,title="Живое действие",
                description="PRIVATE DESCRIPTION",due_at=datetime(2030,1,1,tzinfo=timezone.utc),creation_key=make_creation_key(author))
            event=task.events[0];event.details={**event.details,"crm":{"source":"crm","event_id":"x"}}
            info.assert_not_called();db.session.commit()
            values=self.payloads(info)
            self.assertEqual(1,len(values));self.assertEqual(author.id,values[0]["actor"]["id"])
            self.assertIn("из CRM",values[0]["message"])
            self.assertIn("Живое действие",values[0]["message"])
            self.assertNotIn("PRIVATE DESCRIPTION",str(values))

    def test_password_change_has_actor_and_target_without_password_or_hash(self):
        self.login()
        with patch.object(logger,"info") as info:
            response=self.client.post(f'/admin/users/{self.ids["employee"]}/edit',data=self.form("employee",password="PRIVATE_PASSWORD"))
        self.assertEqual(302,response.status_code)
        values=self.payloads(info)
        self.assertEqual(1,len(values));self.assertEqual(self.ids["root"],values[0]["actor"]["id"])
        self.assertIn("Сменил пароль пользователя",values[0]["message"])
        self.assertNotIn("PRIVATE_PASSWORD",str(values));self.assertNotIn("password_hash",str(values))

    def test_outer_rollback_never_publishes_action(self):
        with self.app.app_context(),patch.object(logger,"info") as info:
            create_account(actor=db.session.get(User,self.ids["root"]),full_name="Не сохранён",username="rollback",password="PRIVATE",role="employee",department_id=self.department_id)
            db.session.rollback();db.session.commit()
            info.assert_not_called()
            self.assertIsNone(User.query.filter_by(username="rollback").first())

    def test_savepoint_commit_is_not_published_until_outer_commit_and_rollback_is_discarded(self):
        with self.app.app_context(),patch.object(logger,"info") as info:
            actor=db.session.get(User,self.ids["root"])
            record_admin_event("account_updated","user",self.ids["employee"],actor=actor,after={"full_name":"Внешнее действие"})
            db.session.flush()
            try:
                with db.session.begin_nested():
                    record_admin_event("account_updated","user",self.ids["employee"],actor=actor,after={"full_name":"Откаченное действие"})
                    db.session.flush();raise ValueError("rollback-savepoint")
            except ValueError:pass
            with db.session.begin_nested():
                record_admin_event("account_updated","user",self.ids["employee"],actor=actor,after={"full_name":"Вложенное действие"})
                db.session.flush()
            info.assert_not_called();db.session.commit()
            values=self.payloads(info)
            self.assertEqual(2,len(values));self.assertNotIn("Откаченное действие",str(values))

    def test_logger_failure_does_not_break_committed_change_and_login_has_no_activity(self):
        with patch.object(logger,"info") as info:
            self.login();self.client.post("/logout")
            info.assert_not_called()
        with self.app.app_context(),patch.object(logger,"info",side_effect=RuntimeError("logging-only")),patch.object(logger,"warning"):
            user=create_account(actor=db.session.get(User,self.ids["root"]),full_name="Сохранён",username="saved",password="PRIVATE",role="employee",department_id=self.department_id)
            db.session.commit()
            self.assertIsNotNone(db.session.get(User,user.id))
