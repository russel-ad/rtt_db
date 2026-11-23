import copy
import csv
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from rtc_validation.core import Collector,Session,Stamp,parse_sample

EPOCH=1795392000
def sample(**changes):
    s=dict(id=10,boot=1,tick=1000,hz=1000,epoch=EPOCH,rc=0,c1=0,c2=32,adj=0,
           init2=32,inita=0,initrc=0,reads=1,errors=0,writes=1,lock=1,sid=7,fw='RTC1')
    s.update(changes); return s

def line(s=None):
    return 'RTCV1 '+' '.join(f'{k}={v}' for k,v in (s or sample()).items())+'\r\n'

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        environment=patch.dict(os.environ,{'LOCALAPPDATA':self.tmp.name,'RTT_DATA_DIR':self.tmp.name})
        environment.start(); self.addCleanup(environment.stop)
        self.path=Path(self.tmp.name)/'session'
    def session(self,**kw):
        defaults=dict(device='board1',reference='lab reference',reference_bound_s=.1,limit_s_day=2,session_id=7)
        defaults.update(kw)
        s=Session(self.path,**defaults); self.addCleanup(s.close); return s
    def add(self,s,elapsed=0,drift=0,**kw):
        kw.setdefault('epoch',EPOCH+elapsed+drift)
        return s.add(sample(**kw),Stamp(EPOCH+elapsed,100+elapsed),Stamp(EPOCH+elapsed+.1,100+elapsed+.1))
    def test_three_day_drift_subtracts_initial_offset(self):
        s=self.session(); self.add(s)
        r=self.add(s,259200,3,tick=259201000)
        self.assertAlmostEqual(r['drift_s'],3)
        self.assertAlmostEqual(r['seconds_per_day'],1)
        self.assertAlmostEqual(r['ppm'],11.574074,places=5)
        self.assertGreater(r['drift_bound_s'],1)
        self.assertEqual(s.report()['status'],'PASS_RELATIVE_TO_DECLARED_REFERENCE')
        s.close()
        with (self.path/'samples.csv').open(encoding='utf-8-sig',newline='') as f:
            self.assertEqual(len(list(csv.DictReader(f))),2)
        self.assertTrue(json.loads((self.path/'report.json').read_text())['closed'])
    def test_fail_and_inconclusive_use_uncertainty(self):
        s=self.session(); self.add(s); self.add(s,86400,10)
        self.assertEqual(s.report()['status'],'FAIL_RELATIVE_TO_DECLARED_REFERENCE')
    def test_uncertainty_does_not_false_pass(self):
        s=self.session(); self.add(s); self.add(s,86400,2)
        self.assertEqual(s.report()['status'],'INCONCLUSIVE')
    def test_short_test_no_acceptance(self):
        s=self.session(); self.add(s); self.add(s,3600,0)
        self.assertEqual(s.report()['status'],'COLLECTING')
    def test_unverified_reference_stays_relative(self):
        s=self.session(reference_bound_s=None); self.add(s); self.add(s,86400,1)
        self.assertEqual(s.report()['status'],'MEASURED_RELATIVE_TO_PC')
    def test_read_error_cannot_establish_baseline(self):
        s=self.session(); self.add(s,rc=-1,epoch=0)
        self.assertIsNone(s.baseline)
    def test_stop_and_unlock_evidence_preserved(self):
        s=self.session(); self.add(s); self.add(s,60,c2=48)
        self.assertIn('rtc_stop_recorded',s.review_flags)
    def test_trim_write_and_time_jump(self):
        s=self.session(); self.add(s); self.add(s,60,20,adj=1,writes=2)
        self.assertTrue({'rtc_adjust_changed','time_write_during_test','rtc_time_jump'} <= s.review_flags)
    def test_pc_clock_step_cannot_pass(self):
        s=self.session(); self.add(s)
        s.add(sample(epoch=EPOCH+86400),Stamp(EPOCH+86401,86500),Stamp(EPOCH+86401.1,86500.1))
        self.assertIn('pc_clock_changed_since_baseline',s.review_flags)
    def test_reboot_retention_same_baseline(self):
        s=self.session(); self.add(s); self.add(s,86400,1,boot=2,tick=100,writes=0)
        self.assertEqual(s.boot_changes,1)
        self.assertEqual(s.baseline['boot'],1)
        self.assertEqual(s.report()['status'],'INCONCLUSIVE')
    def test_invalid_rtc_at_reboot_is_not_accepted(self):
        s=self.session(); self.add(s); self.add(s,86400,0,boot=2,writes=0,init2=48)
        self.assertIn('invalid_rtc_at_reboot',s.review_flags)
    def test_unlocked_device_is_not_baseline(self):
        s=self.session(); self.add(s,lock=0)
        self.assertIsNone(s.baseline)
    def test_duplicate_unknown_invalid_protocol(self):
        self.assertIsNone(parse_sample('weight=2\r\n'))
        self.assertEqual(parse_sample('[timestamp] '+line())['epoch'],EPOCH)
        for bad in (line()+' id=1',line().replace('hz=1000','hz=0'),line().replace('fw=RTC1','fw=other'),line().replace('epoch='+str(EPOCH),'epoch=0')):
            with self.subTest(bad=bad),self.assertRaises(ValueError): parse_sample(bad)
    def test_session_directory_never_overwritten(self):
        self.session()
        with self.assertRaises(FileExistsError): self.session()
    def test_split_delayed_duplicate_replies(self):
        sent=[]; received=[]; events=[]
        c=Collector(sent.append,lambda *x:received.append(x),lambda *x:events.append(x),lambda:Stamp(EPOCH,100))
        c.sequence=9; c.poll(); self.assertFalse(c.poll())
        c.feed(line(sample(id=9)))
        reply=line(); c.feed(reply[:30]); c.feed(reply[30:]); c.feed(reply)
        self.assertEqual(len(received),1)
        self.assertEqual([x[0] for x in events].count('unmatched_sample'),2)
    def test_request_timeout_and_no_overlapping_poll(self):
        now=[Stamp(EPOCH,100)]; events=[]
        c=Collector(lambda b:None,lambda *x:None,lambda *x:events.append(x),lambda:now[0])
        c.poll(); self.assertFalse(c.poll()); now[0]=Stamp(EPOCH+4,104); c.check_timeout()
        self.assertIsNone(c.pending); self.assertEqual(events[0][0],'request_timeout')


class PanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        from rtc_validation.panel import RtcValidationWidget
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        environment=patch.dict(os.environ,{'LOCALAPPDATA':self.tmp.name,'RTT_DATA_DIR':self.tmp.name})
        environment.start(); self.addCleanup(environment.stop)
        self.sent=[]
        self.panel=RtcValidationWidget(send=self.sent.append,guard=lambda:None)
        self.panel.timer.stop(); self.addCleanup(self.panel.deleteLater)
        self.panel.device.setText('offline-board')
    def test_acknowledged_start_pause_reboot_resume_stop(self):
        p=self.panel
        with patch('rtc_validation.panel.QFileDialog.getExistingDirectory',return_value=self.tmp.name): p.start()
        s=p.session; self.addCleanup(s.close)
        self.assertEqual(p.mode,'arming'); self.assertIsNone(s.baseline)
        p.feed(f'RTCV1 ACK start={s.sid}\r\n'); p.tick()
        self.assertEqual(p.mode,'running')
        token=p.collector.pending[0]
        p.feed(line(sample(id=token,sid=s.sid)))
        self.assertEqual(s.count,1)
        p.pause_resume(); self.assertEqual(p.mode,'paused')
        p.pause_resume(); p.feed(f'RTCV1 ACK start={s.sid}\r\n')
        p.stop(); self.assertEqual(p.mode,'stopping')
        p.feed(f'RTCV1 ACK stop={s.sid}\r\n')
        self.assertFalse(p.active); self.assertTrue(s.closed)
        self.assertTrue((s.directory/'report.json').exists())

    def test_time_axis_can_switch_units_during_collection(self):
        p = self.panel
        with patch('rtc_validation.panel.QFileDialog.getExistingDirectory', return_value=self.tmp.name):
            p.start()
        session = p.session
        self.addCleanup(session.close)
        p.feed(f'RTCV1 ACK start={session.sid}\r\n')
        axis = p.plot.getAxis('bottom')

        def add_sample(elapsed_s, drift_s):
            stamp = Stamp(EPOCH + elapsed_s, 100 + elapsed_s)
            p._sample(sample(sid=session.sid, epoch=EPOCH + elapsed_s + drift_s), stamp, stamp)

        add_sample(0, 0)
        add_sample(60, -1)
        x, y = p.curve.getData()
        self.assertEqual(axis.labelText, '测试时长（分钟）')
        self.assertEqual(axis.autoSIPrefixScale, 1.0)
        self.assertEqual((x.tolist(), y.tolist()), ([0, 1], [0, -1]))

        sent = self.sent.copy()
        p.time_axis_unit.setCurrentIndex(1)
        self.assertEqual(self.sent, sent)
        self.assertEqual(axis.labelText, '测试时长（小时）')
        self.assertEqual(p.mode, 'running')
        add_sample(3600, 2)
        x, y = p.curve.getData()
        self.assertEqual((x.tolist(), y.tolist()), ([0, 1 / 60, 1], [0, -1, 2]))

        p.time_axis_unit.setCurrentIndex(0)
        x, y = p.curve.getData()
        self.assertEqual((x.tolist(), y.tolist()), ([0, 1, 60], [0, -1, 2]))
        self.assertEqual(session.report()['latest']['elapsed_s'], 3600)

    def test_no_auto_set_during_collection(self):
        p=self.panel
        with patch('rtc_validation.panel.QFileDialog.getExistingDirectory',return_value=self.tmp.name): p.start()
        self.addCleanup(p.session.close)
        p.sync_once()
        self.assertFalse(any(b.startswith(b'rtct set') for b in self.sent))
    def test_connection_loss_pauses_without_recalibrating(self):
        p=self.panel
        with patch('rtc_validation.panel.QFileDialog.getExistingDirectory',return_value=self.tmp.name): p.start()
        self.addCleanup(p.session.close)
        p.feed(f'RTCV1 ACK start={p.session.sid}\r\n')
        p.guard=lambda:'disconnected'; p.tick()
        self.assertEqual(p.mode,'paused'); self.assertFalse(any(b.startswith(b'rtct set') for b in self.sent))
    def test_reference_requires_source(self):
        p=self.panel; p.reference_checked.setChecked(True); p.start()
        self.assertFalse(p.active)
    def test_complete_application_tab_and_guards(self):
        from config_manager import DEFAULT_CONFIG
        from rtt_t2 import MainWindow
        class Window(MainWindow):
            def start_hw_thread(self): pass
        w=Window(copy.deepcopy(DEFAULT_CONFIG)); self.addCleanup(w.deleteLater)
        self.addCleanup(w.ble_dfu_page.file_handler.close)
        self.assertEqual(w.workspace_tabs.count(),5)
        self.assertEqual(w.workspace_tabs.tabText(4),'RTC 验证')
        self.assertIn('连接',w.rtc_guard())
        w.rtc_validation_page.session=object()
        self.assertIn('RTC',w.ble_upgrade_guard())
        self.assertIn('RTC',w.factory_pair_guard())
        self.assertIn('RTC',w.sc201_workbench_guard())
        w.rtc_validation_page.session=None

if __name__=='__main__': unittest.main()
