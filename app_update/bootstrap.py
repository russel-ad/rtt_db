"""Migrate legacy launchers using the bootstrap inside the signed app package.

No running executable/runtime is overwritten: runtime names are release-specific
and the top-level EXE is atomically replaced after all application leases end.
"""
from __future__ import annotations
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from .core import UpdateError, atomic_json, digest, file_lock, read_json


def bundled_bootstrap():
    return Path(sys.executable).resolve().parent / 'bootstrap'


def migration_needed():
    return (getattr(sys, 'frozen', False) and os.environ.get('RTT_UPDATE_ROOT')
            and os.environ.get('RTT_LAUNCHER_REVISION') != '2'
            and (bundled_bootstrap() / 'RTTTool.exe').is_file())


def schedule_migration(root, home, *, restart=False):
    if not migration_needed():
        return False
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1')
    if os.name == 'nt':
        import ctypes
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    subprocess.Popen([str(bundled_bootstrap() / 'RTTTool.exe'), '--install-bootstrap',
                      str(root), str(home), 'restart' if restart else 'close'],
                     env=env, cwd=str(home),
                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    return True


def copy_bootstrap(source, root):
    """Install an immutable runtime first, then atomically publish its EXE."""
    index = read_json(source / 'bootstrap-files.json')
    if not isinstance(index, dict) or index.get('revision') != 2:
        raise UpdateError('Invalid launcher migration manifest')
    runtime = index.get('runtime', '')
    if not runtime.startswith('_launcher_') or Path(runtime).name != runtime:
        raise UpdateError('Invalid launcher runtime directory')
    files = index.get('files', {})
    if 'RTTTool.exe' not in files or not files:
        raise UpdateError('Incomplete launcher migration manifest')
    for name, expected in files.items():
        parts = Path(name).parts
        if (Path(name).is_absolute() or '..' in parts or
                (name != 'RTTTool.exe' and (len(parts) < 2 or parts[0] != runtime))):
            raise UpdateError('Invalid launcher package path')
        if digest(source / name) != expected:
            raise UpdateError('Launcher package hash mismatch: ' + name)
    # Runtime directories are uniquely named by release and must never be
    # overwritten if present (a scanner or another launcher could be using it).
    target = root / runtime
    if not target.exists():
        staging = root / (runtime + '.stage-' + uuid.uuid4().hex)
        shutil.copytree(source / runtime, staging)
        os.replace(staging, target)
    for name, expected in files.items():
        if name != 'RTTTool.exe' and digest(root / name) != expected:
            raise UpdateError('Existing launcher runtime differs: ' + name)
    temporary = root / ('RTTTool.' + uuid.uuid4().hex + '.tmp')
    shutil.copyfile(source / 'RTTTool.exe', temporary)
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                os.replace(temporary, root / 'RTTTool.exe')
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        atomic_json(root / 'launcher-version.json', {'revision': 2, 'sha256': files['RTTTool.exe']})
    finally:
        temporary.unlink(missing_ok=True)


def install_bootstrap(root, home, *, restart=False, source=None):
    source = source or Path(sys.executable).resolve().parent
    try:
        if not (root / 'RTTTool.exe').is_file() or not (root / 'updater.json').is_file():
            raise UpdateError('Select the original complete RTTTool base directory')
        # Consistent lock order with the launcher. Legacy launchers only hold
        # session.lock, which we wait on after their GUI has requested exit.
        with file_lock(home / 'startup.lock', timeout=120):
            with file_lock(home / 'session.lock', timeout=120):
                copy_bootstrap(source, root)
        atomic_json(home / 'bootstrap-result.json', {'installed': True, 'root': str(root)})
    except Exception as error:
        atomic_json(home / 'bootstrap-result.json', {'installed': False, 'error': str(error),
            'hint': '退出全部窗口后，解压新版完整基础包到可写目录运行顶层 RTTTool.exe'})
        # Keep the old working launcher when migration fails.
        if restart:
            subprocess.Popen([str(root / 'RTTTool.exe')], env=dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1'))
        return 1
    if restart:
        subprocess.Popen([str(root / 'RTTTool.exe')], env=dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1'))
    return 0
