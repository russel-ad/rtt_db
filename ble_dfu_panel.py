"""PyQt5 BLE DFU workspace, isolated from RTT/serial I/O and its threads."""
from __future__ import annotations

import asyncio
import csv
from concurrent.futures import Future
from datetime import datetime
import logging
import os
from pathlib import Path
import threading

from PyQt5.QtCore import QObject, Qt, pyqtSignal, QUrl, QSize
from PyQt5.QtGui import QDesktopServices, QFont
from PyQt5.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy,
    QSpinBox, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
    QScrollArea, QStyledItemDelegate, QStyleOptionProgressBar, QStyle, QApplication,
)

from onmicro_dfu.controller import DfuController
from onmicro_dfu.batch import BatchController, DeviceLogFilter
from onmicro_dfu.package import DfuError, FirmwarePackage
from onmicro_dfu.protocol import CommitUncertain, TransferOptions


class Bridge(QObject):
    event = pyqtSignal(str, object)


class BleWorker:
    """One loop owns every Bleak object from connect through disconnect."""
    def __init__(self, bridge):
        self.bridge = bridge
        self.loop = asyncio.new_event_loop()
        self.controller = DfuController(bridge.event.emit)
        self.batch = BatchController(bridge.event.emit)
        self.action = None
        self.future: Future | None = None
        self.task = None
        self.thread = threading.Thread(target=self._run, name="OnMicro-BLE", daemon=True)
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()

    def submit(self, action, coroutine):
        if self.future is not None and not self.future.done():
            coroutine.close()
            return False

        async def execute():
            self.action = action
            self.task = asyncio.current_task()
            try:
                return {"action": action, "status": "ok", "result": await coroutine}
            except asyncio.CancelledError:
                await self.controller.disconnect()
                return {"action": action, "status": "cancelled"}
            except CommitUncertain as exc:
                logging.getLogger("onmicro_dfu.gui").exception("最终提交结果未确认")
                return {"action": action, "status": "uncertain", "error": str(exc)}
            except Exception as exc:
                logging.getLogger("onmicro_dfu.gui").exception("%s 操作失败", action)
                return {"action": action, "status": "error", "error": str(exc)}
            finally:
                self.task = None
                self.action = None

        self.future = asyncio.run_coroutine_threadsafe(execute(), self.loop)
        def finished(future):
            try:
                outcome = future.result()
            except BaseException as exc:
                outcome = {"action": action, "status": "error", "error": str(exc)}
            self.bridge.event.emit("completed", outcome)
        self.future.add_done_callback(finished)
        return True

    async def upgrade_batch(self, *args, **kwargs):
        await self.controller.disconnect()
        return await self.batch.run(*args, **kwargs)

    def cancel(self):
        def cancel_on_owner():
            if self.action == "batch":
                # Never cancel the parent gather: stop children selectively.
                if self.batch.running:
                    self.batch.cancel()
                elif self.task is not None:
                    self.task.cancel()  # Still releasing a prior single connection.
            elif self.controller.critical:
                self.bridge.event.emit("cancel_blocked", None)
            elif self.task is not None:
                self.task.cancel()
        self.loop.call_soon_threadsafe(cancel_on_owner)

    def stop(self):
        if self.thread.is_alive():
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=1.0)


class PanelLogHandler(logging.Handler):
    def __init__(self, bridge):
        super().__init__(logging.INFO)
        self.bridge = bridge
        self.addFilter(DeviceLogFilter())
        self.setFormatter(logging.Formatter("%(asctime)s  %(device_prefix)s%(message)s", "%H:%M:%S"))

    def emit(self, record):
        self.bridge.event.emit("log", self.format(record))


class DeviceProgressDelegate(QStyledItemDelegate):
    """Draw a real progress bar from each row's existing percentage value."""
    def paint(self, painter, option, index):
        text = str(index.data(Qt.DisplayRole) or "0.0%")
        try:
            value = round(float(text.rstrip("%")) * 10)
        except ValueError:
            value = 0
        bar = QStyleOptionProgressBar()
        if option.widget is not None:
            bar.initFrom(option.widget)
        bar.rect = option.rect.adjusted(4, 4, -4, -4)
        bar.minimum, bar.maximum = 0, 1000
        bar.progress = min(1000, max(0, value))
        bar.text, bar.textVisible = text, True
        bar.textAlignment = Qt.AlignCenter
        QApplication.style().drawControl(QStyle.CE_ProgressBar, bar, painter, option.widget)

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        return QSize(max(140, size.width()), max(34, size.height()))


