import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtCore import QEvent, QLockFile, QSettings, QMetaObject, Q_ARG, Qt, QObject
from PyQt5.QtNetwork import QSslSocket
from PyQt5.QtWidgets import QApplication, QListWidget, QMainWindow, QStackedWidget, QWidget, QSpinBox, QPushButton, QLabel, QLineEdit, QCheckBox, QPlainTextEdit, QTableWidget, QHeaderView
from PyQt5.QtTest import QTest
from PyQt5 import sip
from sc201_workbench.native import NativeWorkbench, NATIVE_DIRECTORY, WorkbenchError
from sc201_workbench.panel import Sc201WorkbenchWidget

APP = QApplication.instance() or QApplication([])


class FakeNative:
    def __init__(self):
        self.status = {'ready': True, 'connected': False, 'busy': False, 'scanning': False}
        self.created = []
        self.shutdown_calls = 0

    def create(self, parent, directory):
        self.created.append(Path(directory))
        return QWidget(parent)

    def state(self, _widget):
        return dict(self.status)

    def error(self):
        return '工作台操作仍在进行'

    def shutdown(self, _widget):
        self.shutdown_calls += 1
        return not self.status['busy']


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.native = FakeNative()
        self.panel = Sc201WorkbenchWidget(native_factory=lambda: self.native,
                                          data_directory=Path(self.folder.name))
        self.addCleanup(self.cleanup_panel)

    def cleanup_panel(self):
        self.native.status['busy'] = False
        self.panel.request_shutdown()
        sip.delete(self.panel)

    def test_loading_is_lazy_and_a_legacy_connection_blocks_creation(self):
        self.assertFalse(self.native.created)
        self.panel.guard = lambda: '旧 BLE 连接仍在使用'
        self.assertFalse(self.panel.ensure_loaded())
        self.assertFalse(self.native.created)
        self.assertIn('旧 BLE', self.panel.notice.text())
        self.panel.guard = lambda: None
        self.assertTrue(self.panel.ensure_loaded())
        self.assertEqual(self.native.created, [Path(self.folder.name)])

    def test_active_state_blocks_data_directory_switch(self):
        self.panel.ensure_loaded()
        self.native.status['connected'] = True
        self.panel.refresh_state()
        self.assertTrue(self.panel.active)
        self.assertFalse(self.panel.choose_button.isEnabled())
        self.native.status['connected'] = False
        self.panel.refresh_state()
        self.assertTrue(self.panel.choose_button.isEnabled())

    def test_close_waits_for_the_operation_and_emits_completion_after_it_finishes(self):
        self.panel.ensure_loaded()
        self.native.status['busy'] = True
        completed = []
        self.panel.closed.connect(lambda: completed.append(True))
        self.assertFalse(self.panel.request_shutdown())
        self.assertFalse(self.panel.is_closed)
        self.native.status['busy'] = False
        self.panel.refresh_state()
        self.assertTrue(self.panel.is_closed)
        self.assertEqual(completed, [True])

    def test_directory_change_uses_explicit_selection_without_copying_original_records(self):
        self.panel.ensure_loaded()
        original = Path(self.folder.name) / 'original.json'
        original.write_bytes(b'unchanged operator data')
        selected = Path(self.folder.name) / 'selected'
        with patch('sc201_workbench.panel.QFileDialog.getExistingDirectory', return_value=str(selected)):
            self.panel.choose_directory()
        self.assertEqual(self.native.created[-1], selected)
        self.assertEqual(self.panel.settings['sc201_workbench_data_dir'], str(selected))
        self.assertEqual(original.read_bytes(), b'unchanged operator data')
        self.assertFalse(selected.exists())  # The adapter, not the UI, owns directory creation.


