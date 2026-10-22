"""Persistent BLE connection and upgrade workflow for desktop front ends.

All methods and callbacks run on ONE asyncio loop, outside the GUI thread.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
from pathlib import Path
import time

from .ble import BleTransport, doctor, scan, verify_version
from .package import DfuError, FirmwarePackage
from .protocol import DfuSession, TransferOptions

LOG = logging.getLogger("onmicro_dfu.controller")


class DfuController:
    def __init__(self, emit):
        self.emit = emit
        self.connection = None
        self.info = None
        self.monitor = None
        self.critical = False
        self.upgrading = False

    async def environment(self):
        result = await doctor()
        self.emit("environment", result)
        return result

    async def scan_devices(self, timeout=8.0):
        devices = await scan(timeout)
        self.emit("devices", devices)
        LOG.info("扫描完成：%d 个设备", len(devices))
        return devices

    async def connect(self, address, address_type="public"):
        await self.disconnect()
        connection = BleTransport(address, address_type=address_type)
        try:
            await connection.__aenter__()
            info = await connection.information()
        except BaseException:
            await connection.__aexit__(None, None, None)
            raise
        self.connection = connection
        self.info = dict(info, mtu=connection.client.mtu_size,
                         max_packet_size=connection.max_packet_size)
        self.emit("connected", self.info)
        self.monitor = asyncio.create_task(self._watch(connection))
        LOG.info("已连接 %s，软件版本 %s", address, info.get("software_version"))
        return self.info

    async def _watch(self, connection):
        await connection.disconnected.wait()
        if self.connection is connection:
            self.connection = None
            self.emit("disconnected", {"expected": self.upgrading})

    async def query(self):
        if self.connection is None:
            raise DfuError("请先连接设备")
        info = await self.connection.information()
        self.info = dict(info, mtu=self.connection.client.mtu_size,
                         max_packet_size=self.connection.max_packet_size)
        self.emit("connected", self.info)
        return self.info

    async def disconnect(self):
        connection = self.connection
        self.connection = None
        if self.monitor is not None:
            self.monitor.cancel()
            with suppress(asyncio.CancelledError):
                await self.monitor
            self.monitor = None
        if connection is not None:
            await connection.__aexit__(None, None, None)
            self.emit("disconnected", {"expected": self.upgrading})

    async def upgrade(self, path: Path, target: str, options: TransferOptions,
                      *, expanded=False, allow_same=False, expected_sha256=None):
        package = FirmwarePackage.load(path, expanded=expanded)
        if expected_sha256 is not None and package.sha256 != expected_sha256:
            raise DfuError("固件包在批量任务期间已变化，停止本台升级")
        if not target or (package.version is not None and package.version != target):
            raise DfuError("目标版本与固件包不一致，请重新检查固件")
        connection = self.connection
        if connection is None or not connection.client.is_connected:
            raise DfuError("设备未连接，请先连接并查询")
        # Re-query immediately before starting, rather than trusting UI state.
        info = await connection.information()
        if (info["rom_id"] != f"0x{package.rom_id:08X}" or
                info["execution_address"] != "0x00404000" or
                info["boot_marker"] != "0x46533252"):
            raise DfuError("设备 ROM ID 或启动布局与固件包不匹配")
        if info["software_version"] == target and not allow_same:
            raise DfuError("设备已是目标版本。如需重装，请勾选“允许重装同版本”")
        address = connection.address
        address_type = connection.address_type
        self.upgrading = True
        self.emit("phase", {"name": "transfer", "critical": False})
        started = time.monotonic()
        last_update = 0.0

        def progress(done, total):
            nonlocal last_update
            now = time.monotonic()
            if now - last_update >= 0.15 or done in (0, total):
                last_update = now
                elapsed = max(0.001, now - started)
                self.emit("progress", {"done": done, "total": total,
                          "percent": done * 100 / total, "elapsed": elapsed,
                          "speed": done / 1024 / elapsed})

        def committing():
            self.critical = True
            self.emit("phase", {"name": "commit", "critical": True})

        try:
            LOG.info("开始升级 %s → %s；包 SHA-256=%s", info.get("software_version"),
                     target, package.sha256)
            session = DfuSession(connection, options, progress, committing)
            await session.transfer(package)
            self.emit("phase", {"name": "reboot", "critical": True})
            await connection.wait_for_reboot(20.0)
            self.emit("phase", {"name": "verify", "critical": True})
            version = await verify_version(address, target, 90.0, address_type)
            result = {"address": address, "version": version,
                      "elapsed": time.monotonic() - started}
            self.emit("upgrade_success", result)
            LOG.info("升级成功：重启后已确认版本 %s，耗时 %.1fs", version, result["elapsed"])
            return result
        finally:
            try:
                owned = self.connection is connection
                await self.disconnect()
                # A disconnect monitor can already have cleared the owner.
                if not owned:
                    await connection.__aexit__(None, None, None)
            finally:
                self.critical = False
                self.upgrading = False

    async def shutdown(self):
        await self.disconnect()
