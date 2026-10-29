"""Disposable loopback UI fixture. Never imports working configuration or data.

Run: python -X utf8 tests/support/navigation_smoke.py
The printed stop-file terminates only this owned server; all storage is temporary.
"""
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from werkzeug.serving import make_server
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from app import create_app
from app.extensions import db
from app.models import Department,Task,User,TaskCategory


def main():
    with TemporaryDirectory(prefix="portal_t012_smoke_") as folder:
        root=Path(folder).resolve();stop=root/"stop";database=root/"synthetic.sqlite3"
        app=create_app(dict(TESTING=True,DATABASE="sqlite:///"+database.as_posix(),SECRET_KEY="synthetic-ui-only",
            ADMIN_USERNAME="smoke-admin",ADMIN_PASSWORD="Synthetic-demo-123",WTF_CSRF_ENABLED=True,TASK_UPLOAD_DIR=str(root/"uploads"),SESSION_COOKIE_SECURE=False))
        # TESTING disables CSRF in the factory; the UI fixture must enable it afterwards.
        app.config["WTF_CSRF_ENABLED"]=True
        with app.app_context():
            departments=Department.query.order_by(Department.id).all();sender,recipient=departments[:2]
            users={}
            for name,role,department in (("smoke-employee","employee",recipient),("smoke-head","head",recipient),("smoke-employee-two","employee",recipient),("smoke-author","employee",sender)):
                user=User(username=name,full_name=name,role=role,department_id=department.id);user.set_password("Synthetic-demo-123");db.session.add(user);users[name]=user
            db.session.flush();now=datetime.now(timezone.utc)
            for index in range(3):db.session.add(Task(title=("Проверить рабочий процесс" if index==0 else f"Поручение {index+1}"),description="Синтетические материалы для проверки T012. Реальных данных нет.",author_id=users["smoke-author"].id,sender_department_id=sender.id,recipient_department_id=recipient.id,status="new",priority="high",due_at=now+timedelta(days=2),sent_at=now,created_at=now))
            db.session.add(TaskCategory(name="Документы",normalized_name="документы"));db.session.commit()
        server=make_server("127.0.0.1",0,app);server.timeout=0.5
        print(json.dumps(dict(url=f"http://127.0.0.1:{server.server_port}",stop_file=str(stop),database=str(database),accounts=["smoke-employee","smoke-head","smoke-admin"],password="Synthetic-demo-123"),ensure_ascii=False),flush=True)
        try:
            while not stop.exists():server.handle_request()
        finally:
            server.server_close()
            with app.app_context():db.session.remove();db.engine.dispose()
        print("Synthetic T012 server stopped; temporary files removed.",flush=True)


if __name__=="__main__":main()
