"""Single-board SC20x production station. UART replies are correlated per request."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NamedTuple

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtSerialPort import QSerialPort, QSerialPortInfo
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)


MODEL_PAGE = '设备信息'
LCD_PAGE = 'LCD 屏幕测试'
LCD_COLORS = ('红', '绿', '蓝', '黑', '白')
SCALE_SIZE = {'SC201-S': '小秤', 'SC202-S': '大秤'}
HOST = ('RTC 对时', '三轴传感器', '蜂鸣器声压', '指示灯', LCD_PAGE, 'P 键', 'T 键和旋钮', 'USB 口充电')
SCALE = ('三点标定', MODEL_PAGE, '三轴传感器', '指示灯', '从机按键', 'USB 口充电')
WIRELESS = ('BLE 升级测试', '写 SN')
COLORS = ('白', '蓝', '橙', '紫', '红')
SCALE_MODELS = ('SC201-S', 'SC202-S')
DEVICE_INFO = re.compile(
    r'model=(\S+) name=(.*?) sn=(\S*) sn_source=(\d+) mac=([0-9A-Fa-f]{12}) '
    r'flags=([0-9A-Fa-f]{2}) state=(\w+)'
)
KEYS = {'P 键': (('短按', 1), ('长按', 2)),
        'T 键和旋钮': (('短按', 4), ('长按', 8), ('正向旋转', 16), ('反向旋转', 32)),
        '从机按键': (('短按', 1), ('长按', 2))}


class PendingRequest(NamedTuple):
    operation: str
    token: str
    deadline: float
    callback: Callable[[str], None]


def verified_ota_row(raw, serial, version, models):
    rows = csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
    matches = [row for row in rows if row.get('SN') == serial and row.get('型号') in models]
    if len(matches) != 1:
        raise ValueError('报告须包含唯一匹配本板型号和 SN 的一行')
    row = matches[0]
    total = row.get('总字节') or ''
    if (not version or row.get('结果') != '成功（已验版）' or
            row.get('当前版本') != version or row.get('目标版本') != version or
            not total.isdigit() or int(total) <= 0 or
            row.get('已传字节') != row['总字节']):
        raise ValueError('升级记录未完成验版或与工单目标版本不一致')
    return row


class StationWidget(QWidget):
    active_changed = pyqtSignal(bool)

    def __init__(self, parent, log_dir, open_workbench, guard):
        super().__init__(parent)
        self.log_dir = log_dir
        self.open_workbench = open_workbench
        self.guard = guard
        self.serial = QSerialPort(self)
        self.serial.readyRead.connect(self.receive)
        self.serial.errorOccurred.connect(self.port_error)
        self.tick = QTimer(self)
        self.tick.timeout.connect(self.poll)
        self.tick.start(150)
        self.pending = None
        self.rx = bytearray()
        self.identity = None
        self.report = None
        self.step = 0
        self.live = ''
        self.until = 0
        self.cal_generation = None
        self.cal_point = None
        self.poll_at = 0
        self.failed = False
        self.finished = False
        self.page = ''
        self.verdicts = {}
        self.adcs = ()
        self.model_write_from = None
        self.device_size = ''
        self.cal_saved = ''
        self.lcd_target = None
        self.lcd_findings = []

        outer = QVBoxLayout(self)
        self.setObjectName('productionStation')
        self.setStyleSheet("""
            QWidget#productionStation {background:#f5f7fa;}
            QWidget#productionStation QLabel {color:#22354a;}
            QWidget#productionStation QListWidget, QWidget#productionStation QPlainTextEdit {
                background:#fff; color:#21354b; border:1px solid #ced9e3; border-radius:7px; padding:6px;}
            QWidget#productionStation QListWidget::item {padding:7px 4px;}
            QWidget#productionStation QListWidget::item:selected {color:#144f81; background:#e4eff8;}
            QFrame#stationTask {background:#eaf1f7; border:1px solid #cbdbe8; border-radius:8px;}
            QLabel#stationValue {font-size:26px; font-weight:600; color:#163f65;}
            QLabel#stationRoute {font-weight:600; color:#356184; padding:4px 0;}
            QPushButton#stationGo {background:#235b89; color:white; padding:6px 16px;
                border:1px solid #235b89; border-radius:5px;}
            QPushButton#stationGo:disabled {background:#d9e1e8; color:#596878; border-color:#d9e1e8;}
        """)
        outer.setContentsMargins(15, 12, 15, 12)
        outer.setSpacing(8)
        top = QHBoxLayout()
        self.role = QComboBox()
        self.role.addItems(['从机单测', '主机单测'])
        self.port = QComboBox()
        self.refresh = QPushButton('刷新串口')
        self.connect_btn = QPushButton('连接并核对设备')
        for item in (self.role, self.port, QLabel('115200 · 8N1'), self.refresh, self.connect_btn):
            top.addWidget(item)
        top.addStretch()
        outer.addLayout(top)
        self.board = QLabel('等待连接')
        outer.addWidget(self.board)
        body = QHBoxLayout()
        self.menu = QListWidget()
        self.menu.setMaximumWidth(185)
        body.addWidget(self.menu)
        right = QVBoxLayout()
        body.addLayout(right, 1)
        outer.addLayout(body)
        self.title = QLabel()
        self.title.setStyleSheet('font-size:20px;font-weight:600')
        right.addWidget(self.title)
        self.guide = QLabel()
        self.guide.setWordWrap(True)
        self.steps_label = QLabel()
        self.steps_label.setObjectName('stationRoute')
        self.steps_label.setWordWrap(True)
        right.addWidget(self.steps_label)
        task = QFrame()
        task.setObjectName('stationTask')
        task_layout = QVBoxLayout(task)
        task_layout.setContentsMargins(18, 13, 18, 13)
        self.readout = QLabel()
        self.readout.setObjectName('stationValue')
        task_layout.addWidget(self.readout)
        task_layout.addWidget(self.guide)
        right.addWidget(task)
        self.cal_rows = QLabel()
        self.cal_rows.setTextFormat(Qt.RichText)
        right.addWidget(self.cal_rows)
        self.model = QComboBox()
        self.model.addItems(SCALE_MODELS)
        self.model_label = QLabel('实物目标型号')
        self.db = QDoubleSpinBox()
        self.db.setRange(0, 150)
        self.db.setSpecialValueText('等待仪表读数')
        self.db.setSuffix(' dB')
        form = QFormLayout()
        form.addRow(self.model_label, self.model)
        form.addRow(self.db)
        self.lcd_form = QWidget()
        lcd_fields = QHBoxLayout(self.lcd_form)
        lcd_fields.setContentsMargins(0, 0, 0, 0)
        self.lcd_checks = [QCheckBox(name) for name in ('条纹', '异色', '花屏', '黑点', '亮点')]
        for check in self.lcd_checks:
            lcd_fields.addWidget(check)
        self.lcd_note = QLineEdit()
        self.lcd_note.setPlaceholderText('缺陷位置或备注（可选）')
        lcd_fields.addWidget(self.lcd_note)
        form.addRow(self.lcd_form)
        self.ota_sn = QLineEdit()
        self.ota_sn.setPlaceholderText('扫描本板 SN')
        self.ota_version = QLineEdit()
        self.ota_version.setPlaceholderText('输入工单目标固件版本')
        self.ota_import = QPushButton('核对并归档工作台验版 CSV')
        self.ota_import.clicked.connect(self.import_ota)
        for field in (self.ota_sn, self.ota_version, self.ota_import):
            form.addRow(field)
        right.addLayout(form)
        self.feedback = QLabel()
        self.feedback.setWordWrap(True)
        self.feedback.setTextInteractionFlags(Qt.TextSelectableByMouse)
        right.addWidget(self.feedback)
        actions = QHBoxLayout()
        self.go = QPushButton()
        self.go.setObjectName('stationGo')
        self.write_model = QPushButton('写入目标型号…')
        self.refresh_cal = QPushButton('重新读取标定状态')
        self.stop = QPushButton('中止本项')
        actions.addWidget(self.go)
        actions.addWidget(self.write_model)
        actions.addWidget(self.refresh_cal)
        actions.addWidget(self.stop)
        actions.addStretch()
        right.addLayout(actions)
        right.addWidget(QLabel('操作与回读记录'))
        self.history = QPlainTextEdit()
        self.history.setReadOnly(True)
        self.history.document().setMaximumBlockCount(350)
        right.addWidget(self.history, 1)
        self.refresh.clicked.connect(self.refresh_ports)
        self.connect_btn.clicked.connect(self.connect_port)
        self.role.currentIndexChanged.connect(self.change_role)
        self.menu.currentTextChanged.connect(self.select)
        self.go.clicked.connect(self.advance)
        self.write_model.clicked.connect(self.confirm_model_write)
        self.refresh_cal.clicked.connect(lambda: self.select('三点标定'))
        self.model.currentTextChanged.connect(self.model_target_changed)
        self.stop.clicked.connect(self.abort)
        self.refresh_ports()
        self.change_role()

    def show_verdicts(self):
        for index in range(self.menu.count()):
            item = self.menu.item(index)
            result = self.verdicts.get(item.text(), '')
            item.setToolTip(result)
            style = self.style()
            symbol = style.SP_FileIcon
            if result == '通过':
                symbol = style.SP_DialogApplyButton
            elif result:
                symbol = style.SP_MessageBoxWarning
            item.setIcon(style.standardIcon(symbol))

    def refresh_ports(self):
        previous = self.port.currentText()
        self.port.clear()
        self.port.addItems(sorted(p.portName() for p in QSerialPortInfo.availablePorts()))
        if self.port.findText(previous) >= 0:
            self.port.setCurrentText(previous)

    def change_role(self):
        self.disconnect_port()
        self.ota_sn.clear()
        self.ota_version.clear()
        self.menu.blockSignals(True)
        self.menu.clear()
        self.menu.addItems((HOST if self.role.currentIndex() else SCALE) + WIRELESS)
        self.menu.blockSignals(False)
        self.menu.setCurrentRow(0)

    def record(self, message):
        now = datetime.now().astimezone()
        self.feedback.setText(message)
        serial = self.identity[1] if self.identity else self.role.currentText()
        self.history.appendPlainText(f'{now:%H:%M:%S}  {serial}  {self.page}  {message}')
        if self.report:
            row = dict(time=now.isoformat(), role=self.role.currentText(), port=self.serial.portName(),
                       device=self.identity, item=self.page, message=message)
            try:
                with self.report.open('a', encoding='utf-8') as out:
                    out.write(json.dumps(row, ensure_ascii=False) + '\n')
            except OSError as exc:
                self.report = None
                self.disconnect_port()
                QMessageBox.warning(self, '工位记录中断', str(exc))

    def connect_port(self):
        if self.serial.isOpen():
            self.disconnect_port()
            return
        reason = self.guard()
        if reason or not self.port.currentText():
            self.record(reason or '请连接工装并刷新串口')
            return
        self.serial.setPortName(self.port.currentText())
        self.serial.setBaudRate(115200)
        if not self.serial.open(QSerialPort.ReadWrite):
            self.record(self.serial.errorString())
            return
        self.active_changed.emit(True)
        self.connect_btn.setText('断开串口')
        self.port.setEnabled(False)
        self.refresh.setEnabled(False)
        self.serial.clear()
        self.request('id', self.identified)

    def identified(self, reply):
        parts = reply.split()
        models = ('SC200-D',) if self.role.currentIndex() else ('SC201-S', 'SC202-S')
        if len(parts) != 3 or parts[0] not in models:
            self.disconnect_port()
            self.record('板型或身份不符：'+reply)
            return
        self.identity = tuple(parts)
        self.ota_sn.setText(parts[1])
        try:
            folder = Path(self.log_dir()) / 'production_station'
            folder.mkdir(parents=True, exist_ok=True)
            self.report = folder / (datetime.now().strftime('%Y%m%d_%H%M%S_') + secrets.token_hex(4) + '.jsonl')
            self.report.touch(exist_ok=False)
        except OSError as exc:
            self.disconnect_port()
            self.record(str(exc))
            return
        self.board.setText('　/　'.join([*parts, self.serial.portName()]))
        self.record('身份核对通过：' + reply)
        self.read_page_on_entry()
        self.render()

    def request(self, operation, callback):
        if self.pending or not self.serial.isOpen():
            return
        # FT1 exposes device_info only as a plain read-only console command.
        token = '' if operation == 'device_info' else secrets.token_hex(4)
        command = f'ft {token} {operation}' if token else operation
        packet = (command + '\n').encode('ascii')
        self.pending = PendingRequest(operation, token, time.monotonic() + 3, callback)
        if self.serial.write(packet) != len(packet):
            self.fail('UART 写入失败')
        self.render()

    def receive(self):
        self.rx.extend(bytes(self.serial.readAll()))
        while b'\n' in self.rx:
            line, _, rest = self.rx.partition(b'\n')
            self.rx = bytearray(rest)
            if not self.pending:
                continue
            pending = self.pending
            prefix = f'FT {pending.token} '.encode() if pending.token else b'DEVICE INFO '
            # A binary dock frame can precede the ASCII reply on the same line.
            start = line.rfind(prefix)
            if start < 0:
                continue
            raw = line[start+len(prefix):].strip()
            self.pending = None
            if pending.token:
                status, _, raw = raw.partition(b' ')
                if status != b'OK':
                    if pending.operation.startswith('cal read ') and self.page == '三点标定' and not self.live:
                        self.cal_saved = '未确认三点记录'
                        self.record('已有标定代次，但未回读到有效三点曲线。可重新读取或重新标定')
                        self.render()
                    elif pending.operation.startswith('lcd '):
                        self.fail('主机未接受屏幕测试，请核对支持 LCD 产测的主机固件和屏幕开机状态')
                    elif pending.operation.startswith('model '):
                        self.fail('固件拒绝型号写入。请重新核对设备信息，并检查标定、升级或设备存储状态')
                    else:
                        self.fail('固件拒绝或读取失败，请核对本板状态')
                    continue
            try:
                pending.callback(raw.decode('ascii'))
            except (ValueError, IndexError) as exc:
                self.fail('无效设备应答：' + str(exc))
            self.render()
        if len(self.rx) > 4096:
            self.rx.clear()

    def cleanup(self):
        operations = {'led':'led 0', 'keys':'keys off', 'tone':'tone off',
                      'cal':'cal cancel', 'lcd':'lcd 0'}
        release = operations.get(self.live)
        if self.pending and self.pending.operation == 'cal start':
            release = 'cal cancel'
        if release and self.serial.isOpen():
            packet = f'ft {secrets.token_hex(4)} {release}\n'.encode()
            self.serial.write(packet)
            self.serial.flush()
        self.live = ''
        self.model_write_from = None
        self.pending = None
        self.cal_point = None
        self.lcd_target = None

    def fail(self, message):
        self.cleanup()
        self.failed = True
        self.verdicts[self.page] = '暂停 · '+message
        self.record('暂停 · '+message)
        self.render()

    def abort(self):
        self.fail('操作员中止')

    def disconnect_port(self):
        self.cleanup()
        if self.serial.isOpen():
            self.serial.close()
            self.active_changed.emit(False)
        self.rx.clear()
        self.report = self.identity = None
        self.device_size = self.cal_saved = ''
        self.verdicts.clear()
        self.adcs = ()
        self.step = 0
        self.finished = self.failed = False
        self.board.setText('等待连接')
        self.connect_btn.setText('连接并核对设备')
        self.port.setEnabled(True)
        self.refresh.setEnabled(True)
        self.render()

    def port_error(self, error):
        if error == QSerialPort.ResourceError and self.serial.isOpen():
            self.record('设备断开，未完成项目须重新执行')
            self.disconnect_port()

    def select(self, page):
        if not page:
            return
        if self.live:
            self.record('离开项目，终止本次操作')
        if not self.identity and self.pending:
            self.disconnect_port()
        self.cleanup()
        self.page = page
        self.step = 0
        self.adcs = ()
        self.failed = self.finished = False
        self.cal_saved = self.device_size = ''
        self.lcd_findings.clear()
        self.clear_lcd_checks()
        self.db.setValue(0)
        self.feedback.clear()
        self.read_page_on_entry()
        self.render()

    def read_page_on_entry(self):
        if not self.identity:
            return
        if self.page == '三点标定':
            self.verdicts.pop(self.page, None)
            self.request('cal status', self.inspect_cal)
        elif self.page == MODEL_PAGE:
            self.check_model(auto_detect=True)

    def render(self):
        page = self.page
        guide = {
            '三点标定':'先核对大小秤型号。启动后依次清空、放置总重 500 g、2000 g，等待本点稳定。最后一点后设备自动保存并校验回读。',
            MODEL_PAGE:'通过本板 UART 自动读取已保存的型号、广播名和 SN。需要配置时，按实物选择目标型号，核对后确认写入。',
            LCD_PAGE:'在主机屏幕目检当前纯色。勾选所见缺陷并记录，无缺陷可直接继续。五色结束后恢复正常显示。',
            'RTC 对时':'确认电脑时钟已同步。将电脑 UTC 写入主机 RTC 并再次读取。24 小时漂移需独立长测。',
            '三轴传感器':'先面朝上平放采样，再转动本板采样。记录两组三轴实测值，容差待工程判定。',
            '蜂鸣器声压':'分贝仪正对主机，距离 10 cm。启动高音后录入读数，判据 70～80 dB。',
            '指示灯':'按白、蓝、橙、紫、红依次目检。不符时中止。每一色最长保持 30 秒。',
            'USB 口充电':'使用未充满的电池，依次拔出、插入、拔出本板 USB，三次读取实际充电状态。',
            'BLE 升级测试':'在设备工作台按实物 SN 选择目标、核对型号与镜像，执行 BLE 升级。重启后同一 SN 的目标版本核验成功才通过，可在升级页导出批次记录。',
            '写 SN':'进入设备工作台，核对实物，在工厂生产页领号并执行生产事务，重新认证后核对 SN。',
        }.get(page,'开始后操作实体按键，工位会读取本板消抖后的事件。')
        action = '开始'
        if page=='三点标定' and self.live=='cal':
            action = ('确认空载','确认 500 g','确认 2000 g','等待保存回读')[min(self.step,3)]
        elif page==MODEL_PAGE: action='核对型号'
        elif page=='RTC 对时': action='对时并回读'
        elif page=='指示灯': action=('目检通过 · '+COLORS[self.step]) if self.live=='led' else '点亮白灯'
        elif page=='三轴传感器': action='采集转动后读数' if self.step else '采集平放读数'
        elif page=='USB 口充电': action=('确认已拔出并读取','确认已插入并读取','确认已拔出并复验')[min(self.step,2)]
        elif page=='蜂鸣器声压' and self.live=='tone': action='记录声压并停音'
        elif page in WIRELESS: action='打开'+('固件升级' if page==WIRELESS[0] else '工厂生产')
        if self.finished or self.failed: action='重新核对' if page == MODEL_PAGE else '重新开始'
        if page == '三点标定' and not self.live:
            action = ('重新读取标定状态' if self.failed or not self.cal_saved else
                      '开始标定' if self.cal_saved == '尚未标定' else '重新标定')
        if page == LCD_PAGE and self.live == 'lcd':
            action = ('记录白屏并结束' if self.step == 4 else '记录本色并显示' + LCD_COLORS[self.step + 1] + '屏')
        self.title.setText(page)
        self.show_verdicts()
        self.guide.setText(guide)
        stages = {
            '三点标定': ('空载', '500 g', '2000 g', '落盘回读'),
            'USB 口充电': ('拔出', '插入', '再拔出'),
            '指示灯': COLORS,
            LCD_PAGE: LCD_COLORS,
            '三轴传感器': ('平放', '转动'),
            'BLE 升级测试': ('身份核对', '传输', '重启重连', '目标验版'),
            '写 SN': ('领号', '写入', '重认证回读'),
        }.get(page, ())
        self.steps_label.setVisible(bool(stages))
        self.steps_label.setText('　→　'.join(
            ('✓ ' if self.finished or i < self.step else '') + label for i, label in enumerate(stages)))
        self.readout.setText({
            '三点标定': (('0 g','500 g','2000 g','回读')[min(self.step,3)] if self.live == 'cal'
                     else self.cal_saved or ('正在读取标定状态' if self.identity else '等待从机连接')),
            LCD_PAGE: (('恢复正常显示' if self.lcd_target == 0 else LCD_COLORS[min(self.step,4)] + '屏')
                       if self.live == 'lcd' else '五色目检 · 80% 背光'),
            'USB 口充电': ('复验完成' if self.finished else ('拔出 USB','插入 USB','再次拔出')[min(self.step,2)]),
            '指示灯': ('目检完成' if self.finished else COLORS[min(self.step,4)]+'灯'),
            '蜂鸣器声压': '70 – 80 dB',
            '三轴传感器': ('平放采样','转动采样','采样完成')[min(self.step,2)],
            'RTC 对时': 'UTC 对时与回读', '写 SN':'生产认证通道', 'BLE 升级测试':'重启后验版',
            MODEL_PAGE:self.device_size or ('正在读取设备配置' if self.identity else '等待从机连接'),
        }.get(page,'实体输入事件'))
        self.cal_rows.setVisible(page == '三点标定')
        if page == '三点标定':
            rows = []
            for i, mass in enumerate((0, 500, 2000)):
                state = '已回读' if self.adcs else ('已采集' if i < self.step else '待采集')
                raw = self.adcs[i] if self.adcs else '—'
                rows.append(f'<tr><td>{mass} g</td><td>{state}</td><td>{raw}</td></tr>')
            self.cal_rows.setText('<table width="100%" cellpadding="4"><tr><td><b>目标载荷</b></td>'
                                  '<td><b>本点状态</b></td><td><b>曲线回读 ADC</b></td></tr>'
                                  + ''.join(rows) + '</table>')
        self.model.setVisible(page == MODEL_PAGE)
        self.model_label.setVisible(page == MODEL_PAGE)
        self.model.setEnabled(not self.pending)
        self.lcd_form.setVisible(page == LCD_PAGE)
        self.lcd_form.setEnabled(self.live == 'lcd' and self.lcd_target is None and not self.pending)
        self.write_model.setVisible(page == MODEL_PAGE and self.model_write_from is not None)
        self.refresh_cal.setVisible(page == '三点标定' and self.cal_saved == '未确认三点记录' and not self.live)
        self.refresh_cal.setEnabled(not self.pending)
        self.db.setVisible(page=='蜂鸣器声压')
        for field in (self.ota_sn, self.ota_version, self.ota_import):
            field.setVisible(page == WIRELESS[0])
        self.go.setText(action)
        self.go.setEnabled(not self.pending and (page in WIRELESS or bool(self.identity))
                           and self.cal_point is None and self.lcd_target is None and self.live!='keys')
        self.stop.setEnabled(bool(self.live))

    def import_ota(self):
        serial = self.ota_sn.text().strip()
        version = self.ota_version.text().strip()
        if not serial or not version:
            self.record('请扫入目标 SN 并填写工单版本')
            return
        path, _ = QFileDialog.getOpenFileName(self, '选择固件升级页导出的验版记录', '', 'CSV (*.csv)')
        if not path:
            return
        try:
            raw = Path(path).read_bytes()
            models = ('SC200-D',) if self.role.currentIndex() else ('SC201-S', 'SC202-S')
            if self.identity:
                if serial != self.identity[1]:
                    raise ValueError('扫码 SN 与已连接设备的 UART 身份不一致')
                models = (self.identity[0],)
            row = verified_ota_row(raw, serial, version, models)
            directory = Path(self.log_dir()) / 'production_station'
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / ('ota_' + secrets.token_hex(8) + '.json')).open('x', encoding='utf-8') as out:
                json.dump(dict(imported=datetime.now().astimezone().isoformat(), row=row,
                               source=Path(path).name, sha256=hashlib.sha256(raw).hexdigest()),
                          out, ensure_ascii=False, indent=2)
        except (ValueError, csv.Error, OSError) as exc:
            self.record('验版记录未归档：' + str(exc))
            return
        self.record(f'已核对并归档工作台验版报告 · {serial} · {version}')

    def done(self, result='通过'):
        self.cleanup()
        self.finished = True
        self.verdicts[self.page] = ('通过' if '通过' in result else result)
        self.record(result)

    def advance(self):
        page = self.page
        if self.pending:
            return
        if (self.failed or self.finished) and page not in ('三点标定', MODEL_PAGE):
            self.select(page)
            return
        if page in WIRELESS:
            self.record('进入 BLE 设备工作台，结果以工作台事务为准')
            self.disconnect_port()
            self.open_workbench('固件升级' if page==WIRELESS[0] else '工厂生产')
            return
        if not self.identity or self.guard():
            self.fail(self.guard() or '请先核对设备身份')
            return
        if page=='三点标定':
            if self.failed or not self.cal_saved and self.live != 'cal':
                self.select(page)
            elif self.live!='cal':
                self.request('cal status',self.begin_cal)
            else:
                self.request('cal point '+str(self.step),self.point_accepted)
        elif page==MODEL_PAGE:
            self.finished = self.failed = False
            self.check_model()
        elif page == LCD_PAGE:
            if self.live == 'lcd':
                self.request('lcd status', self.record_lcd_color)
            else:
                self.live = 'lcd'
                self.show_lcd_color(1)
        elif page=='RTC 对时':
            stamp=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            self.request('rtc set '+stamp,lambda _:self.request('rtc get',self.rtc_read))
        elif page=='三轴传感器':
            self.request('xyz',self.xyz)
        elif page=='USB 口充电':
            self.request('usb',self.usb)
        elif page in KEYS:
            self.live='keys'
            self.request('keys start',lambda _:self.record('等待 '+ '、'.join(k for k,_ in KEYS[page])))
        elif page=='指示灯':
            if self.live=='led':
                if time.monotonic()>=self.until:
                    self.fail('灯色已超时')
                    return
                self.record(COLORS[self.step]+'色目检通过')
                if self.step==4:
                    self.done()
                    self.render()
                    return
                self.step+=1
            self.live='led'
            self.request('led '+str(self.step+1),self.led_on)
        elif page=='蜂鸣器声压':
            if self.live=='tone':
                val=self.db.value()
                if val<=0:
                    self.record('请先录入分贝仪读数')
                elif time.monotonic()>=self.until:
                    self.fail('测试音已超时')
                else:
                    self.request('tone off',lambda _:self.done(f'{val:g} dB · '+('通过' if 70<=val<=80 else '超限')))
            else:
                self.live='tone'
                self.request('tone on',self.tone_on)
        self.render()

    def led_on(self,_):
        self.until=time.monotonic()+29
        self.record('请目检 '+COLORS[self.step]+'灯')

    def tone_on(self,_):
        self.until=time.monotonic()+29
        self.record('高音已启动，等待分贝仪读数')

    def model_target_changed(self):
        if self.page == MODEL_PAGE:
            self.verdicts.pop(MODEL_PAGE, None)
            self.model_write_from = None
            self.failed = self.finished = False
            self.feedback.setText('已选择目标型号，点击“核对型号”')
            self.render()

    def confirm_model_write(self):
        current = self.model_write_from
        if current is None or not self.identity or self.pending:
            return
        target = self.model.currentText()
        session = self.report
        answer = QMessageBox.question(
            self, '确认写入目标型号',
            f'SN　{self.identity[1]}\n当前配置　{current}\n目标型号　{target}\n\n确认与实物一致并写入？',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        if (session == self.report and self.identity and self.page == MODEL_PAGE
                and current == self.model_write_from and target == self.model.currentText()):
            self.check_model(confirmed_from=current)

    def check_model(self, confirmed_from=None, after_write=False, auto_detect=False):
        self.model_write_from = None
        self.verdicts.pop(MODEL_PAGE, None)
        target = None if auto_detect else self.model.currentText()
        if target == '':
            self.record('请按实物选择目标型号')
            self.render()
            return
        expected_sn, version = self.identity[1:]
        self.feedback.setText('正在读取设备身份和型号配置…')

        def read_snapshot(reply):
            # Fence the untagged FT1 device_info read with a fresh tagged id.
            parts = reply.split()
            if len(parts) != 3 or parts[0] not in SCALE_MODELS or parts[1:] != [expected_sn, version]:
                self.fail('本板身份与连接时不一致，请断开后重新连接')
                return
            self.identity = tuple(parts)
            self.board.setText('　/　'.join(parts + [self.serial.portName()]))
            self.request('device_info', lambda raw: self.review_model(raw, target, confirmed_from, after_write))

        self.request('id', read_snapshot)

    def review_model(self, raw, target, confirmed_from, after_write):
        fields = DEVICE_INFO.fullmatch(raw)
        if not fields:
            self.fail('设备信息回包不完整，请重新核对')
            return
        model, name, sn, source, mac, flags_hex, state = fields.groups()
        flags = int(flags_hex, 16)
        if (model, sn) != self.identity[:2]:
            self.fail('设备信息与本次 UART 身份不一致，请重新连接')
            return
        if state not in ('saved', 'pending') or flags & 6 != 6 or source not in ('1', '2'):
            self.fail(f'设备身份记录异常（state={state}，flags={flags_hex}，sn_source={source}），请检查设备存储')
            return
        if name != f'{model} {mac[-6:].upper()}':
            self.fail(f'广播名与本板型号或 MAC 不符，设备回读为「{name}」')
            return
        current = model if flags & 1 else '未配置'
        self.device_size = (f'{SCALE_SIZE[model]}　{model}' if current == model and state == 'saved'
                            else f'型号未确认保存　板端上报 {model}')
        detecting = target is None
        if detecting:
            self.model.blockSignals(True)
            self.model.setCurrentIndex(self.model.findText(model) if current == model else -1)
            self.model.blockSignals(False)
            if current != model:
                self.record(f'本板未配置大小秤型号 · SN {sn} · 广播名 {name} · ' +
                            ('已写正式 SN，型号写保护' if source == '2' else '请按实物选择目标型号'))
                return
            target = model
        sn_type = '正式 SN' if source == '2' else '默认 SN'
        details = f'当前配置 {current} · 目标 {target} · 广播名 {name} · {sn_type} {sn}'
        if detecting and state == 'saved':
            self.record(f'已自动识别{SCALE_SIZE[model]} · 型号 {model} · 广播名 {name} · {sn_type} {sn}')
        elif current == target and state == 'saved':
            self.done(('写入后回读通过' if after_write else '只读核对通过') + ' · ' + details)
        elif after_write:
            self.fail(f'写入后配置回读不符（state={state}）· ' + details)
        elif source == '2':
            self.fail('已写正式 SN，型号写保护 · ' + details +
                      (' · 设备存储待确认' if state == 'pending' else ''))
        elif confirmed_from == current:
            self.record('已确认写入 · ' + details)
            if self.identity and self.page == MODEL_PAGE:
                self.request('model ' + target, lambda _: self.check_model(after_write=True))
        else:
            self.model_write_from = current
            self.record('待确认写入 · ' + details + (' · 设备存储待确认' if state == 'pending' else ''))

    def rtc_read(self,reply):
        observed=datetime.strptime(reply,'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        delta=(datetime.now(timezone.utc)-observed).total_seconds()
        if abs(delta)>2:
            self.fail(f'立即回读偏差 {delta:.2f} 秒')
        else:
            self.done('对时回读通过 · '+reply+' UTC')

    def xyz(self,reply):
        x,y,z=map(int,reply.split())
        measured = ' / '.join(f'{v * 0.00980665:.3f}' for v in (x, y, z))
        self.record(('转动' if self.step else '平放')+f' X/Y/Z = {measured} m/s²（{x}/{y}/{z} mg）')
        self.step+=1
        if self.step==2:
            self.done('两次读数已记录，待工程判定')

    def usb(self,reply):
        present,charging,full,fault=map(int,reply.split())
        self.record(f'USB={present} 充电={charging} 充满={full} 故障={fault}')
        inserted=self.step==1
        if fault or present!=int(inserted) or charging!=int(inserted) or full:
            self.fail('状态与未充满电池的插拔工况不符')
            return
        self.step+=1
        if self.step==3: self.done()

    def inspect_cal(self, reply):
        _, _, running, generation = map(int, reply.split())
        self.cal_generation = generation
        if running:
            self.fail('设备已有标定会话，请在结束后重新读取')
        elif generation == 0:
            self.cal_saved = '尚未标定'
            self.record('未读到已有标定代次，可开始三点标定')
        else:
            self.request(f'cal read {generation}', self.existing_curve)

    def existing_curve(self, reply):
        zero, mid, full = map(int, reply.split())
        self.adcs = (zero, mid, full)
        self.cal_saved = '已完成标定'
        self.done(f'已完成标定 · 三点曲线回读通过 · 代次 {self.cal_generation} · ADC = {zero}/{mid}/{full}')

    def begin_cal(self,reply):
        _,_,running,generation=map(int,reply.split())
        if running:
            self.fail('设备已有标定会话，请等待结束后重试')
            return
        if generation != self.cal_generation:
            self.finished = False
            self.adcs = ()
            self.cal_saved = ''
            self.inspect_cal(reply)
            return
        self.cal_generation=generation
        def started(_):
            self.live = 'cal'
            self.finished = self.failed = False
            self.step = 0
            self.adcs = ()
            self.verdicts.pop(self.page, None)
            self.record('已开始，清空秤盘并确认')
        self.request('cal start', started)

    def point_accepted(self,_):
        self.cal_point=self.step
        self.until=time.monotonic()+65
        self.record('等待稳定采样')

    def cal_status(self,reply):
        point,waiting,running,generation=map(int,reply.split())
        if not running:
            expected=(self.cal_generation+1)&0xffffffff or 1
            if self.cal_point!=2 or generation!=expected:
                self.fail('标定结束但本轮曲线未提交')
            else:
                def verify(raw):
                    zero, mid, full = map(int, raw.split())
                    self.adcs = (zero, mid, full)
                    self.cal_generation = generation
                    self.cal_saved = '已完成标定'
                    self.done(f'曲线落盘回读通过 · 代次 {generation} · 0/500/2000 g ADC = {zero}/{mid}/{full}')
                self.request(f'cal read {generation}', verify)
        elif waiting:
            self.step=point
            self.record('请重放当前砝码后确认' if point==self.cal_point else '本点已采集，请准备下一点')
            self.cal_point=None

    def clear_lcd_checks(self):
        for check in self.lcd_checks:
            check.setChecked(False)
        self.lcd_note.clear()

    def show_lcd_color(self, color):
        self.clear_lcd_checks()
        self.lcd_target = color
        self.until = time.monotonic() + 4
        self.feedback.setText('等待主机恢复正常显示' if color == 0 else '等待主机完成纯色画面刷新')
        self.request(f'lcd {color}', lambda _: self.request('lcd status', self.lcd_displayed))

    def lcd_displayed(self, reply):
        requested, displayed = map(int, reply.split())
        if requested != self.lcd_target:
            self.fail('主机已退出屏幕测试，请确认主机已开机并重新测试')
        elif displayed == self.lcd_target:
            self.lcd_target = None
            if displayed == 0:
                self.live = ''
                self.done('屏幕目检不合格 · ' + '；'.join(self.lcd_findings)
                          if self.lcd_findings else '五色目检通过 · 已恢复正常显示')
            else:
                self.until = time.monotonic() + 85
                self.record('请目检主机' + LCD_COLORS[self.step] + '屏')

    def record_lcd_color(self, reply):
        requested, displayed = map(int, reply.split())
        if requested != self.step + 1 or displayed != requested:
            self.fail('主机当前显示已变化，本色目检未记录')
            return
        defects = [check.text() for check in self.lcd_checks if check.isChecked()]
        note = self.lcd_note.text().strip()
        result = LCD_COLORS[self.step] + '屏 · ' + ('、'.join(defects) or '未见缺陷')
        if note:
            result += ' · ' + note
        if defects:
            self.lcd_findings.append(result)
        self.record(result)
        if self.identity:
            if self.step < 4:
                self.step += 1
                self.show_lcd_color(self.step + 1)
            else:
                self.show_lcd_color(0)

    def key_read(self,reply):
        bits=int(reply)
        events=KEYS[self.page]
        missing=[label for label,mask in events if not bits&mask]
        seen='、'.join(label for label,mask in events if bits&mask)
        if not missing: self.done('实体事件全部通过 · '+seen)
        else: self.feedback.setText('已识别：'+(seen or '无')+'　等待：'+'、'.join(missing))

    def poll(self):
        now=time.monotonic()
        if self.pending:
            if now>self.pending.deadline:
                operation = self.pending.operation
                if not self.identity:
                    self.disconnect_port()
                if operation == 'device_info':
                    self.fail('读取设备信息超时，请检查串口并重新核对')
                elif operation.startswith('model '):
                    self.fail('型号写入应答超时，请重新核对设备已保存的型号')
                else:
                    self.fail('UART 应答超时；请核对工装和产测固件')
            return
        if self.live in ('tone','led','lcd') and now>=self.until:
            self.fail('工位操作超时，已释放输出')
        if self.cal_point is not None and now>=self.until:
            self.fail('稳定采样超时')
        if now<self.poll_at: return
        self.poll_at=now+0.4
        if self.lcd_target is not None:
            self.request('lcd status', self.lcd_displayed)
        elif self.live=='keys':
            self.request('keys read',self.key_read)
        elif self.cal_point is not None:
            self.request('cal status',self.cal_status)
