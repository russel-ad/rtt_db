import asyncio
import copy
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["PYQTGRAPH_QT_LIB"] = "PyQt5"

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QEventLoop, Qt, QPoint, QRect
from ble_dfu_panel import BleDfuWidget, BleWorker, Bridge
from config_manager import DEFAULT_CONFIG
from test_dfu import make_zip


APP = QApplication.instance() or QApplication([])


class FakeWorker:
    def __init__(self, bridge):
        self.bridge = bridge
        self.actions = []
        self.cancelled = False
        self.stopped = False
        async def operation(*_, **__): pass
        self.controller = SimpleNamespace(
            environment=operation, scan_devices=operation, connect=operation,
            query=operation, disconnect=operation, upgrade=operation, shutdown=operation)
        self.upgrade_batch = operation
    def submit(self, action, coroutine):
        self.actions.append(action)
        coroutine.close()
        return True
    def cancel(self): self.cancelled = True
    def stop(self): self.stopped = True


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.folder.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.panel = BleDfuWidget(worker_factory=FakeWorker)
        self.panel.batch_mode.setChecked(False)  # Retain single-device regression coverage.
        self.addCleanup(self.clean_panel)
        self.path = Path(self.folder.name) / "firmware.zip"
        make_zip(self.path)

    def clean_panel(self):
        if not self.panel.is_closed:
            self.panel._finish_shutdown()
        self.panel.deleteLater()
        APP.processEvents()

    def connected(self):
        self.panel.on_event("connected", {"address": "AA:BB:CC:DD:EE:FF",
            "software_version": "V001_003", "rom_id": "0x6626A100", "mtu": 247,
            "max_packet_size": 244})

    def test_requires_connection_and_valid_package(self):
        self.assertFalse(self.panel.start_btn.isEnabled())
        self.panel.load_package(self.path)
        self.assertFalse(self.panel.start_btn.isEnabled())
        self.connected()
        self.assertTrue(self.panel.start_btn.isEnabled())
        self.panel.path_input.setText("changed.zip")
        self.assertFalse(self.panel.start_btn.isEnabled())

    def test_filter_and_select_device_without_touching_radio(self):
        self.panel.on_event("devices", [
            {"name": "Motion mini", "address": "AA:BB:CC:DD:EE:FF", "rssi": -48},
            {"name": "Other", "address": "11:22:33:44:55:66", "rssi": -70}])
        self.panel.filter_input.setText("Motion")
        self.assertEqual(self.panel.device_tree.topLevelItemCount(), 1)
        self.panel.device_tree.setCurrentItem(self.panel.device_tree.topLevelItem(0))
        self.assertEqual(self.panel.address_input.text(), "AA:BB:CC:DD:EE:FF")

    def test_upgrade_disables_settings_and_cannot_cancel_commit(self):
        self.panel.load_package(self.path)
        self.connected()
        self.panel.on_upgrade()
        self.assertTrue(self.panel.busy)
        self.assertFalse(self.panel.path_input.isEnabled())
        self.assertTrue(self.panel.cancel_btn.isEnabled())
        self.panel.on_event("phase", {"name": "commit", "critical": True})
        self.assertFalse(self.panel.cancel_btn.isEnabled())
        self.assertFalse(self.panel.request_shutdown())
        self.assertNotIn("shutdown", self.panel.worker.actions)

    def test_progress_one_hundred_does_not_claim_success(self):
        self.panel.on_event("phase", {"name": "transfer", "critical": False})
        self.panel.on_event("progress", {"done": 100, "total": 100,
            "percent": 100, "speed": 12.0, "elapsed": 1.0})
        self.assertEqual(self.panel.progress_bar.value(), 1000)
        self.assertNotIn("升级成功", self.panel.status_label.text())
        self.panel.on_event("upgrade_success", {"address": "AA", "version": "V001_004", "elapsed": 2.0})
        self.assertIn("升级成功", self.panel.status_label.text())

    def test_resource_transfer_guard_prevents_dfu(self):
        self.panel.upgrade_guard = lambda: "资源传输正在进行"
        self.panel.load_package(self.path)
        self.connected()
        self.panel.on_upgrade()
        self.assertNotIn("upgrade", self.panel.worker.actions)

    def test_shutdown_cancels_background_task_then_disconnects(self):
        self.panel.on_scan()
        self.assertFalse(self.panel.request_shutdown())
        self.assertTrue(self.panel.worker.cancelled)
        self.panel.on_event("completed", {"action": "scan", "status": "cancelled"})
        self.assertEqual(self.panel.worker.actions[-1], "shutdown")
        self.panel.on_event("completed", {"action": "shutdown", "status": "ok"})
        self.assertTrue(self.panel.is_closed)

    def test_failed_operation_restores_controls(self):
        self.panel.on_scan()
        self.panel.on_event("completed", {"action": "scan", "status": "error", "error": "adapter off"})
        self.assertTrue(self.panel.scan_btn.isEnabled())
        self.assertIn("adapter off", self.panel.status_label.text())

    def batch_ready(self):
        self.panel.batch_mode.setChecked(True)
        self.panel.load_package(self.path)
        self.panel.on_event("devices", [
            {"name": "Motion 1", "address": "AA:BB:CC:DD:EE:01", "rssi": -48},
            {"name": "Motion 2", "address": "AA:BB:CC:DD:EE:02", "rssi": -50}])
        self.panel.check_visible(True)

    def test_batch_checks_persist_through_filter_and_can_clear_hidden(self):
        self.batch_ready()
        self.panel.filter_input.setText("Motion 1")
        self.assertEqual(len(self.panel.checked_devices), 2)
        self.assertEqual(self.panel.device_tree.topLevelItemCount(), 1)
        self.assertIn("2 台", self.panel.batch_hint.text())
        self.panel.clear_checked()
        self.assertFalse(self.panel.checked_devices)
        self.assertFalse(self.panel.start_btn.isEnabled())

    def test_batch_starts_without_manual_connect_and_keeps_commit_stop_available(self):
        self.batch_ready()
        self.assertTrue(self.panel.start_btn.isEnabled())
        self.assertFalse(self.panel.connected)
        self.assertEqual(self.panel.concurrency_input.value(), 2)
        self.panel.on_upgrade()
        self.assertEqual(self.panel.worker.actions[-1], "batch")
        self.assertFalse(self.panel.concurrency_input.isEnabled())
        self.panel.on_event("batch_device", {"address": "AA:BB:CC:DD:EE:01", "kind": "phase",
                                             "data": {"name": "commit", "critical": True}})
        self.assertTrue(self.panel.critical)
        self.assertTrue(self.panel.cancel_btn.isEnabled())
        self.panel.on_cancel()
        self.assertTrue(self.panel.worker.cancelled)

    def test_batch_progress_is_independent_and_only_result_confirms_success(self):
        self.batch_ready()
        self.panel.on_upgrade()
        address = "AA:BB:CC:DD:EE:01"
        self.panel.on_event("batch_device", {"address": address, "kind": "progress", "data": {
            "done": len(self.panel.package.image), "total": len(self.panel.package.image),
            "percent": 100, "speed": 10, "elapsed": 1}})
        self.assertNotEqual(self.panel.batch_rows[address].text(1), "升级成功")
        self.assertEqual(self.panel.batch_rows["AA:BB:CC:DD:EE:02"].text(2), "0.0%")
        self.panel.on_event("batch_device", {"address": address, "kind": "result", "data": {
            "status": "success", "version": "V001_004", "elapsed": 2}})
        self.assertEqual(self.panel.batch_rows[address].text(1), "升级成功")
        self.panel.on_event("batch_finished", {"devices": {address: {"status": "success"},
            "AA:BB:CC:DD:EE:02": {"status": "error"}}, "elapsed": 3})
        self.assertIn("成功 1 / 2", self.panel.status_label.text())
        self.panel.on_event("completed", {"action": "batch", "status": "ok"})
        self.assertFalse(self.panel.upgrading)
        self.assertTrue(self.panel.export_results_btn.isEnabled())

    def test_batch_close_waits_for_children_before_shutting_down(self):
        self.batch_ready()
        self.panel.on_upgrade()
        self.panel.critical = True
        self.assertFalse(self.panel.request_shutdown())
        self.assertTrue(self.panel.worker.cancelled)
        self.assertNotIn("shutdown", self.panel.worker.actions)
        self.panel.on_event("completed", {"action": "batch", "status": "ok"})
        self.assertEqual(self.panel.worker.actions[-1], "shutdown")

    def test_batch_csv_export_includes_device_identity_and_result(self):
        self.batch_ready()
        self.panel.on_upgrade()
        address = "AA:BB:CC:DD:EE:01"
        self.panel.on_event("batch_device", {"address": address, "kind": "result", "data": {
            "status": "uncertain", "error": "最终提交结果未确认"}})
        report = Path(self.folder.name) / "results.csv"
        with patch("ble_dfu_panel.QFileDialog.getSaveFileName", return_value=(str(report), "CSV")):
            self.panel.export_results()
        content = report.read_text(encoding="utf-8-sig")
        self.assertIn(address, content)
        self.assertIn("uncertain", content)
        self.assertIn("最终提交结果未确认", content)

    def test_progress_and_stop_remain_visible_when_settings_scroll(self):
        self.panel.setAttribute(Qt.WA_DontShowOnScreen, True)
        self.panel.show()
        for width, height in ((1024, 640), (1366, 768), (1920, 1080)):
            for expanded in (True, False):
                with self.subTest(size=(width, height), settings=expanded):
                    self.panel.settings_toggle.setChecked(expanded)
                    self.panel.resize(width, height)
                    APP.processEvents()
                    self.assertFalse(self.panel.body_scroll.isAncestorOf(self.panel.progress_footer))
                    for widget in (self.panel.progress_bar, self.panel.cancel_btn):
                        rect = QRect(widget.mapTo(self.panel, QPoint(0, 0)), widget.size())
                        self.assertTrue(widget.isVisible())
                        self.assertTrue(self.panel.rect().contains(rect))
                    before = self.panel.progress_bar.mapTo(self.panel, QPoint(0, 0))
                    bar = self.panel.body_scroll.verticalScrollBar()
                    bar.setValue(bar.maximum())
                    APP.processEvents()
                    self.assertEqual(self.panel.progress_bar.mapTo(self.panel, QPoint(0, 0)), before)


