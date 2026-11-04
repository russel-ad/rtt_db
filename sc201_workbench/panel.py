"""Embed all five original SC201 workbench pages in the RTT Qt application."""
from __future__ import annotations

from pathlib import Path
import os
import sys

from PyQt5.QtCore import QTimer, pyqtSignal
from PyQt5.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from .native import NativeWorkbench, WorkbenchError, build_manifest


def default_directory(settings: dict) -> Path:
    configured = settings.get('sc201_workbench_data_dir')
    if configured:
        return Path(configured)
    original = build_manifest().get('source_data_directory')
    if original and Path(original).is_dir():
        return Path(original)
    if os.environ.get('RTT_DATA_DIR'):
        return Path(os.environ['RTT_DATA_DIR']) / 'private/workbench'
    base = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]
    return base / 'private/workbench'


class Sc201WorkbenchWidget(QWidget):
    closed = pyqtSignal()
    active_changed = pyqtSignal(bool)

    def __init__(self, parent=None, *, settings=None, operation_guard=None,
                 native_factory=NativeWorkbench, data_directory=None):
        super().__init__(parent)
        self.setObjectName('sc201WorkbenchPage')
        self.settings = settings if settings is not None else {}
        self.guard = operation_guard
        self.native_factory = native_factory
        self.directory = Path(data_directory) if data_directory else default_directory(self.settings)
        self.native = None
        self.workbench = None
        self.is_closed = False
        self.closing = False
        self._active = False
        self._last_state = {}
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(8, 8, 8, 8)
        row = QHBoxLayout()
        row.addWidget(QLabel('工作台资料：'))
        self.directory_input = QLineEdit(str(self.directory))
        self.directory_input.setReadOnly(True)
        self.directory_input.setToolTip('沿用所选目录的设备记录、账号缓存和操作日志。')
        row.addWidget(self.directory_input, 1)
        self.choose_button = QPushButton('选择资料目录')
        self.choose_button.clicked.connect(self.choose_directory)
        row.addWidget(self.choose_button)
        self.load_button = QPushButton('载入工作台')
        self.load_button.clicked.connect(self.ensure_loaded)
        row.addWidget(self.load_button)
        self.layout.addLayout(row)
        self.notice = QLabel('载入后可使用日常操作、加密固件升级、工厂生产、连接测试和资料调试。')
        self.notice.setWordWrap(True)
        self.layout.addWidget(self.notice)
        self.layout.addStretch(1)
        self.timer = QTimer(self)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self.refresh_state)

    def state(self):
        if self.workbench is not None:
            self._last_state = self.native.state(self.workbench)
        return dict(self._last_state)

    @property
    def active(self):
        state = self.state()
        return bool(state.get('connected') or state.get('busy') or state.get('scanning'))

    def ensure_loaded(self):
        reason = self.guard() if self.guard else None
        if reason:
            self.notice.setText(reason)
            return False
        if self.workbench is not None:
            return True
        try:
            self.native = self.native_factory()
            self.workbench = self.native.create(self, self.directory)
        except (OSError, ValueError, WorkbenchError) as error:
            self.notice.setText(f'工作台尚未载入：{error}')
            return False
        self.layout.takeAt(self.layout.count() - 1)
        self.layout.addWidget(self.workbench, 1)
        self.notice.hide()
        self.load_button.hide()
        self.timer.start()
        self.refresh_state()
        return True

    def refresh_state(self):
        state = self.state()
        active = bool(state.get('connected') or state.get('busy') or state.get('scanning'))
        self.choose_button.setEnabled(not active)
        if active != self._active:
            self._active = active
            self.active_changed.emit(active)
        if self.closing and not state.get('busy') and self.request_shutdown():
            self.closed.emit()

    def choose_directory(self):
        if self.active:
            self.notice.setText('请先停止工作台操作并断开设备，再更换资料目录。')
            self.notice.show()
            return
        selected = QFileDialog.getExistingDirectory(self, '选择原有或新的工作台资料目录', str(self.directory))
        if not selected:
            return
        if self.workbench is not None:
            if not self.native.shutdown(self.workbench):
                self.notice.setText(self.native.error())
                self.notice.show()
                return
            self.timer.stop()
            from PyQt5 import sip
            self.layout.removeWidget(self.workbench)
            sip.delete(self.workbench)
            self.workbench = None
            self._last_state = {}
            self.layout.addStretch(1)
        self.directory = Path(selected)
        self.settings['sc201_workbench_data_dir'] = str(self.directory)
        self.directory_input.setText(str(self.directory))
        self.ensure_loaded()

    def request_shutdown(self):
        if self.is_closed:
            return True
        self.closing = True
        if self.workbench is not None and not self.native.shutdown(self.workbench):
            self.notice.setText(self.native.error())
            self.notice.show()
            return False
        self.timer.stop()
        self.is_closed = True
        if self._active:
            self._active = False
            self.active_changed.emit(False)
        return True
