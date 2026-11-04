"""GUI integration: worker downloads; only the idle GUI requests a restart."""
from __future__ import annotations
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import ssl
import threading
import time
import urllib.error

from PyQt5.QtCore import QEvent, QObject, QTimer, Qt, pyqtSignal
from PyQt5.QtWidgets import QAction, QApplication, QHBoxLayout, QLabel, QMessageBox, QProgressBar, QWidget
from . import APP_VERSION
from .core import UpdateError, atomic_json, check_and_stage, read_json
from .tls import diagnostics as tls_diagnostics


def error_summary(error):
    from .download import DownloadInterrupted
    from .manifest import ManifestUnavailable
    if isinstance(error, DownloadInterrupted):
        return '下载暂时中断，已保留进度，稍后自动续传'
    if isinstance(error, ManifestUnavailable):
        return '更新信息获取失败，各更新入口暂不可用，请查看详情'
    reason = getattr(error, 'reason', error)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return 'HTTPS 证书链校验失败，请查看更新详情中的证书诊断'
    if isinstance(error, urllib.error.HTTPError):
        return '更新服务器返回 HTTP ' + str(error.code)
    if isinstance(reason, TimeoutError):
        return '网络超时，请稍后重试'
    if isinstance(error, (urllib.error.URLError, ConnectionError, ssl.SSLError)):
        return '网络连接失败，请检查网络后重试'
    if isinstance(error, PermissionError):
        return '更新目录没有写入权限'
    if isinstance(error, json.JSONDecodeError):
        return '更新清单格式错误'
    if isinstance(error, UpdateError):
        message = str(error)
        if 'expired or is not yet valid' in message:
            return '发布信息时间无效，请检查电脑日期和时间'
        if 'signature' in message or 'metadata is invalid' in message:
            return '更新签名校验失败'
        if 'hash/length' in message:
            return '更新包大小或哈希校验失败'
        if 'older release' in message or 'immutable release' in message:
            return '更新渠道的版本信息不一致'
        if 'Another application/update' in message:
            return '另一个更新任务正在运行'
        if 'interrupted' in message:
            return '更新下载中断，请重试'
    return '更新失败，请查看更新详情'


def format_bytes(value):
    if value >= 1024 * 1024:
        return f'{value / (1024 * 1024):.1f} MiB'
    if value >= 1024:
        return f'{value / 1024:.1f} KiB'
    return f'{value:.0f} B'


def busy_reason(window):
    page = window.sc201_workbench_page
    state = page.state()
    batch = state.get('batch_ota', {})
    if (state.get('connected') or state.get('busy') or state.get('scanning') or
            state.get('ota_active') or batch.get('active') or batch.get('running') or batch.get('queued')):
        return '设备工作台正在连接、升级或等待重启验版'
    for name in ('ble_dfu_page', 'factory_pair_page'):
        other = getattr(window, name)
        if other.connected or other.busy:
            return '蓝牙设备仍在连接或执行操作'
    for name in ('resource_thread', 'resource_abort_thread'):
        job = getattr(window, name, None)
        if job and job.isRunning():
            return '资源传输仍在进行'
    if getattr(window, 'resource_needs_abort', False):
        return '资源传输尚未完成收尾'
    hw = getattr(window, 'hw_obj', None)
    if hw and hw.hw_is_open():
        return 'RTT 或串口仍已连接'
    if QApplication.activeModalWidget() is not None:
        return '请先完成当前对话框操作'
    return None


