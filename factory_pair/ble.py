"""BLE stream adapter. Actual libmlink owns framing/HELLO/ACK/retry/keepalive."""
from __future__ import annotations
import asyncio
from contextlib import suppress
import json
import time

from onmicro_dfu.ble import DISCOVERED_DEVICES, bleak_classes, client_options
from .native import NativeLink

SERVICE='6b1a0001-c701-4e11-9a2c-7e5d00a11e55'
WRITE='6b1a0002-c701-4e11-9a2c-7e5d00a11e55'
NOTIFY='6b1a0003-c701-4e11-9a2c-7e5d00a11e55'


class PairError(RuntimeError): pass
class PairUncertain(PairError): pass


async def scan_hosts(timeout=6):
    _,Scanner=bleak_classes()
    found=await Scanner.discover(timeout=timeout,return_adv=True)
    rows=[]
    for device,adv in found.values():
        name=adv.local_name or device.name or ''
        if not name.upper().startswith('SC200-D'): continue
        DISCOVERED_DEVICES[device.address.upper()]=device
        rows.append({'address':device.address,'name':name,'rssi':adv.rssi})
    return sorted(rows,key=lambda r:r['rssi'],reverse=True)


class PairTransport:
    def __init__(self,address,address_type='public',event=lambda *_:None):
        self.address=address; self.address_type=address_type; self.event=event
        self.client=None; self.native=None; self.pump=None
        self.tx=asyncio.Queue(maxsize=128); self.rx=asyncio.Queue(maxsize=128)
        self.messages=asyncio.Queue(maxsize=64); self.ack={}; self.tag=0
        self.failure=None; self.started=time.monotonic(); self.stopping=False
        self.loop=asyncio.get_running_loop()

    def _put(self,queue,data):
        if queue.full(): self._fail(PairError('蓝牙接收队列已满，请断开后重新查询'))
        else: queue.put_nowait(data)

    def _fail(self,error):
        if self.failure is not None: return
        self.failure=error
        for future in tuple(self.ack.values()):
            if not future.done(): future.set_exception(error)
        if not self.messages.full(): self.messages.put_nowait(error)
        self.event('link_lost',str(error))

    def _disconnected(self,_):
        if not self.stopping and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._fail,PairError('主机 BLE 已断开'))

    def _notify(self,_,data):
        if not self.stopping and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._put,self.rx,bytes(data))

    def _write(self,data):
        if self.tx.full(): return 0
        self.tx.put_nowait(data); return len(data)

    def _json(self,result,data):
        if result: self._fail(PairError('主机响应的 mlink JSON 无效')); return
        try: message=json.loads(data.decode('utf-8'))
        except (ValueError,UnicodeError) as exc: self._fail(PairError(str(exc))); return
        self._put(self.messages,message)

    def _done(self,tag,result,reason):
        future=self.ack.get(tag)
        if future is None or future.done(): return
        if result==0: future.set_result(None)
        else: future.set_exception(PairError(f'mlink 发送未确认：result={result}, reason={reason}'))

    def now(self): return int((time.monotonic()-self.started)*1000)

    async def connect(self):
        Client,_=bleak_classes()
        self.client=Client(DISCOVERED_DEVICES.get(self.address.upper(),self.address),
            disconnected_callback=self._disconnected,**client_options(self.address_type,15))
        try:
            await asyncio.wait_for(self.client.connect(),20)
            service=self.client.services.get_service(SERVICE)
            self.write_char=service.get_characteristic(WRITE) if service else None
            notify=service.get_characteristic(NOTIFY) if service else None
            if not self.write_char or not notify or 'notify' not in notify.properties:
                raise PairError('设备没有 mlink 通信服务，请确认连接的是主机并更新主机固件')
            props=set(self.write_char.properties)
            if not props.intersection({'write','write-without-response'}): raise PairError('主机 mlink 写特征不可用')
            self.write_response='write-without-response' not in props
            self.native=NativeLink(self._write,self._json,self._done)
            await self.client.start_notify(notify,self._notify)
            self.pump=asyncio.create_task(self._pump())
            deadline=time.monotonic()+10
            while not self.native.up:
                self.check()
                if time.monotonic()>=deadline: raise PairError('mlink 握手超时；请确认固件支持出厂配对')
                await asyncio.sleep(.02)
        except BaseException:
            await self.close(); raise

    async def _pump(self):
        was_up=False; last_hello=-1000
        try:
            while not self.stopping and self.failure is None:
                now=self.now()
                while not self.rx.empty(): self.native.receive(self.rx.get_nowait(),now)
                self.native.poll(now)
                if self.native.up: was_up=True
                elif was_up: raise PairError('mlink 会话已结束，请重新连接主机查询记录')
                elif now-last_hello>=1000: self.native.hello(); last_hello=now
                while not self.tx.empty():
                    await asyncio.wait_for(self.client.write_gatt_char(self.write_char,self.tx.get_nowait(),
                        response=self.write_response),5)
                await asyncio.sleep(.005)
        except asyncio.CancelledError: raise
        except Exception as exc:
            self._fail(PairError(str(exc)))
            if self.client:
                with suppress(Exception): await asyncio.wait_for(self.client.disconnect(),5)

    def check(self):
        if self.failure is not None: raise self.failure
        if self.stopping: raise PairError('BLE 已关闭')

    async def send(self,message):
        self.check(); self.tag=(self.tag+1)&0x7fffffff or 1
        tag=self.tag; future=self.loop.create_future(); self.ack[tag]=future
        body=json.dumps(message,separators=(',',':'),ensure_ascii=False).encode('utf-8')
        if len(body)>512: self.ack.pop(tag); raise PairError('出厂配对命令超过主机 512 字节限制')
        try:
            deadline=time.monotonic()+4
            while True:
                self.check(); rc=self.native.send(body,tag)
                if rc==0: break
                if time.monotonic()>=deadline: raise PairError(f'mlink 发送队列忙或拒绝：{rc}')
                await asyncio.sleep(.02)
            await asyncio.wait_for(future,6)
        finally:
            self.ack.pop(tag,None)
            if not future.done(): future.cancel()
            elif not future.cancelled(): future.exception()

    async def receive(self,timeout):
        self.check()
        value=await asyncio.wait_for(self.messages.get(),timeout)
        if isinstance(value,Exception): raise value
        return value

    async def close(self):
        self.stopping=True
        if self.pump:
            self.pump.cancel()
            with suppress(asyncio.CancelledError): await self.pump
            self.pump=None
        if self.client:
            with suppress(Exception): await asyncio.wait_for(self.client.disconnect(),5)
            self.client=None
        if self.native: self.native.close(); self.native=None
        for future in self.ack.values():
            if not future.done(): future.cancel()
        self.ack.clear()
