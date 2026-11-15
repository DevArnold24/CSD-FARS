"""Recoverable publication of verified application files.

Internal building block, not a client entry point. The Windows coordinator must
hold the installation lifecycle lock and establish owner-private update storage
throughout every call. Its callbacks create a matching private recovery set and
perform read-only installation validation. This module never restores private
data, starts a server, runs package code, changes permissions or removes history.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Callable, Mapping
from uuid import uuid4

from scripts.client.update_package import (
    BUNDLE_MANIFEST, MAX_ARCHIVE_BYTES, MAX_FILE_BYTES, UpdatePackageError, VerifiedUpdate, verify_update_bytes,
)

# Inherit the coordinator's restricted Windows ACL. POSIX keeps owner-only mode.
PRIVATE_DIRECTORY_MODE = 0o777 if os.name == 'nt' else 0o700


class UpdateTransactionError(RuntimeError):
    """Publication or recovery could not be completed safely."""


def _normal(path: Path, *, directory=False) -> Path:
    path = Path(os.path.abspath(path))
    if os.name == 'nt' and str(path).startswith('\\\\'):
        raise UpdateTransactionError('Updates require a local installation drive.')
    for component in reversed((path, *path.parents)):
        try:
            info = component.lstat()
        except FileNotFoundError:
            return path
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise UpdateTransactionError('Update storage must not use links or junctions.')
        if component != path or directory:
            if not stat.S_ISDIR(info.st_mode):
                raise UpdateTransactionError('An update directory is invalid.')
        elif not stat.S_ISREG(info.st_mode):
            raise UpdateTransactionError('An update file is not a regular file.')
    return path


def _target(root: Path, relative: str, *, create_parents=False) -> Path:
    target = root.joinpath(*relative.split('/'))
    # Package verification already validates relative paths; retain containment
    # at the write boundary and inspect every existing ancestor for reparse points.
    if not Path(os.path.abspath(target)).is_relative_to(root):
        raise UpdateTransactionError('An update path escapes its installation.')
    parent = root
    for part in relative.split('/')[:-1]:
        parent /= part
        _normal(parent, directory=True)
        if not parent.exists():
            if not create_parents:
                break
            parent.mkdir(mode=PRIVATE_DIRECTORY_MODE)
    return _normal(target)


def _bytes(path: Path, *, limit=MAX_FILE_BYTES) -> bytes | None:
    path = _normal(path)
    try:
        with path.open('rb') as stream:
            data = stream.read(limit+1)
    except FileNotFoundError:
        return None
    if len(data) > limit:
        raise UpdateTransactionError('An installation file exceeds the update limit.')
    return data


def _matches(data: bytes | None, expected: tuple[str, int] | None) -> bool:
    if data is None or expected is None:
        return data is None and expected is None
    return len(data) == expected[1] and hashlib.sha256(data).hexdigest() == expected[0]


def _write_new(path: Path, data: bytes) -> None:
    _normal(path)
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _reserve_recovery_space(path: Path, size: int) -> None:
    """Keep rollback scratch space allocated until publication has finished."""
    _normal(path)
    identity = None
    try:
        with path.open('xb') as stream:
            info = os.fstat(stream.fileno())
            identity = (info.st_dev, info.st_ino)
            # Incompressible bytes avoid a compressed NTFS directory turning
            # a large zero-filled reserve into a nearly empty physical file.
            chunk = os.urandom(64*1024)
            remaining = size
            while remaining:
                written = stream.write(chunk[:min(remaining,len(chunk))])
                if not written:
                    raise OSError('Recovery reserve could not be written.')
                remaining -= written
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        if identity is not None:
            info = _normal(path).stat()
            if (info.st_dev,info.st_ino) == identity:
                path.unlink()
        raise UpdateTransactionError('There is not enough usable space to prepare update recovery.') from None


def _replace(path: Path, data: bytes) -> None:
    _normal(path)
    temporary = path.with_name('.csd-update-'+uuid4().hex+'.tmp')
    _write_new(temporary, data)
    _move(temporary, path, replace=True)


def _sync_directory(path: Path) -> None:
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _move(source: Path, destination: Path, *, replace=False) -> None:
    """Same-directory publication with ordered metadata writes."""
    source, destination = _normal(source), _normal(destination)
    if source.parent != destination.parent:
        raise UpdateTransactionError('Update publication must stay in one directory.')
    if os.name == 'nt':
        move = ctypes.WinDLL('kernel32', use_last_error=True).MoveFileExW
        move.argtypes = [ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_uint32]
        move.restype = ctypes.c_int
        # MOVEFILE_WRITE_THROUGH; no cross-volume copy or delayed reboot action.
        # https://learn.microsoft.com/windows/win32/api/winbase/nf-winbase-movefileexw
        flags = 0x8 | (0x1 if replace else 0)
        if not move('\\\\?\\'+str(source),'\\\\?\\'+str(destination),flags):
            raise ctypes.WinError(ctypes.get_last_error())
    elif replace:
        os.replace(source,destination)
        _sync_directory(destination.parent)
    else:
        os.link(source,destination)
        _sync_directory(destination.parent)
        source.unlink()
        _sync_directory(destination.parent)


def _finish_guard(pending: Path, transaction: str, outcome: str) -> None:
    completed = pending.with_name('finished-'+transaction+'-'+outcome+'.json')
    if completed.exists():
        # Recovery of the POSIX link/unlink crash window. A different receipt
        # must never be overwritten to make the startup guard disappear.
        if _bytes(completed,limit=4096) != _bytes(pending,limit=4096):
            raise UpdateTransactionError('The update completion receipt is inconsistent.')
        pending.unlink()
        _sync_directory(pending.parent)
    else:
        _move(pending,completed)


def _journal(path: Path, record: dict, *, first=False) -> None:
    payload = json.dumps(record, sort_keys=True, separators=(',', ':')).encode('ascii')
    if first:
        temporary = path.with_name('.pending-'+uuid4().hex+'.tmp')
        _write_new(temporary, payload)
        _move(temporary,path)  # Complete publication; refuse an existing guard.
    else:
        _replace(path, payload)


def _unique_record(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('Duplicate recovery record field')
        result[name] = value
    return result


def _rollback(root: Path, transaction: Path, update: VerifiedUpdate) -> None:
    # Validate every original backup and current destination before changing any
    # file, so an unexpected manual edit cannot be partly overwritten by recovery.
    originals = {}
    for name, expected_after in update.after.items():
        expected_before = update.before.get(name)
        current = _bytes(_target(root, name))
        if not (_matches(current, expected_after) or _matches(current, expected_before)):
            raise UpdateTransactionError('A source file changed unexpectedly. Keep the recovery folder for review.')
        if expected_before is not None:
            original = _bytes(_target(transaction/'original', name))
            if not _matches(original, expected_before):
                raise UpdateTransactionError('The original source backup failed verification. Recovery remains pending.')
            originals[name] = original
    reserve = _normal(transaction/'recovery-reserve.bin')
    if reserve.exists():
        reserve.unlink()
    for name in reversed(sorted(update.after)):
        destination = _target(root, name)
        if name in originals:
            if not _matches(_bytes(destination), update.before[name]):
                _replace(destination, originals[name])
        elif destination.exists():
            # Only a new, signed package file can reach this branch.
            destination.unlink()
    for name, expected in update.before.items():
        if not _matches(_bytes(_target(root, name)), expected):
            raise UpdateTransactionError('The original source was not fully restored.')


def apply_update(root: Path, raw: bytes, publishers: Mapping[str, bytes], *, from_commit: str,
                 prepare_private_recovery: Callable[[Path], None],
                 validate_private_recovery: Callable[[Path], None],
                 validate_installation: Callable[[], None],
                 assert_exclusive_access: Callable[[], None],
                 prepare_validation: Callable[[Path], None] | None = None) -> dict:
    """Apply only while the trusted coordinator owns the root and recovery set."""
    update = verify_update_bytes(raw, publishers, from_commit=from_commit)
    root = _normal(root, directory=True)
    assert_exclusive_access()
    try:
        installed = json.loads(_bytes(_target(root, BUNDLE_MANIFEST), limit=1024*1024), object_pairs_hook=_unique_record)
        if not isinstance(installed, dict) or installed.get('sourceCommit') != from_commit:
            raise ValueError('Mismatched source')
    except (ValueError, TypeError):
        raise UpdateTransactionError('The installed version record does not match this update.') from None
    storage = _normal(root/'.updates', directory=True)
    if not storage.is_dir():
        raise UpdateTransactionError('The coordinator must prepare private update storage first.')
    pending = _normal(storage/'pending.json')
    if pending.exists():
        raise UpdateTransactionError('An earlier update needs recovery before another update can begin.')
    originals = {}
    for name in update.after:
        # Preflight only reads. New source directories are created after the
        # matching recovery set and pending guard have been published.
        current = _bytes(_target(root, name))
        expected = update.before.get(name)
        if not _matches(current, expected):
            raise UpdateTransactionError('Installed source differs from the supported version. No source files were replaced.')
        if expected is not None:
            originals[name] = current
    identifier = uuid4().hex
    transaction = _normal(storage/identifier, directory=True)
    transaction.mkdir(mode=PRIVATE_DIRECTORY_MODE)
    (transaction/'original').mkdir(mode=PRIVATE_DIRECTORY_MODE)
    _write_new(transaction/'package.zip', raw)
    for name, data in originals.items():
        _write_new(_target(transaction/'original', name, create_parents=True), data)
    recovery = transaction/'private-recovery'
    recovery.mkdir(mode=PRIVATE_DIRECTORY_MODE)
    prepare_private_recovery(recovery)
    validate_private_recovery(recovery)
    changed = [name for name in update.before if update.before[name] != update.after[name]]
    reserve_bytes = (2*1024*1024 + max((update.before[name][1] for name in changed),default=0)
                     + sum(max(0,update.before[name][1]-update.after[name][1]) for name in changed))
    _reserve_recovery_space(transaction/'recovery-reserve.bin', reserve_bytes)
    # A real application check needs a reusable source/database scratch copy.
    # The Windows coordinator reserves it before the pending guard or cutover;
    # unit callers with no disk-based validation need no additional reservation.
    if prepare_validation is not None:
        prepare_validation(recovery)
    assert_exclusive_access()
    record = dict(format=1, transaction=identifier, from_commit=update.from_commit,
                  to_commit=update.to_commit, archive_sha256=update.archive_sha256, phase='applying')
    _journal(pending, record, first=True)
    try:
        for name, payload in sorted(update.payloads.items()):
            destination = _target(root, name, create_parents=True)
            if not _matches(_bytes(destination), update.before.get(name)):
                raise UpdateTransactionError('Installed source changed while preparing the update.')
            if update.before.get(name) != update.after[name]:
                _replace(destination, payload)
        assert_exclusive_access()
        validate_installation()
        validate_private_recovery(recovery)
        for name, expected in update.after.items():
            if not _matches(_bytes(_target(root, name)), expected):
                raise UpdateTransactionError('Installed update bytes failed verification.')
        record['phase'] = 'validated'
        _journal(pending, record)
        _write_new(transaction/'result.json', json.dumps({'status':'applied','commit':update.to_commit}).encode())
        (transaction/'recovery-reserve.bin').unlink()
        _finish_guard(pending,identifier,'applied')  # Last operation allowing startup again.
        return {'status':'applied', 'commit':update.to_commit, 'transaction':identifier}
    except Exception:
        persisted = json.loads(_bytes(pending,limit=4096),object_pairs_hook=_unique_record)
        if persisted.get('phase') == 'validated':
            raise UpdateTransactionError('The updated source was validated, but completion needs recovery before startup.') from None
        validate_private_recovery(recovery)
        _rollback(root, transaction, update)
        validate_installation()
        _replace(transaction/'result.json', b'{"status":"restored"}')
        _finish_guard(pending,identifier,'restored')
        raise UpdateTransactionError('The update did not complete. The previous source was restored and verified.') from None


def recover_update(root: Path, publishers: Mapping[str, bytes], *,
                   validate_private_recovery: Callable[[Path], None],
                   validate_installation: Callable[[], None],
                   assert_exclusive_access: Callable[[], None]) -> dict:
    """Resolve interrupted publication before startup; never delete history."""
    root = _normal(root, directory=True)
    assert_exclusive_access()
    storage = _normal(root/'.updates', directory=True)
    pending = _normal(storage/'pending.json')
    try:
        raw_record = _bytes(pending, limit=4096)
        if raw_record is None:
            return {'status':'nothing_pending'}
        record = json.loads(raw_record, object_pairs_hook=_unique_record)
        if (not isinstance(record, dict) or set(record) != {'format','transaction','from_commit','to_commit','archive_sha256','phase'}
                or type(record['format']) is not int or record['format'] != 1
                or not isinstance(record['transaction'], str) or len(record['transaction']) != 32
                or any(c not in '0123456789abcdef' for c in record['transaction'])
                or record['phase'] not in {'applying','validated'}):
            raise ValueError('Invalid journal')
        transaction = _normal(storage/record['transaction'], directory=True)
        package = _bytes(transaction/'package.zip', limit=MAX_ARCHIVE_BYTES)
        update = verify_update_bytes(package, publishers, from_commit=record['from_commit'])
        if update.archive_sha256 != record['archive_sha256'] or update.to_commit != record['to_commit']:
            raise ValueError('Invalid journal binding')
    except (ValueError, TypeError, KeyError, UpdatePackageError):
        raise UpdateTransactionError('The pending update record could not be verified. Keep recovery files unchanged.') from None
    validate_private_recovery(_normal(transaction/'private-recovery', directory=True))
    if record['phase'] == 'validated':
        # A completed validation record must not roll a successfully published
        # release back merely because guard removal was interrupted/resurfaced.
        for name, expected in update.after.items():
            if not _matches(_bytes(_target(root,name)),expected):
                raise UpdateTransactionError('Validated update files changed. Recovery needs review before startup.')
        assert_exclusive_access()
        validate_installation()
        _replace(transaction/'result.json',json.dumps({'status':'applied','commit':update.to_commit}).encode())
        reserve = _normal(transaction/'recovery-reserve.bin')
        if reserve.exists():
            reserve.unlink()
        _finish_guard(pending,record['transaction'],'applied')
        return {'status':'completed','commit':update.to_commit,'transaction':record['transaction']}
    _rollback(root, transaction, update)
    assert_exclusive_access()
    validate_installation()
    _replace(transaction/'result.json', b'{"status":"restored"}')
    _finish_guard(pending,record['transaction'],'restored')
    return {'status':'restored', 'commit':update.from_commit, 'transaction':record['transaction']}
