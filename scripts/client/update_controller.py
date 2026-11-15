"""Protected updater actions. The graphical Windows parent owns the lock."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from scripts.client import update_package as package, update_transaction as transaction
from scripts.client import update_recovery as recovery, update_validation as validation


class ControllerError(RuntimeError):
    pass


def _read(path, limit):
    path = recovery._normal(Path(path))
    with path.open('rb') as stream:
        value = stream.read(limit+1)
    if len(value) > limit:
        raise ControllerError('The selected file is too large for this update.')
    return value


def _installed(root):
    try:
        value = json.loads(_read(root/package.BUNDLE_MANIFEST, 1024*1024))['sourceCommit']
        if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{40}', value):
            raise ValueError('Invalid release')
        return value
    except (ValueError, KeyError, TypeError):
        raise ControllerError('The installed release could not be identified.') from None


def execute(root, request, publishers, guard):
    root = Path(root)
    if not isinstance(request, dict) or set(request) != {'action','package','sha256'}:
        raise ControllerError('The update request is invalid.')
    action = request['action']
    if action not in {'status','review','apply','recover'}:
        raise ControllerError('The update action is invalid.')
    guard.assert_exclusive_access()
    guard.assert_private_access()
    pending = root/'.updates/pending.json'
    if action == 'status':
        return {'status': 'recovery_required' if pending.exists() else 'ready'}
    if action in {'review','apply'} and pending.exists():
        raise ControllerError('Recover the interrupted update before choosing another package.')
    if action == 'recover' and not pending.exists():
        return {'status':'nothing_pending'}

    if action in {'review','apply'}:
        if not isinstance(request['package'], str) or not Path(request['package']).is_absolute():
            raise ControllerError('Choose a local update package.')
        raw = _read(Path(request['package']), package.MAX_ARCHIVE_BYTES)
        update = package.verify_update_bytes(raw, publishers, from_commit=_installed(root))
        for name, expected in update.before.items():
            data = _read(root/name, package.MAX_FILE_BYTES)
            if (hashlib.sha256(data).hexdigest(), len(data)) != expected:
                raise ControllerError('Installed application files differ from this package. They were not overwritten.')
        if action == 'review':
            return {'status':'reviewed', 'version':update.version, 'from_commit':update.from_commit,
                    'to_commit':update.to_commit, 'sha256':update.archive_sha256,
                    'changed_files':sum(update.before.get(name) != value for name,value in update.after.items())}
        if request['sha256'] != update.archive_sha256:
            raise ControllerError('The package changed after review. Choose and review it again.')
    else:
        # Transaction recovery independently checks the complete journal and its
        # binding again before any source replacement. This read selects smoke inputs.
        try:
            record = json.loads(_read(pending, 4096))
            identifier = record['transaction']
            if not isinstance(identifier, str) or not re.fullmatch('[0-9a-f]{32}', identifier):
                raise ValueError('Invalid transaction')
            raw = _read(root/'.updates'/identifier/'package.zip', package.MAX_ARCHIVE_BYTES)
            update = package.verify_update_bytes(raw, publishers, from_commit=record['from_commit'])
        except (ValueError, KeyError, TypeError):
            raise ControllerError('The interrupted update needs review. Keep its recovery files unchanged.') from None

    active = {}
    def prepare(path):
        recovery.prepare_private_recovery(root, path, assert_exclusive_access=guard.assert_exclusive_access,
                                          assert_private_access=guard.assert_private_access)
    def verify(path):
        recovery.verify_private_recovery(root, path, assert_exclusive_access=guard.assert_exclusive_access,
                                         assert_private_access=guard.assert_private_access)
        active['snapshot'] = path
    def prepare_validation(path):
        validation.prepare_validation_space(root, path, (update.before, update.after),
            assert_exclusive_access=guard.assert_exclusive_access, assert_private_access=guard.assert_private_access)
    def smoke():
        current = _installed(root)
        if current not in {update.from_commit, update.to_commit}:
            raise ControllerError('The application release changed unexpectedly.')
        inventory = update.after if current == update.to_commit else update.before
        validation.validate_application(root, active['snapshot'], inventory,
            assert_exclusive_access=guard.assert_exclusive_access, assert_private_access=guard.assert_private_access)
        verify(active['snapshot'])
    if action == 'apply':
        return transaction.apply_update(root, raw, publishers, from_commit=update.from_commit,
            prepare_private_recovery=prepare, validate_private_recovery=verify,
            validate_installation=smoke, assert_exclusive_access=guard.assert_exclusive_access,
            prepare_validation=prepare_validation)
    return transaction.recover_update(root, publishers, validate_private_recovery=verify,
        validate_installation=smoke, assert_exclusive_access=guard.assert_exclusive_access)
