"""Bounded, passive network observations. Never open a camera or scan a subnet.

Only peer addresses from authenticated application requests are retained, in
memory for ten minutes. No users, cookies, URLs, device fingerprints or history
files are collected. Addresses are observations, not identities or access rules.
"""

from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time

import psutil

MAX_ROWS = 128
PEER_RETENTION_SECONDS = 600
OS_REFRESH_SECONDS = 30


def _text(value, limit=100):
    return ''.join(c for c in str(value or '') if c.isprintable())[:limit]


def _address(value, *, loopback=False):
    try:
        address = ipaddress.ip_address(value)
    except (ValueError, TypeError):
        return None
    if address.is_unspecified or address.is_multicast or (address.is_loopback and not loopback):
        return None
    if str(address) == '255.255.255.255':
        return None
    return str(address)


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _adapter_advisory(interfaces):
    """Compare observed IPv4 prefixes only; this is not a destination route lookup."""
    unavailable = {"state": "unavailable", "connected_count": None,
                   "overlap_count": 0, "incomplete": True}
    if not isinstance(interfaces, dict) or interfaces.get('available') is not True:
        return unavailable
    rows = interfaces.get('items')
    if not isinstance(rows, list):
        return unavailable
    incomplete = bool(interfaces.get('incomplete') or interfaces.get('truncated') or len(rows) > MAX_ROWS)
    connected = []
    for row in rows[:MAX_ROWS]:
        if not isinstance(row, dict):
            incomplete = True
            continue
        if row.get('up') is False:
            continue
        if row.get('up') is not True:
            incomplete = True
            continue
        values = row.get('addresses')
        if not isinstance(values, list):
            incomplete = True
            continue
        incomplete = incomplete or len(values) > 16
        prefixes = set()
        for value in values[:16]:
            try:
                if not isinstance(value, str) or '/' not in value:
                    raise ValueError('Missing IPv4 prefix')
                interface = ipaddress.IPv4Interface(value)
                if not _address(str(interface.ip)):
                    raise ValueError('Unsupported address')
                prefixes.add(interface.network)
            except (ValueError, TypeError):
                incomplete = True
        if prefixes:
            connected.append(prefixes)
        else:
            incomplete = True
    # Multiple addresses on one adapter do not establish multiple connections.
    overlaps = sum(any(left.overlaps(right) for left in prefixes for right in other)
                   for index, prefixes in enumerate(connected) for other in connected[index + 1:])
    count = len(connected)
    state = ('overlap' if overlaps else 'incomplete' if incomplete else
             'multiple' if count > 1 else 'single' if count == 1 else 'none')
    return {"state": state, "connected_count": count,
            "overlap_count": overlaps, "incomplete": incomplete}


class RecentClients:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.peers = OrderedDict()

    def _expire(self, now):
        for key, seen in list(self.peers.items()):
            if now - seen > PEER_RETENTION_SECONDS or now < seen:
                del self.peers[key]

    def observe(self, remote_address):
        address = _address(remote_address, loopback=True)
        if address is None:
            return
        now = self.clock()
        with self.lock:
            self._expire(now)
            self.peers[address] = now
            self.peers.move_to_end(address)
            while len(self.peers) > MAX_ROWS:
                self.peers.popitem(last=False)

    def snapshot(self):
        now = self.clock()
        with self.lock:
            self._expire(now)
            return [{"address": address, "age_seconds": round(now - seen, 1)}
                    for address, seen in reversed(self.peers.items())]


def observe_authenticated_client():
    """Called only after the normal database-backed login validation."""
    from flask import current_app, request
    # REMOTE_ADDR is set by the server. Never parse browser forwarding headers.
    registry = current_app.extensions.get('csd_recent_clients')
    if registry is not None:
        registry.observe(request.remote_addr)


