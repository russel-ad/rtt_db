import base64
from datetime import datetime, timedelta, timezone
import io
import itertools
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile
import ssl
import urllib.error

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from app_update import APP_ENTRY, APP_ID
from app_update.core import (UpdateError, archive_path, atomic_json, canonical, check_and_stage,
                             digest, file_lock, https_url, install_pending, read_json, rollback, safe_members, verify)
from app_update.publish import make_release


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'updates'
        self.key = Ed25519PrivateKey.generate()
        self.app = self.root / 'app'
        self.app.mkdir()
        (self.app / APP_ENTRY).write_bytes(b'fixture executable')
        (self.app / '_internal').mkdir()
        (self.app / '_internal' / 'component.dll').write_bytes(b'new DLL')
        atomic_json(self.app / 'app-release.json', {'app_id': APP_ID, 'version': '2.10.1', 'entry': APP_ENTRY})
        self.envelope = make_release(self.app, self.root / 'release', '2.10.1',
                                     'https://example.org/v2.10.1/app.zip', self.key)
        public = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.keys = {self.envelope['key_id']: base64.b64encode(public).decode()}
        self.settings = {'enabled': True, 'feed_url': 'https://example.org/latest.json', 'public_keys': self.keys}
        self.package = self.root / 'release' / 'RTTTool-2.10.1-windows-x64.zip'

    def opener(self, url):
        if url.endswith('latest.json'):
            return io.BytesIO(json.dumps(self.envelope).encode())
        return io.BytesIO(self.package.read_bytes())

    def stage(self):
        return check_and_stage(self.home, self.settings, '2.10.0', opener=self.opener)

    def resign(self):
        self.envelope['signature'] = base64.b64encode(self.key.sign(canonical(self.envelope['payload']))).decode()

    def test_full_package_is_staged_then_switched_and_can_rollback_without_touching_data(self):
        data = self.root / 'data' / 'private' / 'workbench' / 'cache.json'
        data.parent.mkdir(parents=True)
        data.write_bytes(b'keep current account data')
        self.stage()
        self.assertFalse((self.home / 'current.json').exists())
        with file_lock(self.home / 'session.lock'):
            self.assertTrue(install_pending(self.home, self.settings, '2.10.0'))
        self.assertEqual((self.home / 'versions/2.10.1/_internal/component.dll').read_bytes(), b'new DLL')
        self.assertTrue(read_json(self.home / 'current.json')['trial'])
        self.assertTrue(rollback(self.home))
        state = read_json(self.home / 'current.json')
        self.assertEqual(state['current'], '2.10.0')
        self.assertIn('2.10.1', state['failed_versions'])
        self.assertIsNone(self.stage())
        self.assertEqual(data.read_bytes(), b'keep current account data')

    def test_signature_tampering_never_downloads_or_stages(self):
        self.envelope['payload']['url'] = 'https://example.org/untrusted.zip'
        with self.assertRaises(UpdateError):
            self.stage()
        self.assertFalse((self.home / 'pending.json').exists())

    def test_download_progress_reports_real_bytes_before_package_is_staged(self):
        events = []
        class PartialReader(io.BytesIO):
            def read(self, amount=-1):
                return super().read(min(amount, 31))
        def opener(url):
            return self.opener(url) if url.endswith('latest.json') else PartialReader(self.package.read_bytes())
        def observe(event):
            self.assertEqual((self.home/'pending.json').exists(), event['phase'] == 'ready')
            events.append(event)
        clock = itertools.count(1, 0.25)
        with patch('app_update.core.time.monotonic', side_effect=lambda: next(clock)):
            check_and_stage(self.home, self.settings, '2.10.0', opener=opener, on_transfer=observe)
        received = [item['received'] for item in events]
        self.assertEqual(received, sorted(received))
        self.assertEqual(received[0], 0)
        self.assertEqual(received[-1], self.package.stat().st_size)
        self.assertTrue(any(0 < item['received'] < item['total'] for item in events))
        self.assertTrue(any(item['speed'] > 0 for item in events if item['phase'] == 'download'))
        self.assertEqual(events[-2]['phase'], 'verify')
        self.assertEqual(events[-1]['phase'], 'ready')
        self.assertTrue(all(0 <= item['received'] <= item['total'] for item in events))

    def test_invalid_download_cannot_report_ready(self):
        events = []
        def opener(url):
            return self.opener(url) if url.endswith('latest.json') else io.BytesIO(b'x' * self.package.stat().st_size)
        with self.assertRaises(UpdateError):
            check_and_stage(self.home, self.settings, '2.10.0', opener=opener, on_transfer=events.append)
        self.assertNotIn('ready', [item['phase'] for item in events])
        self.assertFalse((self.home/'pending.json').exists())

    def test_verified_cached_package_reports_full_size_without_redownload(self):
        self.stage()
        events = []
        def manifest_only(url):
            self.assertTrue(url.endswith('latest.json'))
            return self.opener(url)
        check_and_stage(self.home, self.settings, '2.10.0', opener=manifest_only, on_transfer=events.append)
        self.assertIn('cached', [item['phase'] for item in events])
        self.assertEqual(events[-1]['received'], self.package.stat().st_size)
        self.assertEqual(events[-1]['phase'], 'ready')

    def test_expired_future_wrong_platform_and_missing_key_are_rejected(self):
        now = datetime.now(timezone.utc)
        for field, value in [('expires_at', (now-timedelta(days=1)).isoformat()),
                             ('published_at', (now+timedelta(days=1)).isoformat()),
                             ('platform', 'other'), ('min_launcher', 100)]:
            with self.subTest(field=field):
                original = self.envelope['payload'][field]
                self.envelope['payload'][field] = value
                self.resign()
                with self.assertRaises(UpdateError):
                    verify(self.envelope, self.keys)
                self.envelope['payload'][field] = original
        self.resign()
        with self.assertRaises(UpdateError):
            verify(self.envelope, {})

    def test_interrupted_and_extra_download_bytes_leave_no_installable_pending(self):
        for data in (self.package.read_bytes()[:-1], self.package.read_bytes()+b'changed'):
            with self.subTest(length=len(data)):
                def opener(url):
                    return self.opener(url) if url.endswith('latest.json') else io.BytesIO(data)
                with patch('app_update.download.time.sleep'), self.assertRaises(UpdateError):
                    check_and_stage(self.home, self.settings, '2.10.0', opener=opener)
                self.assertFalse((self.home / 'pending.json').exists())

    def test_staged_archive_is_reverified_before_installing(self):
        payload = self.stage()
        archive_path(self.home, payload).write_bytes(b'changed after download')
        with self.assertRaises(UpdateError):
            install_pending(self.home, self.settings, '2.10.0')
        self.assertFalse((self.home / 'current.json').exists())

    def test_older_manifest_and_replaced_same_version_are_rejected(self):
        self.stage()
        for key, value in [('version', '2.9.9'), ('sha256', '0'*64)]:
            previous = self.envelope['payload'][key]
            self.envelope['payload'][key] = value
            self.resign()
            with self.assertRaises(UpdateError):
                self.stage()
            self.envelope['payload'][key] = previous
        self.resign()

    def test_extract_rejects_windows_traversal_and_links(self):
        for name in ('../escape', '/absolute', 'C:/drive', 'x\\..\\escape', 'AUX.txt', 'safe/COM1', 'trailing.', 'x:ads'):
            with self.subTest(name=name):
                stream = io.BytesIO()
                with zipfile.ZipFile(stream, 'w') as archive:
                    archive.writestr(name, b'x')
                with zipfile.ZipFile(stream) as archive, self.assertRaises(UpdateError):
                    safe_members(archive)
        stream = io.BytesIO()
        link = zipfile.ZipInfo('link')
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr(link, '../outside')
        with zipfile.ZipFile(stream) as archive, self.assertRaises(UpdateError):
            safe_members(archive)

    def test_release_packager_rejects_user_data_and_version_relabel(self):
        (self.app / 'private').mkdir()
        with self.assertRaises(UpdateError):
            make_release(self.app, self.root/'other', '2.10.1', 'https://example.org/app.zip', self.key)
        with self.assertRaises(UpdateError):
            make_release(self.app, self.root/'other', '9.0.0', 'https://example.org/app.zip', self.key)

    def test_session_lock_excludes_another_launcher(self):
        with file_lock(self.home / 'session.lock'):
            with self.assertRaises(UpdateError):
                with file_lock(self.home / 'session.lock'):
                    self.fail('A second launcher acquired the active session')

    def test_transient_windows_directory_sharing_failure_preserves_old_pointer_until_switch(self):
        from app_update.core import switch_directory
        staging = self.root/'staging'
        target = self.root/'new-version'
        staging.mkdir()
        error = PermissionError('Fixture sharing violation')
        error.winerror = 32
        original = os.replace
        calls = []
        def replace(source, destination):
            calls.append(True)
            if len(calls) == 1:
                raise error
            original(source, destination)
        with patch('app_update.core.os.replace', side_effect=replace), patch('app_update.core.time.sleep'):
            switch_directory(staging, target)
        self.assertEqual(len(calls), 2)
        self.assertTrue(target.is_dir())

    def test_transport_requires_https_and_no_embedded_password(self):
        for url in ('http://example.org/a', 'file:///x', 'https://user:password@example.org/a'):
            with self.assertRaises(UpdateError):
                https_url(url)

    def test_launcher_restores_previous_version_after_failed_trial(self):
        from app_update.launcher import run_application
        self.stage()
        initial = self.root / 'bundle' / 'app' / '2.10.0'
        initial.mkdir(parents=True)
        (initial / APP_ENTRY).write_bytes(b'initial')
        class Child:
            def __init__(self, args, **kwargs):
                self.trial = kwargs['env']['RTT_UPDATE_TRIAL'] == '1'
            def poll(self):
                return 1
            def wait(self, **kwargs):
                return 1 if self.trial else 0
        with patch('app_update.launcher.subprocess.Popen', side_effect=Child) as spawn:
            self.assertEqual(run_application(self.root/'bundle', self.home, self.settings, bundled_version='2.10.0'), 0)
        self.assertEqual(spawn.call_count, 2)
        self.assertEqual(read_json(self.home/'current.json')['current'], '2.10.0')


class GuiUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt5.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def window(self):
        from PyQt5.QtWidgets import QMainWindow, QStatusBar
        result = QMainWindow()
        result.status_bar = QStatusBar(result)
        result.setStatusBar(result.status_bar)
        result.sc201_workbench_page = SimpleNamespace(state=lambda: {})
        result.ble_dfu_page = SimpleNamespace(connected=False, busy=False)
        result.factory_pair_page = SimpleNamespace(connected=False, busy=False)
        result.hw_obj = None
        result.requests = []
        result.request_update_restart = lambda: result.requests.append(True)
        return result

    def test_batch_transfer_and_version_wait_block_restart_even_when_single_session_is_idle(self):
        from app_update.qt_client import busy_reason
        window = self.window()
        self.assertIsNone(busy_reason(window))
        for state in ({'ota_active': True}, {'batch_ota': {'active': True}},
                      {'batch_ota': {'running': 4}}, {'busy': True}, {'connected': True}):
            window.sc201_workbench_page.state = lambda: state
            self.assertIsNotNone(busy_reason(window))
        window.close()

    def test_auto_restart_requires_a_full_idle_period_after_device_job_finishes(self):
        from app_update.qt_client import AutoUpdater
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,
                RTT_UPDATE_ROOT=folder, RTT_UPDATE_HOME=folder):
            window = self.window()
            client = AutoUpdater(window)
            client.pending = {'version': '2.10.1'}
            client.last_input = 0
            window.sc201_workbench_page.state = lambda: {'ota_active': True}
            with patch('app_update.qt_client.time.monotonic', return_value=1000):
                client.tick()
            self.assertFalse(window.requests)
            window.sc201_workbench_page.state = lambda: {}
            for now in (1001, 1060):
                with patch('app_update.qt_client.time.monotonic', return_value=now):
                    client.tick()
            self.assertFalse(window.requests)
            with patch('app_update.qt_client.time.monotonic', return_value=1061):
                client.tick()
            self.assertEqual(len(window.requests), 1)
            client.stop()
            window.close()

    def test_configuration_and_selected_workbench_path_survive_version_directory_changes(self):
        import config_manager
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, RTT_DATA_DIR=folder):
            value = {'sc201_workbench_data_dir': 'E:/existing/private/workbench', 'filter': '原配置'}
            config_manager.save_config(value)
            with patch('config_manager.get_app_dir', return_value='X:/a-new-app-version'):
                self.assertEqual(config_manager.load_config(), value)
                self.assertEqual(Path(config_manager.get_config_path()).parent, Path(folder))

    def test_worker_progress_is_displayed_while_download_is_still_running(self):
        from app_update.qt_client import AutoUpdater
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,
                RTT_UPDATE_ROOT=folder, RTT_UPDATE_HOME=folder):
            atomic_json(Path(folder)/'updater.json', {'enabled': True, 'public_keys': {'fixture': 'unused'},
                'feed_url': 'https://example.org/latest.json'})
            gate = threading.Event()
            window = self.window()
            client = AutoUpdater(window)
            def download(*args, **kwargs):
                kwargs['on_transfer']({'phase': 'download', 'version': '2.10.3',
                    'received': 524288, 'total': 1048576, 'speed': 131072, 'elapsed': 4})
                if not gate.wait(3):
                    raise TimeoutError('Test did not consume the intermediate progress')
                kwargs['on_transfer']({'phase': 'ready', 'version': '2.10.3',
                    'received': 1048576, 'total': 1048576, 'speed': 0, 'elapsed': 8})
                return {'version': '2.10.3'}
            try:
                with patch('app_update.qt_client.check_and_stage', side_effect=download):
                    client.check()
                    deadline = time.monotonic() + 2
                    while client.last_transfer is None and time.monotonic() < deadline:
                        self.app.processEvents()
                        time.sleep(0.005)
                    self.assertTrue(client.worker.is_alive())
                    self.assertIsNone(client.pending)
                    self.assertEqual(client.progress_bar.value(), 500)
                    self.assertEqual(client.progress_bar.format(), '50.0%')
                    self.assertIn('512.0 KiB / 1.0 MiB', client.numbers.text())
                    self.assertIn('128.0 KiB/s', client.numbers.text())
                    self.assertFalse(client.restart.isEnabled())
                    gate.set()
                    client.worker.join(timeout=3)
                    self.app.processEvents()
                    self.assertEqual(client.pending['version'], '2.10.3')
                    self.assertEqual(client.progress_bar.value(), 1000)
                    self.assertEqual(client.progress_bar.format(), '100.0%')
            finally:
                gate.set()
                if client.worker:
                    client.worker.join(timeout=3)
                client.stop()
                window.close()

    def test_network_failure_is_shown_and_persisted_for_diagnosis(self):
        from app_update.qt_client import AutoUpdater
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,
                RTT_UPDATE_ROOT=folder, RTT_UPDATE_HOME=folder):
            atomic_json(Path(folder)/'updater.json', {'enabled': True, 'public_keys': {'fixture': 'unused'},
                'feed_url': 'https://example.org/latest.json'})
            window = self.window()
            client = AutoUpdater(window)
            try:
                with patch('app_update.qt_client.check_and_stage', side_effect=TimeoutError('fixture socket timeout')):
                    client.check()
                    client.worker.join(timeout=3)
                    self.app.processEvents()
                self.assertIn('网络超时', client.label.text())
                self.assertEqual(client.progress_bar.maximum(), 1000)
                self.assertEqual(client.progress_bar.format(), '失败')
                error = read_json(Path(folder)/'last-check-error.json')
                self.assertEqual(error['error_type'], 'TimeoutError')
                self.assertEqual(error['error'], 'fixture socket timeout')
            finally:
                client.stop()
                window.close()

    def test_real_qt_close_exits_with_update_restart_code(self):
        code = '''
import copy
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QTimer
from config_manager import DEFAULT_CONFIG
from rtt_t2 import MainWindow
class NoIO(MainWindow):
    def start_hw_thread(self): pass
app = QApplication([])
window = NoIO(copy.deepcopy(DEFAULT_CONFIG))
window.show()
QTimer.singleShot(50, window.request_update_restart)
QTimer.singleShot(10000, lambda: app.exit(3))
raise SystemExit(app.exec_())
'''
        with tempfile.TemporaryDirectory() as folder:
            environment = dict(os.environ, RTT_DATA_DIR=folder, QT_QPA_PLATFORM='offscreen')
            environment.pop('RTT_UPDATE_ROOT', None)
            result = subprocess.run([sys.executable, '-B', '-c', code], env=environment,
                                    capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 42, result.stderr.decode(errors='replace'))
            self.assertTrue((Path(folder)/'config.json').is_file())


