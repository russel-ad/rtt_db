"""RTC test page using the existing MainWindow RTT/serial transport."""
from datetime import datetime, timezone
from pathlib import Path
import time
import uuid

from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtWidgets import (QCheckBox,QComboBox,QDoubleSpinBox,QFileDialog,QFormLayout,
    QHBoxLayout,QLabel,QLineEdit,QMessageBox,QPlainTextEdit,QPushButton,QSpinBox,
    QVBoxLayout,QWidget)
import pyqtgraph as pg
from .core import Collector, Session, Stamp


class RtcValidationWidget(QWidget):
    def __init__(self, parent=None, *, send, guard):
        super().__init__(parent)
        self.send,self.guard=send,guard
        self.session=None
        self.last_sid=None
        self.mode='idle'
        self.flow_deadline=0
        self.next_poll=0
        self.points=[]
        self.collector=Collector(self.send,self._sample,self._event)
        layout=QVBoxLayout(self)
        hint=QLabel('先在“RTT / 串口调试”配置并连接主机，使用 RTC1 测试固件。'
                    'RTC 页面统一显示 UTC；采集中锁定设时。关机保时测试请先暂停，开机后继续。')
        hint.setWordWrap(True); layout.addWidget(hint)
        form=QFormLayout()
        self.device=QLineEdit(); self.device.setPlaceholderText('设备编号 / SN / 板号')
        self.source=QLineEdit(); self.source.setPlaceholderText('例如：已与实验室标准时钟核对，或 Windows 时间服务来源')
        self.reference_checked=QCheckBox('已核对电脑标准时间，以下填写参考时间误差上限')
        self.reference_bound=QDoubleSpinBox(); self.reference_bound.setRange(.001,60); self.reference_bound.setDecimals(3)
        self.reference_bound.setValue(.1); self.reference_bound.setSuffix(' 秒'); self.reference_bound.setEnabled(False)
        self.reference_checked.toggled.connect(self.reference_bound.setEnabled)
        self.interval=QSpinBox(); self.interval.setRange(10,3600); self.interval.setValue(60); self.interval.setSuffix(' 秒')
        self.limit_enabled=QCheckBox('按产品指标判定（至少采集24小时）')
        self.limit=QDoubleSpinBox(); self.limit.setRange(.01,600); self.limit.setValue(2); self.limit.setSuffix(' 秒/天')
        self.limit.setEnabled(False); self.limit_enabled.toggled.connect(self.limit.setEnabled)
        form.addRow('测试设备',self.device); form.addRow('参考时间来源',self.source)
        row=QHBoxLayout(); row.addWidget(self.reference_checked); row.addWidget(self.reference_bound)
        form.addRow('参考精度',row)
        form.addRow('采集间隔',self.interval)
        row=QHBoxLayout(); row.addWidget(self.limit_enabled); row.addWidget(self.limit)
        form.addRow('合格线',row); layout.addLayout(form)
        actions=QHBoxLayout()
        self.sync_btn=QPushButton('对时一次（电脑 UTC）')
        self.start_btn=QPushButton('开始采集…')
        self.pause_btn=QPushButton('暂停 / 关机测试'); self.pause_btn.setEnabled(False)
        self.stop_btn=QPushButton('停止并保存'); self.stop_btn.setEnabled(False)
        self.unlock_btn=QPushButton('释放上次设时锁'); self.unlock_btn.setEnabled(False)
        for b in (self.sync_btn,self.start_btn,self.pause_btn,self.stop_btn,self.unlock_btn): actions.addWidget(b)
        self.sync_btn.clicked.connect(self.sync_once); self.start_btn.clicked.connect(self.start)
        self.pause_btn.clicked.connect(self.pause_resume); self.stop_btn.clicked.connect(self.stop)
        self.unlock_btn.clicked.connect(self.unlock)
        layout.addLayout(actions)
        self.status=QLabel('尚未采集。未核验参考时间时，结果只表示相对电脑时钟的偏差。')
        self.status.setWordWrap(True); layout.addWidget(self.status)
        self.metrics=QLabel('RTC UTC: --    累计漂移: --    每天误差: --    ppm: --')
        self.metrics.setWordWrap(True); layout.addWidget(self.metrics)
        self.path_label=QLabel('报告目录：--'); self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse); layout.addWidget(self.path_label)
        self.time_axis_unit = QComboBox()
        self.time_axis_unit.addItem('分钟', 60)
        self.time_axis_unit.addItem('小时', 3600)
        axis_label = QLabel('横轴单位')
        axis_label.setBuddy(self.time_axis_unit)
        plot_controls = QHBoxLayout()
        plot_controls.addWidget(axis_label)
        plot_controls.addWidget(self.time_axis_unit)
        plot_controls.addStretch()
        layout.addLayout(plot_controls)
        self.plot=pg.PlotWidget(); self.plot.setLabel('left','累计漂移','s')
        self.plot.getAxis('bottom').enableAutoSIPrefix(False)
        self.plot.showGrid(x=True,y=True,alpha=.2); self.curve=self.plot.plot(pen=pg.mkPen('#63d9c6',width=2))
        self.time_axis_unit.currentIndexChanged.connect(self._update_time_axis)
        self._update_time_axis()
        layout.addWidget(self.plot,1)
        self.log=QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumBlockCount(1000); self.log.setMaximumHeight(150)
        layout.addWidget(self.log)
        self.timer=QTimer(self); self.timer.timeout.connect(self.tick); self.timer.start(100)

    @property
    def active(self): return self.session is not None

    def _update_time_axis(self):
        unit = self.time_axis_unit.currentText()
        self.plot.setLabel('bottom', f'测试时长（{unit}）')
        self._draw_curve()
        self.plot.enableAutoRange(axis='x')

    def _draw_curve(self):
        seconds_per_unit = self.time_axis_unit.currentData()
        self.curve.setData([elapsed_s / seconds_per_unit for elapsed_s, _ in self.points],
                           [drift_s for _, drift_s in self.points])

    def note(self,text): self.log.appendPlainText(datetime.now().strftime('%H:%M:%S')+' '+text)

    def _send(self,command):
        reason=self.guard()
        if reason: raise RuntimeError(reason)
        self.send((command+'\r\n').encode('ascii'))

    def _controls(self):
        active=self.active
        self.sync_btn.setEnabled(not active and self.mode=='idle')
        self.start_btn.setEnabled(not active and self.mode=='idle')
        self.stop_btn.setEnabled(active and self.mode not in ('stopping',))
        self.pause_btn.setEnabled(active and self.mode in ('running','paused'))
        self.pause_btn.setText('继续采集（重新锁定）' if self.mode=='paused' else '暂停 / 关机测试')
        for w in (self.device,self.source,self.reference_checked,self.interval,self.limit_enabled): w.setEnabled(not active)
        self.reference_bound.setEnabled(not active and self.reference_checked.isChecked())
        self.limit.setEnabled(not active and self.limit_enabled.isChecked())
        self.unlock_btn.setEnabled(not active and bool(self.last_sid) and self.mode=='idle')

    def sync_once(self):
        if self.active or self.mode!='idle': return
        try:
            self._send('rtct set '+datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))
            self.mode='setting'; self.flow_deadline=time.perf_counter()+3
            self.status.setText('正在对时，等待设备确认。对时误差将在测试基线中扣除。')
            self._controls()
        except Exception as e: self.status.setText(str(e))

    def start(self):
        if self.active or self.mode!='idle': return
        reason=self.guard()
        if reason: self.status.setText(reason); return
        if not self.device.text().strip(): self.status.setText('请填写设备编号，便于区分不同样机。'); return
        if self.reference_checked.isChecked() and not self.source.text().strip():
            self.status.setText('请记录已核对的参考时间来源。'); return
        base=QFileDialog.getExistingDirectory(self,'选择报告保存位置')
        if not base: return
        directory=Path(base)/('rtc_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6])
        try:
            self.session=Session(directory,device=self.device.text().strip(),reference=self.source.text().strip(),
                reference_bound_s=self.reference_bound.value() if self.reference_checked.isChecked() else None,
                limit_s_day=self.limit.value() if self.limit_enabled.isChecked() else None)
            self.last_sid=self.session.sid; self.points=[]; self.curve.setData([],[])
            self.path_label.setText('报告目录：'+str(directory))
            self._arm()
        except Exception as e:
            self.status.setText(str(e))
            if self.session: self._finish('start_failed')

    def _arm(self):
        self.collector.pending=None
        self.mode='arming'; self.flow_deadline=time.perf_counter()+3
        self._send(f'rtct start {self.session.sid}')
        self.status.setText('等待设备锁定设时；确认后自动建立基线。')
        self._controls()

    def pause_resume(self):
        if not self.session: return
        if self.mode=='running':
            self.mode='paused'; self.collector.pending=None
            self.session.event('paused_for_power_retention_test')
            self.status.setText('采集已暂停，基线保留。可以关机并保持RTC电源；重新连接后点继续，期间禁止重新对时。')
        elif self.mode=='paused':
            try: self._arm()
            except Exception as e:
                self.mode='paused'; self.status.setText(str(e))
        self._controls()

    def stop(self):
        if not self.session: return
        self.collector.pending=None
        self.mode='stopping'; self.flow_deadline=time.perf_counter()+3
        try:
            self._send(f'rtct stop {self.session.sid}')
            self.status.setText('正在停止，等待设备释放设时锁。')
        except Exception as e:
            self.session.flag('device_unlock_unconfirmed',message=str(e))
            self._finish('device_unlock_unconfirmed')
        self._controls()

    def unlock(self):
        if not self.last_sid or self.active or self.mode!='idle': return
        try:
            self.mode='unlocking'; self.flow_deadline=time.perf_counter()+3
            self._send(f'rtct stop {self.last_sid}'); self._controls()
        except Exception as e:
            self.mode='idle'; self.status.setText(str(e)); self._controls()

    def _finish(self,reason):
        if self.session:
            self.session.event('stop_reason',reason=reason)
            self.session.close(); report=self.session.report()
            self.status.setText('已保存：'+report['status']+'；'+reason)
            self.session=None
        self.mode='idle'; self._controls()

    def _event(self,kind,details):
        if self.session: self.session.event(kind,**details)
        if kind=='reply':
            line=details['line']; self.note(line)
            if self.mode=='arming' and self.session and line==f'RTCV1 ACK start={self.session.sid}':
                self.mode='running'; self.next_poll=0; self._controls()
            elif self.mode=='stopping' and self.session and line==f'RTCV1 ACK stop={self.session.sid}':
                self.last_sid=None; self._finish('device_unlocked')
            elif self.mode=='unlocking' and line==f'RTCV1 ACK stop={self.last_sid}':
                self.mode='idle'; self.last_sid=None; self.status.setText('设备设时锁已释放。'); self._controls()
            elif self.mode=='setting' and line.startswith('RTCV1 SET rc='):
                self.mode='idle'; self.status.setText('对时成功。可以开始采集。' if line=='RTCV1 SET rc=0' else '对时失败：'+line); self._controls()
            elif ' ERR ' in line:
                if self.session:
                    self.session.flag('device_command_error',reply=line)
                    if self.mode in ('arming','stopping'): self._finish('device_command_error')
                else: self.mode='idle'; self._controls()
                self.status.setText(line)
        elif kind not in ('unmatched_sample',):
            self.note(kind+' '+str(details))
            if self.session and kind in ('protocol_error','input_overflow'):
                self.session.flag(kind,**details)

    def _sample(self,sample,sent,received):
        if not self.session or self.mode!='running': return
        try:
            row=self.session.add(sample,sent,received)
            if row['drift_s']!='':
                self.points.append((row['elapsed_s'], row['drift_s']))
                self.points = self.points[-2000:]
                self._draw_curve()
                rate=f"{row['seconds_per_day']:+.3f} ±{row['rate_bound_per_day']:.3f}s/day" if row['seconds_per_day']!='' else '--'
                ppm=f"{row['ppm']:+.2f}" if row['ppm']!='' else '--'
                utc=datetime.fromtimestamp(sample['epoch'],timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                self.metrics.setText(f"RTC UTC: {utc}    漂移: {row['drift_s']:+.3f} ±{row['drift_bound_s']:.3f}s\n每天误差: {rate}    ppm: {ppm}    运行: {row['elapsed_s']/3600:.2f}h    重启: {self.session.boot_changes}")
                # Send only a display annotation; this command never sets RTC.
                if not row['flags'] and abs(row['drift_s'])<=604800:
                    self._send(f"rtct ref {self.session.sid} {round(row['drift_s']*1000)} {max(0,int(row['elapsed_s']))}")
            self.status.setText(self.session.report()['status']+'；已持续保存原始数据和报告。')
            self.note(f"#{row['index']} rc={sample['rc']} XSTP={(sample['c2']>>4)&1} ADJ={sample['adj']:02X} "+row['flags'])
        except Exception as e:
            self.note('记录失败：'+str(e))
            if self.session: self.session.flag('recording_error',message=str(e))
            self.stop()

    def feed(self,text): self.collector.feed(text)

    def tick(self):
        try:
            self.collector.check_timeout()
            now=time.perf_counter()
            if self.mode in ('arming','stopping','unlocking','setting') and now>self.flow_deadline:
                reason='设备确认超时；检查连接与RTC1测试固件。设时锁状态未确认。'
                if self.session:
                    self.session.flag('command_confirmation_timeout')
                    self._finish('command_confirmation_timeout')
                else: self.mode='idle'; self._controls()
                self.status.setText(reason)
            if self.session and self.mode=='running' and now>=self.next_poll:
                reason=self.guard()
                if reason:
                    self.session.event('connection_unavailable',reason=reason)
                    self.mode='paused'; self.status.setText('连接不可用，已暂停：'+reason+'。连接后可继续原基线。'); self._controls()
                elif not self.collector.pending:
                    self.collector.poll(); self.next_poll=now+self.interval.value()
        except Exception as e:
            self.note(str(e))
            if self.session:
                self.session.flag('transport_failure',message=str(e)); self.mode='paused'; self._controls()

    def request_shutdown(self):
        if self.active or self.mode!='idle':
            QMessageBox.information(self,'RTC 验证','请先停止 RTC 采集并保存报告，再关闭程序。')
            return False
        return True
