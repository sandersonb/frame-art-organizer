"""Passive Linux resource sampling; never opens a network connection."""
from __future__ import annotations

import ipaddress
import os
from collections import Counter
from pathlib import Path


def process_record(path):
    # comm may contain spaces or parentheses; numeric fields follow its LAST ')'.
    stat = (path / 'stat').read_text().rsplit(')', 1)[1].split()
    status = dict(line.split(':', 1) for line in (path / 'status').read_text().splitlines() if ':' in line)
    out = {'pid': int(path.name), 'ppid': int(stat[1]), 'state': stat[0],
           'start_ticks': int(stat[19]), 'rss_kib': int(status.get('VmRSS', '0 kB').split()[0]),
           'threads': int(status['Threads']), 'fd_count': None, 'socket_count': None}
    sockets = set()
    try:
        fds = list((path / 'fd').iterdir())
        out['fd_count'] = len(fds)
        for fd in fds:
            try:
                target = os.readlink(fd)
                if target.startswith('socket:['):
                    sockets.add(target[8:-1])
            except OSError:
                pass  # Descriptor may close during sampling.
        out['socket_count'] = len(sockets)
    except OSError:
        out['fd_partial'] = True
    return out, sockets


def endpoint_address(value):
    address = bytes.fromhex(value.split(':')[0])
    # Linux /proc/net encodes each 32-bit word in native byte order.
    import sys
    if sys.byteorder == 'little':
        address = b''.join(address[i:i+4][::-1] for i in range(0, len(address), 4))
    return ipaddress.ip_address(address)


def snapshot(root_pid, *, host, proc=Path('/proc'), tracked_workers=None):
    proc = Path(proc)
    records, sockets = {}, {}
    skipped = 0
    for path in proc.iterdir():
        if not path.name.isdigit():
            continue
        try:
            record, owned = process_record(path)
            records[record['pid']], sockets[record['pid']] = record, owned
        except (OSError, ValueError, KeyError, IndexError):
            skipped += 1
    family = {root_pid} if root_pid in records else set()
    while True:
        added = {pid for pid, row in records.items() if row['ppid'] in family} - family
        if not added:
            break
        family.update(added)
    family_sockets = set().union(*(sockets[pid] for pid in family))
    tcp_states, tv_states = Counter(), Counter()
    tcp_partial = False
    wanted = ipaddress.ip_address(host)
    for name in ('tcp', 'tcp6'):
        try:
            for line in (proc / str(root_pid) / 'net' / name).read_text().splitlines()[1:]:
                fields = line.split()
                state = fields[3]  # Fixed kernel state code, no endpoint/token output.
                if fields[9] in family_sockets:
                    tcp_states[state] += 1
                remote = endpoint_address(fields[2])
                if remote == wanted or getattr(remote, 'ipv4_mapped', None) == wanted:
                    tv_states[state] += 1
        except (OSError, ValueError, IndexError):
            tcp_partial = True
    system = {'readable_processes': len(records), 'unreadable_or_exited_processes': skipped}
    try:
        mem = dict(line.split(':', 1) for line in (proc / 'meminfo').read_text().splitlines())
        system['mem_available_kib'] = int(mem['MemAvailable'].split()[0])
    except (OSError, ValueError, KeyError):
        system['memory_partial'] = True
    survivors = [records[pid] for pid, ticks in (tracked_workers or {}).items()
                 if pid in records and records[pid]['start_ticks'] == ticks]
    for pid in family - {root_pid}:
        try:
            command = (proc / str(pid) / 'cmdline').read_bytes()
            records[pid]['role'] = ('multiprocessing_tracker' if b'multiprocessing.resource_tracker' in command
                                    else 'other_child')
        except OSError:
            records[pid]['role'] = 'unknown'
    return {'parent': records.get(root_pid), 'tracked_worker_survivors': survivors,
            'descendants': [records[pid] for pid in sorted(family - {root_pid})],
            'family_tcp_states': dict(tcp_states), 'tv_tcp_states_systemwide': dict(tv_states),
            'tcp_partial': tcp_partial, 'process_scan_partial': bool(skipped), 'system': system,
            'measurement': 'non-atomic OS sample; TCP 01=established, 06=time-wait, 08=close-wait'}
