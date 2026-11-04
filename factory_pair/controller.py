"""Correlated production jobs; transport ACK never counts as factory PASS."""
from __future__ import annotations
import asyncio
from datetime import datetime,timezone
import json
from pathlib import Path
import re
import secrets
import time

from .ble import PairError,PairUncertain,PairTransport,scan_hosts

def sn_valid(value): return isinstance(value,str) and bool(re.fullmatch(r'[A-Z0-9]{15}',value))
def mac_valid(value): return isinstance(value,str) and bool(re.fullmatch(r'[0-9a-f]{12}',value)) and value not in ('0'*12,'f'*12)
def identity(row): return (row.get('sn'),row.get('ble'),row.get('model'))


class PairController:
    def __init__(self,event=lambda *_:None,transport_factory=PairTransport,report_dir=None):
        self.event=event; self.transport_factory=transport_factory; self.transport=None
        self.host=None; self.inventory=None; self.reports=[]; self.last_save=None
        self.id=secrets.randbelow(0x7ffffffe)+1
        self.report_dir=Path(report_dir) if report_dir else Path('logs/factory_pair')

    async def scan(self): return await scan_hosts()

    async def connect(self,address,address_type='public'):
        await self.disconnect()
        self.transport=self.transport_factory(address,address_type,self.event)
        try:
            await self.transport.connect()
            await self.transport.send({'v':1,'type':'host','cmd':'discover'})
            deadline=time.monotonic()+8
            while True:
                msg=await self.transport.receive(max(.01,deadline-time.monotonic()))
                if msg.get('cmd')!='discover': continue
                host=msg.get('data',{})
                if msg.get('type')!='scale-ui' or host.get('model')!='SC200-D' or not sn_valid(msg.get('sn')) or not mac_valid(host.get('ble')):
                    raise PairError('身份核对失败：必须连接 SC200-D 主机，不能直接连接从机秤')
                self.host={'sn':msg['sn'],'ble':host['ble'],'model':host['model'],'fw':msg.get('fw','')}
                if self.last_save and self.last_save['host']!=self.host:
                    self.last_save=None
                break
            self.event('host',dict(self.host))
            return await self.query()
        except BaseException:
            await self.disconnect(); raise

    async def disconnect(self):
        if self.transport: await self.transport.close()
        self.transport=None; self.host=None; self.inventory=None
        self.event('disconnected',None)

    def _validate(self):
        if self.transport is None or self.host is None: raise PairError('请先连接并查询主机')

    async def _job(self,op,peers=None):
        self._validate(); self.id=self.id%0x7fffffff+1
        data={'id':self.id,'op':op}
        expected_host=dict(self.host)
        if op in (1,2):
            data.update(host_sn=self.host['sn'],host_ble=self.host['ble'],
                        peers=[{'sn':p['sn'],'ble':p['ble']} for p in peers])
            if op==2: data['replace']=True
        message={'v':1,'type':'host','cmd':'factory.pair','data':data}
        rows={'live':{},'saved':{}}; started=datetime.now(timezone.utc).isoformat()
        sent=False
        try:
            # Mark before send: a missing ACK can follow a successfully executed command.
            sent=True; await self.transport.send(message)
            deadline=time.monotonic()+45; retry=time.monotonic()+4
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0: raise PairError('等待出厂配对完成回执超时')
                try: msg=await self.transport.receive(min(remaining,max(.01,retry-time.monotonic())))
                except asyncio.TimeoutError:
                    # Same id + exact payload: host replays result, never starts a second write.
                    await self.transport.send(message); retry=time.monotonic()+4; continue
                if msg.get('cmd')=='error':
                    raise PairError('主机拒绝出厂配对命令；请升级到支持 BLE 出厂配对的固件')
                r=msg.get('data',{})
                if r.get('id')!=self.id: continue
                if msg.get('cmd')=='factory.pair.info':
                    kind=r.get('kind'); slot=r.get('slot')
                    if kind not in rows or type(slot) is not int or slot not in (0,1) or not sn_valid(r.get('sn')) or not mac_valid(r.get('ble')) or r.get('model') not in (1,2):
                        raise PairError('主机返回的秤身份信息无效')
                    if slot in rows[kind] and identity(rows[kind][slot])!=identity(r): raise PairError('同一请求中秤身份发生变化，请重新查询')
                    rows[kind][slot]=r; continue
                if msg.get('cmd')!='factory.pair.result': continue
                if r.get('op')!=op: raise PairError('主机操作回执不匹配')
                if r.get('code')!=0: raise PairError('主机处理失败：'+str(r.get('detail','unknown')))
                if r.get('host_sn')!=expected_host['sn'] or r.get('host_ble')!=expected_host['ble']:
                    raise PairError('回执主机身份变化，请重新连接')
                counts=(r.get('count'),r.get('saved'))
                if any(type(n) is not int or not 0<=n<=2 for n in counts): raise PairError('回执设备数量无效')
                if any(set(rows[k])!=set(range(n)) for k,n in zip(('live','saved'),counts)):
                    # A final result with a missing part is not a complete report.
                    await self.transport.send(message); retry=time.monotonic()+4; continue
                result={'host':expected_host,'live':[rows['live'][i] for i in range(counts[0])],
                        'saved':[rows['saved'][i] for i in range(counts[1])],'result':r,'started_utc':started}
                if op in (1,2) and [identity(p) for p in result['saved']]!=[identity(p) for p in peers]:
                    raise PairError('写后回读的出厂套装与本次选择不一致')
                if op==3:
                    expected=self.last_save['saved'] if self.last_save else self.inventory['saved']
                    if [identity(p) for p in result['saved']]!=[identity(p) for p in expected]:
                        raise PairError('复验套装与已确认记录不一致')
                self.inventory=result
                self.event('inventory',result)
                return result
        except asyncio.CancelledError:
            if op in (1,2) and sent: raise PairUncertain('写入期间操作中断；结果未确认，请重新连接查询，勿直接返工覆盖') from None
            raise
        except Exception as exc:
            if op in (1,2) and sent:
                self.inventory=None
                raise PairUncertain(str(exc)+'；本次写入未确认，请重新连接查询后再处理') from exc
            raise

    async def query(self):
        self.inventory=None
        return await self._job(0)

    async def save(self,peers,rework=False):
        self._validate()
        if not self.inventory: raise PairError('请先查询当前秤和出厂记录')
        live=self.inventory['live']
        if len(peers) not in (1,2) or len(peers)!=len(live) or {identity(p) for p in peers}!={identity(p) for p in live}:
            raise PairError('选择的套装必须包含当前全部在线秤；不属于本套装的秤请先断开')
        if len({p['sn'] for p in peers})!=len(peers) or len({p['ble'] for p in peers})!=len(peers): raise PairError('上下秤不可选择同一设备')
        if len(peers)==2 and any(p['model']!=2 for p in peers): raise PairError('双秤出厂套装仅支持两台 SC202 大秤')
        if not any(p.get('docked') for p in live): raise PairError('请先磁吸本套装的一台秤，再查询一次')
        result=await self._job(2 if rework else 1,peers)
        self.last_save=result
        return self._report('write_readback',result,False)

    async def verify(self,cold_power_confirmed=False):
        self._validate()
        if not cold_power_confirmed: raise PairError('请先确认主从机均已完整断电重开，并移开磁吸')
        if not self.inventory or self.inventory['result'].get('record')!=1 or not self.inventory['saved']:
            raise PairError('主机没有有效出厂记录，请先查询或完成保存')
        if any(p.get('docked') for p in self.inventory['live']): raise PairError('纯 BLE 复验前请移开磁吸，再查询一次')
        result=await self._job(3)
        return self._report('cold_power_ble_verify',result,True)

    def _report(self,stage,result,qualified):
        report={'schema':1,'stage':stage,'qualified':qualified,'cold_power_confirmed_by_operator':qualified,
                'finished_utc':datetime.now(timezone.utc).isoformat(),**result}
        self.reports.append(report)
        try:
            self.report_dir.mkdir(parents=True,exist_ok=True)
            name=datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'_'+result['host']['sn']+'_'+stage+'.json'
            path=self.report_dir/name; path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            report['report_path']=str(path.resolve())
        except OSError as exc:
            report['report_error']=str(exc)
            self.event('log','设备操作已完成，但报告自动保存失败，请使用“导出报告”：'+str(exc))
        self.event('report',report)
        return report