class TransportTests(unittest.TestCase):
    def test_tls_connection_drop_retries_without_weakening_https_verification(self):
        from app_update.core import fetch
        response = io.BytesIO(b'valid response')
        response.geturl = lambda: 'https://example.org/asset'
        transport = SimpleNamespace(open=Mock(side_effect=[urllib.error.URLError(ssl.SSLEOFError('fixture TLS drop')), response]))
        retries = []
        with patch('app_update.core.urllib.request.build_opener', return_value=transport), patch('app_update.core.time.sleep'):
            self.assertIs(fetch('https://example.org/asset', on_retry=retries.append), response)
        self.assertEqual(transport.open.call_count, 2)
        self.assertEqual(retries, [2])
        self.assertEqual(transport.open.call_args.kwargs['timeout'], 45)

    def test_certificate_failures_and_https_downgrades_are_not_retried(self):
        from app_update.core import fetch
        transport = SimpleNamespace(open=Mock(side_effect=urllib.error.URLError(ssl.SSLCertVerificationError('fixture certificate'))))
        with patch('app_update.core.urllib.request.build_opener', return_value=transport), patch('app_update.core.time.sleep') as delay:
            with self.assertRaises(urllib.error.URLError):
                fetch('https://example.org/asset')
            delay.assert_not_called()
        self.assertEqual(transport.open.call_count, 1)
        response = io.BytesIO(b'forbidden downgrade')
        response.geturl = lambda: 'http://example.org/asset'
        transport = SimpleNamespace(open=Mock(return_value=response))
        with patch('app_update.core.urllib.request.build_opener', return_value=transport):
            with self.assertRaises(UpdateError):
                fetch('https://example.org/asset')
        self.assertTrue(response.closed)

    def test_http_service_retries_are_bounded(self):
        from app_update.core import fetch
        transport = SimpleNamespace(open=Mock(side_effect=urllib.error.HTTPError('https://example.org', 503, 'fixture unavailable', None, None)))
        with patch('app_update.core.urllib.request.build_opener', return_value=transport), patch('app_update.core.time.sleep') as delay:
            with self.assertRaises(urllib.error.HTTPError):
                fetch('https://example.org/asset')
        self.assertEqual(transport.open.call_count, 3)
        self.assertEqual(delay.call_count, 2)


