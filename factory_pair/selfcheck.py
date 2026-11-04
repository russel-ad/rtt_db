"""Packaged DLL check using two native endpoints, without BLE/probe access."""
import json
from collections import deque
from .native import NativeLink


def run():
    to_a,to_b=deque(),deque(); replies=[]; receives=[]; acks=[]
    def queue(q,data): q.append(data); return len(data)
    a=NativeLink(lambda d:queue(to_b,d),lambda rc,b:replies.append((rc,b)),lambda *r:acks.append(r))
    b=NativeLink(lambda d:queue(to_a,d),lambda rc,d:receives.append((rc,d)),lambda *_:None)
    def pump(start,n=300):
        for now in range(start,start+n*5,5):
            for target,q in ((a,to_a),(b,to_b)):
                while q:
                    d=q.popleft()
                    for i in range(0,len(d),9): target.receive(d[i:i+9],now)
            a.poll(now); b.poll(now)
    try:
        a.hello(); pump(0,100)
        if not (a.up and b.up): raise RuntimeError('Native HELLO failed')
        body=b'{"v":1,"type":"host","cmd":"factory.pair","data":{"id":7,"op":0}}'
        if a.send(body,17)!=0: raise RuntimeError('Native query send failed')
        pump(500)
        if receives!=[(0,body)] or (17,0,0) not in acks: raise RuntimeError('Native JSON/ACK failed')
        return {'passed':True,'native_libmlink_handshake_json_ack':True,'radio_accessed':False,
                'hardware_written':False,'factory_command':'query only, simulated peer'}
    finally: a.close(); b.close()
