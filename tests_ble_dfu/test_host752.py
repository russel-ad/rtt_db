"""Host layout boundaries and real receiver checks; no radio or device access."""
import asyncio
import ctypes as C
import hashlib
import os
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import zipfile
import zlib

from onmicro_dfu.package import DfuError, FirmwarePackage, HOST_LAYOUT
from onmicro_dfu.protocol import DfuSession, TransferOptions
from onmicro_dfu.controller import DfuController
from onmicro_dfu import ble

WORK = Path(os.environ.get('ONMICRO_HOST_VALIDATION_DIR',
                         Path(__file__).resolve().parents[1] / 'build/host752_validation'))
PACKAGE = next((WORK / "src/build/app_build_r3/release").glob("*.onmicro.zip"), None)
requires_host_fixture = unittest.skipUnless(PACKAGE is not None,
    'Set ONMICRO_HOST_VALIDATION_DIR to the isolated host candidate validation directory')


def host_info(counter=1):
    return {"layout": HOST_LAYOUT, "execution_address": "0x00409000", "release_counter": counter,
            "software_version": "V001_004", "rom_id": "未提供（主机DFU）", "boot_marker": None}


@requires_host_fixture
class HostPackageTests(unittest.TestCase):
    def setUp(self):
        self.package = FirmwarePackage.load(PACKAGE, expanded=HOST_LAYOUT)

    def test_real_release_and_newer_counter(self):
        p = self.package
        self.assertEqual((p.model, p.hardware, p.release_counter, p.version),
                         ("SC200-D", "DFT-SC20X-HUB", 2, "V001_005"))
        p.validate_device(host_info())

    def test_host_package_refused_in_both_scale_profiles(self):
        for profile in (False, True):
            with self.assertRaises(DfuError): FirmwarePackage.load(PACKAGE, expanded=profile)

    def test_scale_package_refused_in_host_profile_and_host_device(self):
        from test_dfu import make_zip
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "scale.zip"
            make_zip(path)
            with self.assertRaises(DfuError): FirmwarePackage.load(path, expanded=HOST_LAYOUT)
            with self.assertRaises(DfuError): FirmwarePackage.load(path).validate_device(host_info())

    def test_host_package_refuses_scale_unknown_and_invalid_counter(self):
        for info in ({}, {"execution_address": "0x00404000"}, host_info(0), host_info(-1), host_info(True), host_info(0xffffffff)):
            with self.assertRaises(DfuError): self.package.validate_device(info)

    def test_host_package_allows_upgrade_equal_and_lower_counter(self):
        for current in (1, 2, 3):
            with self.subTest(current=current): self.package.validate_device(host_info(current))

    def test_native_dat_cannot_smuggle_crc_or_other_image_types(self):
        for offset in (4, 6, 8, 10, 12, 14, 16, 20):
            dat = bytearray(self.package.command);dat[offset] ^= 1
            with self.assertRaises(DfuError): FirmwarePackage.host_image(bytes(dat), self.package.image)

    def test_payload_footer_vectors_and_native_identity_corruption(self):
        for offset in (0, 4, 512, 600, len(self.package.image)-256, len(self.package.image)-1):
            image = bytearray(self.package.image);image[offset] ^= 1
            with self.assertRaises(DfuError): FirmwarePackage.host_image(self.package.command, bytes(image))

    def test_valid_hash_cannot_replace_model_or_hw_check(self):
        for old, new in ((b"SC200-D", b"XC200-D"), (b"DFT-SC20X-HUB", b"DFT-SC20X-XXX")):
            raw = self.package.image[:-256].replace(old, new)
            footer = bytearray(self.package.image[-256:])
            struct.pack_into("<I", footer, 36, zlib.crc32(raw));footer[40:72] = hashlib.sha256(raw).digest()
            struct.pack_into("<I", footer, 252, zlib.crc32(footer[:252]))
            with self.assertRaises(DfuError): FirmwarePackage.host_image(self.package.command, raw + footer)


