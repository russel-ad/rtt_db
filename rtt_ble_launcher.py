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
    parser.add_argument("--layout", choices=("legacy", "expanded", "host752"), default="legacy")
    parser.add_argument("--check-bluetooth", action="store_true")
    parser.add_argument("--batch-preview", action="store_true")
    parser.add_argument("--factory-preview", action="store_true")
    parser.add_argument("--workbench-preview", action="store_true")
    parser.add_argument("--workbench-firmware-preview", action="store_true")
    parser.add_argument("--workbench-data-dir", type=Path)
    parser.add_argument("--rssi-min", type=int)
    parser.add_argument("--width", type=int, default=1380)
    parser.add_argument("--height", type=int, default=1040)
    parser.add_argument("--without-jlink-dll", action="store_true")
    parser.add_argument("--app-update-preview", action="store_true")
    args = parser.parse_args()

    class SmokeWindow(MainWindow):
        def start_hw_thread(self):
            pass  # No RTT/serial I/O or config writes during smoke testing.

        def closeEvent(self, event):
            if (self.sc201_workbench_page.request_shutdown() and
                    self.ble_dfu_page.request_shutdown() and self.factory_pair_page.request_shutdown()):
                event.accept()
            else:
                event.ignore()

    app = QApplication([sys.argv[0]])
    app.setFont(QFont("Microsoft YaHei UI", 9))
    window = SmokeWindow(copy.deepcopy(DEFAULT_CONFIG))
    window.setAttribute(Qt.WA_DontShowOnScreen, True)
    window.resize(args.width, args.height)
    window.workspace_tabs.setCurrentIndex(1)
    if args.workbench_preview or args.workbench_firmware_preview:
        from PyQt5.QtCore import QSettings
        data_dir = (args.workbench_data_dir or args.ui_self_test.parent / 'workbench_test_data').resolve()
        QSettings.setDefaultFormat(QSettings.IniFormat)
        QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(data_dir / 'settings'))
        window.sc201_workbench_page.directory = data_dir
        window.sc201_workbench_page.directory_input.setText(str(data_dir))
        window.workspace_tabs.setCurrentWidget(window.sc201_workbench_page)
        if args.workbench_firmware_preview and window.sc201_workbench_page.workbench:
            from PyQt5.QtWidgets import QListWidget
            window.sc201_workbench_page.workbench.findChild(QListWidget, 'workspaceNavigation').setCurrentRow(1)
    if args.factory_preview:
        window.workspace_tabs.setCurrentIndex(2)
        page=window.factory_pair_page
        page.show_inventory({'host':{'sn':'M21010000A01A80','ble':'cbdf08700000','model':'SC200-D','fw':'V001_004'},
            'live':[{'sn':'M21010000A01R88','ble':'f19533480784','model':2,'docked':False},
                    {'sn':'M21010000A0111V','ble':'f74f81fa8483','model':2,'docked':True}],
            'saved':[],'result':{'record':0}})
        page.note('界面预览（模拟数据）：未扫描、连接或写入真实设备')
    window.ble_dfu_page.layout_combo.setCurrentIndex({"legacy": 0, "expanded": 1, "host752": 2}[args.layout])
    jlink_report = None
    if args.without_jlink_dll:
        # Fault injection only: no DLL, probe, or target device is touched.
        from bds import bds_jlink
        calls = []
        def missing_dll(*_, **__):
            calls.append(True)
            raise TypeError('Expected to be given a valid DLL.')
        bds_jlink.pylink.JLink = missing_dll
        errors = []
        window.hw_obj = bds_jlink.BDS_Jlink(errors.append, errors.append)
        startup_calls = len(calls)
        window.hw_obj.hw_read()
        window.hw_obj.hw_close()
        opened = window.hw_obj.hw_open(reset_flag=False)
        jlink_report = {'simulated_missing_dll': True, 'dll_load_calls_at_startup': startup_calls,
                        'rtt_open_succeeded': opened, 'rtt_error': window.hw_obj.last_error,
                        'application_still_running': True}
    if args.package:
        window.ble_dfu_page.load_package(args.package)
    if args.rssi_min is not None:
        window.ble_dfu_page.rssi_input.setValue(args.rssi_min)
        window.ble_dfu_page.rssi_filter.setChecked(True)
    if args.batch_preview:
        # Demonstration events only: no scan, connection, or device writes.
        panel = window.ble_dfu_page
        device_prefix = "SC200-D HUB" if args.layout == "host752" else "Motion mini"
        panel.on_event("devices", [{"name": f"{device_prefix} {i}",
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
    update_preview = None
    if args.app_update_preview:
        from app_update.core import atomic_json
        from app_update.qt_client import AutoUpdater
        preview_dir = args.ui_self_test.parent / 'app_update_preview'
        preview_dir.mkdir(parents=True, exist_ok=True)
        atomic_json(preview_dir/'updater.json', {'enabled': False})
        os.environ['RTT_UPDATE_ROOT'] = str(preview_dir)
        os.environ['RTT_UPDATE_HOME'] = str(preview_dir/'updates')
        update_preview = AutoUpdater(window)
        update_preview.on_transfer({'phase': 'download', 'version': '2.10.3',
            'received': 43500000, 'total': 87000000, 'speed': 309248, 'elapsed': 140.7})
        update_preview.set_status('上位机更新：下载进度预览（模拟）')
        update_preview.stop()  # Layout inspection only; no network or automatic restart.
    window.show()
    report = {"tabs": [window.workspace_tabs.tabText(i)
                       for i in range(window.workspace_tabs.count())],
              "package_valid": window.ble_dfu_page.package is not None,
              "upgrade_enabled_without_connection": window.ble_dfu_page.start_btn.isEnabled()}
    report["batch_mode"] = window.ble_dfu_page.batch_mode.isChecked()
    report["concurrency"] = window.ble_dfu_page.concurrency_input.value()
    report["batch_preview_simulated"] = args.batch_preview
    report["factory_preview_simulated"] = args.factory_preview
    if update_preview:
        report['app_update_preview'] = {'simulated': True,
            'value': update_preview.progress_bar.value(), 'percent': update_preview.progress_bar.format(),
            'numbers': update_preview.numbers.text(), 'status': update_preview.label.text()}
    if args.workbench_preview or args.workbench_firmware_preview:
        from PyQt5.QtWidgets import QListWidget, QWidget
        from PyQt5.QtNetwork import QSslSocket
        page = window.sc201_workbench_page
        navigation = page.workbench.findChild(QListWidget, 'workspaceNavigation') if page.workbench else None
        report['sc201_workbench'] = {
            'loaded': page.workbench is not None, 'state': page.state(),
            'pages': [navigation.item(i).text() for i in range(navigation.count())] if navigation else [],
            'https_supported': QSslSocket.supportsSsl(),
            'https_version': QSslSocket.sslLibraryVersionString(),
            'data_directory': str(page.directory), 'load_message': page.notice.text(),
            'firmware_page_visible': bool(navigation and navigation.currentRow() == 1),
            'batch_ota_available': bool(page.workbench and page.workbench.findChild(QWidget, 'batchOtaPanel'))}
    report["rssi_filter_enabled"] = window.ble_dfu_page.rssi_filter.isChecked()
    report["rssi_min_dbm"] = window.ble_dfu_page.rssi_input.value()
    report["visible_devices"] = window.ble_dfu_page.device_tree.topLevelItemCount()
    report["layout_choices"] = [window.ble_dfu_page.layout_combo.itemText(i)
                              for i in range(window.ble_dfu_page.layout_combo.count())]
    report["selected_layout"] = args.layout
    if window.ble_dfu_page.package is not None:
        report["package"] = window.ble_dfu_page.package.describe()
    if jlink_report is not None:
        report["jlink_startup_check"] = jlink_report

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
    if "--factory-check" in sys.argv:
        import argparse
        import json
        from pathlib import Path
        from factory_pair.selfcheck import run
        parser=argparse.ArgumentParser()
        parser.add_argument('--factory-check',type=Path,required=True)
        args=parser.parse_args()
        try: result=run(); code=0
        except Exception as exc: result={'passed':False,'error':str(exc),'hardware_written':False}; code=1
        args.factory_check.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        raise SystemExit(code)
    if "--dfu-check" in sys.argv:
        # Offline check preserves the host-compatible tool's existing entry.
        import argparse
        import json
        from pathlib import Path
        from onmicro_dfu.package import FirmwarePackage
        parser = argparse.ArgumentParser()
        parser.add_argument("--dfu-check", type=Path, required=True)
        parser.add_argument("--layout", choices=("legacy", "expanded", "host752"), default="legacy")
        parser.add_argument("--report", type=Path, required=True)
        parser.add_argument("--device-counter", type=int,
                            help="Offline native device-counter admission check; no connection")
        args = parser.parse_args()
        try:
            package = FirmwarePackage.load(args.dfu_check, expanded="host752" if args.layout == "host752" else args.layout == "expanded")
            if args.device_counter is not None:
                package.validate_device({"layout": "host752", "execution_address": "0x00409000",
                                         "release_counter": args.device_counter})
            result = dict(passed=True, hardware_accessed=False, package=package.describe())
            if args.device_counter is not None:
                result["device_counter"] = args.device_counter
            status = 0
        except Exception as error:
            result = dict(passed=False, hardware_accessed=False, error=str(error))
            status = 1
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        raise SystemExit(status)
    if "--ui-self-test" in sys.argv:
        raise SystemExit(self_test())
    from rtt_t2 import main
    main()
