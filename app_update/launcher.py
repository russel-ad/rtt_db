"""Stable launcher: run one GUI, switch complete versions only between runs."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from . import APP_ENTRY, APP_VERSION, RESTART_FOR_UPDATE
from .core import UpdateError, atomic_json, file_lock, install_pending, read_json, rollback


def run_application(root, home, settings, bundled_version=APP_VERSION, *, health_timeout=45):
    with file_lock(home / 'session.lock'):
        state = read_json(home / 'current.json', {'current': bundled_version})
        # An interrupted first startup cannot silently become the active build.
        if state.get('trial'):
            rollback(home)
        try:
            install_pending(home, settings, bundled_version)
        except Exception as error:
            atomic_json(home / 'last-error.json', {'stage': 'install', 'error': str(error)})
        while True:
            state = read_json(home / 'current.json', {'current': bundled_version})
            current = state['current']
            from .core import version
            version(current)
            folder = home / 'versions' / current
            if not folder.exists() and current == bundled_version:
                folder = root / 'app' / bundled_version
            executable = folder / APP_ENTRY
            if not executable.is_file():
                if rollback(home):
                    continue
                raise UpdateError('Application files are missing: ' + str(executable))
            nonce = uuid.uuid4().hex
            health_path = home / 'health' / (nonce + '.json')
            env = dict(os.environ, RTT_UPDATE_ROOT=str(root), RTT_UPDATE_HOME=str(home),
                       RTT_DATA_DIR=str(home.parent / 'data'), RTT_UPDATE_HEALTH=str(health_path),
                       RTT_UPDATE_NONCE=nonce, RTT_UPDATE_VERSION=current,
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
            except OSError as error:
                if rollback(home):
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
            code = child.wait()
            health_path.unlink(missing_ok=True)
            if code != RESTART_FOR_UPDATE:
                return code
            try:
                if not install_pending(home, settings, bundled_version):
                    return 0
            except Exception as error:
                atomic_json(home / 'last-error.json', {'stage': 'install', 'error': str(error)})
                # Relaunch the known working version after an install failure.


def main():
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
        return run_application(root, home, settings)
    except Exception as error:
        if os.name == 'nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(error), 'RTT 工具启动', 0x10)
        return 1
