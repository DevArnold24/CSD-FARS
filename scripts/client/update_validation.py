"""Check signed application code against a disposable copy of matching data.

The smoke process has no listener, camera, worker or external connection. This
is a regression check for trusted publisher code, not an OS security sandbox.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import secrets
import stat
import subprocess
import sys
from uuid import uuid4

from scripts.client import update_recovery as recovery
from scripts.client import update_transaction as transaction

# Inherit the coordinator's restricted Windows ACL. POSIX keeps owner-only mode.
PRIVATE_DIRECTORY_MODE = 0o777 if os.name == 'nt' else 0o700


class ValidationError(RuntimeError):
    pass


def _scratch_boundary(root, snapshot):
    root, snapshot = recovery._boundary(Path(root), Path(snapshot))
    return root, snapshot.parent


def _scratch_bytes(snapshot, inventory):
    return sum((snapshot/'files'/name).stat().st_size for name in recovery.REQUIRED) + sum(
        size for _, size in inventory.values())


def prepare_validation_space(root, snapshot, inventories, *, assert_exclusive_access, assert_private_access):
    """Reserve one reusable validation copy before any application publication."""
    root, folder = _scratch_boundary(root, snapshot)
    assert_exclusive_access()
    assert_private_access(folder)
    before, after = inventories
    required = max(_scratch_bytes(snapshot, inventory) for inventory in (before, after))
    growth = max(0, sum(size for _,size in after.values()) - sum(size for _,size in before.values()))
    if shutil.disk_usage(folder).free < required + growth + recovery.SPACE_MARGIN:
        raise ValidationError('There is not enough free space to check the application and its recovery.')
    transaction._reserve_recovery_space(folder/'validation-reserve.bin', required)


def _scratch_record(root, folder):
    marker = recovery._normal(folder/'validation-scratch.json')
    if not marker.exists():
        return None
    try:
        with marker.open('rb') as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError('Oversized scratch record')
        record = json.loads(raw, object_pairs_hook=transaction._unique_record)
        if (not isinstance(record, dict)
                or set(record) != {'format','root','transaction','stage','identity','status'}
                or type(record['format']) is not int or record['format'] != 1
                or record['root'] != os.path.normcase(str(root)) or record['transaction'] != folder.name
                or not isinstance(record['stage'], str) or not re.fullmatch('validation-[0-9a-f]{32}',record['stage'])
                or record['status'] not in {'running','passed','failed'}
                or (record['identity'] is not None and (not isinstance(record['identity'], list)
                    or len(record['identity']) != 2 or any(type(x) is not int or x < 0 for x in record['identity'])))):
            raise ValueError('Invalid scratch binding')
        return record
    except (ValueError, TypeError, OSError, RecursionError):
        raise ValidationError('The saved application check needs review. Its temporary files were retained.') from None


def _remove_scratch(root, folder, *, assert_private_access):
    """Remove only a recorded, private scratch tree, including after interruption.

    The durable record is published before mkdir and records the directory's
    filesystem identity before any payload copy. Recovery/original source trees
    are never cleanup targets. Unknown directories and changed identities remain.
    """
    record = _scratch_record(root, folder)
    if record is None:
        return
    stage = recovery._normal(folder/record['stage'], directory=True)
    assert_private_access(folder)
    outcome = {'format':1, 'stage':record['stage'],
               'status':'interrupted' if record['status'] == 'running' else record['status']}
    receipt = recovery._normal(folder/(record['stage']+'-result.json'))
    if receipt.exists():
        try:
            if json.loads(transaction._bytes(receipt, limit=4096)) != outcome:
                raise ValueError('Changed receipt')
        except (ValueError, TypeError, OSError):
            raise ValidationError('The application check receipt changed. Keep its files for review.') from None
    if stage.exists():
        info = stage.stat()
        if record['identity'] is None:
            # Crash between mkdir and identity publication: no payload was copied.
            # rmdir refuses a nonempty directory and never recursively deletes it.
            stage.rmdir()
        else:
            if [info.st_dev, info.st_ino] != record['identity']:
                raise ValidationError('The application check folder changed. Its files were retained.')
            pending, entries = [stage], []
            while pending:
                directory = recovery._normal(pending.pop(), directory=True)
                with os.scandir(directory) as children:
                    for child in children:
                        info = child.stat(follow_symlinks=False)
                        path = recovery._normal(Path(child.path), directory=stat.S_ISDIR(info.st_mode))
                        # Windows DirEntry.stat may report a zero file index;
                        # use the same full stat operation as the deletion check.
                        info = path.stat()
                        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                            raise ValidationError('The application check contains unsupported files.')
                        entries.append((path, info.st_dev, info.st_ino, stat.S_ISDIR(info.st_mode)))
                        if len(entries) > 16384:
                            raise ValidationError('The application check contains too many files for automatic cleanup.')
                        if stat.S_ISDIR(info.st_mode):
                            pending.append(path)
            # Inspect the complete tree before deleting anything. Recheck each
            # identity at deletion; no recursive shell command follows links.
            for path, device, identity, directory in reversed(entries):
                current = recovery._normal(path, directory=directory).stat()
                if (current.st_dev, current.st_ino) != (device, identity):
                    raise ValidationError('An application check file changed. Remaining files were retained.')
                path.rmdir() if directory else path.unlink()
            current = recovery._normal(stage, directory=True).stat()
            if [current.st_dev, current.st_ino] != record['identity']:
                raise ValidationError('The application check folder changed. Remaining files were retained.')
            stage.rmdir()
    # Keep small outcome evidence instead of another full database/key copy.
    if not receipt.exists():
        transaction._write_new(receipt, json.dumps(outcome, sort_keys=True).encode('ascii'))
    recovery._normal(folder/'validation-scratch.json').unlink()


def validate_application(root, snapshot, inventory, *, assert_exclusive_access, assert_private_access):
    root, snapshot = Path(root), Path(snapshot)
    root, folder = _scratch_boundary(root, snapshot)
    assert_exclusive_access()
    assert_private_access(snapshot)
    _remove_scratch(root, folder, assert_private_access=assert_private_access)
    reserve = recovery._normal(folder/'validation-reserve.bin')
    if reserve.exists():
        reserve.unlink()
    stage = folder / ('validation-' + uuid4().hex)
    # One independent DB copy and all source bytes, retaining rollback reserve.
    required = _scratch_bytes(snapshot, inventory)
    if shutil.disk_usage(snapshot).free < required + recovery.SPACE_MARGIN:
        raise ValidationError('There is not enough free space to check the updated application.')
    # Only Windows runtime essentials are inherited. Application configuration
    # is loaded from the private copy, never an ambient Python/Flask setting.
    environment = {key:value for key,value in os.environ.items()
                   if key.upper() in {'SYSTEMROOT','WINDIR','PATH','TEMP','TMP','COMSPEC'}}
    nonce = secrets.token_hex(32)
    marker = folder/'validation-scratch.json'
    record = {'format':1, 'root':os.path.normcase(str(root)), 'transaction':folder.name,
              'stage':stage.name, 'identity':None, 'status':'running'}
    transaction._journal(marker, record, first=True)
    try:
        stage.mkdir(mode=PRIVATE_DIRECTORY_MODE)
        info = stage.stat()
        record['identity'] = [info.st_dev, info.st_ino]
        transaction._journal(marker, record)
        for name, (expected, size) in inventory.items():
            source = recovery._normal(root/name)
            with source.open('rb') as stream:
                data = stream.read(size+1)
            if len(data) != size or hashlib.sha256(data).hexdigest() != expected:
                raise ValidationError('The application files changed before their startup check.')
            target = stage/name
            target.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
            target.write_bytes(data)
        for name in recovery.REQUIRED:
            target = stage/name
            target.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
            shutil.copyfile(snapshot/'files'/name, target)
        assert_private_access(stage)
        result = subprocess.run([sys.executable, '-I', '-B', str(Path(__file__).with_name('update_smoke.py')),
            str(stage), nonce], cwd=stage, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode:
            raise ValidationError('The updated application did not pass its startup check.')
        # Some runtime dependencies call sys.exit(0) on import failure. An exit
        # code alone is never proof that the application checks actually ran.
        receipt = recovery._normal(stage/'validation-complete.json')
        with receipt.open('rb') as stream:
            confirmation = stream.read(4097)
        if len(confirmation) > 4096 or json.loads(confirmation) != {'format':1, 'nonce':nonce, 'status':'passed'}:
            raise ValidationError('The application startup check did not confirm completion.')
        record['status'] = 'passed'
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ValidationError('The application startup check could not finish.') from None
    finally:
        assert_exclusive_access()
        if _scratch_record(root, folder) != {**record, 'status':'running'}:
            raise ValidationError('The application check record changed. Its files were retained.')
        if record['status'] != 'passed':
            record['status'] = 'failed'
        transaction._journal(marker, record)
        _remove_scratch(root, folder, assert_private_access=assert_private_access)
    assert_exclusive_access()
    assert_private_access(folder)
