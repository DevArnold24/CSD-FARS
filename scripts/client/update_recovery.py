"""Matching private recovery sets for compatible, offline application updates.

Internal API: the Windows coordinator must own the lifecycle lock and verify
owner-private storage before calling. All reads use the trusted installer copy
of client_setup. Application modules are never imported or executed. This is a
local update recovery set, not an off-machine backup or a restore utility.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import time
from typing import Callable

from scripts.client import client_setup

# Inherit the coordinator's restricted Windows ACL. POSIX keeps owner-only mode.
PRIVATE_DIRECTORY_MODE = 0o777 if os.name == 'nt' else 0o700


DATABASE = 'database/attendance.db'
WAL = DATABASE + '-wal'
REQUIRED = ('.env', '.client-state', DATABASE)
OPTIONAL_FILES = ('.deployment-preference.json',)
OPTIONAL_TREES = ('.lan-server', 'LAN_CLIENT_KIT', '.update-trust')
MAX_DATABASE_BYTES = 4 * 1024**3
MAX_WAL_BYTES = 1024**3
MAX_PRIVATE_BYTES = 512 * 1024**2
MAX_FILES = 4096
MAX_MANIFEST_BYTES = 2 * 1024**2
SPACE_MARGIN = 256 * 1024**2
TIME_LIMIT = 300.0
CHUNK_SIZE = 1024**2


class RecoveryError(RuntimeError):
    """A private recovery set could not be prepared or verified."""


def _normal(path: Path, *, directory=False) -> Path:
    path = Path(os.path.abspath(path))
    if os.name == 'nt' and str(path).startswith('\\\\'):
        raise RecoveryError('Update recovery requires a local installation.')
    for component in reversed((path, *path.parents)):
        try:
            info = component.lstat()
        except FileNotFoundError:
            return path
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise RecoveryError('Private recovery cannot use links or junctions.')
        if component != path or directory:
            if not stat.S_ISDIR(info.st_mode):
                raise RecoveryError('Private recovery has an invalid directory.')
        elif not stat.S_ISREG(info.st_mode):
            raise RecoveryError('Private recovery has a non-regular file.')
    return path


def _boundary(root: Path, destination: Path) -> tuple[Path, Path]:
    root = _normal(root, directory=True)
    destination = _normal(destination, directory=True)
    if (not root.is_dir() or not destination.is_dir()
            or destination.name != 'private-recovery'
            or destination.parent.parent != root / '.updates'
            or not re.fullmatch('[0-9a-f]{32}', destination.parent.name)):
        raise RecoveryError('Recovery must use this installation\'s private update folder.')
    return root, destination


def _deadline() -> Callable[[], None]:
    expires = time.monotonic() + TIME_LIMIT

    def check():
        if time.monotonic() >= expires:
            raise RecoveryError('The recovery check took too long. No update can proceed without a verified recovery copy.')
    return check


def _all_files(directory: Path) -> list[Path]:
    """Bounded walk without following reparse points or silently skipping errors."""
    directory = _normal(directory, directory=True)
    pending = [directory]
    files = []
    entries_seen = 0
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                entries_seen += 1
                if entries_seen > MAX_FILES * 2:
                    raise RecoveryError('Private recovery contains too many directory entries.')
                path = Path(entry.path)
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise RecoveryError('Private recovery cannot include links or junctions.')
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    files.append(path)
                    if len(files) > MAX_FILES:
                        raise RecoveryError('Private recovery contains too many files.')
                else:
                    raise RecoveryError('Private recovery contains an unsupported file.')
    return files


def _source_files(root: Path) -> dict[str, Path]:
    files = {}
    for name in REQUIRED:
        path = _normal(root / name)
        if not path.is_file():
            raise RecoveryError('The installation is missing private setup or database files.')
        files[name] = path
    for name in (WAL, *OPTIONAL_FILES):
        path = _normal(root / name)
        if path.exists():
            files[name] = path
    # A rollback journal could contain an unfinished write. Do not silently
    # treat a raw database as a clean snapshot or run recovery on the live DB.
    if os.path.lexists(root / (DATABASE + '-journal')):
        raise RecoveryError('The attendance database needs its normal startup check before updating.')
    _normal(root / (DATABASE + '-shm'))  # Never copy SQLite's transient locks.
    for name in OPTIONAL_TREES:
        path = _normal(root / name, directory=True)
        if path.exists():
            for child in _all_files(path):
                files[child.relative_to(root).as_posix()] = child
    if len(files) > MAX_FILES:
        raise RecoveryError('Private recovery contains too many files.')
    return files


def _digest(path: Path, limit: int, check_time: Callable[[], None], destination: Path | None = None) -> dict:
    """Stream bounded bytes and detect a file changing while it is read."""
    path = _normal(path)
    digest = hashlib.sha256()
    size = 0
    output = None
    try:
        with path.open('rb') as source:
            before = os.fstat(source.fileno())
            if before.st_size > limit:
                raise RecoveryError('A private recovery file exceeds the supported size limit.')
            if destination is not None:
                destination = _normal(destination)
                output = destination.open('xb')
            while True:
                check_time()
                chunk = source.read(CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise RecoveryError('A private recovery file grew beyond its size limit.')
                digest.update(chunk)
                if output is not None:
                    output.write(chunk)
            after = os.fstat(source.fileno())
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
            if (any(getattr(before, field) != getattr(after, field) for field in fields)
                    or size != after.st_size):
                raise RecoveryError('Private installation data changed during recovery preparation.')
            current = _normal(path).stat()
            if any(getattr(current, field) != getattr(after, field) for field in fields):
                raise RecoveryError('A private installation file was replaced during recovery preparation.')
            if output is not None:
                output.flush()
                os.fsync(output.fileno())
    finally:
        if output is not None:
            output.close()
    return {'bytes': size, 'sha256': digest.hexdigest()}


def _limit(name: str) -> int:
    if name == DATABASE:
        return MAX_DATABASE_BYTES
    if name == WAL:
        return MAX_WAL_BYTES
    if name == '.env':
        return 64 * 1024
    if name == '.client-state':
        return 16 * 1024
    if name == '.deployment-preference.json':
        return 1024
    return MAX_PRIVATE_BYTES


def _inventory(root: Path, check_time: Callable[[], None]) -> dict:
    result = {name: _digest(path, _limit(name), check_time)
              for name, path in sorted(_source_files(root).items())}
    if sum(entry['bytes'] for name, entry in result.items() if name not in (DATABASE, WAL)) > MAX_PRIVATE_BYTES:
        raise RecoveryError('Private settings exceed the supported recovery size limit.')
    return result


def _check_data(folder: Path, check_time: Callable[[], None]) -> str:
    """Verify copied keys, setup identity, schema and database; no app imports."""
    marker = client_setup._read_marker(folder / '.client-state')
    if marker is None or marker['status'] != 'complete':
        raise RecoveryError('Client account setup must be complete before updating.')
    values = client_setup._parse_env(folder / '.env')
    client_setup._validate_env_values(values)
    run_id = marker['run_id']
    expected = client_setup._key_fingerprint(values, run_id)
    if not secrets.compare_digest(marker['key_fingerprint'], expected):
        raise RecoveryError('The database, setup identity and security keys do not belong together.')
    client_setup._validate_database(folder / DATABASE, fresh=False, env_values=values,
                                    run_id=run_id, progress_handler=lambda: _progress(check_time))
    with closing(client_setup._sqlite_read_only(folder / DATABASE)) as connection:
        connection.set_progress_handler(lambda: _progress(check_time), 1000)
        if connection.execute("SELECT 1 FROM attendance_sessions WHERE status = 'Open' LIMIT 1").fetchone():
            raise RecoveryError('Finish or cancel the open attendance session in CSD Attendance before updating.')
    check_time()
    return run_id


def _progress(check_time: Callable[[], None]) -> int:
    try:
        check_time()
        return 0
    except RecoveryError:
        return 1


def _snapshot_database(source: Path, destination: Path, check_time: Callable[[], None]) -> None:
    # Read the private raw copy so SQLite cannot change the live database/WAL.
    # backup() incorporates committed WAL pages into a self-contained database.
    _normal(destination)
    with destination.open('xb'):
        pass
    with closing(client_setup._sqlite_read_only(source)) as incoming:
        with closing(sqlite3.connect(destination, timeout=1.0)) as outgoing:
            incoming.backup(outgoing, pages=256, progress=lambda *_: check_time(), sleep=0.02)
            outgoing.execute('PRAGMA journal_mode=DELETE')
    # Windows FlushFileBuffers requires a writable handle. This is the new
    # recovery database only; the source database is never opened for writing.
    with destination.open('r+b') as stream:
        os.fsync(stream.fileno())


def prepare_private_recovery(root: Path, destination: Path, *,
                             assert_exclusive_access: Callable[[], None],
                             assert_private_access: Callable[[Path], None]) -> dict:
    """Prepare a complete recovery record before any application replacement."""
    assert_exclusive_access()
    root, destination = _boundary(root, destination)
    assert_private_access(destination)
    if any(destination.iterdir()):
        raise RecoveryError('Recovery preparation requires a new, empty private folder.')
    check_time = _deadline()
    try:
        client_setup._require_python_311()
        baseline = _inventory(root, check_time)
        needed = sum(entry['bytes'] for entry in baseline.values())
        needed += baseline[DATABASE]['bytes'] + baseline.get(WAL, {'bytes': 0})['bytes'] + SPACE_MARGIN
        if shutil.disk_usage(destination).free < needed:
            raise RecoveryError('There is not enough free space for a complete update recovery copy.')
        files = destination / 'files'
        raw = destination / 'sqlite-source'
        files.mkdir(mode=PRIVATE_DIRECTORY_MODE)
        raw.mkdir(mode=PRIVATE_DIRECTORY_MODE)
        for name, expected in baseline.items():
            copied = raw / Path(name).name if name in (DATABASE, WAL) else files / name
            copied.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
            if _digest(root / name, _limit(name), check_time, copied) != expected:
                raise RecoveryError('Private installation data changed while it was being copied.')
        assert_exclusive_access()
        if _inventory(root, check_time) != baseline:
            raise RecoveryError('Private installation data changed. The update cannot proceed.')
        (files / 'database').mkdir(exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
        _snapshot_database(raw / 'attendance.db', files / DATABASE, check_time)
        run_id = _check_data(files, check_time)
        # Keep the raw copy as evidence. It is not the database to restore; the
        # completed files/database/attendance.db contains committed WAL data.
        copied_inventory = {path.relative_to(files).as_posix(): _digest(path, _limit(path.relative_to(files).as_posix()), check_time)
                            for path in sorted(_all_files(files))}
        record = {'format': 1, 'root': os.path.normcase(str(root)), 'run_id': run_id,
                  'captured_at': datetime.now(timezone.utc).isoformat(),
                  'files': copied_inventory, 'source_files': baseline}
        assert_exclusive_access()
        assert_private_access(destination)
        if _inventory(root, check_time) != baseline:
            raise RecoveryError('Private installation data changed before recovery preparation finished.')
        payload = json.dumps(record, sort_keys=True, separators=(',', ':')).encode('ascii')
        if len(payload) > MAX_MANIFEST_BYTES:
            raise RecoveryError('The recovery inventory exceeds its size limit.')
        with (destination / 'recovery.json').open('xb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        return record
    except (OSError, sqlite3.Error, client_setup.SetupError):
        raise RecoveryError('The private recovery copy failed validation. No update can proceed; the original data was not changed.') from None


def _unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise RecoveryError('The recovery record contains duplicate fields.')
        result[name] = value
    return result


def _valid_inventory(inventory: object) -> bool:
    if not isinstance(inventory, dict) or not 1 <= len(inventory) <= MAX_FILES:
        return False
    for name, entry in inventory.items():
        if (not isinstance(name, str) or len(name) > 1024 or '\\' in name
                or ':' in name or name.startswith('/') or any(part in ('', '.', '..') for part in name.split('/'))
                or not isinstance(entry, dict) or set(entry) != {'bytes', 'sha256'}
                or type(entry['bytes']) is not int or not 0 <= entry['bytes'] <= _limit(name)
                or not isinstance(entry['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', entry['sha256'])):
            return False
    return True


def verify_private_recovery(root: Path, destination: Path, *,
                            assert_exclusive_access: Callable[[], None],
                            assert_private_access: Callable[[Path], None]) -> dict:
    """Check both the recovery set and unchanged live private data before recovery."""
    assert_exclusive_access()
    root, destination = _boundary(root, destination)
    assert_private_access(destination)
    check_time = _deadline()
    try:
        path = _normal(destination / 'recovery.json')
        with path.open('rb') as stream:
            payload = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(payload) > MAX_MANIFEST_BYTES:
            raise RecoveryError('The recovery record exceeds its size limit.')
        record = json.loads(payload.decode('ascii'), object_pairs_hook=_unique)
        if (not isinstance(record, dict) or set(record) != {'format', 'root', 'run_id', 'captured_at', 'files', 'source_files'}
                or type(record['format']) is not int or record['format'] != 1
                or record['root'] != os.path.normcase(str(root))
                or not isinstance(record['run_id'], str) or not re.fullmatch('[0-9a-f]{32}', record['run_id'])
                or not isinstance(record['captured_at'], str)
                or not _valid_inventory(record['files']) or not _valid_inventory(record['source_files'])
                or not set(REQUIRED).issubset(record['files']) or not set(REQUIRED).issubset(record['source_files'])):
            raise RecoveryError('The recovery record is invalid or belongs to another installation.')
        files = _normal(destination / 'files', directory=True)
        actual = {path.relative_to(files).as_posix(): _digest(path, _limit(path.relative_to(files).as_posix()), check_time)
                  for path in sorted(_all_files(files))}
        if actual != record['files']:
            raise RecoveryError('The private recovery copy changed or is incomplete. Keep it for review.')
        if _inventory(root, check_time) != record['source_files']:
            raise RecoveryError('Private installation data changed after the update began. Recovery needs review.')
        if _check_data(files, check_time) != record['run_id']:
            raise RecoveryError('The recovery database and setup record do not belong together.')
        assert_exclusive_access()
        assert_private_access(destination)
        return record
    except (OSError, sqlite3.Error, client_setup.SetupError, ValueError, UnicodeError, RecursionError):
        raise RecoveryError('The private recovery copy could not be verified. Keep all recovery files unchanged for review.') from None