@requires_host_fixture
class HostTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_information_uses_binary_app_query_and_real_counter(self):
        t = ble.BleTransport("AA:BB:CC:DD:EE:01")
        t.client = SimpleNamespace(read_gatt_char=AsyncMock(return_value=b"V001_004"))
        t.query = AsyncMock(side_effect=[(0, 0, 0), (0x409000, 606984, 1)])
        info = await t.information()
        self.assertEqual(info["layout"], HOST_LAYOUT)
        self.assertEqual(info["release_counter"], 1)
        self.assertEqual(t.query.await_args_list[1].args, (b"\x00",))

    async def test_zero_unknown_or_invalid_counter_is_not_native_device(self):
        for native in ((0, 0, 0), (0x404000, 600000, 1), (0x409000, 600000, 0), (0x409000, 800000, 1)):
            t = ble.BleTransport("AA:BB:CC:DD:EE:01")
            t.client = SimpleNamespace(read_gatt_char=AsyncMock(return_value=b"V001_004"))
            t.query = AsyncMock(side_effect=[(0, 0, 0), native])
            with self.assertRaises(DfuError): await t.information()

    async def test_real_old_receiver_accepts_current_package_with_new_tool_sender(self):
        package = FirmwarePackage.load(PACKAGE, expanded=HOST_LAYOUT)
        lib = C.CDLL(str(WORK / "native_model.dll"))
        lib.model_reset.argtypes = [C.c_char_p] * 3
        for name in ("model_ctrl", "model_data"):
            getattr(lib, name).argtypes = [C.POINTER(C.c_uint8), C.c_uint32, C.POINTER(C.c_uint8)]
        lib.model_status.argtypes = [C.POINTER(C.c_uint32)]
        root = WORK.parents[1]
        baseline = next((root / "build/ota_sc20x_hub_c1_20261019_r1/native_hub_r1/release").glob("*.ota.bin"))
        image = next((WORK / "src/build/app_build_r3/release").glob("*.ota.bin"))
        loader = root / "build/merge_npl_into_newui_20261020_r1/loader_audit_r1/loader/out/nr4_loader.bin"
        lib.model_reset(*[str(p).encode() for p in (baseline, image, loader)])
        class Transport:
            max_packet_size = 244
            def __init__(self): self.q = asyncio.Queue()
            def invoke(self, name, data):
                output = (C.c_uint8 * 32)()
                count = getattr(lib, name)((C.c_uint8 * len(data)).from_buffer_copy(data), len(data), output)
                if count: self.q.put_nowait(bytes(output[:count]))
            async def write_control(self, data): self.invoke("model_ctrl", data)
            async def write_data(self, data): self.invoke("model_data", data)
            async def receive(self, timeout): return await asyncio.wait_for(self.q.get(), timeout)
        await DfuSession(Transport(), TransferOptions(chunk_size=128, prn=8)).transfer(package)
        st = (C.c_uint32 * 10)();lib.model_status(st)
        self.assertEqual(list(st[:6]), [1, 2, 1, 1, 1, 1])

    async def test_controller_post_reboot_verification_includes_counter_and_size(self):
        from test_dfu import Receiver
        class Connection(Receiver):
            def __init__(self):
                super().__init__()
                self.client = SimpleNamespace(is_connected=True)
                self.address = "AA:BB:CC:DD:EE:01";self.address_type = "public"
                self.disconnected = asyncio.Event()
            async def information(self): return host_info()
            async def wait_for_reboot(self, timeout): pass
            async def __aexit__(self, *_): pass
        c = DfuController(lambda *_: None)
        c.connection = Connection()
        verified = AsyncMock(return_value="V001_005")
        with patch("onmicro_dfu.controller.verify_version", verified):
            result = await c.upgrade(PACKAGE, "V001_005", TransferOptions(), expanded=HOST_LAYOUT)
        self.assertEqual(result["version"], "V001_005")
        self.assertEqual(verified.await_args.kwargs,
                         {"expected_counter": 2, "expected_bytes": len(FirmwarePackage.load(PACKAGE, expanded=HOST_LAYOUT).image)})

    async def test_native_verify_requires_installed_counter_and_image_size(self):
        size = len(FirmwarePackage.load(PACKAGE, expanded=HOST_LAYOUT).image)
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.read_gatt_char.return_value = b"V001_005"
        query_char = object()
        client.services = SimpleNamespace(get_service=lambda _: SimpleNamespace(get_characteristic=lambda _: query_char))
        callback = None
        async def notify(_char, cb):
            nonlocal callback
            callback = cb
        async def write(_char, data, **_): callback(None, struct.pack("<III", 0x409000, size, 2))
        client.start_notify.side_effect = notify;client.write_gatt_char.side_effect = write
        with patch("onmicro_dfu.ble.bleak_classes", return_value=(Mock(return_value=client), None)):
            self.assertEqual(await ble.verify_version("AA", "V001_005", 1, expected_counter=2, expected_bytes=size), "V001_005")
        client.stop_notify.assert_awaited_once_with(query_char)

    async def test_matching_display_version_with_wrong_installed_counter_is_failure(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.read_gatt_char.return_value = b"V001_005"
        characteristic = object()
        client.services = SimpleNamespace(get_service=lambda _: SimpleNamespace(get_characteristic=lambda _: characteristic))
        callback = None
        async def notify(_char, cb):
            nonlocal callback
            callback = cb
        async def write(*_, **__): callback(None, struct.pack("<III", 0x409000, 619748, 1))
        client.start_notify.side_effect = notify;client.write_gatt_char.side_effect = write
        with patch("onmicro_dfu.ble.bleak_classes", return_value=(Mock(return_value=client), None)):
            with self.assertRaises(DfuError):
                await ble.verify_version("AA", "V001_005", 0.03, expected_counter=2, expected_bytes=619748)

    async def test_equal_and_lower_counter_run_full_transfer_and_verify(self):
        from test_dfu import Receiver
        for current in (2, 3):
            class Connection(Receiver):
                def __init__(self):
                    super().__init__()
                    self.client = SimpleNamespace(is_connected=True)
                    self.address = "AA:BB:CC:DD:EE:01";self.address_type = "public"
                async def information(self): return dict(host_info(current), software_version="V001_005")
                async def wait_for_reboot(self, timeout): pass
                async def __aexit__(self, *_): pass
            connection = Connection()
            c = DfuController(lambda *_: None);c.connection = connection
            # Same displayed version still requires the explicit UI checkbox.
            with self.assertRaises(DfuError):
                await c.upgrade(PACKAGE, "V001_005", TransferOptions(), expanded=HOST_LAYOUT)
            self.assertFalse(connection.commands)
            verified = AsyncMock(return_value="V001_005")
            with patch("onmicro_dfu.controller.verify_version", verified):
                await c.upgrade(PACKAGE, "V001_005", TransferOptions(), expanded=HOST_LAYOUT, allow_same=True)
            self.assertTrue(connection.committed)
            self.assertEqual(verified.await_args.kwargs, {"expected_counter": 2,
                "expected_bytes": len(FirmwarePackage.load(PACKAGE, expanded=HOST_LAYOUT).image)})


@requires_host_fixture
class HostPanelTests(unittest.TestCase):
    def test_native_profile_and_rssi_are_available_without_connection(self):
        from test_panel import APP, FakeWorker
        from ble_dfu_panel import BleDfuWidget
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        with patch.dict(os.environ, {"LOCALAPPDATA": folder.name}):
            panel = BleDfuWidget(worker_factory=FakeWorker)
        try:
            self.assertEqual(panel.layout_combo.count(), 3)
            panel.layout_combo.setCurrentIndex(2)
            panel.load_package(PACKAGE)
            self.assertIsNotNone(panel.package)
            self.assertEqual(panel.version_input.text(), "V001_005")
            self.assertIn("安装计数 2", panel.package_info.text())
            self.assertFalse(panel.start_btn.isEnabled())
            panel.on_event("devices", [{"address": "AA:BB:CC:DD:EE:01", "name": "SC20X-HUB01", "rssi": -40},
                                       {"address": "AA:BB:CC:DD:EE:02", "name": "Motion mini", "rssi": -90}])
            panel.rssi_input.setValue(-70);panel.rssi_filter.setChecked(True)
            self.assertEqual(panel.device_tree.topLevelItemCount(), 1)
        finally:
            panel._finish_shutdown()
            panel.deleteLater()
            APP.processEvents()