class IntegrationTests(unittest.TestCase):
    def test_existing_rtt_ui_is_preserved_in_first_tab(self):
        from rtt_t2 import MainWindow
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"LOCALAPPDATA": folder}), \
                patch.object(MainWindow, "start_hw_thread"), \
                patch("rtt_t2.BleDfuWidget", side_effect=lambda *args, **kwargs:
                      BleDfuWidget(*args, worker_factory=FakeWorker, **kwargs)):
            window = MainWindow(copy.deepcopy(DEFAULT_CONFIG))
            self.assertEqual(window.workspace_tabs.count(), 2)
            self.assertEqual(window.workspace_tabs.tabText(1), "蓝牙 DFU 升级")
            self.assertIs(window.workspace_tabs.widget(1), window.ble_dfu_page)
            self.assertIsNotNone(window.log_text)
            self.assertIsNotNone(window.resource_send_btn)
            window.on_ble_upgrade_active(True)
            self.assertFalse(window.connect_btn.isEnabled())
            self.assertFalse(window.send_btn.isEnabled())
            self.assertTrue(window.log_text.isEnabled())
            window.on_ble_upgrade_active(False)
            self.assertTrue(window.connect_btn.isEnabled())
            # Avoid the pre-existing host closeEvent config writer in this test.
            window.ble_dfu_page.closed.disconnect(window.close)
            window.ble_dfu_page._finish_shutdown()
            window.deleteLater()
            APP.processEvents()


