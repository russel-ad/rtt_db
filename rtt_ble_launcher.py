"""GUI executable entry point; --ui-self-test performs no device writes."""
import multiprocessing
import os
import sys

os.environ.setdefault("PYQTGRAPH_QT_LIB", "PyQt5")


def self_test():
    import argparse
    import copy
    import json
    from pathlib import Path
    from PyQt5.QtCore import QTimer, Qt
    from PyQt5.QtGui import QFont
    from PyQt5.QtWidgets import QApplication
    from config_manager import DEFAULT_CONFIG
    from rtt_t2 import MainWindow

    parser = argparse.ArgumentParser()
    parser.add_argument("--ui-self-test", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--package", type=Path)
    parser.add_argument("--check-bluetooth", action="store_true")
    parser.add_argument("--batch-preview", action="store_true")
    parser.add_argument("--rssi-min", type=int)
    parser.add_argument("--width", type=int, default=1380)
    parser.add_argument("--height", type=int, default=1040)
    args = parser.parse_args()

    class SmokeWindow(MainWindow):
        def start_hw_thread(self):
            pass  # No RTT/serial I/O or config writes during smoke testing.

        def closeEvent(self, event):
            if self.ble_dfu_page.request_shutdown():
                event.accept()
            else:
                event.ignore()

    app = QApplication([sys.argv[0]])
    app.setFont(QFont("Microsoft YaHei UI", 9))
    window = SmokeWindow(copy.deepcopy(DEFAULT_CONFIG))
    window.setAttribute(Qt.WA_DontShowOnScreen, True)
    window.resize(args.width, args.height)
    window.workspace_tabs.setCurrentIndex(1)
    if args.package:
        window.ble_dfu_page.load_package(args.package)
    if args.rssi_min is not None:
        window.ble_dfu_page.rssi_input.setValue(args.rssi_min)
        window.ble_dfu_page.rssi_filter.setChecked(True)
    if args.batch_preview:
        # Demonstration events only: no scan, connection, or device writes.
        panel = window.ble_dfu_page
        panel.on_event("devices", [{"name": f"Motion mini {i}",
            "address": f"AA:BB:CC:DD:EE:{i:02X}", "rssi": -40 - i * 4} for i in range(1, 5)])
        panel.check_visible(True)
        from PyQt5.QtWidgets import QTreeWidgetItem
        for i, (address, name) in enumerate(panel.checked_devices.items()):
            panel.batch_rows[address] = QTreeWidgetItem([f"{name}  {address}", "", "0.0%", "—", "—", ""])
            panel.result_tree.addTopLevelItem(panel.batch_rows[address])
            panel.batch_states[address] = {"status": "queued", "name": name, "done": 0, "speed": 0}
            panel.on_batch_device({"address": address, "kind": "state", "data": {"status": "transfer" if i < 2 else "queued"}})
            if i < 2:
                size = len(panel.package.image) if panel.package else 265124
                percent = 56 - i * 19
                panel.on_batch_device({"address": address, "kind": "progress", "data": {
                    "done": round(size * percent / 100), "total": size,
                    "percent": percent, "speed": 7.2 - i, "elapsed": 20}})
        selected_count = len(panel.checked_devices)
        panel.settings_toggle.setChecked(False)
        panel.set_status(f"界面预览（模拟数据）· {min(2, selected_count)} 台传输中，{max(0, selected_count - 2)} 台排队 · 未连接真实设备")
    window.show()
    report = {"tabs": [window.workspace_tabs.tabText(i)
                       for i in range(window.workspace_tabs.count())],
              "package_valid": window.ble_dfu_page.package is not None,
              "upgrade_enabled_without_connection": window.ble_dfu_page.start_btn.isEnabled()}
    report["batch_mode"] = window.ble_dfu_page.batch_mode.isChecked()
    report["concurrency"] = window.ble_dfu_page.concurrency_input.value()
    report["batch_preview_simulated"] = args.batch_preview
    report["rssi_filter_enabled"] = window.ble_dfu_page.rssi_filter.isChecked()
    report["rssi_min_dbm"] = window.ble_dfu_page.rssi_input.value()
    report["visible_devices"] = window.ble_dfu_page.device_tree.topLevelItemCount()

    def finish():
        from PyQt5.QtCore import QPoint, QRect
        panel = window.ble_dfu_page
        progress_rect = QRect(panel.progress_bar.mapTo(panel, QPoint(0, 0)), panel.progress_bar.size())
        report["progress_visible_in_page"] = panel.progress_bar.isVisible() and panel.rect().contains(progress_rect)
        report["progress_value"] = panel.progress_bar.value()
        report["progress_footer_outside_scroll"] = not panel.body_scroll.isAncestorOf(panel.progress_footer)
        report["window_size"] = [window.width(), window.height()]
        report["device_progress_delegate"] = type(panel.result_tree.itemDelegateForColumn(2)).__name__
        if args.screenshot:
            window.grab().save(str(args.screenshot))
        report["no_hardware_writes"] = True
        args.ui_self_test.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        window.close()

    if args.check_bluetooth:
        def observe(kind, data):
            if kind == "environment":
                report["bluetooth"] = data
            elif kind == "completed" and data["action"] == "environment":
                report["bluetooth_check_status"] = data["status"]
                QTimer.singleShot(250, finish)
        window.ble_dfu_page.bridge.event.connect(observe, Qt.QueuedConnection)
        QTimer.singleShot(200, window.ble_dfu_page.on_environment)
    else:
        QTimer.singleShot(500, finish)
    QTimer.singleShot(15000, app.quit)
    return app.exec_()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    if "--ui-self-test" in sys.argv:
        raise SystemExit(self_test())
    from rtt_t2 import main
    main()
