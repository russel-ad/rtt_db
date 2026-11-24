"""UART_BER_01 parsing/reporting from the firmware project diagnostic runner."""
import csv
import hashlib
import json
import math
import re

FW = 'V001-008-UART1'
NUMBER_FIELDS = set('run state flags t mode count len gap pattern tx_accept tx_done rx_good rx_bad rx_unique dup stale hdr crc foreign config noise bits diff peer_done peer_tx tx_pending rx_drop overrun frame break dma_err rearm_err submit_err rx_bytes dma_bytes no_data max_silence event_drop log_drop max_poll contact_edges reason'.split())
REQUIRED = set('role boot fw run state flags mode count len gap pattern tx_accept tx_done rx_good peer_done peer_tx tx_pending rx_drop overrun frame break dma_err rearm_err submit_err rx_bytes dma_bytes dup stale hdr crc foreign config noise bits diff contact_edges'.split())
CONFIG = ('run', 'mode', 'count', 'len', 'gap', 'pattern')
ERRORS = ('rx_drop', 'overrun', 'frame', 'break', 'dma_err', 'rearm_err', 'submit_err', 'dup', 'stale', 'hdr', 'crc', 'foreign', 'config', 'noise', 'diff', 'contact_edges')


def parse_line(line):
    line = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', line)
    m = re.search(r'UBER (STAT|FINAL|ACK|BOOT|ERR|SAMPLE)\s+(.*)', line)
    if not m:
        return None
    record = dict(re.findall(r'\b([a-z_]+)=([^\s]+)', m[2]))
    record['type'] = m[1]
    try:
        for k in record.keys() & NUMBER_FIELDS:
            record[k] = int(record[k])
        if m[1] in ('STAT', 'FINAL') and (not REQUIRED <= record.keys() or record['role'] not in ('H', 'S') or not re.fullmatch('[0-9A-Fa-f]{8}', record['boot'])):
            return None  # A partial RTT line cannot be final evidence.
    except ValueError:
        return None
    return record


class Stream:
    """Incremental ASCII lines with Telnet IAC filtering and split-line support."""
    def __init__(self):
        self.buffer = bytearray()
        self.iac = 0
        self.sub = False

    def feed(self, data):
        records = []
        for b in data:
            if self.sub:
                if self.iac == 1 and b == 240:
                    self.sub = False
                self.iac = 1 if b == 255 else 0
                continue
            if self.iac == 2:
                self.iac = 0
                continue
            if self.iac == 1:
                self.iac = 0
                if b == 250:
                    self.sub = True
                    continue
                if b in (251, 252, 253, 254):
                    self.iac = 2
                    continue
                if b != 255:
                    continue
            elif b == 255:
                self.iac = 1
                continue
            if b in (10, 13):
                if self.buffer:
                    record = parse_line(self.buffer.decode('utf-8', errors='replace'))
                    if record:
                        records.append(record)
                    self.buffer.clear()
            elif len(self.buffer) < 8192:
                self.buffer.append(b)
            else:
                self.buffer.clear()
        return records


def records_from(path):
    stream = Stream()
    records = []
    with path.open('rb') as f:
        while data := f.read(65536):
            records.extend(stream.feed(data))
    records.extend(stream.feed(b'\n'))
    return records


