import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from onmicro_dfu.batch import BatchController
from onmicro_dfu.package import DfuError
from onmicro_dfu.protocol import CommitUncertain, TransferOptions
from test_dfu import make_zip


ADDRESSES = [f"AA:BB:CC:DD:EE:{i:02X}" for i in range(1, 6)]


class BatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "fw.zip"
        make_zip(self.path)
        self.events = []
        self.active = 0
        self.maximum = 0
        self.connected = []
        self.instances = []
        self.modes = {}
        self.release = asyncio.Event()
        owner = self

        class Device:
            def __init__(self, emit):
                self.emit = emit
                self.critical = False
                self.opened = False
                self.address = None
                owner.instances.append(self)

            async def connect(self, address, address_type):
                self.address = address
                self.opened = True
                owner.connected.append(address)
                owner.active += 1
                owner.maximum = max(owner.maximum, owner.active)
                if owner.modes.get(address) == "connect_error":
                    raise DfuError("connect failed")

            async def upgrade(self, path, target, options, **kwargs):
                self.expected_sha = kwargs["expected_sha256"]
                mode = owner.modes.get(self.address)
                if mode == "error":
                    raise DfuError("CRC mismatch")
                self.emit("phase", {"name": "transfer", "critical": False})
                if mode in ("critical", "lost_ack", "verify_error"):
                    self.critical = True
                    self.emit("phase", {"name": "commit", "critical": True})
                try:
                    if mode == "lost_ack":
                        raise CommitUncertain("missing Execute ack")
                    if mode == "verify_error":
                        raise DfuError("wrong reboot version")
                    await owner.release.wait()
                    return {"address": self.address, "version": target, "elapsed": 1.0}
                finally:
                    self.critical = False

            async def shutdown(self):
                # Yield here to expose an early semaphore release bug.
                await asyncio.sleep(0)
                if self.opened:
                    self.opened = False
                    owner.active -= 1

        self.batch = BatchController(lambda k, v: self.events.append((k, v)), controller_factory=Device)

    def start(self, addresses=ADDRESSES, **kwargs):
        task = asyncio.create_task(self.batch.run(addresses, self.path, "V001_004", TransferOptions(), **kwargs))
        self.addAsyncCleanup(self.finish, task)
        return task

    async def finish(self, task):
        self.release.set()
        await task

    async def until(self, predicate):
        async with asyncio.timeout(2):
            while not predicate():
                await asyncio.sleep(0)

    async def test_concurrency_bound_includes_cleanup_and_one_owner_per_device(self):
        task = self.start(concurrency=2)
        await self.until(lambda: len(self.connected) == 2)
        self.assertEqual(self.active, 2)
        self.release.set()
        result = await task
        self.assertEqual(self.maximum, 2)
        self.assertEqual(self.active, 0)
        self.assertEqual(len(self.instances), 5)
        self.assertEqual({v["status"] for v in result["devices"].values()}, {"success"})
        self.assertTrue(all(len(c.expected_sha) == 64 for c in self.instances))

    async def test_concurrency_one_runs_sequentially(self):
        task = self.start(concurrency=1)
        await self.until(lambda: len(self.connected) == 1)
        self.release.set()
        await task
        self.assertEqual(self.maximum, 1)

    async def test_failure_isolated_and_no_automatic_retry(self):
        self.modes[ADDRESSES[0]] = "error"
        self.release.set()
        result = await self.start()
        self.assertEqual(result["devices"][ADDRESSES[0]]["status"], "error")
        self.assertEqual(sum(v["status"] == "success" for v in result["devices"].values()), 4)
        self.assertEqual(self.connected.count(ADDRESSES[0]), 1)

    async def test_connection_failure_releases_slot_and_connection(self):
        self.modes[ADDRESSES[0]] = "connect_error"
        self.release.set()
        result = await self.start(concurrency=1)
        self.assertEqual(result["devices"][ADDRESSES[0]]["status"], "error")
        self.assertEqual(self.active, 0)
        self.assertEqual(self.maximum, 1)

    async def test_stop_preserves_committed_device_cancels_transfer_and_queue(self):
        self.modes[ADDRESSES[0]] = "critical"
        task = self.start(concurrency=2)
        await self.until(lambda: self.batch.critical and len(self.connected) == 2)
        self.batch.cancel()
        self.batch.cancel()  # Repeated stop cannot interrupt cleanup either.
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.release.set()
        result = await task
        self.assertEqual(self.connected, ADDRESSES[:2])
        self.assertEqual(result["devices"][ADDRESSES[0]]["status"], "success")
        self.assertEqual(sum(v["status"] == "cancelled" for v in result["devices"].values()), 4)
        self.assertTrue(result["stopped"])
        self.assertEqual(self.active, 0)

    async def test_stop_every_precommit_device_and_all_pending(self):
        task = self.start(concurrency=2)
        await self.until(lambda: len(self.connected) == 2)
        self.batch.cancel()
        result = await task
        self.assertEqual({v["status"] for v in result["devices"].values()}, {"cancelled"})
        self.assertEqual(self.active, 0)

    async def test_uncertain_commit_and_postcommit_error_never_claim_success(self):
        self.modes[ADDRESSES[0]] = "lost_ack"
        self.modes[ADDRESSES[1]] = "verify_error"
        self.release.set()
        result = await self.start()
        for address in ADDRESSES[:2]:
            self.assertEqual(result["devices"][address]["status"], "uncertain")

    async def test_deduplicate_addresses_case_insensitively(self):
        self.release.set()
        result = await self.start([ADDRESSES[0].lower(), ADDRESSES[0]])
        self.assertEqual(len(result["devices"]), 1)
        self.assertEqual(len(self.connected), 1)

    async def test_preflight_rejects_bad_inputs_before_any_connections(self):
        for addresses, target, concurrency in [([], "V001_004", 2),
                (["wrong"], "V001_004", 2), (ADDRESSES, "wrong", 2), (ADDRESSES, "V001_004", 5)]:
            with self.subTest(addresses=addresses, target=target, concurrency=concurrency), self.assertRaises(DfuError):
                await self.batch.run(addresses, self.path, target, TransferOptions(), concurrency=concurrency)
        self.assertFalse(self.connected)

    async def test_busy_batch_rejected_without_replacing_live_tasks(self):
        task = self.start()
        await self.until(lambda: self.active == 2)
        with self.assertRaises(DfuError):
            await self.batch.run(ADDRESSES, self.path, "V001_004", TransferOptions())
        self.assertEqual(len(self.batch.tasks), 5)
        self.release.set()
        await task

    async def test_real_protocol_sessions_are_isolated_across_parallel_receivers(self):
        from test_controller import Connection
        receivers = {}
        def transport(address, **kwargs):
            receiver = Connection()
            receiver.address = address
            receivers[address] = receiver
            return receiver
        real_batch = BatchController(lambda k, v: self.events.append((k, v)))
        with patch("onmicro_dfu.controller.BleTransport", side_effect=transport), \
             patch("onmicro_dfu.controller.verify_version", AsyncMock(return_value="V001_004")):
            result = await real_batch.run(ADDRESSES[:3], self.path, "V001_004", TransferOptions(), concurrency=2)
        self.assertEqual({v["status"] for v in result["devices"].values()}, {"success"})
        self.assertEqual(len(receivers), 3)
        for receiver in receivers.values():
            self.assertTrue(receiver.committed)
            self.assertEqual(receiver.data_executes, 1)
            self.assertEqual(receiver.closed, 1)
