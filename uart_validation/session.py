"""Run one UART BER batch with two already selected USB probes."""

from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import secrets
import time

from . import probes
from .protocol import FW, Stream, save_report


ROLES = {'H': '主机', 'S': '从机'}


@dataclass(frozen=True)
class Profile:
    title: str
    count: int
    length: int
    gap: int = 2

    @property
    def timeout(self):
        wire_ms = math.ceil((self.length + 36) * 10000 / 115200)
        return self.count * (wire_ms + self.gap + 4) / 1000 + 25


QUICK = Profile('快速测试', 1000, 128)
NIGHT = Profile('夜间测试（约 8 小时）', 1000000, 256)


class Stopped(Exception):
    pass


class Session:
    def __init__(self, serials, profile, directory, stop, notify, attach=probes.attach):
        self.serials = dict(serials)
        if (set(self.serials) != set(ROLES) or len(set(self.serials.values())) != 2 or
                any(type(serial) is not int or serial <= 0 for serial in self.serials.values())):
            raise ValueError('请选择两只不同的 J-Link')
        self.attach = attach
        self.stop = stop
        self.notify = notify
        self.profile = profile
        self.run_id = 1 + secrets.randbelow(0xFFFFFFFF)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.directory = Path(directory) / f'{stamp}_{self.run_id}'
        self.settings = dict(run=self.run_id, mode=3, count=profile.count, len=profile.length, gap=profile.gap, pattern=6)
        self.links = {}
        self.logs = {}
        self.parsers = {role: Stream() for role in ROLES}
        self.latest = {}
        self.boot = {}
        self.started = set()
        self.started_at = None
        self.last_progress = 0.0

    def check_stop(self):
        if self.stop.is_set():
            raise Stopped('测试员停止了本轮测试')

    def send(self, role, command, allow_stop=False):
        data = (command + '\n').encode('ascii')
        deadline = time.monotonic() + 1.0
        while data:
            if not allow_stop:
                self.check_stop()
            written = self.links[role].write(data)
            if not 0 <= written <= len(data):
                raise RuntimeError('RTT 写入返回异常')
            data = data[written:]
            if data and time.monotonic() >= deadline:
                raise TimeoutError(f'{ROLES[role]} RTT 命令未写完')
            if data:
                time.sleep(0.02)

    def pump(self, strict=True):
        for role, link in self.links.items():
            try:
                data = link.read()
            except Exception as error:
                if strict:
                    raise RuntimeError(f'{ROLES[role]} RTT 读取失败：{error}') from error
                continue
            if not data:
                continue
            self.logs[role].write(data)
            self.logs[role].flush()
            for record in self.parsers[role].feed(data):
                if strict and role in self.boot and record.get('type') == 'BOOT' and record.get('boot') != self.boot[role]:
                    raise RuntimeError(f'{ROLES[role]} 测试中重启')
                if record['type'] not in ('STAT', 'FINAL'):
                    continue
                if strict and (record['role'] != role or record['fw'] != FW):
                    raise RuntimeError(f'{ROLES[role]}探针读到 {record["role"]} / {record["fw"]}，请核对两端接线及测试固件')
                if strict and role in self.boot and record['boot'] != self.boot[role]:
                    raise RuntimeError(f'{ROLES[role]} 测试中重启')
                if role in self.started and record['run'] != self.run_id:
                    if strict:
                        raise RuntimeError(f'{ROLES[role]} 测试批次已改变')
                    continue
                self.latest[role] = record
        now = time.monotonic()
        if self.started_at is not None and now - self.last_progress >= 0.25:
            progress = {role: r for role, r in self.latest.items() if r['run'] == self.run_id}
            self.notify('progress', (now - self.started_at, progress))
            self.last_progress = now

    def wait_for(self, predicate, timeout, message):
        deadline = time.monotonic() + timeout
        next_query = 0.0
        while time.monotonic() < deadline:
            self.check_stop()
            self.pump()
            if all(role in self.latest and predicate(self.latest[role]) for role in ROLES):
                return
            if time.monotonic() >= next_query:
                for role in self.links:
                    self.send(role, 'ber status')
                next_query = time.monotonic() + 1.5
            self.stop.wait(0.04)
        raise TimeoutError(message)

    def stop_started(self):
        result = {}
        for role in self.started:
            try:
                self.send(role, 'ber stop', allow_stop=True)
                result[role] = 'sent'
            except Exception as error:
                result[role] = str(error)
        if result:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                self.pump(strict=False)
                for role in self.started:
                    r = self.latest.get(role, {})
                    if (r.get('type') == 'FINAL' and r.get('run') == self.run_id and
                            r.get('boot') == self.boot[role]):
                        result[role] = 'final'
                if all(value == 'final' for value in result.values()):
                    break
                time.sleep(0.04)
        return result

    def execute(self):
        self.directory.mkdir(parents=True, exist_ok=False)
        paths = [self.directory / name for name in ('host_rtt.log', 'scale_rtt.log')]
        metadata = dict(self.settings, probe_serials=self.serials, profile=self.profile.title,
                        started_utc=datetime.now(timezone.utc).isoformat())
        (self.directory / 'requested.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
        failure = None
        stopped = {}
        try:
            with ExitStack() as stack:
                self.logs = {role: stack.enter_context(path.open('xb')) for role, path in zip(ROLES, paths)}
                try:
                    for role, serial in self.serials.items():
                        self.check_stop()
                        self.notify('status', f'正在连接{ROLES[role]}探针 {serial}')
                        try:
                            self.links[role] = stack.enter_context(self.attach(role, serial))
                        except Exception as error:
                            raise RuntimeError(f'{ROLES[role]}探针 {serial}：{error}') from error
                    self.notify('status', '正在核对主从角色和测试固件')
                    self.wait_for(lambda r: True, 12, '等待两端测试固件状态超时，请检查 UART1 固件和探针接线')
                    for role, record in self.latest.items():
                        if record['state'] not in (0, 1, 3):
                            raise RuntimeError(f'{ROLES[role]}已有测试在运行，请等待结束或用原会话停止')
                        self.boot[role] = record['boot']
                    self.notify('status', '两端已连接，正在准备本轮测试')
                    for role in ROLES:
                        self.send(role, f'ber arm {self.run_id} 3 {self.profile.count} {self.profile.length} {self.profile.gap} 6')
                    self.wait_for(lambda r: r['state'] == 1 and all(r[k] == v for k, v in self.settings.items()),
                                  12, '两端未同时进入准备状态，请检查是否有升级或者上一轮尚未结束')
                    self.started_at = time.monotonic()
                    for role in ROLES:
                        self.send(role, 'ber start')
                        self.started.add(role)
                    self.notify('status', '测试运行中')
                    self.wait_for(lambda r: r['state'] == 3 and r['type'] == 'FINAL' and r['run'] == self.run_id,
                                  self.profile.timeout, '等待两端最终统计超时，请检查吸合接点和 RTT 连接')
                    self.notify('status', '正在保存结果')
                    self.pump()
                except Exception as error:
                    failure = str(error)
                    self.notify('status', '正在保存当前日志')
                    stopped = self.stop_started()
        except Exception as error:
            failure = f'{failure}；收尾失败：{error}' if failure else str(error)
        for path in paths:
            path.touch(exist_ok=True)
        return save_report(self.directory, paths, self.run_id, metadata=metadata, failure=failure, stopped=stopped)
