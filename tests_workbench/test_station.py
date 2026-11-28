"""Station tests use byte replies and never open a real port."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtWidgets import QApplication, QMessageBox

from production_station import StationWidget, verified_ota_row


class SerialFixture:
    def __init__(self):
        self.open = True
        self.packet = b''
        self.packets = []
        self.data = b''

    def write(self, data):
        self.packet = data
        self.packets.append(data)
        return len(data)

    def isOpen(self):
        return self.open

    def portName(self):
        return 'fixture'

    def close(self):
        self.open = False

    def flush(self):
        return True

    def readAll(self):
        data, self.data = self.data, b''
        return data


class StationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.station = StationWidget(None, lambda: self.directory.name, lambda _: None, lambda: None)
        self.addCleanup(self.station.deleteLater)
        self.station.tick.stop()
        self.serial = SerialFixture()
        self.station.serial = self.serial

    def answer(self, operation, payload='', ok=True):
        _, token, op = self.serial.packet.decode().strip().split(' ', 2)
        self.assertEqual(op, operation)
        self.serial.data = f'FT {token} {"OK" if ok else "ERR"} {payload}\r\n'.encode()
        self.station.receive()

    def identify(self):
        self.station.request('id', self.station.identified)
        self.answer('id', 'SC201-S M22016C28A01001 V001-FT1')

    def test_stale_reply_and_wrong_role(self):
        self.station.request('id', self.station.identified)
        self.serial.data = b'FT unrelated OK SC201-S other V001-FT1\n'
        self.station.receive()
        self.assertIsNone(self.station.identity)
        self.answer('id', 'SC200-D M21016C28A01001 V001-FT1')
        self.assertFalse(self.serial.open)
        self.assertIsNone(self.station.identity)

    def open_model(self, target='SC201-S'):
        self.identify()
        self.station.select('设备信息')
        self.snapshot()
        self.station.model.setCurrentText(target)
        self.station.go.click()

    def snapshot(self, model='SC201-S', sn='M22016C28A01001', source=2, flags='07',
                 name=None, state='saved'):
        self.answer('id', f'{model} M22016C28A01001 V001-FT1')
        self.assertEqual(self.serial.packet, b'device_info\n')
        name = f'{model} A1B2C3' if name is None else name
        line = (f'DEVICE INFO model={model} name={name} sn={sn} sn_source={source} '
                f'mac=001122A1B2C3 flags={flags} state={state}\r\n').encode()
        # The dock UART may mix protocol or debug traffic and split lines.
        self.serial.data = b'noise\r\n\xff\x01' + line[:42]
        self.station.receive()
        self.assertIsNotNone(self.station.pending)
        self.serial.data = line[42:]
        self.station.receive()

    def writes(self):
        return [p for p in self.serial.packets if b' model ' in p]

    def test_factory_model_reinspection_and_wrong_target_are_read_only(self):
        self.open_model()
        self.snapshot()
        self.assertTrue(self.station.finished)
        self.assertIn('只读核对通过', self.station.feedback.text())
        self.assertIn('SC201-S A1B2C3', self.station.feedback.text())
        self.station.model.setCurrentText('SC202-S')
        self.assertFalse(self.station.finished)
        self.assertNotIn('设备信息', self.station.verdicts)
        self.station.go.click()
        self.snapshot()
        message = self.station.feedback.text()
        for field in ('型号写保护', '当前配置 SC201-S', '目标 SC202-S', '正式 SN'):
            self.assertIn(field, message)
        self.assertTrue(self.station.write_model.isHidden())
        self.station.go.click()
        self.snapshot()
        self.assertTrue(self.station.failed)
        self.assertEqual(self.writes(), [])

    def test_default_model_is_written_only_after_confirmation_and_readback(self):
        self.open_model()
        self.snapshot(source=1, flags='06')  # unconfigured SC201-S default
        self.assertFalse(self.station.finished)
        self.assertIn('当前配置 未配置', self.station.feedback.text())
        self.assertFalse(self.station.write_model.isHidden())
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.No):
            self.station.write_model.click()
        self.assertEqual(self.writes(), [])
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes):
            self.station.write_model.click()
        self.snapshot(source=1, flags='06')
        self.answer('model SC201-S')
        self.assertFalse(self.station.finished)
        self.snapshot(source=1)
        self.assertIn('写入后回读通过', self.station.feedback.text())
        self.assertTrue(self.station.finished)
        self.assertEqual(len(self.writes()), 1)

    def test_change_to_large_scale_with_default_sn(self):
        self.open_model('SC202-S')
        self.snapshot(source=1)
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes):
            self.station.write_model.click()
        self.snapshot(source=1)
        self.answer('model SC202-S')
        self.snapshot(model='SC202-S', source=1)
        self.assertTrue(self.station.finished)
        self.assertEqual(self.station.identity[0], 'SC202-S')
        self.assertIn('SC202-S A1B2C3', self.station.feedback.text())

    def test_protection_is_rechecked_immediately_before_write(self):
        self.open_model('SC202-S')
        self.snapshot(source=1)
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes):
            self.station.write_model.click()
        self.snapshot(source=2)
        self.assertTrue(self.station.failed)
        self.assertTrue(self.station.write_model.isHidden())
        self.assertEqual(self.writes(), [])

    def test_write_ack_alone_cannot_pass_and_retry_does_not_repeat_write(self):
        self.open_model('SC202-S')
        self.snapshot(source=1)
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes):
            self.station.write_model.click()
        self.snapshot(source=1)
        self.answer('model SC202-S')
        self.snapshot(source=1)
        self.assertTrue(self.station.failed)
        self.assertIn('写入后配置回读不符', self.station.feedback.text())
        self.station.go.click()
        self.snapshot(model='SC202-S', source=1)
        self.assertTrue(self.station.finished)
        self.assertEqual(len(self.writes()), 1)

    def test_changing_selection_or_disconnecting_revokes_write(self):
        self.open_model('SC202-S')
        self.snapshot(source=1)
        self.station.model.setCurrentText('SC201-S')
        self.assertTrue(self.station.write_model.isHidden())
        self.station.model.setCurrentText('SC202-S')
        self.station.go.click()
        self.snapshot(source=1)
        self.station.disconnect_port()
        self.station.confirm_model_write()
        self.assertTrue(self.station.write_model.isHidden())
        self.assertEqual(self.writes(), [])

    def test_bad_identity_name_or_storage_cannot_pass_or_enable_write(self):
        self.open_model()
        for args in ({'sn':'other'}, {'name':'SC201-S FFFFFF'}, {'state':'read_error'},
                     {'flags':'01'}, {'source':0}):
            with self.subTest(args=args):
                self.snapshot(**args)
                self.assertTrue(self.station.failed)
                self.assertTrue(self.station.write_model.isHidden())
                self.assertFalse(self.station.finished)
                self.station.go.click()
        self.assertEqual(self.writes(), [])

    def test_timeout_and_late_snapshot_after_page_change(self):
        self.open_model()
        self.answer('id', 'SC201-S M22016C28A01001 V001-FT1')
        self.station.pending = self.station.pending._replace(deadline=0)
        self.station.poll()
        self.assertIn('读取设备信息超时', self.station.feedback.text())
        self.station.go.click()
        self.serial.data = b'DEVICE INFO stale\n'
        self.station.receive()
        self.assertEqual(self.station.pending.operation, 'id')
        self.station.select('指示灯')
        self.assertIsNone(self.station.pending)
        self.assertEqual(self.writes(), [])

    def test_three_points_require_matching_nv_generation(self):
        self.identify()
        self.answer('cal status','0 0 0 3')
        self.answer('cal read 3','-320000 -1080000 -3110000')
        self.assertEqual(self.station.readout.text(), '已完成标定')
        self.assertEqual(self.station.go.text(), '重新标定')
        self.assertFalse(any(b'cal start' in packet for packet in self.serial.packets))
        self.station.advance()
        self.answer('cal status','0 0 0 3')
        self.answer('cal start')
        for i in range(3):
            self.station.advance()
            self.answer(f'cal point {i}')
            self.station.poll_at = 0
            self.station.poll()
            self.answer('cal status', f'{i+1} 1 1 3' if i<2 else '3 0 0 4')
        self.assertFalse(self.station.finished)
        self.answer('cal read 4','-360000 -1010000 -2960000')
        self.assertTrue(self.station.finished)
        self.assertIn('代次 4',next(Path(self.directory.name).glob('*/*.jsonl')).read_text(encoding='utf-8'))

    def test_calibration_missing_or_unverified_is_never_shown_as_complete(self):
        self.identify()
        self.answer('cal status','0 0 0 0')
        self.assertEqual(self.station.go.text(), '开始标定')
        self.station.select('三点标定')
        self.answer('cal status','0 0 0 3')
        self.answer('cal read 3', ok=False)
        self.assertFalse(self.station.finished)
        self.assertEqual(self.station.readout.text(), '未确认三点记录')
        self.station.refresh_cal.click()
        self.answer('cal status','0 0 0 3')
        self.answer('cal read 3','-360000 -1010000 -2960000')
        self.assertEqual(self.station.readout.text(),'已完成标定')
        self.assertFalse(any(b'cal start' in p for p in self.serial.packets))

    def test_device_page_detects_large_scale_and_blank_board_automatically(self):
        self.identify()
        self.station.select('设备信息')
        self.snapshot(model='SC202-S')
        self.assertIn('大秤', self.station.readout.text())
        self.assertEqual(self.station.model.currentText(), 'SC202-S')
        self.station.select('设备信息')
        self.snapshot(source=1, flags='06')
        self.assertEqual(self.station.model.currentIndex(), -1)
        self.assertIn('未配置', self.station.feedback.text())
        self.assertEqual(self.writes(), [])

    def open_lcd(self):
        self.station.role.setCurrentIndex(1)
        self.serial.open = True
        self.station.request('id', self.station.identified)
        self.answer('id','SC200-D M21016C28A01001 V001-FT2')
        self.station.select('LCD 屏幕测试')
        self.station.advance()

    def test_lcd_waits_for_frame_then_records_defects_and_restoration(self):
        self.open_lcd()
        for color in range(1, 6):
            self.answer(f'lcd {color}')
            self.answer('lcd status', f'{color} {color-1}')
            self.assertFalse(self.station.go.isEnabled())
            self.station.poll_at = 0
            self.station.poll()
            self.answer('lcd status', f'{color} {color}')
            self.assertTrue(self.station.go.isEnabled())
            if color == 4:
                self.station.lcd_checks[-1].setChecked(True)
                self.station.lcd_note.setText('左上')
            self.station.go.click()
            self.answer('lcd status',f'{color} {color}')
        self.assertFalse(self.station.finished)
        self.answer('lcd 0')
        self.answer('lcd status','0 0')
        self.assertTrue(self.station.finished)
        self.assertIn('屏幕目检不合格', self.station.feedback.text())
        self.assertIn('黑屏 · 亮点 · 左上', self.station.feedback.text())
        self.assertIn('白屏 · 未见缺陷', self.station.history.toPlainText())

    def test_lcd_abort_and_expired_color_do_not_pass(self):
        self.open_lcd()
        self.answer('lcd 1')
        self.answer('lcd status','1 1')
        self.station.go.click()
        self.answer('lcd status','0 0')
        self.assertTrue(self.station.failed)
        self.assertTrue(self.serial.packet.endswith(b' lcd 0\n'))
        self.station.select('LCD 屏幕测试')
        self.station.go.click()
        self.station.select('RTC 对时')
        self.assertTrue(self.serial.packet.endswith(b' lcd 0\n'))

    def test_lcd_clean_screen_passes_only_after_restored_frame(self):
        self.open_lcd()
        for color in range(1, 6):
            self.answer(f'lcd {color}')
            self.answer('lcd status', f'{color} {color}')
            self.station.go.click()
            self.answer('lcd status', f'{color} {color}')
        self.answer('lcd 0')
        self.answer('lcd status', '0 5')
        self.assertFalse(self.station.finished)
        self.station.poll_at = 0
        self.station.poll()
        self.answer('lcd status', '0 0')
        self.assertEqual(self.station.verdicts['LCD 屏幕测试'], '通过')

    def test_usb_rejects_stale_inserted_state(self):
        self.identify()
        self.station.select('USB 口充电')
        self.station.advance()
        self.answer('usb','0 0 0 0')
        self.station.advance()
        self.answer('usb','0 0 0 0')
        self.assertTrue(self.station.failed)
        self.assertFalse(self.station.finished)

    def test_verified_csv_requires_serial_model_version_and_completion(self):
        row='型号,SN,结果,当前版本,目标版本,已传字节,总字节\nSC201-S,ABC,成功（已验版）,FT1,FT1,100,100\n'
        self.assertEqual(verified_ota_row(row.encode('utf-8-sig'),'ABC','FT1',('SC201-S',))['SN'],'ABC')
        for text,sn,fw in [(row,'DEF','FT1'),(row,'ABC','FT2'),(row.replace('已验版','未验版'),'ABC','FT1'),
                           (row.replace('100,100','20,100'),'ABC','FT1')]:
            with self.assertRaises(ValueError):
                verified_ota_row(text.encode(),sn,fw,('SC201-S',))
