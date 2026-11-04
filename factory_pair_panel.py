"""Production pairing workspace; BLE writes require explicit operator action."""
from __future__ import annotations
import asyncio
import json
from pathlib import Path
import threading
from datetime import datetime

from PyQt5.QtCore import QObject,Qt,QUrl,pyqtSignal
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QLineEdit,
    QComboBox,QTreeWidget,QTreeWidgetItem,QHeaderView,QCheckBox,QGroupBox,QGridLayout,
    QPlainTextEdit,QMessageBox,QFileDialog)

from factory_pair.ble import PairUncertain
from factory_pair.controller import PairController,identity


class PairBridge(QObject):
    event=pyqtSignal(str,object)


class PairWorker:
    def __init__(self,bridge):
        self.bridge=bridge; self.loop=asyncio.new_event_loop()
        self.controller=PairController(bridge.event.emit)
        self.future=None
        self.thread=threading.Thread(target=self._run,name='Factory-pair-BLE',daemon=True)
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop); self.loop.run_forever()
        pending=asyncio.all_tasks(self.loop)
        for task in pending: task.cancel()
        if pending: self.loop.run_until_complete(asyncio.gather(*pending,return_exceptions=True))
        self.loop.close()

    def submit(self,action,coroutine):
        if self.future and not self.future.done(): coroutine.close(); return False
        async def execute():
            try: return {'action':action,'status':'ok','result':await coroutine}
            except PairUncertain as exc: return {'action':action,'status':'uncertain','error':str(exc)}
            except Exception as exc: return {'action':action,'status':'error','error':str(exc)}
        self.future=asyncio.run_coroutine_threadsafe(execute(),self.loop)
        self.future.add_done_callback(lambda f:self.bridge.event.emit('completed',f.result()))
        return True

    def stop(self):
        if self.thread.is_alive():
            self.loop.call_soon_threadsafe(self.loop.stop); self.thread.join(timeout=1)


def display_mac(wire):
    return ':'.join(reversed([wire[i:i+2].upper() for i in range(0,len(wire),2)]))