def _windows_tables():
    if os.name != 'nt':
        return {"supported": False}
    executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    script = Path(__file__).resolve().parents[1] / 'scripts/windows/Read-NetworkStatus.ps1'
    result = subprocess.run(
        [str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(script)],
        capture_output=True, timeout=8, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode or len(result.stdout) > 131072:
        raise ValueError('Network tables unavailable')
    raw = json.loads(result.stdout.decode('utf-8-sig'))
    if not isinstance(raw, dict):
        raise ValueError('Network tables unavailable')
    tables = {"supported": True, "routes_available": raw.get('routesAvailable') is True,
              "neighbors_available": raw.get('neighborsAvailable') is True,
              "truncated": raw.get('neighborsTruncated') is True, "routes": [], "neighbors": []}
    for row in (raw.get('routes') or [])[:32]:
        address = _address(row.get('gateway'))
        if address:
            tables['routes'].append({"interface": _text(row.get('interface')), "gateway": address})
    states = {'Unreachable', 'Incomplete', 'Probe', 'Delay', 'Stale', 'Reachable', 'Permanent'}
    for row in (raw.get('neighbors') or [])[:MAX_ROWS]:
        address = _address(row.get('address'))
        if not address:
            continue
        mac = str(row.get('mac', '')).upper().replace(':', '-')
        valid_mac = bool(re.fullmatch(r'(?:[0-9A-F]{2}-){5}[0-9A-F]{2}', mac))
        # Broadcast/multicast cache entries do not represent individual devices.
        if valid_mac and int(mac[:2], 16) & 1:
            continue
        if not valid_mac or mac == '00-00-00-00-00-00':
            mac = None
        tables['neighbors'].append({"interface": _text(row.get('interface')), "address": address,
                                    "mac": mac, "state": row.get('state') if row.get('state') in states else 'Unknown'})
    return tables


class NetworkMonitor:
    def __init__(self, clock=time.monotonic, collector=_windows_tables):
        self.clock = clock
        self.collector = collector
        self.lock = threading.Lock()
        self.busy = False
        self.attempted = None
        self.collected = None
        self.tables = None
        self.previous_io = {}
        self.interface_result = None
        self.interface_at = None

    def _collect(self):
        try:
            tables = self.collector()
        except Exception:
            # Never expose command output or stale success after an error.
            tables = {"supported": os.name == 'nt', "routes_available": False,
                      "neighbors_available": False, "routes": [], "neighbors": []}
        with self.lock:
            self.tables = tables
            self.collected = self.clock()
            self.busy = False

    def _tables_snapshot(self, now):
        if not self.busy and (self.attempted is None or now - self.attempted >= OS_REFRESH_SECONDS):
            self.busy = True
            self.attempted = now
            try:
                threading.Thread(target=self._collect, name='csd-network-tables', daemon=True).start()
            except RuntimeError:
                self.busy = False
        age = None if self.collected is None else max(0, now - self.collected)
        result = deepcopy(self.tables or {})
        if age is not None and age > OS_REFRESH_SECONDS * 3:
            result.update(routes_available=False, neighbors_available=False, routes=[], neighbors=[])
        return {**result, "refreshing": self.busy, "age_seconds": None if age is None else round(age, 1)}

    def _interfaces(self, now):
        if self.interface_at is not None and now - self.interface_at < 5:
            return deepcopy(self.interface_result)
        self.interface_at = now
        try:
            addresses, stats = psutil.net_if_addrs(), psutil.net_if_stats()
            counters = psutil.net_io_counters(pernic=True, nowrap=False)
            rows = []
            next_io = {}
            incomplete = len(addresses) > MAX_ROWS
            for name, assigned in list(addresses.items())[:MAX_ROWS]:
                ips = []
                stat = stats.get(name)
                if len(assigned) > 16 and (stat is None or stat.isup):
                    incomplete = True
                for item in assigned[:16]:
                    if item.family == socket.AF_INET:
                        if not _address(item.address):
                            # Loopback is intentionally excluded, not missing information.
                            try:
                                is_loopback = ipaddress.IPv4Address(item.address).is_loopback
                            except (ValueError, TypeError):
                                is_loopback = False
                            if not is_loopback and (stat is None or stat.isup):
                                incomplete = True
                            continue
                        try:
                            interface = ipaddress.ip_interface(f'{item.address}/{item.netmask}')
                        except (ValueError, TypeError):
                            if stat is None or stat.isup:
                                incomplete = True
                            continue
                        ips.append(str(interface))
                if not ips:
                    continue
                if stat is None:
                    incomplete = True
                counter = counters.get(name)
                previous = self.previous_io.get(name)
                rates = {"received_mbps": None, "sent_mbps": None, "new_errors": None, "new_discards": None, "sample_seconds": None}
                if counter:
                    values = tuple(getattr(counter, key) for key in ('bytes_recv', 'bytes_sent', 'errin', 'errout', 'dropin', 'dropout'))
                    # A reconnect, address change, counter reset or long gap starts a new baseline.
                    identity = (tuple(ips), bool(stat and stat.isup))
                    if previous and previous[2] == identity and identity[1]:
                        elapsed = now - previous[0]
                        deltas = [current - old for current, old in zip(values, previous[1])]
                        if 1 <= elapsed <= 60 and all(delta >= 0 for delta in deltas):
                            rates.update(received_mbps=round(deltas[0] * 8 / elapsed / 1e6, 3),
                                         sent_mbps=round(deltas[1] * 8 / elapsed / 1e6, 3),
                                         new_errors=sum(deltas[2:4]), new_discards=sum(deltas[4:6]),
                                         sample_seconds=round(elapsed, 1))
                    next_io[name] = (now, values, identity)
                rows.append({"name": _text(name), "addresses": ips,
                             "up": None if stat is None else bool(stat.isup),
                             "link_mbps": stat.speed if stat and stat.speed > 0 else None, **rates})
            self.previous_io = next_io
            self.interface_result = {"available": True, "items": rows,
                                     "truncated": len(addresses) > MAX_ROWS, "incomplete": incomplete}
        except Exception:
            self.previous_io = {}
            self.interface_result = {"available": False, "items": []}
        return deepcopy(self.interface_result)

    def snapshot(self):
        now = self.clock()
        with self.lock:
            interfaces = self._interfaces(now)
            tables = self._tables_snapshot(now)
            # Windows can cache failed lookups of this host's own addresses.
            # They belong under Server connections, not the other-device list.
            own_addresses = {value.split('/')[0] for row in interfaces['items'] for value in row['addresses']}
            if tables.get('neighbors_available'):
                tables['neighbors'] = [row for row in tables.get('neighbors', [])
                                       if row['address'] not in own_addresses]
            return {"observed_at": _stamp(), "interfaces": interfaces, "windows": tables,
                    "adapter_advisory": _adapter_advisory(interfaces)}


def install_network_monitor(app):
    app.extensions.setdefault('csd_recent_clients', RecentClients())
    app.extensions.setdefault('csd_network_monitor', NetworkMonitor())