class BleDfuWidget(QWidget):
    closed = pyqtSignal()
    upgrade_active = pyqtSignal(bool)

    def __init__(self, parent=None, *, worker_factory=BleWorker, upgrade_guard=None):
        super().__init__(parent)
        self.setObjectName("bleDfuWorkspace")
        self.setFont(QFont("Microsoft YaHei UI", 9))
        self.busy = False
        self.connected = False
        self.upgrading = False
        self.critical = False
        self.is_closed = False
        self.closing = False
        self.package = None
        self.devices = []
        self.checked_devices = {}
        self.batch_rows = {}
        self.batch_states = {}
        self.batch_running = False
        self.batch_stopping = False
        self.upgrade_guard = upgrade_guard
        self.bridge = Bridge(self)
        self.bridge.event.connect(self.on_event, Qt.QueuedConnection)
        self.worker = worker_factory(self.bridge)
        self._build_ui()
        self._configure_logging()
        self._refresh()

    def _configure_logging(self):
        self.log_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "RTT_T2" / "ble_dfu" / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.log_dir / (datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".log")
        self.logger = logging.getLogger("onmicro_dfu")
        self.old_log_state = self.logger.level, self.logger.propagate
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.file_handler = logging.FileHandler(self.log_path, encoding="utf-8")
        self.file_handler.addFilter(DeviceLogFilter())
        self.file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(device_prefix)s%(message)s"))
        self.panel_handler = PanelLogHandler(self.bridge)
        self.logger.addHandler(self.file_handler)
        self.logger.addHandler(self.panel_handler)
        self.append_log("就绪：批量模式勾选设备后自动连接升级；也可切换单设备操作。")

    @staticmethod
    def button(text, callback, *, primary=False):
        button = QPushButton(text)
        button.setMinimumHeight(32)
        if primary:
            button.setProperty("primary", True)
        button.clicked.connect(callback)
        return button

    def _build_ui(self):
        self.setStyleSheet("""
          QWidget#bleDfuWorkspace { background: #f3f6fa; color: #172b46; }
          QWidget#bleDfuWorkspace QGroupBox { background: white; border: 1px solid #dae2ec;
            border-radius: 8px; margin-top: 12px; padding: 12px; font-weight: 600; }
          QWidget#bleDfuWorkspace QGroupBox::title { subcontrol-origin: margin; left: 14px;
            padding: 0 5px; color: #253e5a; }
          QWidget#bleDfuWorkspace QPushButton { background: #f8fafc; border: 1px solid #cdd8e5;
            border-radius: 5px; padding: 5px 12px; color: #243c57; }
          QWidget#bleDfuWorkspace QPushButton:hover { background: #eaf2fc; border-color: #8db3de; }
          QWidget#bleDfuWorkspace QPushButton[primary="true"] { background: #1768bb; color: white; border: none; }
          QWidget#bleDfuWorkspace QPushButton[primary="true"]:hover { background: #12569c; }
          QWidget#bleDfuWorkspace QPushButton:disabled { background: #edf1f5; color: #9caabb; border-color: #e0e7ee; }
          QWidget#bleDfuWorkspace QLineEdit, QWidget#bleDfuWorkspace QComboBox,
          QWidget#bleDfuWorkspace QSpinBox, QWidget#bleDfuWorkspace QDoubleSpinBox {
            border: 1px solid #cfd9e5; border-radius: 4px; padding: 5px; background: white; min-height: 20px; }
          QWidget#bleDfuWorkspace QTreeWidget { border: 1px solid #e1e7ef; background: white; alternate-background-color: #f7faff; }
          QWidget#bleDfuWorkspace QHeaderView::section { background: #edf3f9; border: none; padding: 7px; color: #536a82; }
          QWidget#bleDfuWorkspace QProgressBar { border: none; border-radius: 5px; background: #e6edf5;
            min-height: 24px; text-align: center; color: #17344f; }
          QWidget#bleDfuWorkspace QProgressBar::chunk { border-radius: 5px; background: #3885cc; }
          QWidget#bleDfuWorkspace QPlainTextEdit { border: 1px solid #d8e2ed; border-radius: 5px;
            background: #102238; color: #d7e5f5; padding: 8px; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(10)
        header = QHBoxLayout()
        title_column = QVBoxLayout()
        title = QLabel("蓝牙 DFU 升级")
        title.setFont(QFont("Microsoft YaHei UI", 17, QFont.Bold))
        subtitle = QLabel("SC201 / SC202 从机  ·  OnMicro DFU  ·  使用电脑蓝牙")
        subtitle.setStyleSheet("color: #667d93;")
        title_column.addWidget(title)
        title_column.addWidget(subtitle)
        header.addLayout(title_column)
        header.addStretch()
        self.settings_toggle = QCheckBox("显示设备 / 固件设置")
        self.settings_toggle.setChecked(True)
        self.settings_toggle.toggled.connect(lambda visible: self.settings_splitter.setVisible(visible))
        header.addWidget(self.settings_toggle)
        self.connection_badge = QLabel("●  未连接")
        self.connection_badge.setStyleSheet("color: #73859a; font-weight: 600;")
        header.addWidget(self.connection_badge)
        layout.addLayout(header)

        # Only settings/results/logs scroll. Progress and stop controls remain
        # outside this viewport, including at high DPI or in a short window.
        root_layout = layout
        self.body_scroll = QScrollArea()
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setFrameShape(QFrame.NoFrame)
        self.body_scroll.setMinimumSize(0, 0)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.body_scroll.setWidget(body)
        root_layout.addWidget(self.body_scroll, 1)

        splitter = QSplitter(Qt.Horizontal)
        self.settings_splitter = splitter
        device_box = QGroupBox("01  设备连接")
        device_layout = QVBoxLayout(device_box)
        device_layout.setSpacing(9)
        scan_row = QHBoxLayout()
        self.environment_btn = self.button("检查蓝牙", self.on_environment)
        self.scan_btn = self.button("扫描设备", self.on_scan, primary=True)
        scan_row.addWidget(self.environment_btn)
        scan_row.addWidget(self.scan_btn)
        device_layout.addLayout(scan_row)
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("按名称或地址筛选扫描结果")
        self.filter_input.textChanged.connect(self.show_devices)
        device_layout.addWidget(self.filter_input)
        rssi_row = QHBoxLayout()
        self.rssi_filter = QCheckBox("RSSI 过滤")
        self.rssi_filter.setToolTip("仅显示 RSSI 大于或等于门限的设备。筛选保留已有勾选；“清空勾选”可移除全部设备。")
        self.rssi_input = QSpinBox()
        self.rssi_input.setRange(-127, 0)
        self.rssi_input.setValue(-70)
        self.rssi_input.setPrefix("≥ ")
        self.rssi_input.setSuffix(" dBm")
        self.rssi_input.setToolTip("例如 -70 dBm：显示 -70、-60 等较强信号，隐藏 -80 等较弱信号。修改后立即筛选本次扫描结果。")
        self.rssi_filter.toggled.connect(self.show_devices)
        self.rssi_input.valueChanged.connect(self.show_devices)
        rssi_row.addWidget(self.rssi_filter)
        rssi_row.addWidget(self.rssi_input)
        rssi_row.addStretch()
        device_layout.addLayout(rssi_row)
        self.device_tree = QTreeWidget()
        self.device_tree.setHeaderLabels(["设备名称", "地址", "信号"])
        self.device_tree.setRootIsDecorated(False)
        self.device_tree.setAlternatingRowColors(True)
        self.device_tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.device_tree.setMinimumHeight(130)
        self.device_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.device_tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.device_tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.device_tree.itemSelectionChanged.connect(self.on_selected)
        self.device_tree.itemChanged.connect(self.on_device_checked)
        device_layout.addWidget(self.device_tree, 1)
        self.address_input = QLineEdit()
        self.address_input.setPlaceholderText("选择设备，或输入 AA:BB:CC:DD:EE:FF")
        self.address_input.textChanged.connect(self._refresh)
        device_layout.addWidget(self.address_input)
        connection_row = QHBoxLayout()
        self.connect_btn = self.button("连接并查询", self.on_connect, primary=True)
        self.disconnect_btn = self.button("断开", self.on_disconnect)
        connection_row.addWidget(self.connect_btn, 1)
        connection_row.addWidget(self.disconnect_btn)
        device_layout.addLayout(connection_row)
        self.device_info = QLabel("软件版本：—\nROM ID：—\nMTU / 分片上限：—")
        self.device_info.setStyleSheet("color: #536b82; line-height: 1.5;")
        device_layout.addWidget(self.device_info)
        splitter.addWidget(device_box)

        firmware_box = QGroupBox("02  固件与升级")
        firmware_layout = QVBoxLayout(firmware_box)
        firmware_layout.setSpacing(10)
        firmware_row = QHBoxLayout()
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("选择原厂 OnMicro DFU ZIP 文件")
        self.path_input.textChanged.connect(self.invalidate_package)
        self.browse_btn = self.button("选择固件", self.on_browse)
        firmware_row.addWidget(self.path_input, 1)
        firmware_row.addWidget(self.browse_btn)
        firmware_layout.addLayout(firmware_row)
        form = QFormLayout()
        self.layout_combo = QComboBox()
        self.layout_combo.addItem("旧设备首次升级 · 最大 280 KiB", False)
        self.layout_combo.addItem("已完成分区迁移 · 最大 400 KiB", True)
        self.layout_combo.currentIndexChanged.connect(self.invalidate_package)
        self.version_input = QLineEdit()
        self.version_input.setPlaceholderText("从包内识别，也可手动填写目标版本")
        self.version_input.textChanged.connect(self._refresh)
        form.addRow("设备分区", self.layout_combo)
        form.addRow("目标版本", self.version_input)
        firmware_layout.addLayout(form)
        self.package_info = QLabel("尚未选择固件\n选择后自动校验格式、ROM ID、长度和 CRC。")
        self.package_info.setWordWrap(True)
        self.package_info.setMinimumHeight(65)
        self.package_info.setStyleSheet("background: #f1f6fc; border-radius: 5px; padding: 10px; color: #5a7088;")
        firmware_layout.addWidget(self.package_info)
        self.same_version = QCheckBox("允许重装同版本")
        firmware_layout.addWidget(self.same_version)

        self.advanced_toggle = QCheckBox("高级传输设置")
        self.advanced = QWidget()
        advanced_form = QGridLayout(self.advanced)
        advanced_form.setContentsMargins(0, 0, 0, 0)
        self.chunk_combo = QComboBox()
        self.chunk_combo.addItem("自动（最多 128 字节）", 0)
        self.chunk_combo.addItem("20 字节（兼容模式）", 20)
        self.prn_input = QSpinBox()
        self.prn_input.setRange(1, 255)
        self.prn_input.setValue(8)
        self.delay_input = QDoubleSpinBox()
        self.delay_input.setRange(0, 1000)
        self.delay_input.setSuffix(" ms")
        self.address_type = QComboBox()
        self.address_type.addItems(["public", "random"])
        for row, (label, control) in enumerate([
            ("分片长度", self.chunk_combo), ("回执间隔 PRN", self.prn_input),
            ("包间延时", self.delay_input), ("地址类型", self.address_type)]):
            advanced_form.addWidget(QLabel(label), row, 0)
            advanced_form.addWidget(control, row, 1)
        self.advanced.setVisible(False)
        self.advanced_toggle.toggled.connect(self.advanced.setVisible)
        firmware_layout.addWidget(self.advanced_toggle)
        firmware_layout.addWidget(self.advanced)
        firmware_layout.addStretch()
        action_row = QHBoxLayout()
        self.check_btn = self.button("重新检查", self.on_check)
        self.start_btn = self.button("开始升级", self.on_upgrade, primary=True)
        self.cancel_btn = self.button("取消操作", self.on_cancel)
        action_row.addWidget(self.check_btn)
        action_row.addStretch()
        action_row.addWidget(self.start_btn)
        action_row.addWidget(self.cancel_btn)
        splitter.addWidget(firmware_box)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 6)
        layout.addWidget(splitter, 3)

        batch_box = QGroupBox("03  批量升级")
        batch_layout = QVBoxLayout(batch_box)
        batch_controls = QHBoxLayout()
        self.batch_mode = QCheckBox("批量升级（勾选设备后自动连接）")
        self.batch_mode.setChecked(True)
        self.batch_mode.toggled.connect(self._refresh)
        batch_controls.addWidget(self.batch_mode)
        batch_controls.addStretch()
        batch_controls.addWidget(QLabel("同时升级"))
        self.concurrency_input = QSpinBox()
        self.concurrency_input.setRange(1, 4)
        self.concurrency_input.setValue(2)
        self.concurrency_input.setSuffix(" 台")
        self.concurrency_input.setToolTip("1 为逐台升级；建议先验证 2 台。实际并发能力取决于蓝牙适配器。")
        batch_controls.addWidget(self.concurrency_input)
        self.select_all_btn = self.button("勾选可见设备", lambda: self.check_visible(True))
        self.select_none_btn = self.button("清空勾选", self.clear_checked)
        self.export_results_btn = self.button("导出结果", self.export_results)
        batch_controls.addWidget(self.select_all_btn)
        batch_controls.addWidget(self.select_none_btn)
        batch_controls.addWidget(self.export_results_btn)
        batch_layout.addLayout(batch_controls)
        self.batch_hint = QLabel("已勾选 0 台 · 默认并发 2；并发 1 为顺序升级")
        batch_layout.addWidget(self.batch_hint)
        self.result_tree = QTreeWidget()
        self.result_tree.setHeaderLabels(["设备 / 地址", "阶段", "进度", "速度", "版本", "结果详情"])
        self.result_tree.setRootIsDecorated(False)
        self.result_tree.setAlternatingRowColors(True)
        self.result_tree.setMinimumHeight(180)
        self.result_tree.setMaximumHeight(240)
        self.result_tree.setItemDelegateForColumn(2, DeviceProgressDelegate(self.result_tree))
        self.result_tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.result_tree.header().setSectionResizeMode(5, QHeaderView.Stretch)
        for column in (1, 2, 3, 4):
            self.result_tree.header().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.result_tree.header().setSectionResizeMode(2, QHeaderView.Fixed)
        self.result_tree.setColumnWidth(2, 155)
        batch_layout.addWidget(self.result_tree)
        layout.addWidget(batch_box, 1)

        self.status_label = QLabel("就绪 · 扫描并连接设备后可开始升级")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("font-weight: 600; color: #405a75;")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("传输进度 %p%")
        self.progress_bar.setToolTip("传输达到 100% 后，仍需等待提交、重启和目标版本确认。")
        self.metrics = QLabel("0 / 0 字节    ·    0.0 KiB/s    ·    0.0 s")
        self.metrics.setWordWrap(True)
        self.metrics.setStyleSheet("color: #698097;")
        self.progress_footer = QFrame()
        self.progress_footer.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        footer_layout = QVBoxLayout(self.progress_footer)
        footer_layout.setContentsMargins(0, 6, 0, 0)
        footer_layout.setSpacing(6)
        footer_layout.addWidget(self.status_label)
        footer_layout.addWidget(self.progress_bar)
        footer_layout.addWidget(self.metrics)
        footer_layout.addLayout(action_row)
        root_layout.addWidget(self.progress_footer)
        log_row = QHBoxLayout()
        log_row.addWidget(QLabel("操作日志"))
        log_row.addStretch()
        self.export_btn = self.button("导出日志", self.export_log)
        self.log_folder_btn = self.button("日志目录", self.open_log_folder)
        self.clear_btn = self.button("清空显示", lambda: self.log_text.clear())
        log_row.addWidget(self.export_btn)
        log_row.addWidget(self.log_folder_btn)
        log_row.addWidget(self.clear_btn)
        layout.addLayout(log_row)
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumBlockCount(4000)
        self.log_text.setFont(QFont("Consolas", 9))
        self.log_text.setMinimumHeight(110)
        layout.addWidget(self.log_text, 1)

    def _refresh(self, *_):
        if not hasattr(self, "start_btn"):
            return
        idle = not self.busy and not self.closing
        batch = self.batch_mode.isChecked()
        self.scan_btn.setEnabled(idle)
        self.environment_btn.setEnabled(idle)
        self.connect_btn.setEnabled(idle and bool(self.address_input.text().strip()))
        self.connect_btn.setText("重新查询" if self.connected else "连接并查询")
        self.disconnect_btn.setEnabled(idle and self.connected)
        self.start_btn.setEnabled(idle and (bool(self.checked_devices) if batch else self.connected) and self.package is not None
                                  and bool(self.version_input.text().strip()))
        self.start_btn.setText("开始批量升级" if batch else "开始升级")
        self.cancel_btn.setText("停止批量" if self.batch_running else "取消操作")
        self.cancel_btn.setEnabled(self.busy and (self.batch_running or not self.critical)
                                  and not self.closing and not self.batch_stopping)
        self.check_btn.setEnabled(idle and bool(self.path_input.text().strip()))
        self.address_input.setReadOnly(self.connected or not idle)
        for widget in (self.browse_btn, self.path_input, self.layout_combo,
                       self.version_input, self.same_version, self.chunk_combo,
                       self.prn_input, self.delay_input, self.address_type,
                       self.batch_mode, self.concurrency_input, self.select_all_btn, self.select_none_btn):
            widget.setEnabled(idle)
        self.device_tree.setEnabled(idle)
        self.filter_input.setEnabled(idle)
        self.rssi_filter.setEnabled(idle)
        self.rssi_input.setEnabled(idle and self.rssi_filter.isChecked())
        self.result_tree.setVisible(batch)
        self.export_results_btn.setEnabled(bool(self.batch_states) and not self.batch_running)
        if not self.batch_running:
            visible = {self.device_tree.topLevelItem(i).data(0, Qt.UserRole).upper()
                       for i in range(self.device_tree.topLevelItemCount())}
            hidden_count = len(set(self.checked_devices) - visible)
            hidden_hint = f"（筛选隐藏 {hidden_count} 台）" if hidden_count else ""
            self.batch_hint.setText(f"已勾选 {len(self.checked_devices)} 台{hidden_hint} · 并发 1 为顺序升级；其余为并行升级")

    def set_status(self, text, tone="normal"):
        colors = {"normal": "#405a75", "busy": "#1768bb", "ok": "#168263", "error": "#c04343"}
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"font-weight: 600; color: {colors[tone]};")

    def submit(self, action, coroutine, text):
        if self.worker.submit(action, coroutine):
            self.busy = True
            self.set_status(text, "busy")
            self._refresh()

    def on_environment(self):
        self.submit("environment", self.worker.controller.environment(), "正在检查本机蓝牙环境…")

    def on_scan(self):
        self.submit("scan", self.worker.controller.scan_devices(), "正在扫描附近设备，请稍候…")

    def show_devices(self, *_):
        keyword = self.filter_input.text().casefold().strip()
        self.device_tree.blockSignals(True)
        self.device_tree.clear()
        for device in self.devices:
            if keyword and keyword not in (device["name"] + device["address"]).casefold():
                continue
            rssi = device.get("rssi")
            if self.rssi_filter.isChecked() and (rssi is None or rssi < self.rssi_input.value()):
                continue
            signal_text = f"{rssi} dBm" if rssi is not None else "未知"
            item = QTreeWidgetItem([device["name"] or "未命名设备", device["address"], signal_text])
            item.setData(0, Qt.UserRole, device["address"])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(0, Qt.Checked if device["address"].upper() in self.checked_devices else Qt.Unchecked)
            self.device_tree.addTopLevelItem(item)
        self.device_tree.blockSignals(False)
        self._refresh()

    def on_device_checked(self, item, column):
        if column != 0 or self.busy:
            return
        address = item.data(0, Qt.UserRole).upper()
        if item.checkState(0) == Qt.Checked:
            self.checked_devices[address] = item.text(0)
        else:
            self.checked_devices.pop(address, None)
        self._refresh()

    def check_visible(self, checked):
        for index in range(self.device_tree.topLevelItemCount()):
            self.device_tree.topLevelItem(index).setCheckState(0, Qt.Checked if checked else Qt.Unchecked)

    def clear_checked(self):
        self.checked_devices.clear()
        self.show_devices()

    def on_selected(self):
        if self.connected or self.busy:
            return
        items = self.device_tree.selectedItems()
        if items:
            self.address_input.setText(items[0].data(0, Qt.UserRole))

    def on_connect(self):
        if self.connected:
            coroutine = self.worker.controller.query()
        else:
            coroutine = self.worker.controller.connect(self.address_input.text().strip(), self.address_type.currentText())
        self.submit("connect", coroutine, "正在连接并查询设备…")

    def on_disconnect(self):
        self.submit("disconnect", self.worker.controller.disconnect(), "正在断开设备…")

    def invalidate_package(self, *_):
        self.package = None
        if hasattr(self, "package_info"):
            self.package_info.setText("固件待检查\n请点击“重新检查”，确认包内容与所选分区。")
        self._refresh()

    def on_browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 OnMicro DFU 固件包", self.path_input.text(), "OnMicro DFU ZIP (*.zip)")
        if path:
            self.path_input.setText(path)
            self.on_check()

    def on_check(self):
        self.load_package(self.path_input.text().strip())

    def load_package(self, path):
        self.path_input.setText(str(path))
        self.path_input.setToolTip(str(path))
        try:
            package = FirmwarePackage.load(Path(path), expanded=self.layout_combo.currentData())
        except Exception as exc:
            self.package = None
            self.package_info.setText(f"固件检查未通过\n{exc}")
            self.set_status("固件检查失败，请核对 ZIP 和设备分区", "error")
            self.append_log(str(exc))
        else:
            self.package = package
            self.version_input.setText(package.version or "")
            self.package_info.setText(
                f"✓  校验通过    版本 {package.version or '未识别，请填写目标版本'}\n"
                f"{len(package.image):,} 字节 / {len(package.image)/1024:.2f} KiB    ·    ROM 0x{package.rom_id:08X}\n"
                f"CRC16 {package.crc16:04X}    ·    SHA-256 {package.sha256[:20]}…")
            self.set_status("固件检查通过 · 连接设备后可开始升级", "ok")
            self.append_log(f"已检查固件：{Path(path).name}，{len(package.image)} 字节")
        self._refresh()

    def on_upgrade(self):
        if self.upgrade_guard:
            reason = self.upgrade_guard()
            if reason:
                self.set_status(reason, "error")
                return
        if self.package is None:
            return
        if self.batch_mode.isChecked():
            self.on_batch_upgrade()
            return
        if not self.connected:
            return
        options = TransferOptions(self.chunk_combo.currentData(), self.prn_input.value(),
                                  packet_delay=self.delay_input.value() / 1000)
        self.upgrading = True
        self.settings_toggle.setChecked(False)
        self.critical = False
        self.upgrade_active.emit(True)
        self.progress_bar.setValue(0)
        self.submit("upgrade", self.worker.controller.upgrade(
            Path(self.path_input.text().strip()), self.version_input.text().strip(), options,
            expanded=self.layout_combo.currentData(), allow_same=self.same_version.isChecked()),
            "正在准备升级并核对设备…")

    def on_batch_upgrade(self):
        if not self.checked_devices or self.busy:
            return
        self.batch_running = True
        self.batch_stopping = False
        self.upgrading = True
        self.settings_toggle.setChecked(False)
        self.critical = False
        self.upgrade_active.emit(True)
        self.batch_states.clear()
        self.batch_rows.clear()
        self.result_tree.clear()
        for address, name in self.checked_devices.items():
            row = QTreeWidgetItem([f"{name}  {address}", "排队中", "0.0%", "—", "—", ""])
            row.setToolTip(0, address)
            self.result_tree.addTopLevelItem(row)
            self.batch_rows[address] = row
            self.batch_states[address] = {"status": "queued", "done": 0, "speed": 0,
                                           "critical": False, "name": name}
        self.progress_bar.setValue(0)
        options = TransferOptions(self.chunk_combo.currentData(), self.prn_input.value(),
                                  packet_delay=self.delay_input.value() / 1000)
        self.submit("batch", self.worker.upgrade_batch(list(self.checked_devices),
            Path(self.path_input.text().strip()), self.version_input.text().strip(), options,
            concurrency=self.concurrency_input.value(), expanded=self.layout_combo.currentData(),
            allow_same=self.same_version.isChecked(), address_type=self.address_type.currentText()),
            "批量升级开始 · 正在按并发上限自动连接…")

    def on_batch_device(self, event):
        address, kind, data = event["address"], event["kind"], event["data"]
        row = self.batch_rows.get(address)
        if row is None:
            return
        state = self.batch_states[address]
        phases = {"queued": "排队中", "connecting": "连接并查询", "transfer": "传输中",
                  "commit": "提交中", "reboot": "等待重启", "verify": "确认版本",
                  "success": "升级成功", "error": "失败", "uncertain": "结果待确认", "cancelled": "已停止"}
        if kind == "state":
            state["status"] = data["status"]
        elif kind == "connected":
            state["connected"] = True
            state["version"] = data.get("software_version") or "未读取"
            row.setText(4, state["version"])
        elif kind == "phase":
            state.update(status=data["name"], critical=data["critical"])
            if data["critical"]:
                state["speed"] = 0
                row.setText(3, "—")
            self.append_log(f'[{address}] {phases.get(data["name"], data["name"])}')
        elif kind == "progress":
            state.update(done=data["done"], speed=data["speed"])
            row.setText(2, f'{data["percent"]:.1f}%')
            row.setText(3, f'{data["speed"]:.1f} KiB/s')
        elif kind == "disconnected":
            state["connected"] = False
            if not data.get("expected"):
                row.setText(5, "连接已断开，等待任务结果")
        elif kind == "result":
            state.update(data, critical=False, speed=0, connected=False)
            row.setText(3, "—")
            if data["status"] == "success":
                state["done"] = len(self.package.image) if self.package else 0
                row.setText(2, "100.0%")
                row.setText(4, data["version"])
            detail = data.get("error") or f'已确认目标版本；耗时 {data.get("elapsed", 0):.1f}s'
            state["detail"] = detail
            row.setText(5, detail)
            row.setToolTip(5, detail)
            self.append_log(f'[{address}] {phases[data["status"]]}：{detail}')
        row.setText(1, phases.get(state["status"], state["status"]))
        self.critical = any(value.get("critical") for value in self.batch_states.values())
        connected_count = sum(bool(value.get("connected")) for value in self.batch_states.values())
        self.connection_badge.setText(f"●  本批连接 {connected_count} 台")
        terminal = {"success", "error", "uncertain", "cancelled"}
        completed = sum(value["status"] in terminal for value in self.batch_states.values())
        successful = sum(value["status"] == "success" for value in self.batch_states.values())
        total = (len(self.package.image) if self.package else 0) * len(self.batch_states)
        done = sum(value.get("done", 0) for value in self.batch_states.values())
        self.progress_bar.setValue(round(done / total * 1000) if total else 0)
        self.metrics.setText(f'累计传输 {done:,} / {total:,} 字节 · 合计速度 {sum(v.get("speed", 0) for v in self.batch_states.values()):.1f} KiB/s')
        self.batch_hint.setText(f'本批 {len(self.batch_states)} 台 · 已结束 {completed} 台 · 成功 {successful} 台 · 并发上限 {self.concurrency_input.value()}')

    def export_results(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出批量升级结果", "ble_dfu_results.csv", "CSV (*.csv)")
        if path:
            try:
                with open(path, "w", newline="", encoding="utf-8-sig") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(["设备名称", "地址", "状态", "版本", "结果详情"])
                    for address, state in self.batch_states.items():
                        writer.writerow([state.get("name", ""), address, state["status"],
                                         state.get("version", ""), state.get("detail", "")])
            except OSError as exc:
                self.set_status(f"结果导出失败：{exc}", "error")

    def on_cancel(self):
        self.worker.cancel()
        self.cancel_btn.setEnabled(False)
        self.set_status("正在停止当前操作…", "busy")

    def on_event(self, kind, data):
        if self.is_closed:
            return
        if kind == "log":
            self.append_log(data)
        elif kind == "devices":
            self.devices = data
            present = {device["address"].upper() for device in data}
            self.checked_devices = {a: name for a, name in self.checked_devices.items() if a in present}
            self.show_devices()
        elif kind == "batch_device":
            self.on_batch_device(data)
        elif kind == "batch_started":
            self.append_log(f'批量任务：{len(data["addresses"])} 台，并发上限 {data["concurrency"]}')
        elif kind == "batch_stopping":
            self.batch_stopping = True
            self.set_status("已停止排队及可取消的传输，等待已提交设备完成版本确认…", "busy")
        elif kind == "batch_finished":
            outcomes = list(data["devices"].values())
            counts = {status: sum(v["status"] == status for v in outcomes)
                      for status in ("success", "error", "uncertain", "cancelled")}
            text = (f'批量结束 · 成功 {counts["success"]} / {len(outcomes)} 台 · '
                    f'失败 {counts["error"]} · 待确认 {counts["uncertain"]} · 已停止 {counts["cancelled"]} · {data["elapsed"]:.1f}s')
            self.set_status(text, "ok" if counts["success"] == len(outcomes) else "error")
            self.append_log(text)
        elif kind == "connected":
            self.connected = True
            self.connection_badge.setText("●  已连接")
            self.connection_badge.setStyleSheet("color: #168263; font-weight: 600;")
            self.address_input.setText(data["address"])
            self.device_info.setText(f'软件版本：{data.get("software_version") or "未读取"}\n'
                                     f'ROM ID：{data["rom_id"]}\nMTU / 分片上限：{data.get("mtu", "—")} / {data.get("max_packet_size", "—")}')
        elif kind == "disconnected":
            self.connected = False
            self.connection_badge.setText("●  未连接")
            self.connection_badge.setStyleSheet("color: #73859a; font-weight: 600;")
            if not self.busy:
                self.set_status("设备已断开")
        elif kind == "environment":
            ok = data["adapter_found"] and data["central_supported"] and data["ble_supported"] and data["bluetooth_on"]
            self.append_log("蓝牙环境正常，可扫描设备。" if ok else f"蓝牙尚未就绪：{data}")
        elif kind == "progress":
            self.progress_bar.setValue(round(data["percent"] * 10))
            self.metrics.setText(f'{data["done"]:,} / {data["total"]:,} 字节    ·    {data["speed"]:.1f} KiB/s    ·    {data["elapsed"]:.1f} s')
        elif kind == "phase":
            self.critical = data["critical"]
            names = {"transfer": "正在传输固件…", "commit": "正在校验并提交固件，请等待…",
                     "reboot": "固件已提交，正在等待设备自动重启…", "verify": "正在重连并确认升级后的版本…"}
            self.set_status(names[data["name"]], "busy")
        elif kind == "upgrade_success":
            self.progress_bar.setValue(1000)
            self.device_info.setText(f'重启后版本：{data["version"]}\n设备：{data["address"]}\n可重新连接查看设备信息')
            self.set_status(f'升级成功 · 已确认版本 {data["version"]} · {data["elapsed"]:.1f} s', "ok")
        elif kind == "cancel_blocked":
            self.closing = False
            self.critical = True
            self.set_status("正在提交或确认重启，请等待完成", "busy")
        elif kind == "completed":
            self.busy = False
            if data["action"] in ("upgrade", "batch"):
                self.upgrading = False
                self.critical = False
                self.batch_running = False
                self.batch_stopping = False
                self.upgrade_active.emit(False)
                if data["action"] == "batch" and data["status"] != "ok":
                    for address, state in list(self.batch_states.items()):
                        if state["status"] in ("queued", "connecting"):
                            self.on_batch_device({"address": address, "kind": "result", "data": {
                                "status": "cancelled" if data["status"] == "cancelled" else "error",
                                "error": data.get("error", "批量任务已停止，未确认升级成功")}})
            if data["status"] == "error":
                self.set_status("操作失败 · " + data["error"], "error")
            elif data["status"] == "uncertain":
                self.set_status("最终提交结果未确认 · 请重新查询设备版本后再操作", "error")
            elif data["status"] == "cancelled":
                self.set_status("操作已取消 · 未确认升级成功")
            elif data["action"] == "scan":
                self.set_status(f"扫描完成 · 发现 {len(self.devices)} 个设备，选择后连接")
            elif data["action"] == "connect":
                self.set_status("设备已连接 · 可选择固件开始升级", "ok")
            elif data["action"] == "disconnect":
                self.set_status("设备已断开")
            elif data["action"] == "environment":
                self.set_status("蓝牙环境检查完成，请查看日志")
            if data["action"] == "shutdown":
                self._finish_shutdown()
                return
            if self.closing:
                self._begin_shutdown()
        self._refresh()

    def append_log(self, text):
        self.log_text.appendPlainText(str(text))

    def export_log(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出操作日志", "ble_dfu.log", "日志文件 (*.log)")
        if path:
            try:
                Path(path).write_text(self.log_text.toPlainText(), encoding="utf-8")
            except OSError as exc:
                self.set_status(f"日志导出失败：{exc}", "error")

    def open_log_folder(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.log_dir)))

    def request_shutdown(self):
        if self.is_closed:
            return True
        if self.critical and not self.batch_running:
            self.set_status("正在提交或确认重启，请等待完成后关闭", "busy")
            return False
        self.closing = True
        if self.busy:
            self.worker.cancel()
        else:
            self._begin_shutdown()
        self._refresh()
        return False

    def _begin_shutdown(self):
        self.submit("shutdown", self.worker.controller.shutdown(), "正在关闭蓝牙连接…")

    def _finish_shutdown(self):
        self.is_closed = True
        self.worker.stop()
        self.logger.removeHandler(self.file_handler)
        self.logger.removeHandler(self.panel_handler)
        self.file_handler.close()
        self.panel_handler.close()
        self.logger.setLevel(self.old_log_state[0])
        self.logger.propagate = self.old_log_state[1]
        self.closed.emit()
