from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import MagicMock, patch

from PyQt5.QtWidgets import QApplication

from uart_validation import probes
from uart_validation.panel import UartValidationWidget
from uart_validation.protocol import FW, NUMBER_FIELDS
from uart_validation.session import NIGHT, QUICK, Profile, Session


class Device:
    def __init__(self, role, *, corrupt=False, disconnect=False, busy=False):
        self.role = role
        self.corrupt = corrupt
        self.disconnect = disconnect
        self.commands = []
        self.partial = bytearray()
        self.rx = bytearray()
        self.running = busy
        self.record = dict.fromkeys(NUMBER_FIELDS, 0)
        self.record.update(role=role, boot='12345678' if role == 'H' else '87654321', fw=FW, state=2 if busy else 0)

    def read(self):
        if self.running and self.disconnect:
            raise IOError('probe detached')
        chunk = bytes(self.rx[:101])
        del self.rx[:101]
        return chunk

    def write(self, data):
        accepted = min(len(data), 19)
        self.partial.extend(data[:accepted])
        if not self.partial.endswith(b'\n'):
            return accepted
        command = self.partial.decode('ascii').strip()
        self.partial.clear()
        self.commands.append(command)
        if command.startswith('ber arm '):
            fields = ('run', 'mode', 'count', 'len', 'gap', 'pattern')
            self.record.update(zip(fields, map(int, command.split()[2:])))
            self.record.update(state=1)
        elif command == 'ber start':
            self.record.update(state=2)
            self.running = True
        elif command == 'ber stop':
            self.record.update(state=3, flags=1)
            self.running = False
        elif command == 'ber status':
            status_type = 'FINAL' if self.record['state'] == 3 else 'STAT'
            line = 'UBER ' + status_type + ' ' + ' '.join(f'{k}={v}' for k, v in self.record.items())
            self.rx.extend((line + '\r\n').encode('ascii'))
        return accepted

    def finish(self):
        count, length = self.record['count'], self.record['len']
        self.running = False
        self.record.update(state=3, tx_done=count, tx_accept=count, rx_good=count - int(self.corrupt),
                           rx_unique=count, rx_bad=int(self.corrupt), crc=int(self.corrupt), diff=int(self.corrupt),
                           peer_done=1, peer_tx=count, dma_bytes=count * (length + 36) + 36, bits=count * length * 8)


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.devices = {'H': Device('H'), 'S': Device('S')}
        self.closed = []
        self.stop = Event()
        self.events = []

    @contextmanager
    def attach(self, role, serial):
        self.assertEqual({'H': 111, 'S': 222}[role], serial)
        try:
            yield self.devices[role]
        finally:
            self.closed.append(role)

    def execute(self, after_start=None):
        def notify(kind, payload):
            self.events.append((kind, payload))
            if kind == 'status' and payload == '测试运行中':
                if after_start:
                    after_start()
                else:
                    for device in self.devices.values():
                        device.finish()

        session = Session({'H': 111, 'S': 222}, Profile('Fixture', 10, 16), Path(self.temp.name), self.stop,
                          notify, attach=self.attach)
        result = session.execute()
        self.assertEqual(self.closed, ['S', 'H'])
        self.assertEqual(json.loads((session.directory / 'summary.json').read_text(encoding='utf-8')), result)
        return session, result

    def test_quick_and_night_presets(self):
        self.assertEqual((QUICK.count, QUICK.length, NIGHT.count, NIGHT.length), (1000, 128, 1000000, 256))

    def test_pass_and_report_from_two_fragmented_transports(self):
        session, result = self.execute()
        self.assertEqual(result['verdict'], 'PASS')
        self.assertEqual(result['directions']['H_to_S']['received_correct_unique_frames'], 10)
        self.assertEqual(result['requested']['probe_serials'], {'H': 111, 'S': 222})
        for name in ('host_rtt.log', 'scale_rtt.log', 'directions.csv', 'snapshots.csv'):
            self.assertGreater((session.directory / name).stat().st_size, 0)
        self.assertTrue(any(kind == 'progress' for kind, _ in self.events))

    def test_corrupt_direction_fails(self):
        self.devices['S'].corrupt = True
        _, result = self.execute()
        self.assertEqual(result['verdict'], 'FAIL')
        self.assertEqual(result['directions']['H_to_S']['failed_or_missing_frames'], 1)
        self.assertEqual(result['directions']['S_to_H']['failed_or_missing_frames'], 0)

    def test_wrong_role_does_not_arm_or_start(self):
        self.devices['H'].record['role'] = 'S'
        _, result = self.execute()
        self.assertEqual(result['verdict'], 'INCOMPLETE')
        self.assertIn('请核对两端接线', result['controller_error'])
        self.assertTrue(all(set(device.commands) <= {'ber status'} for device in self.devices.values()))

    def test_existing_run_not_stopped(self):
        self.devices['H'].record.update(state=2)
        _, result = self.execute()
        self.assertIn('已有测试在运行', result['controller_error'])
        self.assertTrue(all(set(device.commands) <= {'ber status'} for device in self.devices.values()))

    def test_disconnected_records_incomplete_and_stops_started_peers(self):
        self.devices['S'].disconnect = True
        _, result = self.execute(lambda: None)
        self.assertEqual(result['verdict'], 'INCOMPLETE')
        self.assertIn('probe detached', result['controller_error'])
        self.assertTrue(all('ber stop' in device.commands for device in self.devices.values()))

    def test_operator_stop_persists_incomplete(self):
        _, result = self.execute(self.stop.set)
        self.assertEqual(result['verdict'], 'INCOMPLETE')
        self.assertIn('测试员停止', result['controller_error'])