class FactoryPairWidget(QWidget):
    closed=pyqtSignal()
    active_changed=pyqtSignal(bool)

    def __init__(self,parent=None,operation_guard=None,worker_factory=PairWorker):
        super().__init__(parent)
        self.guard=operation_guard; self.busy=False; self.connected=False
        self.is_closed=False; self.closing=False; self.inventory=None; self.report=None
        self.bridge=PairBridge(); self.bridge.event.connect(self.on_event)
        self.worker=worker_factory(self.bridge)
        self._build(); self.refresh()

    def _build(self):
        outer=QVBoxLayout(self)
        title=QLabel('出厂配对 · 主机通过 BLE 固化当前套装')
        title.setStyleSheet('font-size: 19px; font-weight: bold;'); outer.addWidget(title)
        text=QLabel('① 磁吸配对并等秤上线 → ② 连接主机并查询 → ③ 核对 SN / MAC、选择角色并保存 → '
                    '④ 主从机完整断电重开，分开磁吸，纯 BLE 复验。\n'
                    '保存的是现有设备身份，不修改 SN；仅“保存并回读”成功尚未完成产线验收。')
        text.setWordWrap(True); outer.addWidget(text)
        row=QHBoxLayout(); self.scan_btn=QPushButton('扫描主机'); row.addWidget(self.scan_btn)
        self.devices=QComboBox(); self.devices.setMinimumWidth(270); row.addWidget(self.devices,1)
        self.address=QLineEdit(); self.address.setPlaceholderText('主机 BLE 地址 AA:BB:CC:DD:EE:FF'); row.addWidget(self.address,1)
        self.address_type=QComboBox(); self.address_type.addItems(['public','random']); row.addWidget(self.address_type)
        self.connect_btn=QPushButton('连接并查询'); self.disconnect_btn=QPushButton('断开')
        row.addWidget(self.connect_btn); row.addWidget(self.disconnect_btn); outer.addLayout(row)
        self.host_label=QLabel('主机：未连接'); self.host_label.setTextInteractionFlags(Qt.TextSelectableByMouse); outer.addWidget(self.host_label)
        self.tree=QTreeWidget(); self.tree.setHeaderLabels(['来源 / 角色','SN','BLE MAC','型号','磁吸状态'])
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tree.header().setStretchLastSection(True); outer.addWidget(self.tree,2)
        box=QGroupBox('套装选择'); grid=QGridLayout(box)
        grid.addWidget(QLabel('单秤 / Top 上秤'),0,0); self.top=QComboBox(); grid.addWidget(self.top,0,1)
        grid.addWidget(QLabel('Bottom 下秤（双秤）'),1,0); self.bottom=QComboBox(); grid.addWidget(self.bottom,1,1)
        self.query_btn=QPushButton('刷新当前秤 / 出厂记录'); grid.addWidget(self.query_btn,0,2)
        self.save_btn=QPushButton('保存出厂配对并回读'); grid.addWidget(self.save_btn,1,2)
        hint=QLabel('双秤必须两台均为 SC202 大秤；请按实际套装选择 Top / Bottom，保存时至少一台保持磁吸。')
        hint.setWordWrap(True); grid.addWidget(hint,2,0,1,3); outer.addWidget(box)
        row=QHBoxLayout(); self.cold=QCheckBox('已将主从机完整断电重开，并移开磁吸')
        row.addWidget(self.cold,1); self.verify_btn=QPushButton('纯 BLE 复验'); row.addWidget(self.verify_btn); outer.addLayout(row)
        row=QHBoxLayout(); self.rework=QCheckBox('返工：允许替换已有出厂套装')
        row.addWidget(self.rework,1); self.rework_btn=QPushButton('返工替换并回读'); row.addWidget(self.rework_btn); outer.addLayout(row)
        self.status=QLabel('准备就绪：请打开主机设置中的 BLE 开关，关闭手机 App 的连接后扫描。')
        self.status.setWordWrap(True); self.status.setStyleSheet('font-size: 14px; padding: 6px;'); outer.addWidget(self.status)
        row=QHBoxLayout(); self.report_label=QLabel('尚无操作报告'); self.report_label.setWordWrap(True)
        row.addWidget(self.report_label,1); self.export_btn=QPushButton('导出报告'); self.guide_btn=QPushButton('操作说明')
        row.addWidget(self.export_btn); row.addWidget(self.guide_btn); outer.addLayout(row)
        self.log=QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumBlockCount(1000); outer.addWidget(self.log,1)
        self.scan_btn.clicked.connect(lambda:self.submit('scan',self.worker.controller.scan(),'正在扫描 SC200-D 主机…'))
        self.devices.currentIndexChanged.connect(self.select_device)
        self.connect_btn.clicked.connect(self.connect_host)
        self.disconnect_btn.clicked.connect(lambda:self.submit('disconnect',self.worker.controller.disconnect(),'正在断开…'))
        self.query_btn.clicked.connect(lambda:self.submit('query',self.worker.controller.query(),'正在查询身份及出厂记录…'))
        self.save_btn.clicked.connect(lambda:self.save(False)); self.rework_btn.clicked.connect(lambda:self.save(True))
        self.verify_btn.clicked.connect(self.verify); self.cold.toggled.connect(self.refresh); self.rework.toggled.connect(self.refresh)
        self.top.currentIndexChanged.connect(self.refresh); self.bottom.currentIndexChanged.connect(self.refresh)
        self.export_btn.clicked.connect(self.export_report); self.guide_btn.clicked.connect(self.open_guide)

    def note(self,text,error=False):
        self.status.setText(text); self.status.setStyleSheet('font-size:14px; padding:6px; color:'+('#b00020' if error else '#12605a'))
        self.log.appendPlainText(datetime.now().strftime('%H:%M:%S')+'  '+text)

    def refresh(self,*_):
        idle=not self.busy and not self.closing
        ready=bool(idle and self.connected and self.inventory and self.inventory['result'].get('record') in (0,1))
        live=self.inventory['live'] if self.inventory else []
        selected=self.selected_peers()
        can_save=bool(ready and len(live) in (1,2) and len(selected)==len(live) and
            len({p['ble'] for p in selected})==len(live) and any(p.get('docked') for p in live) and
            (len(live)==1 or all(p['model']==2 for p in live)))
        for control in (self.scan_btn,self.devices,self.address,self.address_type,self.connect_btn): control.setEnabled(idle and not self.connected)
        self.disconnect_btn.setEnabled(idle and self.connected); self.query_btn.setEnabled(idle and self.connected)
        self.top.setEnabled(ready and bool(live)); self.bottom.setEnabled(ready and len(live)==2)
        self.save_btn.setEnabled(can_save); self.rework.setEnabled(idle)
        self.rework_btn.setEnabled(can_save and self.rework.isChecked())
        self.verify_btn.setEnabled(bool(ready and self.inventory['result'].get('record')==1 and self.cold.isChecked() and not any(p.get('docked') for p in live)))
        self.cold.setEnabled(idle); self.export_btn.setEnabled(bool(self.report)); self.active_changed.emit(self.busy or self.connected)

    def selected_peers(self):
        first=self.top.currentData() if hasattr(self,'top') else None
        if not first: return []
        rows=[first]
        if self.inventory and len(self.inventory['live'])==2 and self.bottom.currentData(): rows.append(self.bottom.currentData())
        return rows

    def submit(self,action,coroutine,status):
        if self.busy or self.is_closed: coroutine.close(); return False
        if action not in ('disconnect','shutdown') and self.guard:
            reason=self.guard()
            if reason: coroutine.close(); self.note(reason,True); return False
        if not self.worker.submit(action,coroutine): return False
        self.busy=True; self.note(status); self.refresh(); return True

    def select_device(self,index):
        address=self.devices.itemData(index)
        if address: self.address.setText(address)

    def connect_host(self):
        address=self.address.text().strip()
        if not address: self.note('请先扫描选择一台主机，或输入主机 BLE 地址',True); return
        self.submit('connect',self.worker.controller.connect(address,self.address_type.currentText()),'正在连接主机、完成握手并查询…')

    def save(self,rework=False):
        peers=self.selected_peers()
        if not self.inventory or not peers: return
        host=self.inventory['host']; title='返工替换出厂套装' if rework else '保存出厂配对'
        lines=[f'主机 {host["sn"]} / {display_mac(host["ble"])}']
        for i,p in enumerate(peers):
            role=('Top 上秤' if i==0 else 'Bottom 下秤') if len(peers)==2 else '单秤'
            lines.append(f'{role}：{p["sn"]} / {display_mac(p["ble"])}')
        lines.append('确认上述设备属于同一出厂套装。' + ('本操作将替换两端已有的出厂记录。' if rework else '已有不同套装时会拒绝覆盖。'))
        if QMessageBox.question(self,title,'\n'.join(lines),QMessageBox.Yes|QMessageBox.No,QMessageBox.No)!=QMessageBox.Yes: return
        self.cold.setChecked(False)
        self.submit('rework' if rework else 'save',self.worker.controller.save(peers,rework),'正在写入并回读主从出厂记录，请保持供电及连接…')

    def verify(self):
        self.submit('verify',self.worker.controller.verify(self.cold.isChecked()),'正在进行断电后纯 BLE 复验…')

    def show_inventory(self,value):
        self.inventory=value; self.connected=True; self.tree.clear()
        host=value['host']; self.host_label.setText(f'主机：{host["model"]}   SN {host["sn"]}   MAC {display_mac(host["ble"])}   FW {host.get("fw", "")}')
        for kind,name in (('live','当前在线'),('saved','出厂记录')):
            for i,p in enumerate(value[kind]):
                role=('Top' if i==0 else 'Bottom') if len(value[kind])==2 else '单秤'
                self.tree.addTopLevelItem(QTreeWidgetItem([name+' / '+role if kind=='saved' else name,
                    p['sn'],display_mac(p['ble']),'SC202-S' if p['model']==2 else 'SC201-S',
                    ('吸合' if p.get('docked') else '分开') if kind=='live' else '—']))
        previous=[p['ble'] for p in self.selected_peers()]
        self.top.blockSignals(True); self.bottom.blockSignals(True)
        self.top.clear(); self.bottom.clear()
        for p in value['live']:
            label=f'{p["sn"]}  /  {display_mac(p["ble"])}'
            self.top.addItem(label,p); self.bottom.addItem(label,p)
        preferred=[p['ble'] for p in value['saved']] or previous
        for i,control in enumerate((self.top,self.bottom)):
            index=next((j for j,p in enumerate(value['live']) if i<len(preferred) and p['ble']==preferred[i]),min(i,max(0,len(value['live'])-1)))
            control.setCurrentIndex(index)
        self.top.blockSignals(False); self.bottom.blockSignals(False); self.refresh()

    def on_event(self,kind,value):
        if kind=='host':
            self.connected=True; self.note('主机身份已确认：'+value['sn'])
        elif kind=='inventory': self.show_inventory(value)
        elif kind=='disconnected':
            self.connected=False; self.inventory=None; self.host_label.setText('主机：未连接'); self.tree.clear()
        elif kind=='link_lost':
            self.connected=False; self.inventory=None; self.note(str(value),True)
        elif kind=='report':
            self.report=value; self.report_label.setText(value.get('report_path','报告未自动保存，请导出'))
        elif kind=='log': self.log.appendPlainText(str(value))
        elif kind=='completed':
            self.busy=False; action=value['action']; status=value['status']
            if action=='shutdown':
                self.is_closed=True; self.worker.stop(); self.closed.emit(); return
            if status!='ok':
                if status=='uncertain' or action in ('query','connect'): self.inventory=None
                self.note(value.get('error','操作失败'),True)
            elif action=='scan':
                self.devices.clear()
                for row in value['result']: self.devices.addItem(f'{row["name"]}  {row["address"]}  {row["rssi"]} dBm',row['address'])
                self.note(f'找到 {len(value["result"])} 台主机；请选择正确设备后连接')
            elif action in ('save','rework'):
                self.note('主从出厂记录保存并回读成功。下一步：两端完整断电重开，分开磁吸并重新连接主机，执行纯 BLE 复验。')
            elif action=='verify': self.note('产线复验通过：断电后主从出厂记录一致，纯 BLE 连接正常。请保留合格报告。')
            elif action in ('query','connect'):
                self.note('查询完成：'+('尚无出厂记录，可核对套装后保存' if self.inventory and self.inventory['result'].get('record')==0 else '已有出厂记录，请核对设备身份与角色'))
            else: self.note('操作完成')
            if self.closing:
                self.submit('shutdown',self.worker.controller.disconnect(),'正在关闭出厂配对连接…')
        self.refresh()

    def export_report(self):
        if not self.report: return
        path,_=QFileDialog.getSaveFileName(self,'导出出厂配对报告','factory_pair_report.json','JSON (*.json)')
        if path:
            try: Path(path).write_text(json.dumps(self.report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8'); self.note('报告已导出：'+path)
            except OSError as exc: self.note('导出失败：'+str(exc),True)

    def open_guide(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(__file__).resolve().parent/'docs/BLE_FACTORY_PAIR.md')))

    def request_shutdown(self):
        if self.is_closed: return True
        self.closing=True
        if self.busy: self.note('正在执行操作，完成后将安全关闭，请稍候'); return False
        self.submit('shutdown',self.worker.controller.disconnect(),'正在关闭出厂配对连接…')
        return False
