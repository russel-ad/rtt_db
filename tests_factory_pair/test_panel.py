import asyncio
import copy
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PyQt5.QtWidgets import QApplication,QMessageBox
from factory_pair_panel import FactoryPairWidget,display_mac
from test_controller import HOST,PEERS
APP=QApplication.instance() or QApplication([])


class FakeWorker:
    def __init__(self,bridge):
        self.actions=[]; self.stopped=False
        async def no_io(*args): pass
        self.controller=SimpleNamespace(scan=no_io,connect=no_io,disconnect=no_io,query=no_io,save=no_io,verify=no_io)
    def submit(self,action,coroutine): self.actions.append(action); coroutine.close(); return True
    def stop(self): self.stopped=True


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.page=FactoryPairWidget(worker_factory=FakeWorker)
        self.addCleanup(self.page.deleteLater)
    def inventory(self,saved=False,docked=True):
        live=copy.deepcopy(PEERS)
        if not docked:
            for p in live: p['docked']=False
        value={'host':dict(HOST),'live':live,'saved':copy.deepcopy(PEERS) if saved else [],'result':{'record':1 if saved else 0}}
        self.page.show_inventory(value); return value
    def test_default_page_is_readonly_and_does_no_io(self):
        self.assertFalse(self.page.save_btn.isEnabled()); self.assertFalse(self.page.verify_btn.isEnabled())
        self.assertFalse(self.page.worker.actions)
    def test_save_is_explicit_and_uses_rotation_selection_order(self):
        self.inventory(); self.assertTrue(self.page.save_btn.isEnabled())
        self.page.bottom.setCurrentIndex(0); self.assertFalse(self.page.save_btn.isEnabled())
        self.page.top.setCurrentIndex(1); self.assertTrue(self.page.save_btn.isEnabled())
        with patch.object(QMessageBox,'question',return_value=QMessageBox.No): self.page.save()
        self.assertFalse(self.page.worker.actions)
        with patch.object(QMessageBox,'question',return_value=QMessageBox.Yes): self.page.save()
        self.assertEqual(self.page.worker.actions,['save']); self.assertTrue(self.page.busy)
        self.assertFalse(self.page.rework_btn.isEnabled())
    def test_verify_requires_baseline_cold_confirmation_and_no_dock(self):
        self.inventory(True); self.page.cold.setChecked(True); self.assertFalse(self.page.verify_btn.isEnabled())
        self.inventory(True,False); self.assertTrue(self.page.verify_btn.isEnabled())
        self.page.verify(); self.assertEqual(self.page.worker.actions,['verify'])
    def test_guard_blocks_radio_and_resource_conflicts(self):
        self.page.guard=lambda:'DFU is connected'
        self.page.address.setText('00:00:70:08:DF:CB'); self.page.connect_host()
        self.assertFalse(self.page.worker.actions); self.assertIn('DFU',self.page.status.text())
    def test_uncertain_clears_write_permission_and_keeps_close_pending(self):
        self.inventory(); self.page.busy=True
        self.assertFalse(self.page.request_shutdown()); self.assertFalse(self.page.worker.actions)
        self.page.on_event('completed',{'action':'save','status':'uncertain','error':'result missing'})
        self.assertIsNone(self.page.inventory); self.assertEqual(self.page.worker.actions,['shutdown'])
        self.page.on_event('completed',{'action':'shutdown','status':'ok'})
        self.assertTrue(self.page.worker.stopped); self.assertTrue(self.page.request_shutdown())
    def test_wire_mac_is_reversed_only_for_display(self):
        self.assertEqual(display_mac('cbdf08700000'),'00:00:70:08:DF:CB')
