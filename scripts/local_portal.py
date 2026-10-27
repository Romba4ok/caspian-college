"""Isolated Windows development database and portal launcher. No production config."""
import argparse
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from urllib.request import urlopen
from datetime import datetime, timedelta
from hashlib import sha256
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
HOME = Path(os.environ.get("PORTAL_LOCAL_HOME", Path.home() / ".caspian-college-portal")).resolve()
CONFIG = HOME / "config.json"
HIDDEN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
sys.path.insert(0, str(ROOT))


def run(args, **kwargs):
    result = subprocess.run([str(arg) for arg in args], cwd=ROOT, creationflags=HIDDEN, stdin=subprocess.DEVNULL,
                            capture_output=True, encoding="oem" if os.name == "nt" else "utf-8", errors="replace", **kwargs)
    if result.returncode:
        raise RuntimeError(f"{Path(str(args[0])).name} failed: {result.stderr or result.stdout}")
    return result.stdout.strip()


def load():
    if not CONFIG.exists():
        raise RuntimeError("Run: python scripts/local_portal.py setup")
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    if Path(cfg["project"]).resolve() != ROOT:
        raise RuntimeError("This local configuration belongs to a different project checkout")
    return cfg


def connection(cfg, database="postgres", *, admin=False):
    import psycopg
    return psycopg.connect(host="127.0.0.1", port=cfg["port"], dbname=database,
        user="portal_cluster_admin" if admin else "caspian_portal",
        password=cfg["cluster_password"] if admin else cfg["database_password"],
        connect_timeout=5, autocommit=True)


def database_url(cfg, database="caspian_portal"):
    from sqlalchemy import URL
    return URL.create("postgresql+psycopg", username="caspian_portal", password=cfg["database_password"],
                      host="127.0.0.1", port=cfg["port"], database=database).render_as_string(hide_password=False)


