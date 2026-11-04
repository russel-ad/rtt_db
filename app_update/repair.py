"""Small standalone recovery UI for base clients with an older downloader."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

from . import APP_ENTRY, APP_ID, APP_VERSION
from .core import UpdateError, atomic_json, check_and_stage, file_lock, read_json, version
from .tls import certificate_error, diagnostics as tls_diagnostics


def failure_details(error):
    result = {'at': datetime.now(timezone.utc).isoformat(), 'helper_version': APP_VERSION,
        'error_type': type(error).__name__, 'error': str(error)[:4096],
        'https': tls_diagnostics(error), 'hardware_access': False}
    if hasattr(error, 'attempts'):
        result['manifest_attempts'] = error.attempts
    result['summary'] = ('HTTPS 证书链校验失败，请点击“HTTPS 诊断”查看证书错误码'
                         if certificate_error(error) else '更新未完成：' + str(error)[:230])
    return result


def check_https():
    """Probe only signed official metadata and its package, without staging."""
    from .core import fetch, verify
    from .manifest import read_manifest
    result = {'passed': False, 'helper_version': APP_VERSION, 'https': tls_diagnostics(),
        'hardware_access': False, 'installed': False, 'staged': False,
        'at': datetime.now(timezone.utc).isoformat()}
    try:
        settings = read_json(Path(__file__).with_name('release_config.json'))
        envelope, source = read_manifest(settings, opener=lambda url: fetch(url, timeout=15, attempts=1))
        payload = verify(envelope, settings['public_keys'])
        result.update(manifest_source=source, target_version=payload['version'], signature='Ed25519 PASS')
        from urllib.parse import urlsplit
        with fetch(payload['url'], timeout=20, attempts=1) as response:
            result['package_host'] = urlsplit(response.geturl()).hostname
            if response.read(4) != b'PK\x03\x04':
                raise UpdateError('更新包 HTTPS 探测返回了非 ZIP 内容')
        result['passed'] = True
        result['package_probe_only'] = True
    except Exception as error:
        result.update(failure_details(error))
    return result


def base_version(root):
    if not (root/'RTTTool.exe').is_file():
        raise UpdateError('请选择原上位机目录顶层的 RTTTool.exe')
    candidates = []
    for folder in (root/'app').glob('*'):
        metadata = read_json(folder/'app-release.json', {})
        if metadata.get('app_id') != APP_ID or metadata.get('entry') != APP_ENTRY:
            continue
        release = metadata.get('version', '')
        if release != folder.name or not (folder/APP_ENTRY).is_file():
            continue
        version(release)
        candidates.append(release)
    if not candidates:
        raise UpdateError('所选目录没有完整的自动更新基础包，请完整解压原上位机 ZIP')
    return max(candidates, key=version)


def prepare(root, home, *, settings=None, progress=lambda text: None, on_transfer=lambda data: None):
    """Stage only. The original launcher retains installation and rollback."""
    bundled = base_version(root)
    if settings is None:
        settings = read_json(Path(__file__).with_name('release_config.json'), {})
    if not settings.get('enabled') or not settings.get('public_keys') or not settings.get('feed_url'):
        raise UpdateError('修复工具缺少可信更新配置，请重新下载完整修复包')
    try:
        with file_lock(home/'session.lock'):
            state = read_json(home/'current.json', {})
            current = state.get('current', bundled)
            version(current)
            payload = check_and_stage(home, settings, current, progress=progress, on_transfer=on_transfer)
            result = {'passed': True, 'staged': bool(payload), 'source_version': current,
                'target_version': payload['version'] if payload else current,
                'original_launcher': str(root/'RTTTool.exe'), 'home': str(home),
                'hardware_access': False, 'installed_by_repair': False,
                'at': datetime.now(timezone.utc).isoformat(), 'https': tls_diagnostics()}
            atomic_json(home/'last-repair-result.json', result)
            return result
    except UpdateError as error:
        if 'Another application/update session' in str(error):
            raise UpdateError('请先退出上位机，再点击“下载并准备更新”；修复工具不会中断设备任务') from error
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, help='Folder containing the original top-level RTTTool.exe')
    parser.add_argument('--home', type=Path, help='Explicit isolated update profile for verification')
    parser.add_argument('--headless', action='store_true', help='Stage only, without starting any application')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--ui-check', type=Path, help='Render a simulated preview, write proof, then exit')
    parser.add_argument('--tls-check', type=Path, help='Probe verified HTTPS without installing or staging')
    args = parser.parse_args()
    home = args.home or Path(os.environ.get('LOCALAPPDATA', Path.home()))/'RTTTool/updates'
    if args.tls_check:
        report = check_https()
        atomic_json(args.tls_check, report)
        return 0 if report['passed'] else 1
    if args.headless:
        if not args.root or not args.home or not args.report:
            parser.error('Headless verification requires explicit --root, --home and --report')
        def log(event):
            print(json.dumps(event, ensure_ascii=True), flush=True)
        try:
            result = prepare(args.root.resolve(), home.resolve(),
                progress=lambda text: log({'status': text}), on_transfer=log)
        except Exception as error:
            result = {'passed': False, **failure_details(error)}
        atomic_json(args.report, result)
        return 0 if result['passed'] else 1

    import tkinter as tk
    from tkinter import filedialog, ttk
    window = tk.Tk()
    window.title('RTT 上位机更新修复 v' + APP_VERSION)
    window.geometry('730x310')
    window.minsize(650, 300)
    frame = ttk.Frame(window, padding=18)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='修复旧版上位机的更新下载', font=('Microsoft YaHei UI', 13, 'bold')).pack(anchor='w')
    ttk.Label(frame, text='请先退出上位机，选择原文件夹顶层的 RTTTool.exe。\n下载中断会保留进度；准备完成后，由原启动器安装并保留配置。').pack(anchor='w', pady=(8, 12))
    selected = tk.StringVar(value=str(args.root/'RTTTool.exe') if args.root else '')
    row = ttk.Frame(frame)
    row.pack(fill='x')
    entry = ttk.Entry(row, textvariable=selected)
    entry.pack(side='left', fill='x', expand=True)
    def browse():
        path = filedialog.askopenfilename(parent=window, title='选择原上位机顶层 RTTTool.exe',
            filetypes=[('RTTTool.exe', 'RTTTool.exe')])
        if path:
            selected.set(path)
    choose = ttk.Button(row, text='选择上位机…', command=browse)
    choose.pack(side='left', padx=(8, 0))
    status = tk.StringVar(value='等待选择原上位机')
    ttk.Label(frame, textvariable=status, wraplength=680).pack(anchor='w', pady=(12, 4))
    bar = ttk.Progressbar(frame, maximum=100)
    bar.pack(fill='x')
    numbers = tk.StringVar(value='0.0%')
    ttk.Label(frame, textvariable=numbers).pack(anchor='w', pady=(4, 10))
    buttons = ttk.Frame(frame)
    buttons.pack(fill='x')
    events = queue.Queue()
    running = False
    launch_root = None
    ready_to_launch = False

    def start():
        nonlocal running, launch_root, ready_to_launch
        if running:
            return
        selected_file = Path(selected.get())
        if selected_file.name.lower() != 'rtttool.exe' or not selected_file.is_file():
            status.set('请选择原上位机文件夹顶层的 RTTTool.exe')
            return
        launch_root = selected_file.resolve().parent
        running = True
        ready_to_launch = False
        download.configure(state='disabled')
        choose.configure(state='disabled')
        entry.configure(state='disabled')
        launch.configure(state='disabled')
        diagnose.configure(state='disabled')
        def work():
            try:
                result = prepare(launch_root, home,
                    progress=lambda text: events.put(('status', text)),
                    on_transfer=lambda data: events.put(('transfer', data)))
                events.put(('done', result))
            except Exception as error:
                details = failure_details(error)
                try:
                    atomic_json(home/'last-repair-error.json', details)
                except OSError:
                    pass
                events.put(('error', details))
        threading.Thread(target=work, name='update-repair-download', daemon=True).start()

    def launch_application():
        if launch_root is None or running:
            return
        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1')
        for name in ('RTT_UPDATE_ROOT', 'RTT_UPDATE_HOME', 'RTT_UPDATE_HEALTH', 'RTT_UPDATE_NONCE',
                     'RTT_UPDATE_VERSION', 'RTT_UPDATE_TRIAL', 'RTT_DATA_DIR'):
            env.pop(name, None)
        if os.name == 'nt' and getattr(sys, 'frozen', False):
            import ctypes
            ctypes.windll.kernel32.SetDllDirectoryW(None)
        try:
            subprocess.Popen([str(launch_root/'RTTTool.exe')], cwd=launch_root, env=env)
            window.destroy()
        except OSError as error:
            status.set('启动失败：' + str(error))

    download = ttk.Button(buttons, text='下载并准备更新', command=start)
    download.pack(side='left')
    launch = ttk.Button(buttons, text='启动上位机', state='disabled', command=launch_application)
    launch.pack(side='left', padx=8)

    def diagnose_https():
        nonlocal running
        if running:
            return
        running = True
        for control in (download, choose, entry, launch, diagnose):
            control.configure(state='disabled')
        status.set('正在诊断更新清单及安装包的 HTTPS 连接…')
        def work():
            report = check_https()
            try:
                atomic_json(home/'last-https-check.json', report)
            except OSError:
                pass
            events.put(('diagnostic', report))
        threading.Thread(target=work, name='update-https-diagnostic', daemon=True).start()
    diagnose = ttk.Button(buttons, text='HTTPS 诊断', command=diagnose_https)
    diagnose.pack(side='right')

    def show_diagnostic(report):
        dialog = tk.Toplevel(window)
        dialog.title('上位机更新 HTTPS 诊断')
        dialog.geometry('760x540')
        content = json.dumps(report, ensure_ascii=False, indent=2)
        text = tk.Text(dialog, wrap='word', padx=12, pady=12)
        text.pack(fill='both', expand=True)
        text.insert('1.0', content)
        text.configure(state='disabled')
        def copy():
            dialog.clipboard_clear()
            dialog.clipboard_append(content)
        ttk.Button(dialog, text='复制诊断', command=copy).pack(pady=8)

    def poll():
        nonlocal running, ready_to_launch
        while True:
            try:
                kind, value = events.get_nowait()
            except queue.Empty:
                break
            if kind == 'status':
                status.set(value)
            elif kind == 'transfer':
                received, total = value['received'], value['total']
                percentage = received*100/total
                bar['value'] = percentage
                numbers.set(f'{percentage:.1f}%    {received/1048576:.1f} / {total/1048576:.1f} MiB'
                    + (f'    {value["speed"]/1024:.1f} KiB/s' if value['phase'] == 'download' else ''))
                if value['phase'] == 'verify':
                    status.set('下载完成，正在校验更新包…')
            else:
                running = False
                download.configure(state='normal')
                choose.configure(state='normal')
                entry.configure(state='normal')
                diagnose.configure(state='normal')
                if kind == 'done':
                    status.set('更新包已准备好，点击“启动上位机”完成切换。' if value['staged']
                        else '当前没有可安装的新版本，可以启动上位机。')
                    ready_to_launch = True
                elif kind == 'diagnostic':
                    status.set('HTTPS 诊断通过：清单验签与安装包连接正常' if value['passed']
                               else value.get('summary', 'HTTPS 诊断失败，请查看诊断详情'))
                    show_diagnostic(value)
                else:
                    status.set(value['summary'])
                launch.configure(state='normal' if ready_to_launch else 'disabled')
        window.after(100, poll)
    poll()
    if args.ui_check:
        events.put(('status', '界面预览（模拟，不联网，不操作设备）'))
        events.put(('transfer', {'phase': 'download', 'received': 34812712,
            'total': 87008573, 'speed': 262144}))
        def finish_preview():
            atomic_json(args.ui_check, {'passed': True, 'simulated': True,
                'progress': bar['value'], 'numbers': numbers.get(), 'network_access': False, 'hardware_access': False})
            window.destroy()
        window.after(1800, finish_preview)
    window.mainloop()
    return 0