@unittest.skipUnless(os.name == 'nt' and shutil.which('gcc'), 'Windows native child fixture')
class RealProcessTests(unittest.TestCase):
    setUp = UpdateTests.setUp
    opener = UpdateTests.opener
    stage = UpdateTests.stage

    def run_fixture(self, fail):
        source = self.root/'child.c'
        source.write_text(r'''
#include <windows.h>
#include <stdio.h>
#include <wchar.h>
#include <direct.h>
#ifndef FAIL_STARTUP
#define FAIL_STARTUP 0
#endif
int main(void) {
    wchar_t path[32768], parent[32768];
    char nonce[128], version[64];
    if(FAIL_STARTUP) return 1;
    if(!GetEnvironmentVariableW(L"RTT_UPDATE_HEALTH",path,32768)) return 2;
    wcscpy(parent,path);
    wchar_t *end=wcsrchr(parent,L'\\');
    if(!end) end=wcsrchr(parent,L'/');
    if(!end) return 3;
    *end=0; _wmkdir(parent);
    GetEnvironmentVariableA("RTT_UPDATE_NONCE",nonce,128);
    GetEnvironmentVariableA("RTT_UPDATE_VERSION",version,64);
    FILE *stream=_wfopen(path,L"wb");
    if(!stream) return 4;
    fprintf(stream,"{\"version\":\"%s\",\"nonce\":\"%s\"}",version,nonce);
    fclose(stream);
    Sleep(400);
    return 0;
}
''', encoding='ascii')
        good = self.root/'good.exe'
        bad = self.root/'bad.exe'
        for target, flag in ((good, 0), (bad, 1)):
            subprocess.run(['gcc', '-O2', '-mwindows', '-DFAIL_STARTUP='+str(flag), str(source), '-o', str(target)],
                           check=True, capture_output=True)
        bundle = self.root/'bundle 空格'/'app'/'2.10.0'
        bundle.mkdir(parents=True)
        shutil.copyfile(good, bundle/APP_ENTRY)
        shutil.copyfile(bad if fail else good, self.app/APP_ENTRY)
        self.envelope = make_release(self.app, self.root/'native-release', '2.10.1',
                                     'https://example.org/v2.10.1/app.zip', self.key)
        self.package = self.root/'native-release/RTTTool-2.10.1-windows-x64.zip'
        self.stage()
        from app_update.launcher import run_application
        self.assertEqual(run_application(bundle.parents[1], self.home, self.settings, bundled_version='2.10.0', health_timeout=5), 0)
        state = read_json(self.home/'current.json')
        self.assertFalse(state['trial'])
        self.assertEqual(state['current'], '2.10.0' if fail else '2.10.1')

    def test_real_child_startup_health_commits_new_version(self):
        self.run_fixture(False)

    def test_real_child_failure_restarts_previous_executable(self):
        self.run_fixture(True)


if __name__ == '__main__':
    unittest.main()
