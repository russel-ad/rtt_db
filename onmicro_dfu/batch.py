"""Bounded batch DFU; independent devices share one owning asyncio loop."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from contextlib import suppress
import logging
from pathlib import Path
import re
import time

from .controller import DfuController
from .package import DfuError, FirmwarePackage
from .protocol import CommitUncertain

LOG = logging.getLogger("onmicro_dfu.batch")
DEVICE_CONTEXT = ContextVar("dfu_device_address", default="")


class DeviceLogFilter(logging.Filter):
    def filter(self, record):
        address = DEVICE_CONTEXT.get()
        record.device_prefix = f"[{address}] " if address else ""
        return True


class BatchController:
    def __init__(self, emit, *, controller_factory=DfuController):
        self.emit = emit
        self.controller_factory = controller_factory
        self.running = False
        self.stopping = False
        self.tasks = {}
        self.controllers = {}
        self.results = {}
        self.finishing = set()

    @property
    def critical(self):
        return any(controller.critical for controller in self.controllers.values())

    def device_event(self, address, kind, data):
        self.emit("batch_device", {"address": address, "kind": kind, "data": data})

    def cancel(self):
        """Called ONLY on the owning loop. Committed devices keep verifying."""
        self.stopping = True
        for address, task in self.tasks.items():
            controller = self.controllers.get(address)
            if (not task.done() and not task.cancelling() and address not in self.finishing
                    and not (controller and controller.critical)):
                task.cancel()
        self.emit("batch_stopping", {"critical": self.critical})

    async def run(self, addresses, path: Path, target, options, *, concurrency=2,
                  expanded=False, allow_same=False, address_type="public"):
        if self.running:
            raise DfuError("批量升级正在运行")
        if not isinstance(concurrency, int) or not 1 <= concurrency <= 4:
            raise DfuError("并发数必须为 1～4")
        addresses = list(dict.fromkeys(str(address).strip().upper() for address in addresses))
        if not addresses or any(not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", a) for a in addresses):
            raise DfuError("请选择设备，且设备地址应为 AA:BB:CC:DD:EE:FF 格式")
        package = FirmwarePackage.load(Path(path), expanded=expanded)
        if not target or (package.version is not None and package.version != target):
            raise DfuError("目标版本与固件包不一致")
        self.running = True
        self.stopping = False
        self.tasks = {}
        self.controllers = {}
        self.results = {}
        self.finishing = set()
        started = time.monotonic()
        gate = asyncio.Semaphore(concurrency)
        self.emit("batch_started", {"addresses": addresses, "concurrency": concurrency,
                                    "total_bytes": len(package.image)})
        LOG.info("批量开始：%d 台，并发 %d，目标 %s，包 SHA-256=%s", len(addresses), concurrency, target, package.sha256)

        async def one(address):
            log_token = DEVICE_CONTEXT.set(address)
            controller = None
            acquired = False
            committed = False
            outcome = {"status": "cancelled", "error": "已停止，未确认升级成功"}

            def forward(kind, data):
                nonlocal committed
                if kind == "phase" and data.get("critical"):
                    committed = True
                self.device_event(address, kind, data)

            try:
                await gate.acquire()
                acquired = True
                if self.stopping:
                    return
                controller = self.controller_factory(forward)
                self.controllers[address] = controller
                self.device_event(address, "state", {"status": "connecting"})
                await controller.connect(address, address_type)
                result = await controller.upgrade(Path(path), target, options,
                            expanded=expanded, allow_same=allow_same,
                            expected_sha256=package.sha256)
                outcome = dict(result, status="success")
            except asyncio.CancelledError:
                pass
            except CommitUncertain as exc:
                outcome = {"status": "uncertain", "error": str(exc)}
                LOG.warning("[%s] 最终提交结果未确认：%s", address, exc)
            except Exception as exc:
                # Failure after Execute must not look safe to retry blindly.
                status = "uncertain" if committed else "error"
                outcome = {"status": status, "error": str(exc)}
                LOG.exception("[%s] 升级未成功：%s", address, exc)
            finally:
                self.finishing.add(address)
                if controller is not None:
                    with suppress(Exception):
                        await controller.shutdown()
                self.controllers.pop(address, None)
                if acquired:
                    gate.release()
                self.results[address] = outcome
                self.device_event(address, "result", outcome)
                LOG.info("[%s] 本次结果：%s", address, outcome)
                DEVICE_CONTEXT.reset(log_token)

        try:
            for address in addresses:
                self.device_event(address, "state", {"status": "queued"})
                self.tasks[address] = asyncio.create_task(one(address))
            # Exceptions are isolated per device. A stopped queued coroutine
            # may never enter its try/finally, so fill its outcome below too.
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
            for address in addresses:
                if address not in self.results:
                    outcome = {"status": "cancelled", "error": "已取消排队，未连接设备"}
                    self.results[address] = outcome
                    self.device_event(address, "result", outcome)
            result = {"devices": dict(self.results), "elapsed": time.monotonic() - started,
                      "stopped": self.stopping}
            self.emit("batch_finished", result)
            return result
        finally:
            self.running = False
            self.tasks = {}

    async def shutdown(self):
        if self.running:
            self.cancel()
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        for controller in list(self.controllers.values()):
            await controller.shutdown()