class AutoUpdater(QObject):
    status = pyqtSignal(str)
    staged = pyqtSignal(object)
    transfer = pyqtSignal(object)
    failed = pyqtSignal(object)

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.root = Path(os.environ['RTT_UPDATE_ROOT'])
        self.home = Path(os.environ['RTT_UPDATE_HOME'])
        self.settings = read_json(self.root / 'updater.json', {})
        self.worker = None
        self.closing = False
        self.pending = None
        self.last_input = time.monotonic()
        self.idle_since = None
        self.last_check = 0.0
        self.last_error = None
        self.last_transfer = None
        self.target_version = None
        self.panel = QWidget(window.status_bar)
        self.panel.setObjectName('appUpdateStatus')
        layout = QHBoxLayout(self.panel)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.setSpacing(8)
        self.label = QLabel('上位机更新：等待检查')
        self.label.setMinimumWidth(0)
        layout.addWidget(self.label, 1)
        self.progress_bar = QProgressBar(self.panel)
        self.progress_bar.setObjectName('appUpdateProgress')
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat('0.0%')
        self.progress_bar.setMinimumWidth(160)
        self.progress_bar.setMaximumWidth(230)
        self.progress_bar.setAlignment(Qt.AlignCenter)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)
        self.numbers = QLabel(self.panel)
        self.numbers.setObjectName('appUpdateNumbers')
        self.numbers.hide()
        layout.addWidget(self.numbers)
        window.status_bar.addPermanentWidget(self.panel, 1)
        self.status.connect(self.set_status)
        self.staged.connect(self.on_staged)
        self.transfer.connect(self.on_transfer)
        self.failed.connect(self.on_failed)
        menu = window.menuBar().addMenu('更新')
        check = QAction('检查上位机更新', self)
        check.triggered.connect(self.check)
        menu.addAction(check)
        self.restart = QAction('重启并更新', self)
        self.restart.setEnabled(False)
        self.restart.triggered.connect(window.request_update_restart)
        menu.addAction(self.restart)
        details = QAction('查看更新详情', self)
        details.triggered.connect(self.show_details)
        menu.addAction(details)
        QApplication.instance().installEventFilter(self)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        QTimer.singleShot(3000, self.check)

    def set_status(self, text):
        self.label.setText(text)
        self.label.setToolTip(text)

    def on_transfer(self, data):
        if self.closing:
            return
        self.last_transfer = dict(data)
        self.target_version = data['version']
        received, total = data['received'], data['total']
        phase = data['phase']
        self.progress_bar.show()
        self.numbers.show()
        if phase == 'connecting':
            self.progress_bar.setRange(0, 0)
            self.set_status('正在连接上位机 v' + data['version'] + ' 下载…')
        else:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(min(1000, int(received * 1000 / total)))
            self.progress_bar.setFormat(f'{received * 100 / total:.1f}%')
            if phase == 'download':
                self.set_status('正在下载上位机 v' + data['version'])
            elif phase == 'verify':
                self.set_status('下载完成，正在校验更新包…')
            elif phase == 'cached':
                self.set_status('已校验缓存的更新包')
            elif phase in ('resuming', 'retrying', 'paused'):
                self.set_status('上位机更新：已保留下载进度，正在重新连接' if phase != 'paused'
                    else '上位机更新：下载已暂停，保留进度等待续传')
        speed = max(0, data.get('speed', 0))
        self.numbers.setText(format_bytes(received) + ' / ' + format_bytes(total)
            + (' · ' + format_bytes(speed) + '/s' if phase == 'download' else ''))
        self.numbers.setToolTip(f'已下载 {received:,} / {total:,} 字节')

    def on_failed(self, details):
        if self.closing:
            return
        self.last_error = details
        self.set_status('上位机更新：' + details['summary'] + '；稍后重试')
        if not self.progress_bar.isHidden():
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setFormat('已暂停' if details.get('resumable') else '失败')

    def show_details(self):
        feed = self.settings.get('feed_url', '')
        channel = '正式版' if '/releases/latest/download/' in feed else '测试或自定义更新源'
        dialog = QMessageBox(self.window)
        dialog.setWindowTitle('上位机更新详情')
        dialog.setIcon(QMessageBox.Information)
        dialog.setText(f'当前版本：v{APP_VERSION}\n更新渠道：{channel}\n状态：{self.label.text()}')
        diagnostic = {'current_version': APP_VERSION, 'target_version': self.target_version,
            'feed_url': feed, 'download': self.last_transfer, 'error': self.last_error,
            'manifest_source': read_json(self.home / 'manifest-source.json', {}),
            'https': tls_diagnostics(),
            'error_log': str(self.home / 'last-check-error.json')}
        dialog.setDetailedText(json.dumps(diagnostic, ensure_ascii=False, indent=2))
        dialog.exec_()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.KeyPress, QEvent.MouseButtonPress, QEvent.MouseMove, QEvent.Wheel):
            self.last_input = time.monotonic()
        return False

    def check(self):
        if self.closing or (self.worker and self.worker.is_alive()):
            return
        self.last_check = time.monotonic()
        if not self.settings.get('enabled') or not self.settings.get('public_keys') or not self.settings.get('feed_url'):
            self.status.emit('上位机自动更新尚未配置发布入口')
            return
        self.progress_bar.setRange(0, 0)
        self.progress_bar.show()
        self.numbers.hide()
        self.set_status('正在检查上位机更新…')

        def work():
            try:
                payload = check_and_stage(self.home, self.settings, APP_VERSION,
                    progress=self.status.emit, on_transfer=self.transfer.emit)
                self.last_error = None
                try:
                    (self.home / 'last-check-error.json').unlink(missing_ok=True)
                except OSError:
                    pass
                if not self.closing:
                    if payload:
                        self.staged.emit(payload)
                    else:
                        self.staged.emit(None)
            except Exception as error:
                details = {'at': datetime.now(timezone.utc).isoformat(), 'current_version': APP_VERSION,
                    'summary': error_summary(error), 'error_type': type(error).__name__, 'error': str(error)[:4096],
                    'feed_url': self.settings.get('feed_url')}
                from .download import DownloadInterrupted
                details['resumable'] = isinstance(error, DownloadInterrupted)
                details['https'] = tls_diagnostics(error)
                if hasattr(error, 'attempts'):
                    details['manifest_attempts'] = error.attempts
                try:
                    atomic_json(self.home / 'last-check-error.json', details)
                except OSError:
                    pass
                if not self.closing:
                    self.failed.emit(details)
        self.worker = threading.Thread(target=work, name='app-update-download', daemon=True)
        self.worker.start()

    def on_staged(self, payload):
        if self.closing:
            return
        if payload is None:
            self.progress_bar.hide()
            self.numbers.hide()
            self.status.emit('上位机 v' + APP_VERSION + ' 已是当前可用版本')
            return
        self.pending = payload
        self.last_input = time.monotonic()
        self.idle_since = None
        self.status.emit('新版 ' + payload['version'] + ' 已下载，空闲后或下次启动自动更新')

    def tick(self):
        if self.closing:
            return
        now = time.monotonic()
        if not self.pending:
            # Reconnect recovery without a resident service or a busy retry loop.
            if now - self.last_check >= 300:
                self.check()
            return
        reason = busy_reason(self.window)
        self.restart.setEnabled(not bool(reason))
        if reason:
            self.idle_since = None
            self.set_status('新版 v' + self.pending['version'] + ' 已就绪 · 等待当前任务结束')
            return
        if self.idle_since is None:
            self.idle_since = now
        remaining = max(0, 60 - int(now - max(self.last_input, self.idle_since)))
        self.set_status('新版 v' + self.pending['version'] + f' 已就绪 · 空闲 {remaining} 秒后重启')
        if remaining == 0:
            self.window.request_update_restart()

    def stop(self):
        self.closing = True
        self.timer.stop()


def start(window):
    if not os.environ.get('RTT_UPDATE_ROOT'):
        return None
    return AutoUpdater(window)


def startup_health(window):
    path = os.environ.get('RTT_UPDATE_HEALTH')
    if not path:
        return
    # DLL imports and ABI validation only: no widget, BLE scan, or device write.
    from sc201_workbench.native import NativeWorkbench
    NativeWorkbench()
    if os.environ.get('RTT_UPDATE_VERSION') != APP_VERSION:
        raise RuntimeError('Packaged application version does not match its release')
    atomic_json(Path(path), {'version': APP_VERSION, 'nonce': os.environ['RTT_UPDATE_NONCE']})
    window.setEnabled(True)
