"""OS-owned per-window leases, used for update UI coordination only.

The shared session.lock is the authoritative installation barrier. Lease files
are never treated as proof of life without checking their OS lock.
"""
from .core import LockBusy, file_lock


def other_instances(home, own_id):
    for path in (home / 'sessions').glob('*.lock'):
        if path.stem == own_id:
            continue
        try:
            with file_lock(path):
                pass
        except LockBusy:
            return True
    return False