def start_database(cfg):
    data = HOME / "postgres"
    status = subprocess.run([str(Path(cfg["pg_bin"]) / "pg_ctl.exe"), "-D", str(data), "status"],
                            stdin=subprocess.DEVNULL, capture_output=True, creationflags=HIDDEN)
    if status.returncode:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", cfg["port"])) == 0:
                raise RuntimeError("Local database port is occupied by a different process")
        # A long-lived Windows child must not inherit a captured output pipe:
        # pg_ctl can exit while postgres still holds that pipe open.
        started = subprocess.run([str(Path(cfg["pg_bin"]) / "pg_ctl.exe"), "-D", str(data), "-l",
            str(HOME / "logs/postgresql.log"), "-w", "-t", "30", "start"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=HIDDEN, timeout=40)
        if started.returncode:
            raise RuntimeError("PostgreSQL failed to start; see logs/postgresql.log")
    with connection(cfg, admin=True) as conn:
        actual = Path(conn.execute("SHOW data_directory").fetchone()[0]).resolve()
        if actual != data.resolve():
            raise RuntimeError("Database identity check failed; refusing changes")


def application(cfg, *, database="caspian_portal"):
    from app import create_app
    return create_app({"DATABASE": database_url(cfg, database), "SECRET_KEY": cfg["secret_key"],
        "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": cfg["admin_password"],
        "TASK_UPLOAD_DIR": str(HOME / "uploads"), "DEBUG": False})


def setup(pg_bin, port):
    from psycopg import sql
    HOME.mkdir(parents=True, exist_ok=True)
    account = run(["whoami"])
    run(["icacls", HOME, "/inheritance:r", "/grant:r", f"{account}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"])
    (HOME / "logs").mkdir(exist_ok=True)
    if not CONFIG.exists():
        cfg = {"project": str(ROOT), "pg_bin": str(Path(pg_bin).resolve()), "port": port,
               "cluster_password": secrets.token_urlsafe(32), "database_password": secrets.token_urlsafe(32),
               "secret_key": secrets.token_urlsafe(48), "admin_password": secrets.token_urlsafe(18)}
        with CONFIG.open("x", encoding="utf-8") as output:
            json.dump(cfg, output, indent=2)
    cfg = load()
    data = HOME / "postgres"
    if not (data / "PG_VERSION").exists():
        if data.exists() and any(data.iterdir()):
            raise RuntimeError("Unrecognised non-empty cluster directory; refusing to overwrite")
        password_file = HOME / "bootstrap-password.tmp"
        try:
            password_file.write_text(cfg["cluster_password"], encoding="utf-8")
            run([Path(cfg["pg_bin"]) / "initdb.exe", "-D", data, "-U", "portal_cluster_admin",
                 "--pwfile", password_file, "--encoding=UTF8", "--locale=C",
                 "--auth-host=scram-sha-256", "--auth-local=scram-sha-256"])
        finally:
            password_file.unlink(missing_ok=True)
        with (data / "postgresql.conf").open("a", encoding="utf-8") as output:
            output.write(f"\nlisten_addresses = '127.0.0.1'\nport = {cfg['port']}\nmax_connections = 30\nshared_buffers = '64MB'\n")
    start_database(cfg)
    with connection(cfg, admin=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname='caspian_portal'").fetchone():
            conn.execute(sql.SQL("CREATE ROLE caspian_portal LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(sql.Literal(cfg["database_password"])))
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname='caspian_portal'").fetchone():
            conn.execute("CREATE DATABASE caspian_portal OWNER caspian_portal")
    app = application(cfg)
    from flask_migrate import upgrade
    with app.app_context():
        upgrade(directory=str(ROOT / "migrations"))
    print("Local PostgreSQL and portal database are ready. Credentials: python scripts/local_portal.py credentials")


def reminders(cfg):
    start_database(cfg)
    app = application(cfg)
    result = app.test_cli_runner().invoke(args=["send-task-reminders"])
    if result.exit_code:
        raise RuntimeError("Reminder CLI failed; database transaction was rolled back")
    logging.info("Reminder CLI completed: %s", result.output.strip())
    if sys.stdout:
        print(result.output.strip())


def schedule(cfg):
    sid = run(["powershell.exe", "-NoProfile", "-Command", "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value"])
    suffix = sha256(str(ROOT).encode()).hexdigest()[:8]
    for label, command in [("Database", "start-db"), ("Reminders", "reminders")]:
        name = f"CaspianPortal-Local-{label}-{suffix}"
        query = subprocess.run(["schtasks.exe", "/Query", "/TN", name, "/XML"], stdin=subprocess.DEVNULL, capture_output=True, creationflags=HIDDEN)
        if query.returncode == 0:
            existing = query.stdout.decode("utf-16" if query.stdout.startswith(b'\xff\xfe') else "oem", errors="replace")
            if str(ROOT) not in existing:
                raise RuntimeError(f"Task {name} is not owned by this checkout")
        ns = "http://schemas.microsoft.com/windows/2004/02/mit/task"
        ET.register_namespace("", ns)
        task = ET.Element(f"{{{ns}}}Task", version="1.2")
        def child(parent, tag, value=None):
            element = ET.SubElement(parent, tag)
            if value is not None:
                element.text = value
            return element
        info = child(task, "RegistrationInfo")
        child(info, "Description", "Caspian College Portal local runtime: " + str(ROOT))
        triggers = child(task, "Triggers")
        if command == "start-db":
            trigger = child(triggers, "LogonTrigger")
            child(trigger, "Enabled", "true")
            child(trigger, "UserId", sid)
        else:
            trigger = child(triggers, "TimeTrigger")
            repetition = child(trigger, "Repetition")
            child(repetition, "Interval", "PT15M")
            child(repetition, "StopAtDurationEnd", "false")
            child(trigger, "StartBoundary", (datetime.now()+timedelta(minutes=1)).isoformat(timespec="seconds"))
            child(trigger, "Enabled", "true")
        principal = child(child(task, "Principals"), "Principal")
        principal.set("id", "CurrentUser")
        child(principal, "UserId", sid)
        child(principal, "LogonType", "InteractiveToken")
        child(principal, "RunLevel", "LeastPrivilege")
        settings = child(task, "Settings")
        for tag, value in [("MultipleInstancesPolicy","IgnoreNew"),("DisallowStartIfOnBatteries","false"),
                           ("StopIfGoingOnBatteries","false"),("StartWhenAvailable","true"),
                           ("Enabled","true"),("ExecutionTimeLimit","PT5M")]:
            child(settings, tag, value)
        actions = child(task, "Actions")
        actions.set("Context", "CurrentUser")
        action = child(actions, "Exec")
        # A private bootstrap supplies standard streams for a GUI Python process
        # and keeps the scheduler's entry path independent of the checkout path.
        wrapper = HOME / f"scheduled-{label.lower()}.py"
        bootstrap = ("import os, runpy, sys\n"
            f"sys.stdout = open({str(HOME/'logs'/(label.lower()+'-task.log'))!r}, 'a', encoding='utf-8', buffering=1)\n"
            "sys.stderr = sys.stdout\nsys.stdin = open(os.devnull)\n"
            f"print('Starting {command}', flush=True)\n"
            f"sys.argv = [{str(Path(__file__).resolve())!r}, {command!r}, '--home', {str(HOME)!r}]\n"
            f"runpy.run_path({str(Path(__file__).resolve())!r}, run_name='__main__')\n")
        wrapper.write_text(bootstrap, encoding="utf-8")
        child(action, "Command", str(Path(sys.executable).with_name("pythonw.exe")))
        child(action, "Arguments", f'"{wrapper}"')
        child(action, "WorkingDirectory", str(ROOT))
        xml = HOME / f"{label.lower()}-task.xml"
        ET.ElementTree(task).write(xml, encoding="utf-16", xml_declaration=True)
        run(["schtasks.exe", "/Create", "/TN", name, "/XML", xml, "/F"])
        print(name)


def verify(cfg, pattern="test_*.py"):
    from psycopg import sql
    start_database(cfg)
    name = "portal_verify_" + secrets.token_hex(6)
    with connection(cfg, admin=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {} OWNER caspian_portal").format(sql.Identifier(name)))
    try:
        env = os.environ.copy()
        env["PORTAL_TEST_DATABASE_URL"] = database_url(cfg, name)
        result = subprocess.run([sys.executable,"-m","unittest","discover","-s","tests","-p",pattern,"-v"], cwd=ROOT, env=env)
        return result.returncode
    finally:
        with connection(cfg, admin=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def site_pid():
    marker = HOME / "site.pid"
    if not marker.exists():
        return None
    pid = int(marker.read_text().strip())
    command = run(["powershell.exe", "-NoProfile", "-Command",
                   f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"])
    return pid if str(Path(__file__).resolve()) in command and " serve" in command else None


def start_site(cfg):
    if site_pid():
        print("Site already running: http://127.0.0.1:5000")
        return
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 5000)) == 0:
            raise RuntimeError("Port 5000 is occupied; refusing to replace an unknown process")
    start_database(cfg)
    with (HOME / "logs/site.log").open("ab") as log:
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "serve", "--home", str(HOME)],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=HIDDEN)
    (HOME / "site.pid").write_text(str(process.pid))
    for _ in range(60):
        try:
            with urlopen("http://127.0.0.1:5000/login", timeout=1) as response:
                if response.status == 200:
                    print("Site running: http://127.0.0.1:5000")
                    return
        except OSError:
            if process.poll() is not None:
                break
            time.sleep(.2)
    raise RuntimeError("Site did not become ready; see logs/site.log")


def main():
    global HOME, CONFIG
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["setup","start-db","stop-db","serve","start-site","stop-site","reminders","schedule","verify","credentials","status"])
    parser.add_argument("--pg-bin", default="D:/PostgreSql/17/bin")
    parser.add_argument("--port", type=int, default=55432)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--pattern", default="test_*.py", help="Unittest discovery pattern for verify (default: full suite)")
    args = parser.parse_args()
    if args.home:
        HOME = args.home.resolve()
        CONFIG = HOME / "config.json"
    if args.command == "setup":
        setup(args.pg_bin, args.port)
        return 0
    (HOME / "logs").mkdir(exist_ok=True)
    logging.basicConfig(filename=HOME/"logs/runtime.log", encoding="utf-8", level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load()
    if args.command == "start-db":
        start_database(cfg)
    elif args.command == "stop-db":
        run([Path(cfg["pg_bin"])/"pg_ctl.exe","-D",HOME/"postgres","-m","fast","-w","stop"])
    elif args.command == "serve":
        start_database(cfg)
        application(cfg).run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
    elif args.command == "start-site":
        start_site(cfg)
    elif args.command == "stop-site":
        pid = site_pid()
        if pid:
            run(["taskkill.exe", "/PID", str(pid), "/F"])
        (HOME / "site.pid").unlink(missing_ok=True)
        print("Local site stopped")
    elif args.command == "reminders":
        reminders(cfg)
    elif args.command == "schedule":
        schedule(cfg)
    elif args.command == "verify":
        return verify(cfg, args.pattern)
    elif args.command == "credentials":
        print("Initial portal username: admin")
        print("Initial portal password:",cfg["admin_password"])
    elif args.command == "status":
        start_database(cfg)
        with connection(cfg, "caspian_portal") as conn:
            print("PostgreSQL:",conn.execute("SELECT version()").fetchone()[0])
            print("Database: caspian_portal; host: 127.0.0.1; port:",cfg["port"])
            print("Migration:",conn.execute("SELECT version_num FROM alembic_version").fetchone()[0])
            print("Role superuser:",conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user").fetchone()[0])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        message = str(error)
        if CONFIG.exists():
            for key,value in json.loads(CONFIG.read_text(encoding="utf-8")).items():
                if "password" in key or key == "secret_key":
                    message = message.replace(value,"***")
        logging.error("Local runtime failed: %s",message)
        if sys.stderr:
            print(message,file=sys.stderr)
        sys.exit(1)
