"""J-Link is optional until an explicit RTT connection is requested."""
import copy
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_panel import APP, FakeWorker
from PyQt5.QtCore import Qt
from bds.bds_jlink import BDS_Jlink
from ble_dfu_panel import BleDfuWidget
from config_manager import DEFAULT_CONFIG


class JlinkOptionalTests(unittest.TestCase):
    def test_constructor_poll_close_and_serial_query_never_load_dll(self):
        with patch('bds.bds_jlink.pylink.JLink', side_effect=TypeError('Expected to be given a valid DLL.')) as native:
            hardware = BDS_Jlink(Mock(), Mock())
            hardware.hw_read()
            hardware.hw_close()
            self.assertFalse(hardware.hw_is_open())
            self.assertEqual(hardware.get_hw_serial_number(), 0)
            native.assert_not_called()

    def test_missing_or_wrong_architecture_dll_reports_actionable_error(self):
        for error in (TypeError('Expected to be given a valid DLL.'), OSError('WinError 193: invalid Win32 application')):
            with self.subTest(error=error), patch('bds.bds_jlink.pylink.JLink', side_effect=error):
                errors = []
                hardware = BDS_Jlink(errors.append, errors.append)
                self.assertFalse(hardware.hw_open(reset_flag=False))
                self.assertIsNone(hardware.jlink)
                self.assertIn('SEGGER', hardware.last_error)
                self.assertIn('BLE OTA', hardware.last_error)
                self.assertEqual(len(errors), 1)
                hardware.hw_close()

    def test_install_then_retry_and_normal_rtt_lifecycle(self):
        native = Mock()
        native.connected.return_value = True
        native.opened.return_value = True
        native.serial_number = 123
        with patch('bds.bds_jlink.pylink.JLink', side_effect=[TypeError('DLL missing'), native]) as loader:
            hardware = BDS_Jlink(Mock(), Mock())
            self.assertFalse(hardware.hw_open(reset_flag=False))
            self.assertTrue(hardware.hw_open(reset_flag=False))
            self.assertEqual(hardware.last_error, '')
            native.reset.assert_not_called()
            native.rtt_start.assert_called_once_with(None)
            self.assertEqual(hardware.get_hw_serial_number(), 123)
            hardware.hw_close()
            native.rtt_stop.assert_called_once()
            native.close.assert_called_once()
            self.assertTrue(hardware.hw_open(reset_flag=False))
            self.assertEqual(loader.call_count, 2)

    def test_real_main_initializes_default_jlink_mode_without_loading_dll(self):
        import rtt_t2
        original_window = rtt_t2.MainWindow
        windows = []
        def create_window(config):
            window = original_window(config)
            window.setAttribute(Qt.WA_DontShowOnScreen, True)
            windows.append(window)
            return window
        app = SimpleNamespace(setFont=Mock(), exec_=Mock(return_value=0))
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'LOCALAPPDATA': folder}), \
                patch.object(original_window, 'start_hw_thread'), \
                patch('rtt_t2.MainWindow', side_effect=create_window), \
                patch('rtt_t2.BleDfuWidget', side_effect=lambda *args, **kwargs:
                      BleDfuWidget(*args, worker_factory=FakeWorker, **kwargs)), \
                patch('rtt_t2.QApplication', return_value=app), \
                patch('rtt_t2.config_manager.initialize_app_environment', return_value=folder), \
                patch('rtt_t2.config_manager.load_config', return_value=copy.deepcopy(DEFAULT_CONFIG)), \
                patch('rtt_t2.log.basicConfig'), patch('rtt_t2.wv.wave_init'), \
                patch('rtt_t2.threading.Thread'), patch('rtt_t2.sys.exit') as exiting, \
                patch('bds.bds_jlink.pylink.JLink', side_effect=TypeError('Expected to be given a valid DLL.')) as loader:
            try:
                rtt_t2.main()
                exiting.assert_called_once_with(0)
                loader.assert_not_called()
                self.assertEqual(len(windows), 1)
                self.assertIsInstance(windows[0].hw_obj, BDS_Jlink)
                self.assertIsNone(windows[0].hw_obj.jlink)
                self.assertEqual(windows[0].workspace_tabs.count(), 4)
            finally:
                for window in windows:
                    window.ble_dfu_page.closed.disconnect(window.close)
                    window.ble_dfu_page._finish_shutdown()
                    window.factory_pair_page.worker.stop()
                    window.hide()
                    window.deleteLater()
                APP.processEvents()
