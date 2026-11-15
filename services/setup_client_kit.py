"""Export only a bounded, verified public LAN browser connection kit.

The caller supplies public connection metadata validated during server startup.
Requests never inspect private LAN state, environment files or attendance data.
"""

from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

from scripts.lan import lan_setup


MAX_FILE_BYTES = 128 * 1024
MAX_ARCHIVE_INPUT_BYTES = 8 * MAX_FILE_BYTES
UNAVAILABLE_MESSAGE = "The verified LAN connection kit is unavailable. Ask the server operator to check LAN setup."
_CONNECTION_FIELDS = frozenset({
    "schemaVersion", "product", "serverUrl", "serverIpv4", "networkCidr",
    "certificateFile", "certificateSha256", "certificateThumbprintSha1",
    "certificateSubject", "sourceCommit", "bundleVersion",
})


class ClientKitUnavailable(RuntimeError):
    """Fixed nonsecret failure suitable for an authenticated UI response."""

    def __init__(self):
        super().__init__(UNAVAILABLE_MESSAGE)


def _require(condition: bool) -> None:
    if not condition:
        raise ClientKitUnavailable()


def _metadata_is_link(metadata) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _validate_directory_path(path: Path) -> None:
    # Validate the lexical path before resolve() can hide a junction/symlink.
    for parent in (*reversed(path.parents), path):
        metadata = parent.lstat()
        _require(stat.S_ISDIR(metadata.st_mode) and not _metadata_is_link(metadata))


def _file_identity(metadata) -> tuple:
    return (metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns)


def _validate_regular_file(metadata) -> None:
    _require(stat.S_ISREG(metadata.st_mode) and not _metadata_is_link(metadata))
    _require(metadata.st_nlink == 1 and 0 < metadata.st_size <= MAX_FILE_BYTES)


def _read_public_file(path: Path) -> bytes:
    _validate_directory_path(path.parent)
    before = path.lstat()
    _validate_regular_file(before)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            opened = os.fstat(descriptor)
            _validate_regular_file(opened)
            _require(_file_identity(opened) == _file_identity(before))
            content = source.read(MAX_FILE_BYTES + 1)
            after = os.fstat(descriptor)
            _validate_regular_file(after)
            _require(_file_identity(after) == _file_identity(before))
        _validate_directory_path(path.parent)
        final = path.lstat()
        _validate_regular_file(final)
        _require(_file_identity(final) == _file_identity(before))
        _require(len(content) == before.st_size)
        return content
    finally:
        os.close(descriptor)


def _validate_connection(connection: dict) -> None:
    _require(type(connection) is dict and set(connection) == _CONNECTION_FIELDS)
    _require(type(connection["schemaVersion"]) is int and connection["schemaVersion"] == 1)
    _require(all(type(connection[name]) is str and 0 < len(connection[name]) <= 1024
                 for name in _CONNECTION_FIELDS - {"schemaVersion"}))
    _require(connection["product"] == "CSD Attendance LAN browser connection")
    _require(connection["certificateFile"] == lan_setup.CLIENT_CERTIFICATE_NAME)
    network = ipaddress.IPv4Network(connection["networkCidr"], strict=True)
    address, expected_network = lan_setup.parse_private_server_network(
        connection["serverIpv4"], network.prefixlen,
    )
    _require(str(address) == connection["serverIpv4"] and str(expected_network) == connection["networkCidr"])
    _require(connection["serverUrl"] == f"https://{address}/")
    for field, length in (("certificateSha256", 64), ("certificateThumbprintSha1", 40), ("sourceCommit", 40)):
        _require(re.fullmatch(rf"[0-9a-fA-F]{{{length}}}", connection[field]) is not None)


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _validate_certificate(content: bytes, expected: dict) -> None:
    _require(hashlib.sha256(content).hexdigest() == expected["certificateSha256"].lower())
    certificate = x509.load_der_x509_certificate(content)
    # Reject trailing bytes/concatenated material, even when a parser accepts it.
    _require(certificate.public_bytes(serialization.Encoding.DER) == content)
    _require(certificate.fingerprint(hashes.SHA1()).hex() == expected["certificateThumbprintSha1"].lower())
    _require(certificate.subject.rfc4514_string() == expected["certificateSubject"])
    _require(certificate.subject == certificate.issuer)
    constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
    usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
    _require(constraints.ca and usage.key_cert_sign)
    now = datetime.now(timezone.utc)
    _require(certificate.not_valid_before_utc <= now < certificate.not_valid_after_utc)
    certificate.verify_directly_issued_by(certificate)


def _validate_manifest(files: dict[str, bytes]) -> None:
    entries = {}
    for line in files["SHA256SUMS"].decode("ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64}) \*([^\\/]+)", line)
        _require(match is not None)
        checksum, name = match.groups()
        _require(name not in entries)
        entries[name] = checksum
    _require(set(entries) == lan_setup.CLIENT_KIT_EXPECTED_FILES - {"SHA256SUMS"})
    _require(all(hashlib.sha256(files[name]).hexdigest() == checksum for name, checksum in entries.items()))


def build_client_kit_archive(root: Path, expected_connection: dict) -> bytes:
    """Return an in-memory ZIP of exactly eight verified public files.

    ``root`` is installation-owned, never request input. ``expected_connection``
    is the sanitized ``lan_setup._connection_document`` from validated startup.
    No readiness checker, private key, process, network or database is consulted.
    """
    try:
        _validate_connection(expected_connection)
        expected = dict(expected_connection)
        installation = Path(os.path.abspath(root))
        _require(not str(installation).startswith(("\\\\", "//")))
        _validate_directory_path(installation)
        kit = installation / lan_setup.CLIENT_KIT_DIRECTORY
        _validate_directory_path(kit)
        actual_names = set()
        with os.scandir(kit) as entries:
            for entry in entries:
                _require(entry.name in lan_setup.CLIENT_KIT_EXPECTED_FILES)
                actual_names.add(entry.name)
                _require(len(actual_names) <= len(lan_setup.CLIENT_KIT_EXPECTED_FILES))
        _require(actual_names == lan_setup.CLIENT_KIT_EXPECTED_FILES)
        files = {name:_read_public_file(kit / name) for name in sorted(actual_names)}
        _require(sum(map(len, files.values())) <= MAX_ARCHIVE_INPUT_BYTES)
        connection = json.loads(files["CONNECTION.json"].decode("utf-8-sig"), object_pairs_hook=_unique_json_object)
        _validate_connection(connection)
        _require(connection == expected)
        _validate_certificate(files[lan_setup.CLIENT_CERTIFICATE_NAME], expected)
        _validate_manifest(files)
        # A self-consistent kit checksum manifest does not authenticate scripts.
        for name, relative_source in lan_setup.TRACKED_CLIENT_KIT_FILES.items():
            _require(files[name] == _read_public_file(installation / relative_source))
        expected_readme = lan_setup._client_readme({
            "publicUrl": expected["serverUrl"], "networkCidr": expected["networkCidr"],
            "rootCertificateSha256": expected["certificateSha256"],
        }).encode("utf-8")
        _require(files["README.txt"] == expected_readme)
        output = io.BytesIO()
        with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
            for name, content in sorted(files.items()):
                entry = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                entry.compress_type = ZIP_DEFLATED
                entry.external_attr = (stat.S_IFREG | 0o600) << 16
                archive.writestr(entry, content)
        return output.getvalue()
    except ClientKitUnavailable:
        raise
    except Exception:
        # Never expose filesystem paths, malformed content or crypto internals.
        raise ClientKitUnavailable() from None
