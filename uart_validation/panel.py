"""Operator page for two-probe UART verification."""

from pathlib import Path
from threading import Event

from PyQt5.QtCore import QThread, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (
    QComboBox, QFormLayout, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import probes
from .session import NIGHT, QUICK, Session


DIRECTIONS = (('主机 → 从机', 'H_to_S', 'H', 'S'), ('从机 → 主机', 'S_to_H', 'S', 'H'))
VERDICT = {'PASS': '通过', 'FAIL': '失败', 'INCOMPLETE': '未完成'}


class Worker(QThread):
    update = pyqtSignal(str, object)

    def __init__(self, parent, session=None, stop=None):
        super().__init__(parent)
        self.session = session
        self.stop = stop

    def run(self):
        try:
            if self.session is None:
                self.update.emit('probes', probes.serial_numbers())
            else:
                self.update.emit('result', self.session.execute())
        except Exception as error:
            self.update.emit('error', str(error))


class UartValidationWidget(QWidget):
    active_changed = pyqtSignal(bool)

    def __init__(self, parent, *, settings, directory, guard):
        super().__init__(parent)
        self.settings = settings
        self.directory = directory
        self.guard = guard
        self.worker = None
        self.last_directory = None
        self.last_result = None

        layout = QVBoxLayout(self)
        hint = QLabel('两端装 V001-008-UART1，保持吸合和供电。各接一只 J-Link，选择探针后即可开始。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        self.host = QComboBox()
        self.scale = QComboBox()
        self.profile = QComboBox()
        for profile in (QUICK, NIGHT):
            self.profile.addItem(profile.title, profile)
        form.addRow('主机探针', self.host)
        form.addRow('从机探针', self.scale)
        form.addRow('测试方案', self.profile)
        layout.addLayout(form)

        actions = QHBoxLayout()
        self.refresh = QPushButton('刷新探针')
        self.start = QPushButton('开始测试')
        self.stop = QPushButton('停止并保存')
        self.report = QPushButton('打开报告目录')
        for button in (self.refresh, self.start, self.stop, self.report):
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self.status = QLabel('请选择两端探针')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, QUICK.count)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.table = QTableWidget(2, 7)
        self.table.setHorizontalHeaderLabels(('方向', '已发送', '正确接收', '错帧', '差异位', '失败／漏帧', '帧失败率'))
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMaximumHeight(135)
        layout.addWidget(self.table)
        self.details = QLabel('')
        self.details.setWordWrap(True)
        layout.addWidget(self.details)
        layout.addStretch()

        self.fill_probes([])
        self.reset_table()
        self.refresh.clicked.connect(self.refresh_probes)
        self.start.clicked.connect(self.start_run)
        self.stop.clicked.connect(self.stop_run)
        self.report.clicked.connect(self.open_report)
        self.host.currentIndexChanged.connect(self.update_controls)
        self.scale.currentIndexChanged.connect(self.update_controls)
        self.update_controls()

    @property
    def active(self):
        return self.worker is not None

    def fill_probes(self, serials):
        for key, combo in (('uart_host_probe', self.host), ('uart_scale_probe', self.scale)):
            selected = combo.currentData() or self.settings.get(key)
            combo.clear()
            combo.addItem('请选择', None)
            if isinstance(selected, int) and selected > 0 and selected not in serials:
                combo.addItem(f'{selected}（上次使用）', selected)
            for serial in serials:
                combo.addItem(str(serial), serial)
            combo.setCurrentIndex(max(0, combo.findData(selected)))

    def reset_table(self):
        for row, (label, _, _, _) in enumerate(DIRECTIONS):
            self.set_row(row, label, ['—'] * 6)

    def set_row(self, row, label, values):
        for column, value in enumerate((label, *values)):
            text = f'{value:,}' if isinstance(value, int) else str(value)
            self.table.setItem(row, column, QTableWidgetItem(text))
        self.table.resizeColumnsToContents()

    def update_controls(self):
        host, scale = self.host.currentData(), self.scale.currentData()
        for widget in (self.host, self.scale, self.profile, self.refresh):
            widget.setEnabled(not self.active)
        self.start.setEnabled(not self.active and bool(host and scale and host != scale))
        self.stop.setEnabled(self.active and self.worker.stop is not None and not self.worker.stop.is_set())
        self.report.setEnabled(self.last_directory is not None)

    def launch(self, worker):
        self.worker = worker
        worker.update.connect(self.on_update)
        worker.finished.connect(self.on_finished)
        self.update_controls()
        self.active_changed.emit(True)
        worker.start()

    def refresh_probes(self):
        reason = self.guard()
        if self.active or reason:
            if reason:
                self.status.setText(reason)
            return
        self.status.setText('正在查找 USB J-Link')
        self.launch(Worker(self))

    def start_run(self):
        reason = self.guard()
        if self.active or reason:
            if reason:
                self.status.setText(reason)
            return
        serials = {'H': self.host.currentData(), 'S': self.scale.currentData()}
        if not all(serials.values()) or len(set(serials.values())) != 2:
            self.status.setText('请选择两只不同的 J-Link')
            return
        self.settings.update(uart_host_probe=serials['H'], uart_scale_probe=serials['S'])
        profile = self.profile.currentData()
        self.progress.setRange(0, profile.count)
        self.progress.setValue(0)
        self.reset_table()
        self.details.clear()
        self.last_result = None
        stop = Event()
        worker = Worker(self, stop=stop)
        worker.session = Session(serials, profile, self.directory(), stop, worker.update.emit)
        self.status.setText('正在准备测试')
        self.launch(worker)

    def stop_run(self):
        if self.worker and self.worker.stop:
            self.worker.stop.set()
            self.status.setText('正在停止并保存')
            self.update_controls()

    def on_update(self, kind, payload):
        if kind == 'probes':
            self.fill_probes(payload)
            self.status.setText(f'找到 {len(payload)} 只 J-Link，请选择主机和从机探针')
        elif kind in ('status', 'error'):
            self.status.setText(str(payload))
            if kind == 'error' and self.worker.session and self.worker.session.directory.is_dir():
                self.last_directory = self.worker.session.directory
        elif kind == 'progress':
            elapsed, snapshots = payload
            for row, (label, _, sender, receiver) in enumerate(DIRECTIONS):
                tx, rx = snapshots.get(sender, {}), snapshots.get(receiver, {})
                self.set_row(row, label, [tx.get('tx_done', '—'), rx.get('rx_good', '—'),
                                         rx.get('rx_bad', '—'), rx.get('diff', '—'), '待结算', '待结算'])
            self.progress.setValue(min(snapshots.get(role, {}).get('tx_done', 0) for role in ('H', 'S')))
            self.details.setText(f'已运行 {int(elapsed) // 3600:02d}:{int(elapsed) // 60 % 60:02d}:{int(elapsed) % 60:02d}')
        elif kind == 'result':
            self.last_result = payload
            self.last_directory = self.worker.session.directory
            self.status.setText(f'测试{VERDICT[payload["verdict"]]} · 批次 {payload["run"]}')
            if len(payload['finals']) == 2:
                self.progress.setValue(min(r['tx_done'] for r in payload['finals'].values()))
            for row, (label, key, _, receiver) in enumerate(DIRECTIONS):
                result = payload['directions'].get(key)
                if result:
                    rx = payload['finals'][receiver]
                    rate = result['frame_failure_ratio']
                    self.set_row(row, label, [result['sent_drained_frames'], result['received_correct_unique_frames'],
                                             rx.get('rx_bad', '—'), result['different_payload_bits'],
                                             result['failed_or_missing_frames'], f'{rate:.6%}' if rate is not None else '—'])
            error = payload.get('controller_error')
            if error:
                self.details.setText(error)
            elif payload['verdict'] == 'INCOMPLETE':
                self.details.setText('两端最终统计未闭合，请查看报告中的异常原因。')
            else:
                self.details.setText('两端最终统计已保存。')
        self.update_controls()

    def on_finished(self):
        worker, self.worker = self.worker, None
        worker.deleteLater()
        self.update_controls()
        self.active_changed.emit(False)

    def open_report(self):
        if self.last_directory:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.last_directory).resolve())))
