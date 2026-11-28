"""Windows packaged upgrade/migration and two-window smoke test, no device I/O.

Uses a copied base and isolated LOCALAPPDATA; closes only windows it launched.
"""
import argparse
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_update import APP_VERSION
from app_update.core import LockBusy, archive_path, atomic_json, digest, file_lock, read_json, verify


def until(predicate, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.2)
    raise RuntimeError('Timed out waiting for packaged application')


def windows(process):
    try:
        pids = {process.pid, *(p.pid for p in psutil.Process(process.pid).children(recursive=True))}
    except psutil.NoSuchProcess:
        return []
    result = []
    user = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            text = ctypes.create_unicode_buffer(512)
            user.GetWindowTextW(hwnd, text, 512)
            if 'RTT' in text.value and ('v' + APP_VERSION) in text.value:
                result.append((hwnd, text.value, pid.value))
        return True
    user.EnumWindows(callback_type(visit), 0)
    return result


def close(process):
    for hwnd, _, _ in windows(process):
        ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    process.wait(timeout=30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old-base', type=Path, required=True)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--proof', type=Path, required=True)
    args = parser.parse_args()
    args.proof = args.proof.resolve()
    args.old_base = args.old_base.resolve()
    args.release = args.release.resolve()
    args.proof.mkdir(parents=True, exist_ok=False)
    base = args.proof / '旧版 基础包'
    shutil.copytree(args.old_base, base)
    launcher_hash = digest(base/'RTTTool.exe')
    launcher_metadata = read_json(base/'launcher-version.json', {}) or read_json(base/'bootstrap-files.json', {})
    already_multiwindow = launcher_metadata.get('revision') == 2
    profile = args.proof / 'isolated-profile'
    home = profile / 'RTTTool/updates'
    settings = read_json(base/'updater.json')
    settings['enabled'] = False  # no live download/channel change in smoke test
    atomic_json(base/'updater.json', settings)
    settings_hash = digest(base/'updater.json')
    envelope = read_json(args.release/'latest.json')
    payload = verify(envelope, settings['public_keys'])
    package = args.release / ('RTTTool-' + APP_VERSION + '-windows-x64.zip')
    assert digest(package) == payload['sha256']
    destination = archive_path(home,payload)
    destination.parent.mkdir(parents=True)
    shutil.copyfile(package,destination)
    atomic_json(home/'pending.json',envelope)
    from config_manager import DEFAULT_CONFIG
    config = dict(DEFAULT_CONFIG, filter='multiwindow-fixture-keep')
    data = profile/'RTTTool/data'
    atomic_json(data/'config.json',config)
    env = dict(os.environ, LOCALAPPDATA=str(profile), PYINSTALLER_RESET_ENVIRONMENT='1')
    for key in list(env):
        if key.startswith('RTT_'):
            del env[key]
    processes = []
    report = {'version':APP_VERSION,'hardware_accessed':False}
    try:
        legacy = subprocess.Popen([str(base/'RTTTool.exe')],env=env,cwd=base)
        processes.append(legacy)
        until(lambda: windows(legacy))
        until(lambda: read_json(home/'current.json',{}).get('trial') is False)
        assert read_json(home/'current.json')['current'] == APP_VERSION
        report['old_launcher_installed_signed_application'] = True
        print('PASS: previous launcher installed signed app and startup health passed',flush=True)
        close(legacy)
        if already_multiwindow:
            assert digest(base/'RTTTool.exe') == launcher_hash
            report['existing_revision_2_launcher_preserved'] = True
        else:
            until(lambda: read_json(home/'bootstrap-result.json',{}).get('installed') is True)
            assert read_json(base/'launcher-version.json')['revision'] == 2
            report['bootstrap_migrated_with_configuration_preserved'] = True
        assert digest(base/'updater.json') == settings_hash
        assert read_json(data/'config.json')['filter'] == 'multiwindow-fixture-keep'
        report['configuration_preserved'] = True
        print('PASS: launcher revision checked, settings and config preserved',flush=True)
        first = subprocess.Popen([str(base/'RTTTool.exe')],env=env,cwd=base)
        processes.append(first)
        first_windows = until(lambda: windows(first))
        second = subprocess.Popen([str(base/'RTTTool.exe')],env=env,cwd=base)
        processes.append(second)
        second_windows = until(lambda: windows(second))
        assert first.poll() is None and second.poll() is None
        report['windows'] = [first_windows,second_windows]
        try:
            with file_lock(home/'session.lock'):
                raise AssertionError('Install lock acquired while GUIs are running')
        except LockBusy:
            pass
        close(first)
        assert second.poll() is None
        try:
            with file_lock(home/'session.lock'):
                raise AssertionError('Install lock acquired while second GUI is running')
        except LockBusy:
            pass
        close(second)
        with file_lock(home/'session.lock'):
            pass
        report['two_real_windows_and_update_exclusion'] = True
        report['passed'] = True
        print('PASS: two real GUI windows coexist; install waits for last window',flush=True)
    finally:
        for process in processes:
            if process.poll() is None:
                try:
                    close(process)
                except Exception:
                    # Only children of our own test launcher, never global PIDs.
                    for child in psutil.Process(process.pid).children(recursive=True):
                        child.terminate()
                    process.terminate()
                    process.wait(timeout=15)
        atomic_json(args.proof/'report.json',report)


if __name__ == '__main__':
    main()
