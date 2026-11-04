"""Real unmodified C libmlink on both ends: no radio, no hardware writes."""
from collections import deque
import json
import unittest
from factory_pair.native import NativeLink


class WireTests(unittest.TestCase):
    def setUp(self):
        self.to_a=deque(); self.to_b=deque(); self.rx_a=[]; self.rx_b=[]; self.acks_a=[]; self.acks_b=[]
        self.a=NativeLink(lambda b:self._put(self.to_b,b),lambda rc,b:self.rx_a.append((rc,b)),lambda *r:self.acks_a.append(r))
        self.b=NativeLink(lambda b:self._put(self.to_a,b),lambda rc,b:self.rx_b.append((rc,b)),lambda *r:self.acks_b.append(r))
        self.addCleanup(self.a.close); self.addCleanup(self.b.close); self.now=0
    def _put(self,queue,data): queue.append(data); return len(data)
    def pump(self,n=500):
        for _ in range(n):
            # Force fragments smaller than BLE writes to exercise actual stream assembly.
            for target,queue in ((self.a,self.to_a),(self.b,self.to_b)):
                while queue:
                    data=queue.popleft()
                    for i in range(0,len(data),7): target.receive(data[i:i+7],self.now)
            self.a.poll(self.now); self.b.poll(self.now); self.now+=5
    def connect(self):
        self.a.hello(); self.pump(100)
        self.assertTrue(self.a.up and self.b.up)
    def test_handshake_json_ack_and_ten_second_keepalive(self):
        self.connect()
        body=json.dumps({'v':1,'type':'host','cmd':'factory.pair','data':{'id':1,'op':0}},separators=(',',':')).encode()
        self.assertEqual(self.a.send(body,17),0); self.pump()
        self.assertEqual(self.rx_b,[(0,body)]); self.assertIn((17,0,0),self.acks_a)
        response=json.dumps({'v':1,'type':'scale-ui','cmd':'factory.pair.result','data':{'id':1,'op':0,'code':0}},separators=(',',':')).encode()
        self.assertEqual(self.b.send(response,18),0); self.pump()
        self.assertEqual(self.rx_a,[(0,response)]); self.assertIn((18,0,0),self.acks_b)
        self.pump(2000); self.assertTrue(self.a.up and self.b.up)
    def test_lost_ack_retransmits_without_duplicate_delivery(self):
        self.connect()
        body=b'{"v":1,"type":"host","cmd":"factory.pair","data":{"id":2,"op":1}}'
        self.assertEqual(self.a.send(body,3),0)
        self.b.receive(b''.join(self.to_b),self.now); self.to_b.clear(); self.to_a.clear()
        self.pump(350)
        self.assertEqual(len(self.rx_b),1); self.assertIn((3,0,0),self.acks_a)
    def test_native_blocks_unrelated_commands_and_oversize(self):
        self.connect()
        self.assertNotEqual(self.a.send(b'{"v":1,"type":"host","cmd":"svc.reset"}',1),0)
        body=json.dumps({'v':1,'type':'host','cmd':'factory.pair','data':{'text':'a'*490}}).encode()
        self.assertNotEqual(self.a.send(body,2),0); self.assertFalse(self.rx_b)
    def test_independent_contexts_do_not_share_sessions_or_queues(self):
        self.a.hello(); self.assertFalse(self.b.up); self.assertTrue(self.to_b)
        self.a.close(); self.assertFalse(self.b.up)