@unittest.skipUnless((NATIVE_DIRECTORY / 'sc201_workbench.dll').exists(), 'Build the native workbench first')
class NativeWidgetTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        QSettings.setDefaultFormat(QSettings.IniFormat)
        QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, self.folder.name)
        self.parent = QWidget()
        self.api = NativeWorkbench()
        self.widget = None
        self.addCleanup(self.cleanup_widget)

    def cleanup_widget(self):
        if self.widget:
            self.assertTrue(self.api.shutdown(self.widget))
        sip.delete(self.parent)
        APP.processEvents()

    def test_real_original_widget_exposes_all_pages_with_no_connection(self):
        self.widget = self.api.create(self.parent, Path(self.folder.name) / 'workbench')
        navigation = self.widget.findChild(QListWidget, 'workspaceNavigation')
        pages = self.widget.findChild(QStackedWidget, 'workspacePages')
        self.assertEqual([navigation.item(i).text() for i in range(navigation.count())],
                         ['日常操作', '固件升级', '工厂生产', '连接测试', '资料与调试'])
        for index in range(5):
            navigation.setCurrentRow(index)
            APP.processEvents()
            self.assertEqual(pages.currentIndex(), index)
        self.assertFalse(self.widget.findChild(QMainWindow).isWindow())
        state = self.api.state(self.widget)
        self.assertTrue(state['ready'])
        self.assertFalse(state['connected'] or state['busy'] or state['scanning'] or state['ota_active'])
        self.assertTrue(QSslSocket.supportsSsl())

    def test_original_standalone_lock_is_respected_before_loading_records(self):
        lock = QLockFile(str(Path(self.folder.name) / 'workbench-gui.lock'))
        self.assertTrue(lock.tryLock(0))
        try:
            with self.assertRaisesRegex(WorkbenchError, '资料已有工作台'):
                self.api.create(self.parent, Path(self.folder.name))
            self.assertFalse((Path(self.folder.name) / 'backend').exists())
        finally:
            lock.unlock()

    def test_shutdown_releases_the_original_workbench_lock(self):
        self.widget = self.api.create(self.parent, Path(self.folder.name))
        self.assertTrue(self.api.shutdown(self.widget))
        self.assertFalse(self.api.state(self.widget)['ready'])
        lock = QLockFile(str(Path(self.folder.name) / 'workbench-gui.lock'))
        self.assertTrue(lock.tryLock(0))
        lock.unlock()

    def test_batch_page_has_bounded_concurrency_and_validates_empty_selection(self):
        self.widget = self.api.create(self.parent, Path(self.folder.name))
        panel = self.widget.findChild(QWidget, 'batchOtaPanel')
        concurrency = panel.findChild(QSpinBox, 'batchOtaConcurrency')
        self.assertEqual((concurrency.minimum(), concurrency.maximum(), concurrency.value()), (1, 4, 2))
        panel.findChild(QPushButton, 'startBatchOta').click()
        self.assertIn('勾选', panel.findChild(QLabel, 'batchOtaNotice').text())
        state = self.api.state(self.widget)
        self.assertEqual(state['batch_ota']['total'], 0)
        self.assertFalse(state['batch_ota']['active'])

    def test_checked_devices_without_login_never_start_radio_or_server_jobs(self):
        from PyQt5.QtBluetooth import QBluetoothAddress, QBluetoothDeviceInfo
        self.widget = self.api.create(self.parent, Path(self.folder.name))
        ble = next(obj for obj in self.widget.findChildren(QObject)
                   if obj.metaObject().className() == 'BleLink')
        for index in (1, 2):
            info = QBluetoothDeviceInfo(QBluetoothAddress(f'12:34:56:78:90:{index:02X}'), f'isolated fixture {index}', 0)
            info.setRssi(-45)
            QMetaObject.invokeMethod(ble, 'deviceFound', Qt.DirectConnection, Q_ARG(QBluetoothDeviceInfo, info))
        device_list = self.widget.findChild(QListWidget, 'bleDevices')
        self.assertEqual(device_list.count(), 2)
        for index in range(2):
            self.assertTrue(device_list.item(index).flags() & Qt.ItemIsUserCheckable)
            device_list.item(index).setCheckState(Qt.Checked)
        image = Path(self.folder.name) / 'fixture.bin'
        image.write_bytes(b'isolated fixture\x00mlink:model=SC201-S\x00')
        self.widget.findChild(QLineEdit, 'otaImagePath').setText(str(image))
        self.widget.findChild(QLineEdit, 'otaExpectedVersion').setText('V999')
        panel = self.widget.findChild(QWidget, 'batchOtaPanel')
        panel.findChild(QPushButton, 'startBatchOta').click()
        self.assertIn('登录', panel.findChild(QLabel, 'batchOtaNotice').text())
        self.assertFalse(self.api.state(self.widget)['batch_ota']['active'])
        self.assertEqual(self.api.state(self.widget)['batch_ota']['total'], 0)
        self.assertFalse((Path(self.folder.name) / 'server-cache').exists())

    def test_packet_events_do_not_repaint_the_results_table_per_block(self):
        self.widget = self.api.create(self.parent, Path(self.folder.name))
        panel = self.widget.findChild(QWidget, 'batchOtaPanel')
        controller = self.widget.findChild(QObject, 'batchOtaController')
        count = int(panel.property('rttRefreshCount'))
        for _ in range(10000):
            QMetaObject.invokeMethod(controller, 'changed', Qt.DirectConnection)
        self.assertEqual(int(panel.property('rttRefreshCount')), count)
        QTest.qWait(320)
        self.assertGreater(int(panel.property('rttRefreshCount')), count)
        self.assertLessEqual(int(panel.property('rttRefreshCount')) - count, 2)
        table = panel.findChild(QTableWidget, 'batchOtaResults')
        self.assertEqual(table.horizontalHeader().sectionResizeMode(0), QHeaderView.Interactive)
        self.assertIn('传输', table.horizontalHeaderItem(5).text())

    def test_batch_prefix_respects_real_detailed_log_filter(self):
        self.widget = self.api.create(self.parent, Path(self.folder.name))
        window = self.widget.findChild(QMainWindow)
        raw = next(box for box in window.findChildren(QCheckBox) if box.text() == '详细日志')
        log = window.findChild(QPlainTextEdit, 'communicationLog')
        raw.setChecked(False)
        packet = '[batch 12:34:56:78:90:01] [tx] n=244 isolated-packet-marker'
        verdict = '[batch 12:34:56:78:90:01] [ota] failure isolated-verdict-marker'
        for line in (packet, verdict):
            QMetaObject.invokeMethod(window, 'appendLog', Qt.DirectConnection, Q_ARG(str, line))
        self.assertNotIn('isolated-packet-marker', log.toPlainText())
        self.assertIn('isolated-verdict-marker', log.toPlainText())
        raw.setChecked(True)
        self.assertIn('isolated-packet-marker', log.toPlainText())
        self.assertFalse(self.api.state(self.widget)['batch_ota']['compact_log'])
        raw.setChecked(False)
        self.assertNotIn('isolated-packet-marker', log.toPlainText())

    def test_tuning_options_default_off_and_explicit_selection_is_reported(self):
        self.widget = self.api.create(self.parent, Path(self.folder.name))
        option = self.widget.findChild(QCheckBox, 'batchOtaThroughput')
        self.assertFalse(option.isChecked())
        state = self.api.state(self.widget)['batch_ota']
        self.assertFalse(state['throughput_preferred'])
        self.assertTrue(state['compact_log'])
        fit = self.widget.findChild(QCheckBox, 'batchOtaFitAtt')
        self.assertFalse(fit.isChecked())
        self.assertFalse(state['fit_ota_to_att'])
        drive = self.widget.findChild(QCheckBox, 'batchOtaEventDrive')
        self.assertTrue(drive.isChecked())
        self.assertTrue(state['event_driven_ota'])
        drive.setChecked(False)
        self.assertFalse(self.api.state(self.widget)['batch_ota']['event_driven_ota'])
        fit.setChecked(True)
        self.assertTrue(self.api.state(self.widget)['batch_ota']['fit_ota_to_att'])
        self.assertEqual(state['refresh_interval_ms'], 250)
        option.setChecked(True)
        self.assertTrue(self.api.state(self.widget)['batch_ota']['throughput_preferred'])

    def test_batch_activity_participates_in_real_native_close_guard(self):
        self.widget = self.api.create(self.parent, Path(self.folder.name))
        security = self.widget.findChild(QWidget, 'securityPanel')
        try:
            security.setProperty('rttBatchOtaActive', True)
            self.assertTrue(self.api.state(self.widget)['busy'])
            self.assertFalse(self.api.shutdown(self.widget))
        finally:
            security.setProperty('rttBatchOtaActive', False)

    def publish_device(self, index, rssi):
        from PyQt5.QtBluetooth import QBluetoothAddress, QBluetoothDeviceInfo
        info = QBluetoothDeviceInfo(QBluetoothAddress(f'12:34:56:78:90:{index:02X}'), f'isolated fixture {index}', 0)
        info.setRssi(rssi)
        ble = next(obj for obj in self.widget.findChildren(QObject) if obj.metaObject().className() == 'BleLink')
        QMetaObject.invokeMethod(ble, 'deviceFound', Qt.DirectConnection, Q_ARG(QBluetoothDeviceInfo, info))

    def test_rssi_boundaries_unknown_values_and_live_recovery_preserve_indexes(self):
        self.widget = self.api.create(self.parent, Path(self.folder.name))
        devices = self.widget.findChild(QListWidget, 'bleDevices')
        self.publish_device(1, -61)
        self.publish_device(2, -60)
        self.publish_device(3, -49)
        self.publish_device(4, 0)
        self.publish_device(5, 127)
        self.assertEqual(devices.count(), 5)
        self.assertEqual([not devices.item(i).isHidden() for i in range(5)], [False, True, True, False, False])
        self.assertEqual(devices.currentRow(), 1)
        self.assertIn('12:34:56:78:90:02', devices.currentItem().text())
        self.assertIn('2 / 5', self.widget.findChild(QLabel, 'workbenchRssiSummary').text())
        self.publish_device(1, -55)
        self.assertEqual(devices.count(), 5)
        self.assertFalse(devices.item(0).isHidden())
        self.publish_device(2, -70)
        self.assertTrue(devices.item(1).isHidden())
        self.assertEqual(devices.currentRow(), 0)
        self.assertIn('12:34:56:78:90:01', devices.currentItem().text())
        self.assertFalse(self.api.state(self.widget)['connected'])

    def test_rssi_check_all_and_hidden_checks_cannot_start_a_batch(self):
        self.widget = self.api.create(self.parent, Path(self.folder.name))
        self.publish_device(1, -50)
        self.publish_device(2, -65)
        devices = self.widget.findChild(QListWidget, 'bleDevices')
        self.widget.findChild(QPushButton, 'batchOtaCheckAll').click()
        self.assertEqual([devices.item(i).checkState() for i in range(2)], [Qt.Checked, Qt.Unchecked])
        self.widget.findChild(QSpinBox, 'workbenchRssiMinimum').setValue(-45)
        self.assertTrue(all(devices.item(i).isHidden() for i in range(2)))
        self.assertEqual(devices.currentRow(), -1)
        devices.item(1).setCheckState(Qt.Checked)
        self.assertEqual(devices.item(1).checkState(), Qt.Unchecked)
        self.widget.findChild(QPushButton, 'startBatchOta').click()
        self.assertIn('勾选', self.widget.findChild(QLabel, 'batchOtaNotice').text())
        self.assertFalse(self.api.state(self.widget)['batch_ota']['active'])
        self.widget.findChild(QSpinBox, 'workbenchRssiMinimum').setValue(-70)
        self.assertEqual([devices.item(i).checkState() for i in range(2)], [Qt.Unchecked, Qt.Unchecked])

    def test_rssi_empty_or_forced_hidden_selection_does_not_connect(self):
        self.widget = self.api.create(self.parent, Path(self.folder.name))
        self.publish_device(1, -80)
        button = self.widget.findChild(QPushButton, 'connectSelectedDevice')
        self.assertFalse(button.isEnabled())
        devices = self.widget.findChild(QListWidget, 'bleDevices')
        devices.setCurrentRow(0)  # A stale/programmatic selection must be rejected too.
        window = self.widget.findChild(QMainWindow)
        QMetaObject.invokeMethod(window, 'onConnect', Qt.DirectConnection)
        self.assertIn('可见', self.widget.findChild(QLabel, 'bleConnectionStatus').text())
        self.assertFalse(self.api.state(self.widget)['connected'])
        self.assertFalse(self.api.state(self.widget)['busy'])

    def test_rssi_preferences_default_and_saved_values(self):
        self.widget = self.api.create(self.parent, Path(self.folder.name))
        enabled = self.widget.findChild(QCheckBox, 'workbenchRssiEnabled')
        minimum = self.widget.findChild(QSpinBox, 'workbenchRssiMinimum')
        self.assertTrue(enabled.isChecked())
        self.assertEqual(minimum.value(), -60)
        self.assertEqual((minimum.minimum(), minimum.maximum()), (-127, -1))
        minimum.setValue(-75)
        enabled.setChecked(False)
        self.assertFalse(minimum.isEnabled())
        self.assertEqual(QSettings().value('rttWorkbench/rssiMinimum', type=int), -75)
        self.assertFalse(QSettings().value('rttWorkbench/rssiEnabled', type=bool))
        self.publish_device(1, -90)
        self.publish_device(2, 0)
        devices = self.widget.findChild(QListWidget, 'bleDevices')
        self.assertFalse(devices.item(0).isHidden() or devices.item(1).isHidden())

    def test_rssi_preexisting_preferences_load_on_creation(self):
        settings = QSettings(QSettings.IniFormat, QSettings.UserScope, 'SC201', 'SC201')
        settings.setValue('rttWorkbench/rssiEnabled', False)
        settings.setValue('rttWorkbench/rssiMinimum', -82)
        settings.sync()
        self.widget = self.api.create(self.parent, Path(self.folder.name))
        self.assertFalse(self.widget.findChild(QCheckBox, 'workbenchRssiEnabled').isChecked())
        self.assertEqual(self.widget.findChild(QSpinBox, 'workbenchRssiMinimum').value(), -82)


