import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from onmicro_dfu import ble


class NativeDeviceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.addCleanup(ble.DISCOVERED_DEVICES.clear)
        ble.DISCOVERED_DEVICES.clear()

    async def test_scanned_native_devices_retained_without_changing_public_json(self):
        device = SimpleNamespace(address="AA:BB:CC:DD:EE:01", name="Motion")
        advertisement = SimpleNamespace(local_name="Motion", rssi=-40, service_uuids=[ble.SERVICE])
        scanner = SimpleNamespace(discover=AsyncMock(return_value={"one": (device, advertisement)}))
        with patch("onmicro_dfu.ble.bleak_classes", return_value=(None, scanner)):
            result = await ble.scan(0.01)
        self.assertIs(ble.DISCOVERED_DEVICES[device.address], device)
        self.assertIn(device.address, json.dumps(result))

    async def test_reboot_query_reuses_native_device_without_implicit_scan(self):
        address = "AA:BB:CC:DD:EE:01"
        device = object()
        ble.DISCOVERED_DEVICES[address] = device
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.read_gatt_char.return_value = b"V001_004"
        from unittest.mock import Mock
        constructor = Mock(return_value=client)
        with patch("onmicro_dfu.ble.bleak_classes", return_value=(constructor, None)):
            self.assertEqual(await ble.verify_version(address, "V001_004", 1), "V001_004")
        self.assertIs(constructor.call_args.args[0], device)
