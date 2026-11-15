"""Installer-owned, stable recovery tools and publisher trust.

Initialization is invoked only by the Windows setup coordinator while it owns
the lifecycle lock and has created owner-private .update-trust storage. Neither
an update archive nor ordinary application code can nominate new trusted keys.
This module never generates/stores a publisher private key or changes .env/data.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Callable
from uuid import uuid4

BOOTSTRAP_FILES = (
    'scripts/client/update_package.py', 'scripts/client/update_transaction.py',
    'scripts/client/update_bootstrap.py',
    'scripts/client/update_recovery.py',
    'scripts/client/update_windows.py', 'scripts/client/update_controller.py',
    'scripts/client/update_validation.py', 'scripts/client/update_smoke.py',
    'scripts/client/client_setup.py', 'scripts/client/windows_storage.py',
    'scripts/windows/Lifecycle.ps1', 'scripts/windows/NativeLifetime.cs',
    'scripts/windows/Client-Storage.ps1',
    'scripts/windows/Private-Storage.ps1', 'scripts/windows/Setup-Dialogs.ps1',
    'scripts/windows/Update-Access.ps1',
)
MAX_FILE = 2*1024*1024
# The coordinator has already protected the Windows parent. Inherit that exact
# ACL; newer Python interprets 0700 as a different protected OWNER RIGHTS ACL.
PRIVATE_DIRECTORY_MODE = 0o777 if os.name == 'nt' else 0o700


class BootstrapError(RuntimeError):
    """The trusted recovery copy could not be established or verified."""


def publisher_id(key: bytes) -> str:
    # Standalone on purpose: recovery verification cannot import an application
    # module that might be one of the files interrupted during replacement.
    if len(key)!=32:
        raise ValueError('Invalid public key length')
    return hashlib.sha256(key).hexdigest()


def _normal(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    if os.name == 'nt' and str(path).startswith('\\\\'):
        raise BootstrapError('Recovery tools require a local installation.')
    for component in reversed((path,*path.parents)):
        try:
            info = component.lstat()
        except FileNotFoundError:
            return path
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
            raise BootstrapError('Recovery tools cannot use links or junctions.')
        if component != path and not stat.S_ISDIR(info.st_mode):
            raise BootstrapError('Recovery storage contains a non-directory ancestor.')
    return path


def _read(path: Path, limit=MAX_FILE) -> bytes:
    path = _normal(path)
    if not stat.S_ISREG(path.stat().st_mode):
        raise BootstrapError('A recovery file is not a regular file.')
    with path.open('rb') as stream:
        data = stream.read(limit+1)
    if len(data)>limit:
        raise BootstrapError('A recovery file exceeds its size limit.')
    return data


def _unique(pairs):
    result = {}
    for key,value in pairs:
        if key in result:
            raise BootstrapError('A recovery record contains duplicate fields.')
        result[key]=value
    return result


def _json(data):
    try:
        return json.loads(data.decode('utf-8'),object_pairs_hook=_unique)
    except (ValueError,UnicodeError,RecursionError):
        raise BootstrapError('A recovery record is invalid.') from None


def read_publishers(document: object) -> dict[str,bytes]:
    if (not isinstance(document,dict) or set(document)!={'format','publishers'}
            or type(document['format']) is not int or document['format']!=1
            or not isinstance(document['publishers'],list) or not 1<=len(document['publishers'])<=2):
        raise BootstrapError('The installer publisher record is invalid.')
    keys = {}
    for entry in document['publishers']:
        if (not isinstance(entry,dict) or set(entry)!={'id','public_key','label'}
                or not isinstance(entry['id'],str) or not re.fullmatch('[0-9a-f]{64}',entry['id'])
                or not isinstance(entry['public_key'],str) or len(entry['public_key'])!=44
                or not isinstance(entry['label'],str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}',entry['label'])):
            raise BootstrapError('The installer publisher identity is invalid.')
        try:
            key = base64.b64decode(entry['public_key'],validate=True)
            if publisher_id(key)!=entry['id'] or entry['id'] in keys:
                raise ValueError('Mismatched publisher')
        except (ValueError,TypeError):
            raise BootstrapError('The installer publisher key is invalid.') from None
        keys[entry['id']]=key
    return keys


def _identity(root: Path, owner_sid: str, run_id: str) -> None:
    if (not isinstance(owner_sid,str) or not re.fullmatch(r'S-\d{1,3}-(?:\d{1,20}-){1,14}\d{1,20}',owner_sid)
            or not isinstance(run_id,str) or not re.fullmatch('[0-9a-f]{32}',run_id)):
        raise BootstrapError('The installation owner or setup identity is invalid.')
    if not root.is_dir():
        raise BootstrapError('The installation folder is missing.')


def _verify_tree(directory: Path, expected: dict[str,str]) -> None:
    directory = _normal(directory)
    allowed_directories = {str(Path(name).parent).replace('\\','/') for name in expected}
    allowed_directories |= {'scripts'}
    seen = set()
    def unreadable(_error):
        raise BootstrapError('A recovery directory could not be inspected.')
    for current,children,files in os.walk(directory,onerror=unreadable,followlinks=False):
        current = _normal(Path(current))
        for child in children:
            path = _normal(current/child)
            if path.relative_to(directory).as_posix() not in allowed_directories:
                raise BootstrapError('Unexpected content was found in the protected recovery tools.')
        for name in files:
            path = _normal(current/name)
            relative = path.relative_to(directory).as_posix()
            if relative not in expected or hashlib.sha256(_read(path)).hexdigest()!=expected[relative]:
                raise BootstrapError('A protected recovery tool changed. Keep the installation unchanged for repair.')
            seen.add(relative)
    if seen!=set(expected):
        raise BootstrapError('The protected recovery tools are incomplete.')


def verify_bootstrap(root: Path, *, owner_sid: str, run_id: str,
                     assert_private_access: Callable[[],None]) -> dict:
    root = _normal(root)
    _identity(root,owner_sid,run_id)
    assert_private_access()
    storage = _normal(root/'.update-trust')
    record = _json(_read(storage/'anchor.json',32768))
    if (not isinstance(record,dict) or set(record)!={'format','status','root','owner_sid','run_id','publishers','files'}
            or type(record['format']) is not int or record['format']!=1 or record['status']!='ready'
            or record['root']!=os.path.normcase(str(root)) or record['owner_sid']!=owner_sid or record['run_id']!=run_id
            or not isinstance(record['files'],dict) or set(record['files'])!=set(BOOTSTRAP_FILES)):
        raise BootstrapError('The recovery tools do not belong to this installation and Windows owner.')
    read_publishers(record['publishers'])
    for name,expected in record['files'].items():
        if not isinstance(expected,str) or not re.fullmatch('[0-9a-f]{64}',expected):
            raise BootstrapError('The recovery tool inventory is invalid.')
    _verify_tree(storage/'bootstrap',record['files'])
    return record


def initialize_bootstrap(root: Path, *, owner_sid: str, run_id: str,
                         assert_private_access: Callable[[],None],
                         assert_exclusive_access: Callable[[],None]) -> dict:
    """Bind the original installer publisher once; later launches never rotate it."""
    if os.name!='nt':
        raise BootstrapError('Recovery-tool initialization requires the Windows installer.')
    root = _normal(root)
    _identity(root,owner_sid,run_id)
    assert_exclusive_access()
    assert_private_access()
    storage = _normal(root/'.update-trust')
    if not storage.is_dir():
        raise BootstrapError('Windows must prepare private recovery storage first.')
    anchor = _normal(storage/'anchor.json')
    if anchor.exists():
        return verify_bootstrap(root,owner_sid=owner_sid,run_id=run_id,assert_private_access=assert_private_access)
    publisher_document = _json(_read(root/'UPDATE_PUBLISHERS.json',32768))
    read_publishers(publisher_document)
    payloads = {name:_read(root/name) for name in BOOTSTRAP_FILES}
    hashes = {name:hashlib.sha256(data).hexdigest() for name,data in payloads.items()}
    destination = _normal(storage/'bootstrap')
    if destination.exists():
        # Resume the sole rename/anchor-publication crash window using the same
        # installer's exact bytes. Never overwrite an unbound recovery copy.
        if not destination.is_dir():
            raise BootstrapError('An interrupted recovery copy is not a directory. It was not replaced.')
        _verify_tree(destination,hashes)
    else:
        stage = _normal(storage/('stage-'+uuid4().hex))
        stage.mkdir(mode=PRIVATE_DIRECTORY_MODE)
        for name,data in payloads.items():
            path = stage/name
            path.parent.mkdir(parents=True,exist_ok=True,mode=PRIVATE_DIRECTORY_MODE)
            with path.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        assert_exclusive_access()
        assert_private_access()
        os.rename(stage,destination)  # Windows refuses an existing destination.
    record = dict(format=1,status='ready',root=os.path.normcase(str(root)),owner_sid=owner_sid,
                  run_id=run_id,publishers=publisher_document,files=hashes)
    pending = storage/('anchor-'+uuid4().hex+'.tmp')
    with pending.open('xb') as stream:
        stream.write(json.dumps(record,sort_keys=True,separators=(',',':')).encode('ascii'))
        stream.flush()
        os.fsync(stream.fileno())
    assert_exclusive_access()
    assert_private_access()
    os.rename(pending,anchor)  # First publication only; never replace established trust.
    return verify_bootstrap(root,owner_sid=owner_sid,run_id=run_id,assert_private_access=assert_private_access)


def main():
    """Isolated entry point; no application import before protected-copy checks."""
    import argparse
    import importlib.util
    import sys
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--parent-pid', type=int, required=True)
    parser.add_argument('--parent-created', type=int, required=True)
    parser.add_argument('--request', type=Path, required=True)
    arguments = parser.parse_args()
    reply = None
    result = {'ok':False, 'message':'The update could not finish. Keep the installation and recovery files unchanged.'}
    try:
        root = _normal(arguments.root)
        request = _normal(arguments.request)
        if (request.name != 'request.json' or request.parent.parent != root/'.updates'
                or not re.fullmatch('request-[0-9a-f]{32}', request.parent.name)):
            raise BootstrapError('The update request has an invalid location.')
        # This file and the original Windows boundary are immutable v1 installer
        # inputs. Never load a half-updated module to verify the recovery copy.
        spec = importlib.util.spec_from_file_location('csd_update_windows', root/'scripts/client/update_windows.py')
        windows = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(windows)
        guard = windows.WindowsGuard(root, arguments.parent_pid, arguments.parent_created)
        reply = _normal(request.parent/'result.json')
        marker = _json(_read(root/'.client-state', 16384))
        if not isinstance(marker, dict) or marker.get('status') != 'complete':
            raise BootstrapError('Finish server setup before using the updater.')
        if (root/'.updates/pending.json').exists() and not (root/'.update-trust/anchor.json').exists():
            raise BootstrapError('The interrupted update has no trusted recovery tools. Keep its files unchanged.')
        if not (root/'.update-trust/anchor.json').exists() and not (root/'UPDATE_PUBLISHERS.json').exists():
            raise BootstrapError('This installation does not include publisher-approved updates yet. Keep using its current version.')
        record = initialize_bootstrap(root, owner_sid=guard.owner_sid, run_id=marker.get('run_id'),
            assert_private_access=guard.assert_private_access, assert_exclusive_access=guard.assert_exclusive_access)
        sys.path.insert(0, str(root/'.update-trust/bootstrap'))
        from scripts.client.update_controller import execute
        data = execute(root, _json(_read(request, 16384)), read_publishers(record['publishers']), guard)
        result = {'ok':True, 'data':data}
    except Exception as error:
        if type(error).__name__ in {'BootstrapError','WindowsUpdateError','ControllerError',
                'UpdatePackageError','UpdateTransactionError','RecoveryError','ValidationError'}:
            result['message'] = str(error)
    if reply is not None:
        temporary = reply.with_name('result-'+uuid4().hex+'.tmp')
        with temporary.open('xb') as stream:
            stream.write(json.dumps(result, ensure_ascii=True).encode('ascii'))
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(temporary, reply)
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