class WorkerTests(unittest.TestCase):
    def pump_until(self, predicate, seconds=2):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            APP.processEvents(QEventLoop.AllEvents, 20)
            time.sleep(0.005)
        self.assertTrue(predicate())

    def test_worker_owns_one_loop_and_shuts_down(self):
        bridge = Bridge()
        events = []
        bridge.event.connect(lambda kind, value: events.append((kind, value)))
        worker = BleWorker(bridge)
        try:
            async def work(): return id(asyncio.get_running_loop())
            worker.submit("one", work())
            self.pump_until(lambda: len(events) == 1)
            worker.submit("two", work())
            self.pump_until(lambda: len(events) == 2)
            self.assertEqual(events[0][1]["result"], events[1][1]["result"])
        finally:
            worker.stop()
        self.assertFalse(worker.thread.is_alive())

    def test_worker_cancels_before_commit_and_reports_completion(self):
        bridge = Bridge()
        events = []
        bridge.event.connect(lambda kind, value: events.append((kind, value)))
        worker = BleWorker(bridge)
        try:
            async def work():
                bridge.event.emit("ready", None)
                await asyncio.sleep(60)
            worker.submit("upgrade", work())
            self.pump_until(lambda: any(kind == "ready" for kind, _ in events))
            worker.cancel()
            self.pump_until(lambda: any(kind == "completed" for kind, _ in events))
            outcome = next(value for kind, value in events if kind == "completed")
            self.assertEqual(outcome["status"], "cancelled")
        finally:
            worker.stop()

    def test_worker_does_not_cancel_a_commit_from_stale_ui_state(self):
        bridge = Bridge()
        events = []
        bridge.event.connect(lambda kind, value: events.append((kind, value)))
        worker = BleWorker(bridge)
        finished = None
        try:
            async def work():
                nonlocal finished
                finished = asyncio.Event()
                worker.controller.critical = True
                bridge.event.emit("ready", None)
                await finished.wait()
                worker.controller.critical = False
            worker.submit("upgrade", work())
            self.pump_until(lambda: any(kind == "ready" for kind, _ in events))
            worker.cancel()
            self.pump_until(lambda: any(kind == "cancel_blocked" for kind, _ in events))
            self.assertFalse(any(kind == "completed" for kind, _ in events))
            worker.loop.call_soon_threadsafe(finished.set)
            self.pump_until(lambda: any(kind == "completed" for kind, _ in events))
        finally:
            worker.stop()

    def test_worker_batch_stop_finishes_children_without_cancelling_parent(self):
        bridge = Bridge()
        events = []
        bridge.event.connect(lambda kind, value: events.append((kind, value)))
        worker = BleWorker(bridge)
        class Device:
            critical = False
            def __init__(self, emit): self.emit = emit
            async def connect(self, *_): pass
            async def upgrade(self, *_, **__):
                self.emit("phase", {"name": "transfer", "critical": False})
                await asyncio.sleep(60)
            async def shutdown(self): pass
        worker.batch.controller_factory = Device
        try:
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "fw.zip"
                make_zip(path)
                from onmicro_dfu.protocol import TransferOptions
                worker.submit("batch", worker.upgrade_batch(["AA:BB:CC:DD:EE:01"], path, "V001_004", TransferOptions()))
                self.pump_until(lambda: any(kind == "batch_device" and data["kind"] == "phase" for kind, data in events))
                worker.cancel()
                self.pump_until(lambda: any(kind == "completed" for kind, _ in events))
                outcome = next(data for kind, data in events if kind == "completed")
                self.assertEqual(outcome["status"], "ok")
                self.assertEqual(outcome["result"]["devices"]["AA:BB:CC:DD:EE:01"]["status"], "cancelled")
        finally:
            worker.stop()

    def test_worker_can_stop_batch_during_preparation(self):
        bridge = Bridge()
        events = []
        bridge.event.connect(lambda kind, value: events.append((kind, value)))
        worker = BleWorker(bridge)
        try:
            async def releasing_previous_connection():
                bridge.event.emit("preparing", None)
                await asyncio.sleep(60)
            worker.controller.disconnect = releasing_previous_connection
            worker.submit("batch", worker.upgrade_batch([], Path("unused"), "", None))
            self.pump_until(lambda: any(kind == "preparing" for kind, _ in events))
            # Cancellation cleanup must not repeat the artificial slow disconnect.
            async def disconnected(): pass
            worker.controller.disconnect = disconnected
            worker.cancel()
            self.pump_until(lambda: any(kind == "completed" for kind, _ in events))
            self.assertFalse(worker.batch.running)
            self.assertEqual(next(d for k, d in events if k == "completed")["status"], "cancelled")
        finally:
            worker.stop()


if __name__ == "__main__":
    unittest.main()
