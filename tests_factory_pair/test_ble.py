"""Run the actual Bleak adapter/pump and actual native libmlink against a fake GATT peer."""
import asyncio
from collections import deque
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from factory_pair.ble import PairTransport,WRITE,NOTIFY,SERVICE,PairError
from factory_pair.native import NativeLink


class FakeBleakClient:
    instances=[]
    def __init__(self,address,disconnected_callback,**options):
        self.address=address; self.callback=disconnected_callback; self.options=options
        self.connected=False; self.notify=None; self.outgoing=deque(); self.pending=deque(); self.requests=[]
        self.tx_sizes=[]; self.now=0; self.closed=False
        self.write=SimpleNamespace(uuid=WRITE,properties=['write','write-without-response'])
        self.notify_char=SimpleNamespace(uuid=NOTIFY,properties=['notify'])
        service=SimpleNamespace(get_characteristic=lambda uuid:{WRITE:self.write,NOTIFY:self.notify_char}.get(uuid))
        self.services=SimpleNamespace(get_service=lambda uuid:service if uuid==SERVICE else None)
        self.native=NativeLink(self._tx,self._rx,lambda *_:None)
        self.instances.append(self)
    def _tx(self,data): self.outgoing.append(data); return len(data)
    def _rx(self,rc,body):
        assert rc==0
        msg=json.loads(body); self.requests.append(msg)
        if msg['cmd']=='discover':
            self.pending.append({'v':1,'type':'scale-ui','cmd':'discover','sn':'M21010000A01A80',
                'fw':'V001_004','data':{'model':'SC200-D','ble':'cbdf08700000'}})
        else:
            id=msg['data']['id']
            self.pending.append({'v':1,'type':'scale-ui','cmd':'factory.pair.result','data':{'id':id,'op':0,'code':0,
                'detail':'unprovisioned','host_sn':'M21010000A01A80','host_ble':'cbdf08700000','record':0,'count':0,'saved':0}})
    async def connect(self):
        self.connected=True; self.task=asyncio.create_task(self._loop())
    async def start_notify(self,char,callback): self.notify=callback
    async def write_gatt_char(self,char,data,response=False):
        self.tx_sizes.append(len(data)); self.native.receive(data,self.now); await asyncio.sleep(0)
    async def _loop(self):
        while self.connected:
            self.now+=5; self.native.poll(self.now)
            if self.pending:
                data=json.dumps(self.pending[0],separators=(',',':')).encode()
                if self.native.send(data,77)==0: self.pending.popleft()
            while self.outgoing:
                data=self.outgoing.popleft()
                if self.notify:
                    # Split notifications independently from the ATT writes.
                    for i in range(0,len(data),11): self.notify(self.notify_char,data[i:i+11])
            await asyncio.sleep(.005)
    async def disconnect(self):
        if self.closed: return
        self.connected=False
        if hasattr(self,'task'):
            self.task.cancel()
            try: await self.task
            except asyncio.CancelledError: pass
        self.native.close(); self.closed=True; self.callback(self)


class BleTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_adapter_native_handshake_and_rpc_without_any_radio(self):
        with patch('factory_pair.ble.bleak_classes',return_value=(FakeBleakClient,None)):
            transport=PairTransport('00:00:70:08:DF:CB')
            try:
                await transport.connect()
                await transport.send({'v':1,'type':'host','cmd':'discover'})
                reply=await transport.receive(2)
                self.assertEqual(reply['sn'],'M21010000A01A80')
                await transport.send({'v':1,'type':'host','cmd':'factory.pair','data':{'id':123,'op':0}})
                reply=await transport.receive(2)
                self.assertEqual(reply['data']['id'],123)
                self.assertEqual(reply['data']['record'],0)
                peer=FakeBleakClient.instances[-1]
                self.assertLessEqual(max(peer.tx_sizes),20)
                self.assertEqual([r['cmd'] for r in peer.requests],['discover','factory.pair'])
                self.assertIsNone(transport.failure)
            finally: await transport.close()
            self.assertTrue(peer.closed); self.assertIsNone(transport.native)

    async def test_link_loss_is_reported_and_does_not_create_a_success(self):
        events=[]
        with patch('factory_pair.ble.bleak_classes',return_value=(FakeBleakClient,None)):
            transport=PairTransport('00:00:70:08:DF:CB',event=lambda *e:events.append(e))
            try:
                await transport.connect(); await FakeBleakClient.instances[-1].disconnect()
                await asyncio.sleep(.01)
                with self.assertRaises(PairError): await transport.send({'v':1,'type':'host','cmd':'discover'})
                self.assertEqual(events[0][0],'link_lost')
            finally: await transport.close()
