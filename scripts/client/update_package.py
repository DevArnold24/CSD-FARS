"""Signed, bounded packages for compatible Windows application updates.

This module never executes package code or changes an installation. Publisher
keys come from the already trusted installer, never from the update archive.
Version 1 deliberately rejects dependency/schema changes and removed files.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
from pathlib import Path
import re
import stat
import struct
from types import MappingProxyType
from typing import Mapping
import zipfile

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

FORMAT = 'csd-attendance-update-v1'
DOMAIN = b'CSD-ATTENDANCE-UPDATE-V1\0'
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_FILES = 4096
BUNDLE_MANIFEST = 'CLIENT_BUNDLE_MANIFEST.json'
IMMUTABLE_INPUTS = frozenset({
    'requirements.txt', 'client-requirements.in', 'requirements-client.lock',
    'migrate.py', 'database/schema.sql',
    # The pre-verification launcher must remain executable through interruption.
    'UPDATE_CSD.cmd', 'UPDATE_PUBLISHERS.json', 'scripts/client/Update-CSD.ps1',
    'scripts/client/update_bootstrap.py', 'scripts/client/update_windows.py',
    'scripts/windows/Update-Access.ps1', 'scripts/windows/Private-Storage.ps1',
    'scripts/windows/Client-Storage.ps1', 'scripts/client/windows_storage.py',
    'scripts/windows/Lifecycle.ps1', 'scripts/windows/NativeLifetime.cs',
    'scripts/windows/Setup-Dialogs.ps1',
    # v1 preserves generated connection kits and the existing local CA identity.
    # Changes to this protocol need a separately validated kit migration.
    'scripts/lan/lan_setup.py', 'scripts/lan/client/Trust-And-Open.ps1',
    'scripts/lan/client/Remove-Trust.ps1', 'scripts/lan/client/CONNECT_TO_CSD.cmd',
    'scripts/lan/client/REMOVE_CSD_TRUST.cmd',
    'scripts/lan/client/Certificate-Store.ps1',
})
PRIVATE_PARTS = frozenset({
    '.git', '.env', '.client-state', '.lan-server', '.runtime', '.venv', 'venv',
    '.update-state', '.update-trust', '.updates', 'lan_client_kit', 'backups',
    'uploads', '__pycache__', 'node_modules', '.pytest_cache',
    '.deployment-preference.json', '.support',
})
PRIVATE_SUFFIXES = ('.db', '.sqlite', '.sqlite3', '-wal', '-shm', '.key', '.pfx', '.pem', '.log', '.pyc', '.pyo')


class UpdatePackageError(ValueError):
    """A package or publisher identity failed verification."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('ascii')


def publisher_id(public_key: bytes) -> str:
    if not isinstance(public_key, bytes) or len(public_key) != 32:
        raise UpdatePackageError('The configured publisher key is invalid.')
    return hashlib.sha256(public_key).hexdigest()


def _path(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9._ /-]{1,240}', value):
        raise UpdatePackageError('The update contains an unsupported file path.')
    parts = value.split('/')
    if len(parts) > 16 or any(
        not part or part in {'.', '..'} or part != part.strip() or part.endswith('.')
        or part.lower() in PRIVATE_PARTS
        or part.lower().startswith(('.env.', '.client-state.', '.update-', '.client-publish-'))
        or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\..*)?', part)
        for part in parts
    ) or value.lower().endswith(PRIVATE_SUFFIXES):
        raise UpdatePackageError('The update contains a private or unsafe file path.')
    return value


def _inventory(value: object) -> dict[str, tuple[str, int]]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_FILES:
        raise UpdatePackageError('The update file list is invalid.')
    result, folded = {}, set()
    total = 0
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {'path', 'sha256', 'bytes'}:
            raise UpdatePackageError('An update file record is invalid.')
        path = _path(entry['path'])
        if path.casefold() in folded:
            raise UpdatePackageError('The update contains duplicate file paths.')
        if (not isinstance(entry['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256'])
                or type(entry['bytes']) is not int or not 0 <= entry['bytes'] <= MAX_FILE_BYTES):
            raise UpdatePackageError('An update file size or checksum is invalid.')
        result[path] = (entry['sha256'], entry['bytes'])
        folded.add(path.casefold())
        total += entry['bytes']
    if total > MAX_TOTAL_BYTES:
        raise UpdatePackageError('The update is too large.')
    # A file cannot also be an ancestor directory, including Windows case aliases.
    if any('/'.join(path.split('/')[:n]).casefold() in folded
           for path in result for n in range(1, len(path.split('/')))):
        raise UpdatePackageError('The update contains conflicting file paths.')
    return result


