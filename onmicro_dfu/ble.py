"""Bleak/WinRT adapter. Imports Bleak only when a BLE command is requested."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import importlib.metadata
import struct
import sys
import time

from .package import DfuError


SERVICE = "0000fe59-0000-1000-8000-00805f9b34fb"
CONTROL = "8ec90001-f315-4f60-9fb8-838830daea50"
DATA = "8ec90002-f315-4f60-9fb8-838830daea50"
QUERY = "00000003-0000-1000-8000-00805f9b34fb"
SOFTWARE = "00002a28-0000-1000-8000-00805f9b34fb"
LOG = logging.getLogger(__name__)
# Preserve native scan results: using only a string makes Bleak perform an
# implicit discovery for every connection, which competes with parallel DFU.
DISCOVERED_DEVICES = {}


def bleak_classes():
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError as exc:
        raise DfuError("Install dependencies: python -m pip install -r requirements.txt") from exc
    return BleakClient, BleakScanner


def client_options(address_type: str, timeout: float) -> dict:
    options = {"timeout": timeout}
    if sys.platform == "win32":
        options["winrt"] = {"use_cached_services": False, "address_type": address_type}
    return options


class BleTransport:
    def __init__(self, address: str, *, timeout: float = 15.0, address_type: str = "public"):
        self.address = address
        self.timeout = timeout
        self.address_type = address_type
        self.client = None
        self.queue = asyncio.Queue(maxsize=64)
        self.query_queue = asyncio.Queue(maxsize=8)
        self.disconnected = asyncio.Event()
        self.failure = None
        self.max_packet_size = 20
        self.loop = asyncio.get_running_loop()

    def enqueue(self, queue, item):
        if queue.full():
            self.failure = DfuError("DFU notification queue overflow; transfer stopped")
        else:
            queue.put_nowait(item)

    def on_disconnected(self, _client):
        def deliver():
            self.disconnected.set()
            for queue in (self.queue, self.query_queue):
                self.enqueue(queue, DfuError("BLE disconnected"))
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(deliver)

    async def __aenter__(self):
        Client, _ = bleak_classes()
        self.client = Client(DISCOVERED_DEVICES.get(self.address.upper(), self.address), disconnected_callback=self.on_disconnected,
                             **client_options(self.address_type, self.timeout))
        try:
            await asyncio.wait_for(self.client.connect(), self.timeout)
            service = self.client.services.get_service(SERVICE)
            if service is None:
                raise DfuError("Connected device does not expose OnMicro DFU service 0xFE59")
            self.control = service.get_characteristic(CONTROL)
            self.data = service.get_characteristic(DATA)
            self.query_char = service.get_characteristic(QUERY)
            if (self.control is None or self.data is None or
                    not {"write", "notify"}.issubset(self.control.properties) or
                    "write-without-response" not in self.data.properties):
                raise DfuError("DFU control/data characteristics or properties do not match")
            self.max_packet_size = min(self.data.max_write_without_response_size,
                                       self.client.mtu_size - 3, 512)
            await asyncio.wait_for(self.client.start_notify(self.control,
                lambda _char, data: self.enqueue(self.queue, bytes(data))), self.timeout)
            if self.query_char is not None:
                await asyncio.wait_for(self.client.start_notify(self.query_char,
                    lambda _char, data: self.enqueue(self.query_queue, bytes(data))), self.timeout)
            LOG.info("Connected %s; MTU=%d; max packet=%d", self.address,
                     self.client.mtu_size, self.max_packet_size)
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *_):
        if self.client is not None:
            with suppress(Exception):
                await asyncio.wait_for(self.client.disconnect(), 5.0)

    async def write_control(self, packet: bytes):
        LOG.debug("CTRL TX %s", packet.hex(" "))
        try:
            await asyncio.wait_for(self.client.write_gatt_char(
                self.control, packet, response=True), self.timeout)
        except Exception as exc:
            raise DfuError(f"DFU control write failed: {exc}") from exc

    async def write_data(self, packet: bytes):
        try:
            await asyncio.wait_for(self.client.write_gatt_char(
                self.data, packet, response=False), self.timeout)
        except Exception as exc:
            raise DfuError(f"DFU data write failed: {exc}") from exc

    async def get_packet(self, queue, timeout: float) -> bytes:
        if self.failure:
            raise self.failure
        try:
            packet = await asyncio.wait_for(queue.get(), timeout)
        except TimeoutError as exc:
            raise DfuError(f"No DFU response within {timeout:g}s; transfer stopped") from exc
        if self.failure:
            raise self.failure
        if isinstance(packet, Exception):
            raise packet
        return packet

    async def receive(self, timeout: float) -> bytes:
        packet = await self.get_packet(self.queue, timeout)
        LOG.debug("CTRL RX %s", packet.hex(" "))
        return packet

    async def query(self, command: str) -> tuple[int, int, int]:
        if self.query_char is None:
            raise DfuError("Missing OnMicro query characteristic 0x0003; ROM ID cannot be verified")
        await asyncio.wait_for(self.client.write_gatt_char(
            self.query_char, command.encode("ascii"), response=True), self.timeout)
        packet = await self.get_packet(self.query_queue, self.timeout)
        if len(packet) != 12:
            raise DfuError(f"Malformed {command} query response: {packet.hex()}")
        return struct.unpack("<III", packet)

    async def information(self) -> dict:
        try:
            raw = await asyncio.wait_for(self.client.read_gatt_char(
                SOFTWARE, use_cached=False), self.timeout)
            version = bytes(raw).rstrip(b"\0").decode("utf-8", errors="replace")
        except Exception as exc:
            LOG.debug("DIS software version unavailable: %s", exc)
            version = None
        address, marker, rom_id = await self.query("ROID")
        date, type_version, protocol = await self.query("VERN")
        return {"address": self.address, "software_version": version,
                "rom_id": f"0x{rom_id:08X}", "execution_address": f"0x{address:08X}",
                "boot_marker": f"0x{marker:08X}", "dfu_date": f"0x{date:08X}",
                "dfu_type_version": type_version, "dfu_protocol": protocol}

    async def wait_for_reboot(self, timeout: float):
        try:
            await asyncio.wait_for(self.disconnected.wait(), timeout)
        except TimeoutError as exc:
            raise DfuError("DFU was committed, but automatic disconnect/reboot was not observed. "
                           "Use probe to check the device; do not repeat Execute.") from exc


async def scan(timeout: float, name: str | None = None) -> list[dict]:
    _, Scanner = bleak_classes()
    devices = await Scanner.discover(timeout=timeout, return_adv=True)
    DISCOVERED_DEVICES.clear()
    DISCOVERED_DEVICES.update((device.address.upper(), device) for device, _ in devices.values())
    result = []
    for device, advertisement in devices.values():
        display = advertisement.local_name or device.name or ""
        if name and name.casefold() not in display.casefold():
            continue
        result.append({"address": device.address, "name": display,
                       "rssi": advertisement.rssi,
                       "advertises_dfu": SERVICE in advertisement.service_uuids})
    return sorted(result, key=lambda item: item["rssi"], reverse=True)


async def doctor() -> dict:
    """Import the actual native backend and query only the local adapter."""
    if sys.platform != "win32":
        raise DfuError("The packaged executable targets Windows 10/11 x64")
    from bleak.backends.winrt.client import BleakClientWinRT
    from bleak.backends.winrt.scanner import BleakScannerWinRT
    from winrt.windows.devices.bluetooth import BluetoothAdapter
    from winrt.windows.devices.radios import Radio
    adapter = await BluetoothAdapter.get_default_async()
    radios = await Radio.get_radios_async()
    return {"bleak_version": importlib.metadata.version("bleak"),
            "native_client": BleakClientWinRT.__name__,
            "native_scanner": BleakScannerWinRT.__name__,
            "adapter_found": adapter is not None,
            "central_supported": bool(adapter and adapter.is_central_role_supported),
            "ble_supported": bool(adapter and adapter.is_low_energy_supported),
            "bluetooth_on": any(int(radio.kind) == 3 and int(radio.state) == 1 for radio in radios)}


async def verify_version(address: str, expected: str, timeout: float,
                         address_type: str = "public") -> str:
    """Retry only read-only reconnects; never retry a firmware write."""
    Client, _ = bleak_classes()
    deadline = time.monotonic() + timeout
    last = "device has not reappeared"
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            async with asyncio.timeout(min(12.0, remaining)):
                async with Client(DISCOVERED_DEVICES.get(address.upper(), address),
                                  **client_options(address_type, min(10.0, remaining))) as client:
                    raw = await client.read_gatt_char(SOFTWARE, use_cached=False)
                    version = bytes(raw).rstrip(b"\0").decode("utf-8", errors="replace")
                    if version == expected:
                        return version
                    last = f"reported version {version!r}, expected {expected!r}"
        except Exception as exc:
            last = str(exc)
        LOG.debug("Post-reboot retry: %s", last)
        await asyncio.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    raise DfuError(f"DFU committed, but post-reboot version was not confirmed: {last}")