def report(host_records, scale_records, run):
    reasons = []
    finals = {}
    by_role = {'H': host_records, 'S': scale_records}
    for role, records in by_role.items():
        run_records = [r for r in records if r.get('run') == run and r.get('role') == role and 'boot' in r]
        selected = [r for r in records if r.get('run') == run and r.get('type') in ('STAT', 'FINAL')]
        if any(r['role'] != role for r in selected):
            reasons.append(role + ': endpoint role mismatch')
        selected = [r for r in selected if r['role'] == role]
        if len({r['boot'] for r in run_records}) > 1:
            reasons.append(role + ': boot identifier changed during this run')
        candidates = [r for r in selected if r['type'] == 'FINAL' and r['state'] == 3]
        if not candidates:
            reasons.append(role + ': complete FINAL snapshot absent')
            continue
        f = finals[role] = candidates[-1]
        if any(any(f[k] != r[k] for k in CONFIG + ('boot',)) for r in selected):
            reasons.append(role + ': run identifier reused with different settings/boot')
        if f['fw'] != FW:
            reasons.append(role + ': wrong test firmware')
        if f['mode'] not in (1, 2, 3) or not 1 <= f['count'] <= 1000000 or not 1 <= f['len'] <= 512 or not 0 <= f['gap'] <= 1000 or not 0 <= f['pattern'] <= 6:
            reasons.append(role + ': invalid configuration in final evidence')
        expected = f['count'] if f['mode'] & (1 if role == 'H' else 2) else 0
        if f['tx_done'] != expected or f['tx_accept'] != f['tx_done']:
            reasons.append(role + ': planned/accepted/drained TX counts differ')
        if f['dma_bytes'] != f['tx_done'] * (f['len'] + 36) + 36:
            reasons.append(role + ': DMA byte count does not close data plus DONE')
        if not f['peer_done'] or f['tx_pending']:
            reasons.append(role + ': completion handshake/wire drain not closed')
        if f['flags'] & ~16:  # LOCAL=16 is a definite error; the rest limit completeness.
            reasons.append(role + ': aborted/deadline/configuration/stale/OTA flag')
    if len(finals) == 2 and any(finals['H'][k] != finals['S'][k] for k in CONFIG):
        reasons.append('Host and scale settings differ')
    directions = {}
    definite_errors = []
    if len(finals) == 2:
        for name, tx_role, rx_role in (('H_to_S', 'H', 'S'), ('S_to_H', 'S', 'H')):
            tx, rx = finals[tx_role], finals[rx_role]
            sent, good = tx['tx_done'], rx['rx_good']
            if rx['peer_tx'] != sent:
                reasons.append(name + ': peer completion count differs from sender final')
            if good > sent:
                reasons.append(name + ': receiver unique count exceeds actual sender count')
            failed = max(0, sent - good)
            observed_errors = {k: rx[k] for k in ERRORS if rx[k]}
            if failed or observed_errors or (rx['flags'] & 16):
                definite_errors.append(name)
            directions[name] = {
                'sent_drained_frames': sent, 'received_correct_unique_frames': good,
                'failed_or_missing_frames': failed, 'frame_failure_ratio': failed / sent if sent else None,
                'compared_payload_bits': rx['bits'], 'different_payload_bits': rx['diff'],
                'aligned_payload_diff_ratio': rx['diff'] / rx['bits'] if rx['bits'] else None,
                'physical_BER_measured': False,
                'zero_frame_failures_upper_95': -math.expm1(math.log(0.05) / sent) if sent and failed == 0 and not observed_errors else None,
                'observed_error_counters': observed_errors,
            }
    return {'schema': 1, 'test': 'UART_BER_01', 'run': run,
            'verdict': 'INCOMPLETE' if reasons else ('FAIL' if definite_errors else 'PASS'),
            'incomplete_reasons': reasons, 'directions_with_errors': definite_errors, 'directions': directions,
            'finals': finals, 'detail_logs_complete': all(not f.get('log_drop', 0) and not f.get('event_drop', 0) for f in finals.values()) and len(finals) == 2,
            'scope': 'Observed UART data-frame integrity for this run. Payload bit differences exclude unalignable/lost data and start/stop bits; confidence bound assumes stationary independent frame trials.'}


def save_report(output, paths, run, *, metadata, failure, stopped):
    records = [records_from(p) for p in paths]
    result = report(*records, run)
    result['evidence'] = {p.name: {'sha256': hashlib.sha256(p.read_bytes()).hexdigest(), 'bytes': p.stat().st_size} for p in paths}
    output.mkdir(parents=True, exist_ok=True)
    result['requested'] = metadata
    result['stop_status'] = stopped
    if failure:
        result['verdict'] = 'INCOMPLETE'
        result['controller_error'] = failure
    snapshots = [dict(r, source=p.name) for p, rs in zip(paths, records) for r in rs if r['type'] in ('STAT', 'FINAL') and r['run'] == run]
    fields = ['source', 'type', 'role', 'boot'] + sorted(set().union(*(r.keys() for r in snapshots)) - {'source', 'type', 'role', 'boot'})
    with (output / 'snapshots.csv').open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(snapshots)
    with (output / 'directions.csv').open('w', encoding='utf-8-sig', newline='') as f:
        fields = ['direction', 'sent_drained_frames', 'received_correct_unique_frames', 'failed_or_missing_frames', 'frame_failure_ratio', 'compared_payload_bits', 'different_payload_bits', 'aligned_payload_diff_ratio', 'physical_BER_measured', 'zero_frame_failures_upper_95']
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        for name, values in result['directions'].items():
            w.writerow(dict(values, direction=name))
    pending = output / 'summary.tmp'
    pending.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    pending.replace(output / 'summary.json')
    return result
