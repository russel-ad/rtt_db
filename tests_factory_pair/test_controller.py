import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from factory_pair.ble import PairError,PairUncertain
from factory_pair.controller import PairController

HOST={'sn':'M21010000A01A80','ble':'cbdf08700000','model':'SC200-D','fw':'V001_004'}
PEERS=[{'sn':'M21010000A01R88','ble':'f19533480784','model':2,'docked':False},
       {'sn':'M21010000A0111V','ble':'f74f81fa8483','model':2,'docked':True}]


class SimTransport:
    def __init__(self,address,address_type,event):
        self.address=address; self.event=event; self.queue=asyncio.Queue(); self.live=copy.deepcopy(PEERS)
        self.saved=[]; self.state=0; self.jobs={}; self.writes=0; self.sent=[]; self.error=None
        self.bad_model=False; self.wrong_reply=False; self.missing_part=False; self.closed=False
    async def connect(self): pass
    async def close(self): self.closed=True
    async def receive(self,timeout):
        if self.queue.empty(): raise asyncio.TimeoutError()
        return self.queue.get_nowait()
    async def send(self,msg):
        self.sent.append(copy.deepcopy(msg))
        if self.error: raise PairError(self.error)
        if msg['cmd']=='discover':
            self.queue.put_nowait({'v':1,'cmd':'discover','type':'scale' if self.bad_model else 'scale-ui',
                'sn':HOST['sn'],'fw':HOST['fw'],'data':{'model':'SC201-S' if self.bad_model else HOST['model'],'ble':HOST['ble']}}); return
        data=msg['data']; op=data['op']; id=data['id']
        if id not in self.jobs:
            if op in (1,2):
                self.writes+=1
                self.saved=[next(copy.deepcopy(p) for p in self.live if p['ble']==r['ble']) for r in data['peers']]
                self.state=1
            self.jobs[id]=copy.deepcopy(data)
        else: assert self.jobs[id]==data
        if self.wrong_reply: self.queue.put_nowait({'cmd':'factory.pair.result','data':{'id':id+99,'op':op,'code':0}})
        for kind,rows in (('live',self.live),('saved',self.saved)):
            for i,p in reversed(list(enumerate(rows))):
                if self.missing_part and i==0: continue
                self.queue.put_nowait({'cmd':'factory.pair.info','data':{'id':id,'kind':kind,'slot':i,**copy.deepcopy(p)}})
        self.queue.put_nowait({'cmd':'factory.pair.result','data':{'id':id,'op':op,'code':0,'detail':'verified' if self.state else 'unprovisioned',
            'host_sn':HOST['sn'],'host_ble':HOST['ble'],'record':self.state,'count':len(self.live),'saved':len(self.saved)}})
        self.missing_part=False


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.events=[]; self.instances=[]
        def factory(*args):
            t=SimTransport(*args); self.instances.append(t); return t
        self.controller=PairController(lambda *e:self.events.append(e),factory,Path(self.tmp.name)/'reports')
        await self.controller.connect('00:00:70:08:DF:CB'); self.transport=self.instances[-1]
    async def asyncTearDown(self): await self.controller.disconnect()

    async def test_query_only_never_writes_and_collects_unprovisioned_identity(self):
        self.assertEqual(self.transport.writes,0)
        self.assertEqual(len(self.controller.inventory['live']),2)
        self.assertEqual(self.controller.inventory['result']['record'],0)
        self.assertFalse(self.controller.reports)

    async def test_save_preserves_explicit_top_bottom_and_writes_a_pending_report(self):
        report=await self.controller.save(list(reversed(self.controller.inventory['live'])))
        self.assertFalse(report['qualified']); self.assertEqual(self.transport.writes,1)
        self.assertEqual(report['saved'][0]['sn'],PEERS[1]['sn'])
        saved=json.loads(Path(report['report_path']).read_text(encoding='utf-8'))
        self.assertFalse(saved['qualified']); self.assertEqual(saved['host'],HOST)
        self.assertEqual(self.transport.sent[-1]['data']['host_ble'],HOST['ble'])

    async def test_cold_power_confirmation_required_and_verify_has_no_write(self):
        await self.controller.save(self.controller.inventory['live'])
        with self.assertRaisesRegex(PairError,'确认'): await self.controller.verify(False)
        with self.assertRaisesRegex(PairError,'移开磁吸'): await self.controller.verify(True)
        for row in self.transport.live: row['docked']=False
        await self.controller.query()
        report=await self.controller.verify(True)
        self.assertTrue(report['qualified']); self.assertEqual(self.transport.writes,1)
        self.assertTrue(report['cold_power_confirmed_by_operator'])

    async def test_requires_all_peers_unique_two_large_scales_and_a_dock(self):
        peers=self.controller.inventory['live']
        for bad in (peers[:1],[peers[0],peers[0]]):
            with self.assertRaises(PairError): await self.controller.save(bad)
        peers[0]['model']=1
        with self.assertRaisesRegex(PairError,'SC202'): await self.controller.save(peers)
        peers[0]['model']=2
        for p in peers: p['docked']=False
        with self.assertRaisesRegex(PairError,'磁吸'): await self.controller.save(peers)
        self.assertEqual(self.transport.writes,0)

    async def test_unacknowledged_write_never_passes_and_invalidates_inventory(self):
        peers=self.controller.inventory['live']; self.transport.error='BLE 断开'
        with self.assertRaises(PairUncertain): await self.controller.save(peers)
        self.assertIsNone(self.controller.inventory); self.assertFalse(self.controller.reports)
        self.assertIsNone(self.controller.last_save)

    async def test_missing_info_retries_same_id_and_collects_no_duplicate_writes(self):
        self.transport.missing_part=True; self.transport.wrong_reply=True
        report=await self.controller.save(self.controller.inventory['live'])
        writes=[r for r in self.transport.sent if r.get('data',{}).get('op')==1]
        self.assertEqual(len(writes),2); self.assertEqual(writes[0],writes[1])
        self.assertEqual(self.transport.writes,1); self.assertFalse(report['qualified'])

    async def test_report_failure_is_reported_separately_from_verified_write(self):
        with patch.object(Path,'mkdir',side_effect=PermissionError('read-only')):
            report=await self.controller.save(self.controller.inventory['live'])
        self.assertIn('report_error',report); self.assertFalse(report['qualified'])
        self.assertEqual(self.transport.writes,1); self.assertTrue(any(e[0]=='log' for e in self.events))

    async def test_explicit_rework_flag(self):
        await self.controller.save(self.controller.inventory['live'],True)
        request=self.transport.sent[-1]['data']; self.assertEqual(request['op'],2); self.assertTrue(request['replace'])

    async def test_wrong_model_disconnects_without_factory_command(self):
        def factory(*args):
            t=SimTransport(*args); t.bad_model=True; self.instances.append(t); return t
        self.controller.transport_factory=factory
        with self.assertRaisesRegex(PairError,'SC200-D'): await self.controller.connect('wrong')
        self.assertIsNone(self.controller.transport); self.assertTrue(self.instances[-1].closed)
        self.assertEqual([m['cmd'] for m in self.instances[-1].sent],['discover'])

    async def test_unknown_firmware_never_qualifies(self):
        async def old_send(message): self.transport.queue.put_nowait({'cmd':'error','err':1})
        self.transport.send=old_send
        with self.assertRaisesRegex(PairError,'升级'): await self.controller.query()
        self.assertFalse(self.controller.reports)
