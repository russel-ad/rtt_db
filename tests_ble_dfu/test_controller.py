import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from onmicro_dfu.controller import DfuController
from onmicro_dfu.package import DfuError
from onmicro_dfu.protocol import CommitUncertain, TransferOptions
from test_dfu import Receiver, make_zip, image_bytes


class Connection(Receiver):
    def __init__(self, *_, software="V001_003", **__):
        super().__init__()
        self.software = software
        self.client = SimpleNamespace(is_connected=True, mtu_size=131)
        self.address = "AA:BB:CC:DD:EE:FF"
        self.address_type = "public"
        self.disconnected = asyncio.Event()
        self.closed = 0

    async def __aenter__(self): return self
    async def __aexit__(self, *_): self.closed += 1
    async def information(self):
        return {"address": self.address, "software_version": self.software,
                "rom_id": "0x6626A100", "execution_address": "0x00404000",
                "boot_marker": "0x46533252"}
    async def wait_for_reboot(self, timeout):
        self.disconnected.set()
        await asyncio.sleep(0)


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "firmware.zip"
        make_zip(self.path)
        self.events = []
        self.controller = DfuController(lambda kind, value: self.events.append((kind, value)))
        self.addAsyncCleanup(self.controller.shutdown)

    async def connect(self, connection):
        with patch("onmicro_dfu.controller.BleTransport", return_value=connection):
            await self.controller.connect(connection.address)

    async def test_persistent_connect_query_and_disconnect(self):
        connection = Connection()
        await self.connect(connection)
        self.assertIs(self.controller.connection, connection)
        self.assertEqual(connection.closed, 0)
        self.assertEqual((await self.controller.query())["software_version"], "V001_003")
        await self.controller.disconnect()
        self.assertEqual(connection.closed, 1)
        self.assertIsNone(self.controller.connection)

    async def test_failed_information_does_not_leak_connection(self):
        connection = Connection()
        connection.information = AsyncMock(side_effect=DfuError("query failed"))
        with self.assertRaises(DfuError):
            await self.connect(connection)
        self.assertEqual(connection.closed, 1)

    async def test_external_disconnect_updates_owner_and_gui(self):
        connection = Connection()
        await self.connect(connection)
        connection.disconnected.set()
        await asyncio.sleep(0)
        self.assertIsNone(self.controller.connection)
        self.assertIn(("disconnected", {"expected": False}), self.events)

    async def test_upgrade_commit_barrier_and_version_confirmation(self):
        connection = Connection()
        await self.connect(connection)
        async def verified(*_):
            self.assertTrue(self.controller.critical)
            self.assertTrue(connection.committed)
            self.assertFalse(any(kind == "upgrade_success" for kind, _ in self.events))
            return "V001_004"
        with patch("onmicro_dfu.controller.verify_version", verified):
            result = await self.controller.upgrade(self.path, "V001_004", TransferOptions())
        self.assertEqual(result["version"], "V001_004")
        self.assertEqual([value["name"] for kind, value in self.events if kind == "phase"],
                         ["transfer", "commit", "reboot", "verify"])
        self.assertEqual(connection.closed, 1)
        self.assertFalse(self.controller.critical)
        self.assertIsNone(self.controller.connection)

    async def test_same_version_and_invalid_target_never_write(self):
        connection = Connection(software="V001_004")
        await self.connect(connection)
        for target in ("V001_004", "V001_005", ""):
            with self.subTest(target=target), self.assertRaises(DfuError):
                await self.controller.upgrade(self.path, target, TransferOptions())
        self.assertFalse(connection.commands)

    async def test_crc_failure_disconnects_and_releases_barrier(self):
        connection = Connection()
        connection.bad_crc = True
        await self.connect(connection)
        with self.assertRaises(DfuError):
            await self.controller.upgrade(self.path, "V001_004", TransferOptions())
        self.assertFalse(connection.committed)
        self.assertFalse(self.controller.critical)
        self.assertEqual(connection.closed, 1)

    async def test_lost_execute_ack_is_not_success(self):
        connection = Connection()
        connection.final = "lost_ack"
        await self.connect(connection)
        with self.assertRaises(CommitUncertain):
            await self.controller.upgrade(self.path, "V001_004", TransferOptions())
        self.assertTrue(connection.committed)
        self.assertFalse(any(kind == "upgrade_success" for kind, _ in self.events))
        self.assertFalse(self.controller.critical)

    async def test_wrong_reboot_version_never_reports_success(self):
        connection = Connection()
        await self.connect(connection)
        with patch("onmicro_dfu.controller.verify_version", AsyncMock(side_effect=DfuError("wrong version"))):
            with self.assertRaises(DfuError):
                await self.controller.upgrade(self.path, "V001_004", TransferOptions())
        self.assertFalse(any(kind == "upgrade_success" for kind, _ in self.events))

    async def test_changed_package_rejected_before_device_writes(self):
        from onmicro_dfu.package import FirmwarePackage
        original_hash = FirmwarePackage.load(self.path).sha256
        make_zip(self.path, image=image_bytes(size=5000))
        connection = Connection()
        await self.connect(connection)
        with self.assertRaisesRegex(DfuError, "已变化"):
            await self.controller.upgrade(self.path, "V001_004", TransferOptions(), expected_sha256=original_hash)
        self.assertFalse(connection.commands)


if __name__ == "__main__":
    unittest.main()