class ProbeTest(unittest.TestCase):
    def test_attach_uses_explicit_serial_address_and_closes(self):
        client = MagicMock()
        client.serial_number = 222
        client.halted.return_value = False
        client.memory_read8.return_value = b'SEGGER RTT'
        with patch.object(probes, 'new_client', return_value=client):
            with probes.attach('S', 222):
                client.open.assert_called_once_with(serial_no=222)
                client.exec_command.assert_any_call('InhibitConnectRetries = 1')
                client.rtt_start.assert_called_once_with(probes.ADDRESSES['S'])
            client.rtt_stop.assert_called_once_with()
            client.close.assert_called_once_with()
            client.reset.assert_not_called()

    def test_wrong_control_block_closes_without_writing(self):
        client = MagicMock()
        client.serial_number = 222
        client.halted.return_value = False
        client.memory_read8.return_value = bytes(10)
        with patch.object(probes, 'new_client', return_value=client):
            with self.assertRaisesRegex(RuntimeError, '未找到'):
                with probes.attach('S', 222):
                    self.fail('unexpectedly attached')
            client.rtt_start.assert_not_called()
            client.rtt_write.assert_not_called()
            client.close.assert_called_once_with()


class PanelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_panel_runs_on_worker_and_displays_both_directions(self):
        with tempfile.TemporaryDirectory() as directory:
            panel = UartValidationWidget(None, settings={}, directory=lambda: directory, guard=lambda: None)
            panel.fill_probes([111, 222])
            panel.host.setCurrentIndex(panel.host.findData(111))
            panel.scale.setCurrentIndex(panel.scale.findData(111))
            self.assertFalse(panel.start.isEnabled())
            panel.scale.setCurrentIndex(panel.scale.findData(222))
            self.assertTrue(panel.start.isEnabled())
            devices = {'H': Device('H'), 'S': Device('S')}

            @contextmanager
            def attach(role, _):
                device = devices[role]
                original = device.write

                def write(data):
                    written = original(data)
                    if all(x.running for x in devices.values()):
                        for x in devices.values():
                            x.finish()
                    return written

                device.write = write
                yield device

            # Inject the two devices where the panel creates a session.
            with patch('uart_validation.panel.Session', side_effect=lambda *args: Session(*args, attach=attach)):
                panel.start.click()
                deadline = time.monotonic() + 5
                while panel.active and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(.01)
                self.assertFalse(panel.active)
            self.assertEqual(panel.last_result['verdict'], 'PASS')
            self.assertEqual(panel.table.item(0, 2).text(), '1,000')
            self.assertEqual(panel.table.item(1, 2).text(), '1,000')
            self.assertTrue(panel.report.isEnabled())