class HostIntegrationTests(unittest.TestCase):
    def test_new_tab_and_existing_ble_workspaces_exclude_each_other(self):
        from rtt_t2 import MainWindow
        from config_manager import DEFAULT_CONFIG
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'LOCALAPPDATA': folder}), \
                patch.object(MainWindow, 'start_hw_thread'):
            window = MainWindow(copy.deepcopy(DEFAULT_CONFIG))
            native = FakeNative()
            page = window.sc201_workbench_page
            page.native_factory = lambda: native
            page.directory = Path(folder) / 'workbench'
            try:
                window.ble_dfu_page.connected = True
                window.workspace_tabs.setCurrentWidget(page)
                self.assertFalse(native.created)
                window.ble_dfu_page.connected = False
                window._on_workspace_changed()
                self.assertTrue(native.created)
                self.assertFalse(window.ble_dfu_page.isEnabled())
                self.assertFalse(window.factory_pair_page.isEnabled())
                self.assertFalse(window.resource_send_btn.isEnabled())
                self.assertTrue(window.log_text.isEnabled())
                native.status['connected'] = True
                page.refresh_state()
                window.workspace_tabs.setCurrentIndex(0)
                self.assertFalse(window.ble_dfu_page.isEnabled())
                self.assertIn('SC201', window.ble_upgrade_guard())
                self.assertIn('SC201', window.factory_pair_guard())
                native.status['connected'] = False
                page.refresh_state()
                self.assertTrue(window.ble_dfu_page.isEnabled())
                self.assertTrue(window.resource_send_btn.isEnabled())
            finally:
                page.request_shutdown()
                window.ble_dfu_page.closed.disconnect(window.close)
                window.ble_dfu_page._finish_shutdown()
                window.factory_pair_page.worker.stop()
                sip.delete(window)
                APP.processEvents()


if __name__ == '__main__':
    unittest.main()
