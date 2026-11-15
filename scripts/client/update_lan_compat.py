"""Carry LAN release metadata across one verified, compatible signed update.

Internal API: a Windows launcher owns the lifecycle mutex and supplies its
verified guard. No network, database, environment or certificate-store changes.
The original update recovery copy is also the interruption/replay journal.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from types import FunctionType

from scripts.client import update_bootstrap as bootstrap
from scripts.client import update_package as package
from scripts.client import update_recovery as recovery
from scripts.client import update_transaction as transaction
from scripts.lan import lan_setup as lan

SELF = 'scripts/client/update_lan_compat.py'
STATE = '.lan-server/state.json'
CONNECTION = 'LAN_CLIENT_KIT/CONNECTION.json'
SUMS = 'LAN_CLIENT_KIT/SHA256SUMS'
RELEASE_FIELDS = ('sourceCommit', 'bundleVersion')


class LanCompatibilityError(RuntimeError):
    """A compatible signed predecessor could not be established safely."""


def _require(condition):
    if not condition:
        raise LanCompatibilityError('The saved LAN setup cannot be carried forward safely. Keep the installation and update recovery files unchanged.')


def _read(path, limit=package.MAX_FILE_BYTES):
    return bootstrap._read(Path(path), limit)


def _json(path, limit=32768):
    return bootstrap._json(_read(path, limit))


def _record(data):
    return hashlib.sha256(data).hexdigest(), len(data)


def _encode(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode('utf-8')


def needs_reconciliation(root):
    """Read-only launcher gate; ordinary LAN validation still follows this."""
    root = recovery._normal(Path(root), directory=True)
    _require(not bootstrap._normal(root / '.updates/pending.json').exists())
    if not bootstrap._normal(root / STATE).exists():
        return False
    state, manifest = _json(root / STATE), _json(root / package.BUNDLE_MANIFEST)
    return any(state.get(name) != manifest.get(name) for name in RELEASE_FIELDS)


def _predecessor(root, current, publishers):
    receipts = list((root / '.updates').glob('finished-*-applied.json'))
    _require(len(receipts) <= 128)
    matches = []
    for receipt in receipts:
        match = re.fullmatch('finished-([0-9a-f]{32})-applied.json', receipt.name)
        _require(match is not None)
        record = _json(receipt, 4096)
        if record.get('to_commit') != current['sourceCommit']:
            continue
        _require(set(record) == {'format', 'transaction', 'from_commit', 'to_commit', 'archive_sha256', 'phase'}
                 and record['format'] == 1 and record['phase'] == 'validated'
                 and record['transaction'] == match[1])
        folder = recovery._normal(root / '.updates' / match[1], directory=True)
        update = package.verify_update_bytes(_read(folder / 'package.zip', package.MAX_ARCHIVE_BYTES),
                                             publishers, from_commit=record['from_commit'])
        _require(update.to_commit == current['sourceCommit'] and update.archive_sha256 == record['archive_sha256'])
        _require(_json(folder / 'result.json', 4096) == {'status': 'applied', 'commit': update.to_commit})
        original = _read(folder / 'original' / package.BUNDLE_MANIFEST)
        _require(_record(original) == update.before[package.BUNDLE_MANIFEST])
        old_manifest = bootstrap._json(original)
        _require(old_manifest['sourceCommit'] == update.from_commit)
        _require(SELF in update.after and 'scripts/lan/lan_setup.py' in update.after)
        # A receipt alone is never authorization: verify the publisher and the
        # complete installed application inventory, including this helper.
        for name, expected in update.after.items():
            _require(_record(_read(root / name)) == expected)
        # LAN protocol and runtime identity are immutable in update format v1.
        _require(old_manifest.get('caddy') == current.get('caddy'))
        matches.append((folder, update, old_manifest))
    _require(len(matches) == 1)
    return matches[0]


def _snapshot_files(root, folder, run_id):
    snapshot = folder / 'private-recovery'
    record = _json(snapshot / 'recovery.json', recovery.MAX_MANIFEST_BYTES)
    _require(record.get('format') == 1 and record.get('root') == os.path.normcase(str(root))
             and record.get('run_id') == run_id and isinstance(record.get('files'), dict))
    names = {name for name in record['files'] if name.startswith(('.lan-server/', 'LAN_CLIENT_KIT/'))}
    _require(STATE in names and len(names) <= recovery.MAX_FILES)
    result, total = {}, 0
    for name in names:
        _require('..' not in name.split('/') and '\\' not in name)
        expected = record['files'][name]
        _require(isinstance(expected, dict) and type(expected.get('bytes')) is int
                 and 0 <= expected['bytes'] <= package.MAX_FILE_BYTES)
        total += expected['bytes']
        _require(total <= recovery.MAX_PRIVATE_BYTES)
        data = _read(snapshot / 'files' / name, expected['bytes'])
        _require(expected.get('sha256') == _record(data)[0] and expected['bytes'] == len(data))
        result[name] = data
    return snapshot / 'files', result


def _validate_old_lan(root, snapshot, old_manifest, state):
    # Rebind the unchanged LAN validator's functions to isolated globals. The
    # frozen manifest and real installation root retain its original firewall
    # identity while all private reads target the existing recovery copy.
    namespace = dict(vars(lan))
    for name, value in vars(lan).items():
        if isinstance(value, FunctionType) and value.__globals__ is vars(lan):
            copied = FunctionType(value.__code__, namespace, value.__name__, value.__defaults__, value.__closure__)
            copied.__kwdefaults__ = value.__kwdefaults__
            namespace[name] = copied
    namespace['_validate_bundle'] = lambda _path: old_manifest
    namespace['_expected_state'] = lambda _path, *args, **kwargs: lan._expected_state(root, *args, **kwargs)
    if state.get('status') == 'ready':
        _require(namespace['check_ready'](snapshot) == state)
    else:
        _require(state.get('status') == 'prepared' and not (snapshot / 'LAN_CLIENT_KIT').exists())
        namespace['_validate_ready_or_prepared_state'](snapshot, state)


def _reconcile(root, guard):
    root = recovery._normal(Path(root), directory=True)
    guard.assert_exclusive_access()
    _require(not bootstrap._normal(root / '.updates/pending.json').exists())
    if not bootstrap._normal(root / STATE).exists():
        return {'status': 'not_configured'}
    current = lan._validate_bundle(root)
    state = _json(root / STATE)
    if all(state.get(name) == current.get(name) for name in RELEASE_FIELDS):
        return {'status': 'unchanged', 'commit': current['sourceCommit']}
    guard.assert_private_access()
    marker = _json(root / '.client-state')
    _require(marker.get('status') == 'complete')
    anchor = bootstrap.verify_bootstrap(root, owner_sid=guard.owner_sid, run_id=marker.get('run_id'),
                                        assert_private_access=guard.assert_private_access)
    folder, update, old_manifest = _predecessor(root, current, bootstrap.read_publishers(anchor['publishers']))
    guard.assert_private_access(folder)
    snapshot, before = _snapshot_files(root, folder, marker['run_id'])
    old_state = bootstrap._json(before[STATE])
    _require(all(old_state.get(name) == old_manifest.get(name) for name in RELEASE_FIELDS))
    _validate_old_lan(root, snapshot, old_manifest, old_state)
    new_state = {**old_state, **{name: current[name] for name in RELEASE_FIELDS}}
    after = {STATE: _encode(new_state)}
    if old_state['status'] == 'ready':
        old_connection = bootstrap._json(before[CONNECTION])
        _require(old_connection == lan._connection_document(old_state))
        after[CONNECTION] = _encode({**old_connection, **{name: current[name] for name in RELEASE_FIELDS}})
        after[SUMS] = ''.join(
            f'{hashlib.sha256(after.get("LAN_CLIENT_KIT/" + name, before["LAN_CLIENT_KIT/" + name])).hexdigest()} *{name}\n'
            for name in sorted(lan.CLIENT_KIT_EXPECTED_FILES - {'SHA256SUMS'}, key=str.lower)
        ).encode('ascii')
    # Each deterministic scratch name belongs to this verified transaction.
    # A killed partial write can be discarded only when it is a prefix of the
    # exact derived payload. No unrelated temporary file is touched.
    scratch = {name: (root / name).with_name('.csd-lan-' + folder.name + '-' + Path(name).name + '.tmp') for name in after}
    observed = set()
    for tree in ('.lan-server', 'LAN_CLIENT_KIT'):
        directory = recovery._normal(root / tree, directory=True)
        if directory.exists():
            observed.update(path.relative_to(root).as_posix() for path in recovery._all_files(directory))
    scratch_names = {path.relative_to(root).as_posix() for path in scratch.values()}
    _require(observed - scratch_names == set(before))
    for name, original in before.items():
        _require(_read(root / name) in (original, after.get(name, original)))
    for name, path in scratch.items():
        if path.exists():
            _require(after[name].startswith(_read(path)))
    guard.assert_exclusive_access()
    # State is last, so an interruption still enters this helper on restart.
    for name in [key for key in after if key != STATE] + [STATE]:
        guard.assert_exclusive_access()
        target, temporary, payload = root / name, scratch[name], after[name]
        if temporary.exists():
            temporary.unlink()
        if _read(target) != payload:
            transaction._write_new(temporary, payload)
            transaction._move(temporary, target, replace=True)
    for name, payload in after.items():
        _require(_read(root / name) == payload)
    return {'status': 'updated', 'commit': update.to_commit}


def reconcile(root, guard):
    """Return not_configured/unchanged/updated; refuse unproved release drift."""
    try:
        return _reconcile(root, guard)
    except LanCompatibilityError:
        raise
    except Exception:
        # Never surface private network identities, certificate or file data.
        raise LanCompatibilityError('LAN update compatibility could not be verified. Keep the installation and update recovery files unchanged.') from None
