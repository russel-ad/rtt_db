import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from app_update import APP_ENTRY, APP_ID
from app_update.core import LockBusy, UpdateError, atomic_json, digest, file_lock, read_json
from app_update.sessions import other_instances
from app_update.bootstrap import copy_bootstrap, install_bootstrap

ROOT = Path(__file__).resolve().parents[1]


class MultiwindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'updates'

    def test_shared_sessions_exclude_install_until_last_window_exits(self):
        lock = self.home / 'session.lock'
        with file_lock(lock) as first:
            first.downgrade()
            with file_lock(lock, shared=True):
                with self.assertRaises(LockBusy), file_lock(lock):
                    pass
            with self.assertRaises(LockBusy), file_lock(lock):
                pass
        with file_lock(lock):
            pass

    @unittest.skipUnless(os.name == 'nt', 'Windows CRT legacy interoperability')
    def test_legacy_launcher_and_repair_lock_cannot_be_bypassed(self):
        import msvcrt
        lock = self.root / 'legacy.lock'
        lock.write_bytes(b'0')
        with lock.open('r+b') as stream:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                with self.assertRaises(LockBusy), file_lock(lock, shared=True):
                    pass
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            with file_lock(lock, shared=True):
                with self.assertRaises(OSError):
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)

    def test_lease_files_do_not_imply_running_process(self):
        with file_lock(self.home / 'sessions/one.lock'):
            self.assertFalse(other_instances(self.home, 'one'))
            self.assertTrue(other_instances(self.home, 'two'))
        self.assertFalse(other_instances(self.home, 'two'))

    def test_new_launcher_identifies_old_bundled_app_after_migration(self):
        from app_update.launcher import base_version
        app = self.root / 'app/2.10.9'
        app.mkdir(parents=True)
        (app / APP_ENTRY).write_bytes(b'old app')
        atomic_json(app/'app-release.json',{'app_id':APP_ID,'entry':APP_ENTRY,'version':'2.10.9'})
        self.assertEqual(base_version(self.root),'2.10.9')

    def test_two_real_launchers_overlap_and_abandoned_leases_release(self):
        app = self.root / 'bundle/app/2.10.0'
        app.mkdir(parents=True)
        (app / APP_ENTRY).write_bytes(b'fixture')
        child = '''import os,time
from pathlib import Path
from app_update.core import atomic_json
h=Path(os.environ['RTT_UPDATE_HOME'])
n=os.environ['RTT_UPDATE_NONCE']
atomic_json(Path(os.environ['RTT_UPDATE_HEALTH']), {'version':os.environ['RTT_UPDATE_VERSION'],'nonce':n})
atomic_json(h/(n+'.ready'), {'nonce':n})
while not (h/(n+'.stop')).exists(): time.sleep(.05)
'''
        parent = '''import sys,subprocess
from pathlib import Path
from unittest.mock import patch
from app_update.launcher import run_application
real=subprocess.Popen
def spawn(args, **kwargs): return real([sys.executable,'-c',sys.argv[3]],**kwargs)
with patch('app_update.launcher.subprocess.Popen',side_effect=spawn):
 run_application(Path(sys.argv[1]),Path(sys.argv[2]),{},bundled_version='2.10.0')
'''
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        processes = []
        try:
            for i in range(2):
                processes.append(subprocess.Popen([sys.executable, '-c', parent,
                    str(self.root/'bundle'), str(self.home), child], env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
                deadline = time.monotonic() + 15
                while len(list(self.home.glob('*.ready'))) < i+1 and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertEqual(len(list(self.home.glob('*.ready'))), i+1)
                self.assertIsNone(processes[i].poll())
            with self.assertRaises(LockBusy), file_lock(self.home/'session.lock'):
                pass
            ready = sorted(self.home.glob('*.ready'))
            ready[0].with_suffix('.stop').touch()
            deadline = time.monotonic() + 5
            while all(p.poll() is None for p in processes) and time.monotonic() < deadline:
                time.sleep(.05)
            with self.assertRaises(LockBusy), file_lock(self.home/'session.lock'):
                pass
            ready[1].with_suffix('.stop').touch()
            for p in processes:
                self.assertEqual(p.wait(timeout=10), 0)
            with file_lock(self.home/'session.lock'):
                pass
            self.assertFalse(other_instances(self.home, 'new-window'))
        finally:
            for ready in self.home.glob('*.ready'):
                ready.with_suffix('.stop').touch()
            for p in processes:
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
                p.stderr.close()

    def bootstrap(self):
        source = self.root / 'source'
        runtime = source / '_launcher_test'
        runtime.mkdir(parents=True)
        (source/'RTTTool.exe').write_bytes(b'new launcher')
        (runtime/'runtime.dll').write_bytes(b'new runtime')
        atomic_json(source/'bootstrap-files.json', {'revision':2, 'runtime':runtime.name,
            'files':{p.relative_to(source).as_posix():digest(p) for p in source.rglob('*') if p.is_file()}})
        base = self.root/'base'
        base.mkdir()
        (base/'RTTTool.exe').write_bytes(b'old launcher')
        (base/'updater.json').write_bytes(b'keep settings')
        return source, base

    def test_migration_replaces_exe_keeps_settings_and_is_repeatable(self):
        source, base = self.bootstrap()
        for _ in range(2):
            self.assertEqual(install_bootstrap(base,self.home,source=source),0)
        self.assertEqual((base/'RTTTool.exe').read_bytes(),b'new launcher')
        self.assertEqual((base/'updater.json').read_bytes(),b'keep settings')

    def test_migration_rejects_corruption_before_replacing_old_exe(self):
        source, base = self.bootstrap()
        (source/'_launcher_test/runtime.dll').write_bytes(b'changed')
        self.assertEqual(install_bootstrap(base,self.home,source=source),1)
        self.assertEqual((base/'RTTTool.exe').read_bytes(),b'old launcher')
        self.assertFalse(read_json(self.home/'bootstrap-result.json')['installed'])

    def test_migration_never_overwrites_existing_runtime(self):
        source, base = self.bootstrap()
        (base/'_launcher_test').mkdir()
        (base/'_launcher_test/runtime.dll').write_bytes(b'other build')
        with self.assertRaises(UpdateError):
            copy_bootstrap(source,base)
        self.assertEqual((base/'RTTTool.exe').read_bytes(),b'old launcher')
        self.assertEqual((base/'_launcher_test/runtime.dll').read_bytes(),b'other build')

    def test_two_windows_block_gui_restart_but_stale_leases_do_not(self):
        from app_update.qt_client import busy_reason
        with patch.dict(os.environ,RTT_UPDATE_HOME=str(self.home),RTT_UPDATE_SESSION='one'):
            with file_lock(self.home/'sessions/two.lock'):
                self.assertIn('其他上位机窗口',busy_reason(object()))
        self.assertFalse(other_instances(self.home,'one'))


if __name__ == '__main__':
    unittest.main()
