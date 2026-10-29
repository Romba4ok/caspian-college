"""Exercise the real shell entrypoint with isolated command boundaries."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


class StartupScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='portal_startup_')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.bin=self.root/'bin';self.bin.mkdir()
        self.log=self.root/'commands.log'
        self.uploads=self.root/'uploads';self.uploads.mkdir()
        (self.uploads/'existing').write_bytes(b'preserve synthetic content')
        shell=shutil.which('sh')
        if os.name=='nt':
            bash=Path('C:/Program Files/Git/bin/bash.exe')
            self.shell=[str(bash),'--noprofile','--norc'] if bash.is_file() else None
        else:self.shell=[shell] if shell else None
        if not self.shell:self.skipTest('A POSIX shell is required for entrypoint checks')
        python_stub="""#!/bin/sh
if [ "$*" != '-m flask --app app:create_app db upgrade' ]; then
 printf 'unexpected:%s\\n' "$*" >> "$STARTUP_TEST_LOG"
 exit 77
fi
printf 'migrate|%s|%s\\n' "$DATABASE_URL" "$*" >> "$STARTUP_TEST_LOG"
exit "${STARTUP_TEST_MIGRATION_EXIT:-0}"
"""
        gunicorn_stub="""#!/bin/sh
printf 'serve|%s|%s\\n' "$DATABASE_URL" "$*" >> "$STARTUP_TEST_LOG"
"""
        for name,text in [('python',python_stub),('gunicorn',gunicorn_stub)]:
            p=self.bin/name;p.write_text(text,encoding='utf-8');p.chmod(0o755)
        self.env=dict(os.environ,PATH=str(self.bin)+os.pathsep+os.environ.get('PATH',''),
            DATABASE_URL='postgres://synthetic:synthetic@owned-db/portal',
            SECRET_KEY='synthetic-startup-key',PORT='12345',
            TASK_UPLOAD_DIR=str(self.uploads),STARTUP_TEST_LOG=self.log.as_posix())
        for key in ('BASH_ENV','ENV','STARTUP_TEST_MIGRATION_EXIT'):self.env.pop(key,None)

    def invoke(self,**changes):
        env=self.env.copy()
        for key,value in changes.items():
            if value is None:env.pop(key,None)
            else:env[key]=value
        return subprocess.run(self.shell+[str(ROOT/'deploy/start.sh')],cwd=self.root,
            env=env,capture_output=True,text=True,timeout=20)

    def rows(self):
        return [line.split('|',2) for line in self.log.read_text(encoding='utf-8').splitlines()] if self.log.exists() else []

    def test_entrypoint_uses_posix_line_endings(self):
        self.assertNotIn(b"\r",(ROOT/"deploy/start.sh").read_bytes())

    def test_migrates_before_wsgi_without_demo_restore(self):
        result=self.invoke();self.assertEqual(result.returncode,0,result.stderr)
        rows=self.rows();self.assertEqual([r[0] for r in rows],['migrate','serve'])
        self.assertEqual(rows[1][2], 'app:create_app() --bind 0.0.0.0:12345 --workers 1 --threads 4 --timeout 60 --access-logfile - --error-logfile -')
        self.assertEqual([p.name for p in self.uploads.iterdir()],['existing'])
        self.assertEqual((self.uploads/'existing').read_bytes(),b'preserve synthetic content')

    def test_supported_postgresql_urls_are_normalized_for_psycopg3(self):
        for prefix in ('postgres://','postgresql://','postgresql+psycopg://'):
            with self.subTest(prefix=prefix):
                self.log.unlink(missing_ok=True)
                result=self.invoke(DATABASE_URL=prefix+'synthetic:synthetic@owned-db/portal')
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertTrue(all(row[1]=='postgresql+psycopg://synthetic:synthetic@owned-db/portal' for row in self.rows()))

    def test_missing_required_environment_stops_before_any_command(self):
        for key in ('DATABASE_URL','SECRET_KEY'):
            with self.subTest(key=key):
                result=self.invoke(**{key:None})
                self.assertNotEqual(result.returncode,0)
                self.assertEqual(self.rows(),[])
                self.assertNotIn('synthetic:synthetic',result.stderr)

    def test_failed_migration_never_starts_wsgi(self):
        result=self.invoke(STARTUP_TEST_MIGRATION_EXIT='67')
        self.assertEqual(result.returncode,67,result.stderr)
        self.assertEqual([r[0] for r in self.rows()],['migrate'])

    def test_default_hosting_port_is_preserved(self):
        result=self.invoke(PORT=None);self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('--bind 0.0.0.0:10000',self.rows()[1][2])


if __name__=='__main__':unittest.main()
