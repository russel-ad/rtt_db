"""Command-line interface. Only the explicit upgrade subcommand writes firmware."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import logging
import math
import os
from pathlib import Path
import sys
import time

from .package import DfuError, FirmwarePackage
from .protocol import CommitUncertain, DfuSession, TransferOptions


LOG = logging.getLogger(__name__)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="OnMicro PC BLE DFU (APP-only, normal boot)")
    root.add_argument("--log", type=Path, help="Override log file; EXE default is LOCALAPPDATA/SC201_DFU_PC/logs")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Check Windows BLE dependencies/adapter; no scanning or connections")
    scan = commands.add_parser("scan", help="Scan only; no connections or firmware writes")
    scan.add_argument("--timeout", type=float, default=8.0)
    scan.add_argument("--name", help="Case-insensitive name substring; do not filter by DFU advertisement")
    check = commands.add_parser("check", help="Validate a ZIP offline; no Bluetooth needed")
    check.add_argument("package", type=Path)
    check.add_argument("--layout", choices=("legacy", "expanded"), default="legacy")
    probe = commands.add_parser("probe", help="Connect and query ROM ID/version; no DFU objects created")
    upgrade = commands.add_parser("upgrade", help="Write APP, commit, and verify reboot/version")
    for command in (probe, upgrade):
        command.add_argument("--address", required=True, help="Exact device Bluetooth address")
        command.add_argument("--address-type", choices=("public", "random"), default="public")
        command.add_argument("--timeout", type=float, default=15.0)
    upgrade.add_argument("--package", required=True, type=Path)
    upgrade.add_argument("--expect-version", required=True, help="DIS software version required after reboot")
    upgrade.add_argument("--layout", choices=("legacy", "expanded"), default="legacy",
                         help="legacy: <=280 KiB; expanded: <=400 KiB, only for already migrated devices")
    upgrade.add_argument("--allow-same-version", action="store_true", help="Explicitly permit reinstalling the current version")
    upgrade.add_argument("--chunk-size", type=int, default=0, help="0=auto (up to 128); 20 for conservative testing")
    upgrade.add_argument("--prn", type=int, default=8, help="Packets per verified receipt (1..255)")
    upgrade.add_argument("--packet-delay-ms", type=float, default=0)
    upgrade.add_argument("--final-timeout", type=float, default=60.0)
    upgrade.add_argument("--reboot-timeout", type=float, default=20.0)
    upgrade.add_argument("--verify-timeout", type=float, default=90.0)
    return root


async def run(args) -> int:
    if args.command == "check":
        package = FirmwarePackage.load(args.package, expanded=args.layout == "expanded")
        print(json.dumps(package.describe(), indent=2))
        return 0

    # Keep all BLE dependencies optional for offline package validation/tests.
    from .ble import BleTransport, doctor, scan, verify_version
    if args.command == "doctor":
        print(json.dumps(await doctor(), ensure_ascii=False, indent=2))
        return 0
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise DfuError("Timeout must be positive")
    if args.command == "scan":
        print(json.dumps(await scan(args.timeout, args.name), ensure_ascii=False, indent=2))
        return 0
    if args.command == "probe":
        async with BleTransport(args.address, timeout=args.timeout,
                                address_type=args.address_type) as connection:
            print(json.dumps(await connection.information(), indent=2))
        return 0

    package = FirmwarePackage.load(args.package, expanded=args.layout == "expanded")
    if package.version is not None and package.version != args.expect_version:
        raise DfuError(f"ZIP contains {package.version}, not requested {args.expect_version}")
    if (not all(math.isfinite(n) and n > 0 for n in
                (args.reboot_timeout, args.verify_timeout))):
        raise DfuError("Reboot/verification timeouts must be positive")
    options = TransferOptions(args.chunk_size, args.prn, args.timeout,
                              args.final_timeout, args.packet_delay_ms / 1000)
    LOG.info("Package %s", json.dumps(package.describe()))
    started = time.monotonic()
    last_percent = -2

    def progress(offset, total):
        nonlocal last_percent
        percent = offset * 100 // total
        if percent >= last_percent + 2 or offset == total:
            last_percent = percent
            elapsed = max(0.001, time.monotonic() - started)
            LOG.info("Transfer %3d%% %d/%d bytes, %.1f KiB/s", percent,
                     offset, total, offset / 1024 / elapsed)

    async with BleTransport(args.address, timeout=args.timeout,
                            address_type=args.address_type) as connection:
        info = await connection.information()
        LOG.info("Device %s", json.dumps(info))
        if (info["rom_id"] != f"0x{package.rom_id:08X}" or
                info["execution_address"] != "0x00404000" or
                info["boot_marker"] != "0x46533252"):
            raise DfuError("Device ROM ID or normal-boot layout differs from the package")
        if info["software_version"] == args.expect_version and not args.allow_same_version:
            raise DfuError("Device already reports target version; use --allow-same-version to reinstall")
        session = DfuSession(connection, options, progress)
        LOG.info("Starting DFU: chunk=%d PRN=%d", session.chunk_size, options.prn)
        await session.transfer(package)
        LOG.info("Final Execute acknowledged; waiting for automatic reboot")
        await connection.wait_for_reboot(args.reboot_timeout)
    LOG.info("Device disconnected; waiting for version %s", args.expect_version)
    version = await verify_version(args.address, args.expect_version,
                                   args.verify_timeout, args.address_type)
    LOG.info("SUCCESS: post-reboot version=%s, elapsed=%.1fs", version, time.monotonic() - started)
    return 0


def default_log_directory() -> Path:
    # A one-file executable extracts __file__ into a temporary directory.
    # Keep its logs in a persistent, writable location instead.
    if getattr(sys, "frozen", False):
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SC201_DFU_PC" / "logs"
    return Path(__file__).resolve().parents[1] / "logs"


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    log = args.log or (default_log_directory() /
                       (datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".log"))
    root_logger = logging.getLogger()
    previous_handlers = root_logger.handlers[:]
    previous_level = root_logger.level
    handlers = []
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        stream = logging.StreamHandler()
        handlers.append(stream)
        stream.setLevel(logging.INFO)
        file = logging.FileHandler(log, encoding="utf-8")
        handlers.append(file)
        file.setLevel(logging.DEBUG)
        for handler in handlers:
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root_logger.handlers = handlers
        root_logger.setLevel(logging.DEBUG)
        LOG.info("Log: %s", log)
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        LOG.error("Cancelled. If final Execute was in progress, probe the version before retrying.")
        return 130
    except CommitUncertain as exc:
        LOG.error("COMMIT UNCERTAIN: %s", exc)
        return 4
    except Exception as exc:
        LOG.error("FAILED: %s", exc)
        LOG.debug("Failure details", exc_info=True)
        return 2
    finally:
        root_logger.handlers = previous_handlers
        root_logger.setLevel(previous_level)
        for handler in handlers:
            handler.close()
