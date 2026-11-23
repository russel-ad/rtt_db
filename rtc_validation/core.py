"""Protocol and durable drift analysis, independent of Qt and hardware.

epoch is a UTC whole-second reading. A read represents [epoch, epoch+1).
The request/response bracket bounds transport and UI/decoder delays.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import csv
import json
import math
import re
import secrets
import time


@dataclass(frozen=True)
class Stamp:
    utc: float
    monotonic: float

    @classmethod
    def now(cls):
        return cls(time.time(), time.perf_counter())


def parse_sample(line: str) -> dict | None:
    marker = re.search(r'\bRTCV1 id=', line)
    if marker is None:
        return None
    parts = line[marker.start():].strip().split()
    values = {}
    for part in parts[1:]:
        key, sep, value = part.partition('=')
        if not sep or key in values:
            raise ValueError('Malformed or duplicate RTC field')
        values[key] = value
    required = 'id boot tick hz epoch rc c1 c2 adj init2 inita initrc reads errors writes lock sid fw'.split()
    if any(k not in values for k in required) or values['fw'] != 'RTC1':
        raise ValueError('Unsupported RTC test firmware/protocol')
    result = {'fw': values['fw']}
    for k in required[:-1]:
        if re.fullmatch(r'-?\d{1,10}', values[k]) is None:
            raise ValueError('Invalid RTC number: ' + k)
        result[k] = int(values[k])
        if not (-2147483648 <= result[k] <= 2147483647 if k in ('rc', 'initrc')
                else 0 <= result[k] <= 0xffffffff):
            raise ValueError('RTC field out of range: ' + k)
    if not 1 <= result['hz'] <= 1000000 or result['lock'] not in (0, 1):
        raise ValueError('Invalid device clock/lock')
    if any(result[k] > 255 for k in ('c1','c2','adj','init2','inita')):
        raise ValueError('Invalid register byte')
    if result['rc'] == 0 and not 946684800 <= result['epoch'] < 4102444800:
        raise ValueError('RTC date outside 2000..2099')
    return result


CSV_FIELDS = ['index','utc','request_utc','response_utc','rtt_s','boot','tick','hz',
              'epoch','rc','c1','c2','adj','init2','inita','initrc','reads','errors',
              'writes','lock','sid','elapsed_s','offset_s','offset_bound_s',
              'drift_s','drift_bound_s','seconds_per_day','rate_bound_per_day','ppm','flags']


class Session:
    def __init__(self, directory: Path, *, device: str, reference: str,
                 reference_bound_s: float | None = None, limit_s_day: float | None = None,
                 session_id: int | None = None):
        if reference_bound_s is not None and (not math.isfinite(reference_bound_s) or reference_bound_s < 0):
            raise ValueError('Invalid reference uncertainty')
        if limit_s_day is not None and (not math.isfinite(limit_s_day) or limit_s_day <= 0):
            raise ValueError('Invalid acceptance limit')
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.sid = session_id or secrets.randbelow(0xfffffffe)+1
        self.metadata = {'protocol':'RTCV1','session_id':self.sid,'device':device,
                         'reference':reference or 'PC UTC (unverified)',
                         'reference_bound_s':reference_bound_s,'limit_seconds_per_day':limit_s_day,
                         'created_utc':datetime.now(timezone.utc).isoformat(),
                         'hardware_validated':False}
        self.baseline = None
        self.previous = None
        self.last_valid = None
        self.count = 0
        self.boot_changes = 0
        self.review_flags = set()
        self.closed = False
        self._csv = (self.directory/'samples.csv').open('x',encoding='utf-8-sig',newline='')
        self._writer = csv.DictWriter(self._csv,fieldnames=CSV_FIELDS)
        self._writer.writeheader(); self._csv.flush()
        self._events = (self.directory/'events.jsonl').open('x',encoding='utf-8')
        self.event('created',**self.metadata)
        self._save_report()

    def event(self, kind, **details):
        self._events.write(json.dumps({'utc':datetime.now(timezone.utc).isoformat(),
                                     'kind':kind,**details},ensure_ascii=False)+'\n')
        self._events.flush()

    def flag(self, name, **details):
        self.review_flags.add(name)
        self.event(name,**details)
        self._save_report()

    def add(self, sample: dict, sent: Stamp, received: Stamp):
        if self.closed: raise RuntimeError('Session closed')
        self.count += 1
        rtt = received.monotonic-sent.monotonic
        mid = (sent.utc+received.utc)/2
        flags = []
        if rtt < 0 or rtt > 2:
            flags.append('transport_delay')
        if abs((received.utc-sent.utc)-rtt) > .25:
            flags.append('pc_clock_changed_during_read')
        if sample['rc'] != 0:
            flags.append('rtc_read_error')
        if sample['c2'] & 0x10:
            flags.append('rtc_stop_recorded')
        if sample['sid'] != self.sid or not sample['lock']:
            flags.append('device_not_locked_to_session')
        row = {k:sample[k] for k in CSV_FIELDS if k in sample}
        row.update(index=self.count,utc=mid,request_utc=sent.utc,response_utc=received.utc,rtt_s=rtt,
                   elapsed_s='',offset_s='',offset_bound_s='',drift_s='',drift_bound_s='',
                   seconds_per_day='',rate_bound_per_day='',ppm='')
        # Record errors verbatim; only a valid locked read can establish a baseline.
        if not flags:
            offset = sample['epoch']+.5-mid
            bound = .5+rtt/2
            current = dict(row,offset_s=offset,offset_bound_s=bound,
                           sent_mono=sent.monotonic,mid_mono=(sent.monotonic+received.monotonic)/2)
            if self.baseline is None:
                self.baseline = current
                self.event('baseline',sample=current)
            b = self.baseline
            elapsed = mid-b['utc']
            if abs(elapsed-(current['mid_mono']-b['mid_mono'])) > .25:
                flags.append('pc_clock_changed_since_baseline')
            if sample['adj'] != b['adj']:
                flags.append('rtc_adjust_changed')
            prev = self.previous
            if prev:
                if sample['boot'] != prev['boot']:
                    self.boot_changes += 1
                    self.event('boot_changed',before=prev['boot'],after=sample['boot'])
                    if sample['init2'] & 0x10 or sample['initrc'] != 0:
                        flags.append('invalid_rtc_at_reboot')
                    # New boot is allowed only after an explicit re-lock to the
                    # same session; writes must remain zero on the new boot.
                    if sample['writes'] != 0: flags.append('time_written_after_reboot')
                elif sample['writes'] != prev['writes']:
                    flags.append('time_write_during_test')
                if sample['errors'] > prev['errors'] and sample['boot']==prev['boot']:
                    flags.append('device_read_errors_increased')
                observed = sample['epoch']-prev['epoch']
                expected = mid-prev['utc']
                if observed < 0 or (0 < expected <= 300 and abs(observed-expected) > 3+bound+prev['offset_bound_s']):
                    flags.append('rtc_time_jump')
            drift = offset-b['offset_s']
            reference_bound = self.metadata['reference_bound_s'] or 0
            drift_bound = bound+b['offset_bound_s']+2*reference_bound
            row.update(elapsed_s=elapsed,offset_s=offset,offset_bound_s=bound,
                       drift_s=drift,drift_bound_s=drift_bound)
            if elapsed > 0:
                row.update(seconds_per_day=drift/elapsed*86400,
                           rate_bound_per_day=drift_bound/elapsed*86400,
                           ppm=drift/elapsed*1e6)
            self.previous = current
            self.last_valid = dict(row)
        row['flags'] = '|'.join(flags)
        self.review_flags.update(flags)
        self._writer.writerow(row); self._csv.flush()
        self.event('sample',raw=sample,sent=sent.__dict__,received=received.__dict__,result=row)
        self._save_report()
        return row

    def report(self):
        last = self.last_valid or {}
        elapsed = last.get('elapsed_s',0) or 0
        status = 'COLLECTING' if not self.closed else 'RECORDED'
        limit = self.metadata['limit_seconds_per_day']
        if self.review_flags or not self.baseline:
            status = 'REVIEW_REQUIRED'
        elif elapsed >= 86400 and limit and self.metadata['reference_bound_s'] is not None:
            rate, bound = last['seconds_per_day'],last['rate_bound_per_day']
            if abs(rate)+bound <= limit: status='PASS_RELATIVE_TO_DECLARED_REFERENCE'
            elif abs(rate)-bound > limit: status='FAIL_RELATIVE_TO_DECLARED_REFERENCE'
            else: status='INCONCLUSIVE'
        elif elapsed >= 86400:
            status='MEASURED_RELATIVE_TO_PC'
        return {**self.metadata,'status':status,'closed':self.closed,'samples':self.count,
                'boot_changes':self.boot_changes,'review_flags':sorted(self.review_flags),
                'baseline':self.baseline,'latest':last,
                'limitations':['RTC second quantization and request bracket are included.',
                              'Reference uncertainty is declared by the operator, not measured by this tool.',
                              'PASS requires >=24h, a supplied limit, a declared reference bound, and no review flags.',
                              'Gradual PC clock correction may be indistinguishable from RTC drift; recheck the reference.']}

    def _save_report(self):
        p=self.directory/'report.json'; temporary=p.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.report(),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        temporary.replace(p)

    def close(self):
        if self.closed: return
        self.closed=True
        self.event('closed')
        self._save_report()
        self._csv.close(); self._events.close()


class Collector:
    """One outstanding request; IDs reject delayed, unsolicited and duplicate replies."""
    def __init__(self, send, on_sample, on_event, clock=Stamp.now):
        self.send,self.on_sample,self.on_event,self.clock=send,on_sample,on_event,clock
        self.buffer=''
        self.sequence=secrets.randbelow(1000000000)+1
        self.pending=None

    def poll(self):
        if self.pending: return False
        self.sequence=(self.sequence+1)&0xffffffff
        sent=self.clock()
        self.pending=(self.sequence,sent)
        try: self.send(f'rtct get {self.sequence}\r\n'.encode('ascii'))
        except Exception:
            self.pending=None
            raise
        return True

    def check_timeout(self):
        if self.pending and self.clock().monotonic-self.pending[1].monotonic > 3:
            old=self.pending; self.pending=None
            self.on_event('request_timeout',{'id':old[0]})

    def feed(self, text, received=None):
        received=received or self.clock()
        self.buffer+=text
        if len(self.buffer)>8192:
            self.buffer=self.buffer[-4096:]
            self.on_event('input_overflow',{})
        while '\n' in self.buffer:
            line,self.buffer=self.buffer.split('\n',1)
            try: sample=parse_sample(line)
            except ValueError as exc:
                self.on_event('protocol_error',{'message':str(exc),'line':line}); continue
            if sample is None:
                marker=re.search(r'\bRTCV1 (ACK|ERR|SET)\b.*',line)
                if marker: self.on_event('reply',{'line':marker[0].strip()})
                continue
            if self.pending is None or sample['id'] != self.pending[0]:
                self.on_event('unmatched_sample',{'id':sample['id']}); continue
            _,sent=self.pending; self.pending=None
            self.on_sample(sample,sent,received)
