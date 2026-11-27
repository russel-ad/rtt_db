"""Multiple GUIs may run; switching versions requires exclusive access."""
from __future__ import annotations
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from . import APP_ENTRY, APP_ID, APP_VERSION, RESTART_FOR_UPDATE
from .core import LockBusy, UpdateError, atomic_json, file_lock, install_pending, read_json, rollback


def base_version(root):
    # Migrating the top-level launcher must not relabel the original bundled
    # app. In particular a cleared update cache must still boot that app.
    from .core import version
    candidates = []
    for folder in (root / 'app').glob('*'):
        metadata = read_json(folder / 'app-release.json', {})
        if (metadata == {'app_id': APP_ID, 'entry': APP_ENTRY, 'version': folder.name}
                and (folder / APP_ENTRY).is_file()):
            version(folder.name)
            candidates.append(folder.name)
    if not candidates:
        raise UpdateError('基础应用文件缺失，请完整解压自动更新便携包')
    return max(candidates, key=version)


def run_application(root, home, settings, bundled_version=APP_VERSION, *, health_timeout=45):
    while True:
        # Serialize choosing a version, spawning and health checking. Release
        # this short-lived gate before waiting for an ordinary GUI to exit.
        with ExitStack() as lifetime:
            with file_lock(home / 'startup.lock', timeout=120):
                try:
                    session_lock = lifetime.enter_context(file_lock(home / 'session.lock'))
                    alone = True
                except LockBusy:
                    alone = False
                    # This also refuses entry if a legacy launcher/repair holds an
                    # exclusive lock; do not bypass protection across versions.
                    lifetime.enter_context(file_lock(home / 'session.lock', shared=True))
                if alone:
                    state = read_json(home / 'current.json', {'current': bundled_version})
                    if state.get('trial'):
                        rollback(home)
                    try:
                        install_pending(home, settings, bundled_version)
                    except Exception as error:
                        atomic_json(home / 'last-error.json', {'stage': 'install', 'error': str(error)})
                state = read_json(home / 'current.json', {'current': bundled_version})
                if alone and not (home / 'current.json').exists():
                    atomic_json(home / 'current.json', state)
                if state.get('trial') and not alone:
                    raise UpdateError('等待其他窗口完成新版启动检查后再打开')
                current = state['current']
                from .core import version
                version(current)
                folder = home / 'versions' / current
                if not folder.exists() and current == bundled_version:
                    folder = root / 'app' / bundled_version
                executable = folder / APP_ENTRY
                if not executable.is_file():
                    if alone and rollback(home):
                        continue
                    raise UpdateError('Application files are missing: ' + str(executable))
                nonce = uuid.uuid4().hex
                lifetime.enter_context(file_lock(home / 'sessions' / (nonce + '.lock')))
                health_path = home / 'health' / (nonce + '.json')
                env = dict(os.environ, RTT_UPDATE_ROOT=str(root), RTT_UPDATE_HOME=str(home),
                           RTT_DATA_DIR=str(home.parent / 'data'), RTT_UPDATE_HEALTH=str(health_path),
                           RTT_UPDATE_NONCE=nonce, RTT_UPDATE_VERSION=current,
                           RTT_UPDATE_SESSION=nonce, RTT_LAUNCHER_REVISION='2',
                           RTT_UPDATE_TRIAL='1' if state.get('trial') else '0',
                           PYINSTALLER_RESET_ENVIRONMENT='1')
                Path(env['RTT_DATA_DIR']).mkdir(parents=True, exist_ok=True)
                # Do not pass the launcher's bundled DLL search directory to the
                # independently bundled Qt application.
                if os.name == 'nt' and getattr(sys, 'frozen', False):
                    import ctypes
                    ctypes.windll.kernel32.SetDllDirectoryW(None)
                try:
                    child = subprocess.Popen([str(executable)], cwd=env['RTT_DATA_DIR'], env=env)
                    lifetime.callback(child.wait)
                except OSError as error:
                    if alone and rollback(home):
                        atomic_json(home / 'last-error.json', {'stage': 'spawn', 'error': str(error)})
                        continue
                    raise
                if state.get('trial'):
                    deadline = time.monotonic() + health_timeout
                    healthy = False
                    while time.monotonic() < deadline and child.poll() is None:
                        try:
                            receipt = read_json(health_path, {})
                        except (ValueError, UpdateError):
                            receipt = {}
                        if receipt == {'version': current, 'nonce': nonce}:
                            healthy = True
                            break
                        time.sleep(0.1)
                    if not healthy:
                        if child.poll() is None:
                            # Trial UI remains disabled until it writes the health
                            # receipt. This is our own child, not a searched PID.
                            child.terminate()
                        child.wait(timeout=15)
                        rollback(home)
                        atomic_json(home / 'last-error.json', {'stage': 'startup', 'version': current,
                            'error': 'Startup health receipt missing; previous version restored'})
                        continue
                    state['trial'] = False
                    atomic_json(home / 'current.json', state)
                if alone:
                    session_lock.downgrade()
            # Never release the lease while our child might still use its DLLs.
            try:
                code = child.wait()
            finally:
                if child.poll() is None:
                    child.wait()
                health_path.unlink(missing_ok=True)
        if code != RESTART_FOR_UPDATE:
            return code
        # Re-enter through the gate. If another window appeared during restart,
        # run the current version again without installing over its files.


def main():
    if len(sys.argv) == 5 and sys.argv[1] == '--install-bootstrap':
        from .bootstrap import install_bootstrap
        return install_bootstrap(Path(sys.argv[2]), Path(sys.argv[3]), restart=sys.argv[4] == 'restart')
    root = Path(sys.executable).resolve().parent
    if not getattr(sys, 'frozen', False):
        root = Path(__file__).resolve().parents[1]
    home = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'RTTTool' / 'updates'
    settings = read_json(root / 'updater.json', {})
    if len(sys.argv) == 3 and sys.argv[1] == '--launcher-check':
        atomic_json(Path(sys.argv[2]), {'passed': True, 'version': APP_VERSION,
                                      'feed_url': settings.get('feed_url'), 'device_access': False})
        return 0
    try:
        return run_application(root, home, settings, bundled_version=base_version(root))
    except Exception as error:
        if os.name == 'nt':
            import ctypes
            message = ('其他旧版窗口或更新修复工具仍在运行，请退出后重试'
                       if isinstance(error, LockBusy) else str(error))
            ctypes.windll.user32.MessageBoxW(None, message, 'RTT 工具启动', 0x10)
        return 1