def _metadata(document: object):
    required = {'format', 'publisher', 'from_commit', 'to_commit', 'version', 'before', 'after'}
    if not isinstance(document, dict) or set(document) != required or document['format'] != FORMAT:
        raise UpdatePackageError('This update format is not supported.')
    for name, length in (('publisher', 64), ('from_commit', 40), ('to_commit', 40)):
        if not isinstance(document[name], str) or not re.fullmatch(r'[0-9a-f]{' + str(length) + '}', document[name]):
            raise UpdatePackageError('The update release identity is invalid.')
    if (document['from_commit'] == document['to_commit']
            or not isinstance(document['version'], str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', document['version'])):
        raise UpdatePackageError('The update version is invalid.')
    before, after = _inventory(document['before']), _inventory(document['after'])
    if BUNDLE_MANIFEST not in before or BUNDLE_MANIFEST not in after:
        raise UpdatePackageError('The update must include both installation version records.')
    if not set(before).issubset(after):
        raise UpdatePackageError('Removing installed files is not supported by this updater version.')
    if not IMMUTABLE_INPUTS.issubset(before) or any(before[p] != after[p] for p in IMMUTABLE_INPUTS):
        raise UpdatePackageError('This update requires a separately supported dependency or database upgrade, or a newer installer or network connection procedure.')
    return before, after


@dataclass(frozen=True)
class VerifiedUpdate:
    publisher: str
    from_commit: str
    to_commit: str
    version: str
    archive_sha256: str
    before: Mapping[str, tuple[str, int]]
    after: Mapping[str, tuple[str, int]]
    payloads: Mapping[str, bytes]


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UpdatePackageError('The update manifest contains duplicate fields.')
        result[key] = value
    return result


def _read_member(archive, info, limit):
    if info.file_size > limit or info.file_size < 0:
        raise UpdatePackageError('An update member exceeds its size limit.')
    with archive.open(info) as source:
        result = source.read(limit + 1)
    if len(result) != info.file_size or len(result) > limit:
        raise UpdatePackageError('An update member has an invalid size.')
    return result


def _check_zip_directory(raw: bytes) -> None:
    # Bound directory parsing before ZipFile allocates a ZipInfo per entry.
    # Our v1 builder needs neither ZIP64, multi-disk ZIPs, extras nor comments.
    if len(raw) < 22 or not raw.startswith(b'PK\x03\x04'):
        raise UpdatePackageError('The update archive is incomplete.')
    signature, disk, central_disk, count, total, size, offset, comment = struct.unpack_from('<4s4H2IH', raw, len(raw)-22)
    if (signature != b'PK\x05\x06' or disk or central_disk or count != total
            or not 3 <= total <= MAX_FILES+2 or comment or size > 2*1024*1024
            or offset+size != len(raw)-22):
        raise UpdatePackageError('The update archive directory is unsupported.')
    cursor, end = offset, offset+size
    for _ in range(total):
        if cursor+46 > end:
            raise UpdatePackageError('The update archive directory is incomplete.')
        fields = struct.unpack_from('<4s6H3I5H2I', raw, cursor)
        if fields[0] != b'PK\x01\x02' or not 1 <= fields[10] <= 246 or fields[11] or fields[12] or fields[13]:
            raise UpdatePackageError('The update archive contains unsupported directory fields.')
        cursor += 46+fields[10]
    if cursor != end:
        raise UpdatePackageError('The update archive contains unexpected directory entries.')


def verify_update_bytes(raw: bytes, trusted_publishers: Mapping[str, bytes], *, from_commit: str) -> VerifiedUpdate:
    """Validate immutable bytes; no extraction, process, database or network access."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_ARCHIVE_BYTES:
        raise UpdatePackageError('The update archive is empty or too large.')
    try:
        _check_zip_directory(raw)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if not 3 <= len(entries) <= MAX_FILES + 2:
                raise UpdatePackageError('The update archive has an invalid file count.')
            by_name = {}
            for info in entries:
                if (info.is_dir() or info.orig_filename != info.filename or info.filename in by_name or info.flag_bits & 1
                        or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                        or stat.S_IFMT(info.external_attr >> 16) not in {0, stat.S_IFREG}):
                    raise UpdatePackageError('The update archive contains an unsupported member.')
                by_name[info.filename] = info
            if not {'update.json', 'update.sig'}.issubset(by_name):
                raise UpdatePackageError('The signed update manifest is missing.')
            manifest = _read_member(archive, by_name['update.json'], MAX_MANIFEST_BYTES)
            document = json.loads(manifest.decode('ascii'), object_pairs_hook=_unique_object)
            if _canonical(document) != manifest:
                raise UpdatePackageError('The update manifest is not in its signed canonical form.')
            before, after = _metadata(document)
            public = trusted_publishers.get(document['publisher'])
            if public is None or publisher_id(public) != document['publisher']:
                raise UpdatePackageError('This update publisher is not trusted by this installation.')
            signature = _read_member(archive, by_name['update.sig'], 64)
            if len(signature) != 64:
                raise UpdatePackageError('The update signature is invalid.')
            Ed25519PublicKey.from_public_bytes(public).verify(signature, DOMAIN + manifest)
            if document['from_commit'] != from_commit:
                raise UpdatePackageError('This update does not support the installed version.')
            if set(by_name) != {'update.json', 'update.sig', *('files/' + path for path in after)}:
                raise UpdatePackageError('The update contains missing or unexpected files.')
            payloads = {}
            for path, (digest, size) in after.items():
                payload = _read_member(archive, by_name['files/' + path], size)
                if hashlib.sha256(payload).hexdigest() != digest:
                    raise UpdatePackageError('An update file failed its checksum check.')
                payloads[path] = payload
            bundle = json.loads(payloads[BUNDLE_MANIFEST].decode('utf-8'), object_pairs_hook=_unique_object)
            if (not isinstance(bundle, dict) or bundle.get('sourceCommit') != document['to_commit']
                    or bundle.get('bundleVersion') != document['version']):
                raise UpdatePackageError('The updated installation version does not match the signed release.')
    except UpdatePackageError:
        raise
    except (InvalidSignature, OSError, ValueError, UnicodeError, KeyError, TypeError,
            RecursionError, RuntimeError, struct.error, zipfile.BadZipFile, NotImplementedError):
        raise UpdatePackageError('The update package could not be verified. No installation files were changed.') from None
    return VerifiedUpdate(document['publisher'], document['from_commit'], document['to_commit'],
                          document['version'], hashlib.sha256(raw).hexdigest(),
                          MappingProxyType(before), MappingProxyType(after), MappingProxyType(payloads))


def verify_update_file(path: Path, trusted_publishers: Mapping[str, bytes], *, from_commit: str) -> VerifiedUpdate:
    try:
        path = Path(path)
        if not stat.S_ISREG(path.stat().st_mode):
            raise UpdatePackageError('Choose a normal update package file.')
        with path.open('rb') as stream:
            raw = stream.read(MAX_ARCHIVE_BYTES + 1)
    except OSError:
        raise UpdatePackageError('The update package file could not be read.') from None
    return verify_update_bytes(raw, trusted_publishers, from_commit=from_commit)


def build_update_bytes(before: Mapping[str, bytes], after: Mapping[str, bytes], *,
                       from_commit: str, to_commit: str, version: str,
                       signing_key: Ed25519PrivateKey) -> bytes:
    """Publisher-side builder; the private key is never serialized into the ZIP."""
    def records(files):
        return [{'path': path, 'sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload)}
                for path, payload in sorted(files.items())]
    public = signing_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    document = dict(format=FORMAT, publisher=publisher_id(public), from_commit=from_commit,
                    to_commit=to_commit, version=version, before=records(before), after=records(after))
    _metadata(document)
    manifest = _canonical(document)
    if len(manifest) > MAX_MANIFEST_BYTES:
        raise UpdatePackageError('The update manifest is too large.')
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('update.json', manifest)
        archive.writestr('update.sig', signing_key.sign(DOMAIN + manifest))
        for path, payload in sorted(after.items()):
            archive.writestr('files/' + path, payload)
    result = output.getvalue()
    verify_update_bytes(result, {publisher_id(public): public}, from_commit=from_commit)
    return result
