"""Create and validate the private Windows LAN server configuration.

The normal portable client remains loopback-only.  This module prepares a
separate HTTPS gateway which is bound to one pinned RFC1918 address and keeps
Waitress plus SQLite on the server laptop.  It never reads or copies camera
credentials, application secrets, biometric material, logs, or database
contents.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization


STATE_SCHEMA_VERSION = 1
CADDY_VERSION = "2.11.4"
CADDY_ARCHIVE_SHA256 = (
    "1708333f79e274c7697285afe6d592ab39314e0b131e9ec6bea08ad27df62ebf"
)
CADDY_EXECUTABLE_SHA256 = (
    "5cb9ab71e5756ce72840b8234177a2f40c8b4ab47a806b8e841e2b784e9df62b"
)
CADDY_LICENSE_SHA256 = (
    "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
)
CADDY_SBOM_SHA256 = (
    "ecc0c7270380760e6ab62521dcc6baee8a4e7324319bfdaf0874cd5ec74f7e88"
)
HTTPS_PORT = 443
DEFAULT_WORKER_THREADS = 32
MIN_WORKER_THREADS = 16
MAX_WORKER_THREADS = 128
MAX_REQUEST_BYTES = 16 * 1024 * 1024

PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)

PRIVATE_DIRECTORY = Path(".lan-server")
STATE_PATH = PRIVATE_DIRECTORY / "state.json"
CADDYFILE_PATH = PRIVATE_DIRECTORY / "Caddyfile"
CADDY_DATA_DIRECTORY = PRIVATE_DIRECTORY / "caddy-data"
CADDY_CONFIG_DIRECTORY = PRIVATE_DIRECTORY / "caddy-config"
CADDY_ROOT_CERTIFICATE_PATH = (
    CADDY_DATA_DIRECTORY / "caddy" / "pki" / "authorities" / "local" / "root.crt"
)
CADDY_ROOT_KEY_PATH = (
    CADDY_DATA_DIRECTORY / "caddy" / "pki" / "authorities" / "local" / "root.key"
)
CLIENT_KIT_DIRECTORY = Path("LAN_CLIENT_KIT")
RECONFIGURATION_JOURNAL = PRIVATE_DIRECTORY / "reconfiguration.json"
CLIENT_CERTIFICATE_NAME = "CSD_Attendance_LAN_CA.cer"

TRACKED_CLIENT_KIT_FILES = {
    "CONNECT_TO_CSD.cmd": Path("scripts/lan/client/CONNECT_TO_CSD.cmd"),
    "REMOVE_CSD_TRUST.cmd": Path("scripts/lan/client/REMOVE_CSD_TRUST.cmd"),
    "Trust-And-Open.ps1": Path("scripts/lan/client/Trust-And-Open.ps1"),
    "Remove-Trust.ps1": Path("scripts/lan/client/Remove-Trust.ps1"),
    "Certificate-Store.ps1": Path("scripts/lan/client/Certificate-Store.ps1"),
    "Setup-Dialogs.ps1": Path("scripts/windows/Setup-Dialogs.ps1"),
}

CLIENT_KIT_EXPECTED_FILES = frozenset(
    {
        *TRACKED_CLIENT_KIT_FILES,
        CLIENT_CERTIFICATE_NAME,
        "CONNECTION.json",
        "README.txt",
        "SHA256SUMS",
    }
)

SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; object-src 'none'; "
        "frame-ancestors 'none'; frame-src 'none'; form-action 'self'; "
        "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; "
        "media-src 'self' blob:; worker-src 'self' blob:; "
        "upgrade-insecure-requests"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Permissions-Policy": "camera=(self), microphone=(), geolocation=()",
}


class LanSetupError(RuntimeError):
    """A safe, operator-facing LAN setup failure."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.incomplete-{uuid4().hex}")
    try:
        with temporary.open("xb") as destination:
            destination.write(value)
            destination.flush()
            os.fsync(destination.fileno())
        if os.name == "nt":
            import ctypes
            move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
            move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
            move.restype = ctypes.c_int
            if not move(str(temporary), str(path), 0x1 | 0x8):  # REPLACE_EXISTING | WRITE_THROUGH
                raise OSError(ctypes.get_last_error(), "LAN state publication failed")
        else:
            os.replace(temporary, path)
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _atomic_write_text(path: Path, value: str) -> None:
    _atomic_write_bytes(path, value.encode("utf-8"))


def _canonical_root(root: Path) -> Path:
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise LanSetupError(f"The installation folder is unavailable: {root}") from exc
    if not resolved.is_dir():
        raise LanSetupError(f"The installation folder is not a directory: {resolved}")
    return resolved


def _is_reparse_or_symlink(path: Path) -> bool:
    """Return whether *path* redirects filesystem access through a link/reparse point."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise LanSetupError(f"LAN server path metadata is unavailable: {path}") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    return path.is_symlink() or bool(reparse_flag and file_attributes & reparse_flag)


def _lexical_child(root: Path, relative_path: Path) -> tuple[Path, tuple[str, ...]]:
    candidate = Path(os.path.abspath(root / relative_path))
    try:
        parts = candidate.relative_to(root).parts
    except ValueError as exc:
        raise LanSetupError(
            f"LAN server path escapes the installation: {relative_path}"
        ) from exc
    return candidate, parts


def _assert_normal_local_tree(root: Path, relative_path: Path, description: str) -> None:
    """Reject a pre-planted link or reparse point before a managed tree is written."""
    candidate, parts = _lexical_child(root, relative_path)
    current = root
    for part in parts:
        current /= part
        if not os.path.lexists(current):
            break
        if _is_reparse_or_symlink(current):
            raise LanSetupError(f"{description} cannot contain a link or junction: {current}")

    if not os.path.lexists(candidate):
        return
    if not candidate.is_dir():
        raise LanSetupError(f"{description} must be a normal local directory: {candidate}")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise LanSetupError(f"{description} escapes the installation: {candidate}") from exc
    if os.path.normcase(str(resolved)) != os.path.normcase(str(candidate)):
        raise LanSetupError(f"{description} cannot redirect outside its normal path: {candidate}")

    def fail_walk(error: OSError) -> None:
        raise LanSetupError(f"{description} cannot be inspected safely: {candidate}") from error

    for directory, directory_names, file_names in os.walk(
        candidate, topdown=True, onerror=fail_walk, followlinks=False
    ):
        for name in (*directory_names, *file_names):
            child = Path(directory) / name
            if _is_reparse_or_symlink(child):
                raise LanSetupError(f"{description} cannot contain a link or junction: {child}")


def _root_file(root: Path, relative_path: Path, *, required: bool = True) -> Path:
    candidate, parts = _lexical_child(root, relative_path)
    try:
        current = root
        for part in parts:
            current /= part
            if os.path.lexists(current) and _is_reparse_or_symlink(current):
                raise LanSetupError(
                    f"LAN server file cannot use a link or junction: {relative_path}"
                )
        resolved = candidate.resolve(strict=required)
        resolved.relative_to(root)
    except LanSetupError:
        raise
    except (OSError, ValueError) as exc:
        raise LanSetupError(
            f"LAN server file escapes or is missing from the installation: {relative_path}"
        ) from exc
    return resolved


def parse_private_server_network(
    server_ipv4: str, prefix_length: int
) -> tuple[ipaddress.IPv4Address, ipaddress.IPv4Network]:
    try:
        address = ipaddress.IPv4Address(server_ipv4)
    except ipaddress.AddressValueError as exc:
        raise LanSetupError("The LAN server address must be a valid IPv4 address.") from exc
    if not any(address in network for network in PRIVATE_NETWORKS):
        raise LanSetupError(
            "The LAN server must use an RFC1918 private address (10/8, 172.16/12, or 192.168/16)."
        )
    if isinstance(prefix_length, bool) or not 8 <= int(prefix_length) <= 30:
        raise LanSetupError("The LAN prefix length must be from 8 through 30.")
    network = ipaddress.ip_network(f"{address}/{int(prefix_length)}", strict=False)
    if not any(network.subnet_of(private_network) for private_network in PRIVATE_NETWORKS):
        raise LanSetupError(
            "The selected LAN subnet must remain entirely inside one RFC1918 private range."
        )
    if address in {network.network_address, network.broadcast_address}:
        raise LanSetupError("The selected address is reserved as the network or broadcast address.")
    return address, network


def validate_backend_port(port: int) -> int:
    if isinstance(port, bool) or not 1024 <= int(port) <= 65535 or int(port) == HTTPS_PORT:
        raise LanSetupError("The loopback application port must be 1024-65535 and cannot be 443.")
    return int(port)


def validate_worker_threads(worker_threads: int) -> int:
    if (
        isinstance(worker_threads, bool)
        or not MIN_WORKER_THREADS <= int(worker_threads) <= MAX_WORKER_THREADS
    ):
        raise LanSetupError(
            f"The LAN worker count must be {MIN_WORKER_THREADS}-{MAX_WORKER_THREADS}."
        )
    return int(worker_threads)


def firewall_rule_name(root: Path) -> str:
    identity = str(root).rstrip("\\/").upper().encode("utf-8")
    return f"CSD-Attendance-LAN-HTTPS-{hashlib.sha256(identity).hexdigest()[:16]}"


def build_caddyfile(
    server_ipv4: str, backend_port: int, *, prefix_length: int
) -> str:
    address, _ = parse_private_server_network(server_ipv4, prefix_length)
    backend_port = validate_backend_port(backend_port)
    csp = SECURITY_HEADERS["Content-Security-Policy"]
    return f"""{{
	admin off
	persist_config off
	auto_https disable_redirects
	local_certs
	skip_install_trust
	default_sni {address}
	grace_period 15s
	servers {address}:443 {{
		protocols h1 h2
	}}
	pki {{
		ca local {{
			name \"CSD Attendance LAN CA\"
		}}
	}}
}}

https://{address} {{
	bind {address}

	request_body {{
		max_size 16MiB
	}}

	header {{
		Strict-Transport-Security \"{SECURITY_HEADERS['Strict-Transport-Security']}\"
		Content-Security-Policy \"{csp}\"
		X-Frame-Options \"{SECURITY_HEADERS['X-Frame-Options']}\"
		X-Content-Type-Options \"{SECURITY_HEADERS['X-Content-Type-Options']}\"
		?Referrer-Policy \"same-origin\"
		Permissions-Policy \"{SECURITY_HEADERS['Permissions-Policy']}\"
		-Server
	}}

	reverse_proxy 127.0.0.1:{backend_port} {{
		transport http {{
			versions 1.1
			keepalive 30s
			network_proxy none
		}}
	}}
}}

:443 {{
	bind {address}
	header {{
		X-Content-Type-Options \"nosniff\"
		-Server
	}}
	respond \"Misdirected Request\" 421
}}
"""


def _load_json_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LanSetupError(f"{description} is missing or invalid: {path}") from exc
    if not isinstance(value, dict):
        raise LanSetupError(f"{description} must contain a JSON object: {path}")
    return value


def _validate_bundle(root: Path) -> dict[str, Any]:
    manifest_path = _root_file(root, Path("CLIENT_BUNDLE_MANIFEST.json"))
    manifest = _load_json_object(manifest_path, "The client bundle manifest")
    source_commit = manifest.get("sourceCommit")
    bundle_version = manifest.get("bundleVersion")
    if not isinstance(source_commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", source_commit):
        raise LanSetupError("The client bundle manifest has no valid source commit.")
    if not isinstance(bundle_version, str) or not bundle_version.strip():
        raise LanSetupError("The client bundle manifest has no bundle version.")

    expected_caddy = {
        "version": CADDY_VERSION,
        "archiveSha256": CADDY_ARCHIVE_SHA256,
        "executableSha256": CADDY_EXECUTABLE_SHA256,
        "licenseSha256": CADDY_LICENSE_SHA256,
        "sbomSha256": CADDY_SBOM_SHA256,
    }
    if manifest.get("caddy") != expected_caddy:
        raise LanSetupError(
            "The bundle manifest does not describe the reviewed Caddy LAN gateway. "
            "Download the complete verified Windows Client Bundle."
        )

    caddy_executable = _root_file(root, Path("tools/caddy/caddy.exe"))
    caddy_license = _root_file(root, Path("tools/caddy/LICENSE"))
    caddy_sbom = _root_file(root, Path("tools/caddy/SBOM.cdx.json"))
    if sha256_file(caddy_executable) != CADDY_EXECUTABLE_SHA256:
        raise LanSetupError("The bundled Caddy executable failed its pinned SHA-256 check.")
    if sha256_file(caddy_license) != CADDY_LICENSE_SHA256:
        raise LanSetupError("The bundled Caddy license failed its pinned SHA-256 check.")
    if sha256_file(caddy_sbom) != CADDY_SBOM_SHA256:
        raise LanSetupError("The bundled Caddy SBOM failed its pinned SHA-256 check.")
    return manifest


def _state_identity(state: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "schemaVersion",
        "mode",
        "serverIpv4",
        "prefixLength",
        "networkCidr",
        "backendPort",
        "httpsPort",
        "workerThreads",
        "publicUrl",
        "caddyVersion",
        "caddyExecutableSha256",
        "caddyLicenseSha256",
        "caddyArchiveSha256",
        "caddySbomSha256",
        "sourceCommit",
        "bundleVersion",
        "firewallRuleName",
    )
    return {key: state.get(key) for key in keys}


def _expected_state(
    root: Path,
    manifest: Mapping[str, Any],
    server_ipv4: str,
    prefix_length: int,
    backend_port: int,
    worker_threads: int,
) -> dict[str, Any]:
    address, network = parse_private_server_network(server_ipv4, prefix_length)
    port = validate_backend_port(backend_port)
    threads = validate_worker_threads(worker_threads)
    return {
        "schemaVersion": STATE_SCHEMA_VERSION,
        "mode": "lan-server",
        "status": "prepared",
        "serverIpv4": str(address),
        "prefixLength": network.prefixlen,
        "networkCidr": str(network),
        "backendPort": port,
        "httpsPort": HTTPS_PORT,
        "workerThreads": threads,
        "publicUrl": f"https://{address}/",
        "caddyVersion": CADDY_VERSION,
        "caddyExecutableSha256": CADDY_EXECUTABLE_SHA256,
        "caddyLicenseSha256": CADDY_LICENSE_SHA256,
        "caddyArchiveSha256": CADDY_ARCHIVE_SHA256,
        "caddySbomSha256": CADDY_SBOM_SHA256,
        "sourceCommit": str(manifest["sourceCommit"]).lower(),
        "bundleVersion": str(manifest["bundleVersion"]),
        "firewallRuleName": firewall_rule_name(root),
        "preparedAtUtc": datetime.now(timezone.utc).isoformat(),
    }


def prepare(
    root: Path,
    server_ipv4: str,
    prefix_length: int,
    backend_port: int,
    worker_threads: int = DEFAULT_WORKER_THREADS,
) -> dict[str, Any]:
    root = _canonical_root(root)
    _assert_no_pending_reconfiguration(root)
    _assert_normal_local_tree(root, PRIVATE_DIRECTORY, "Private LAN state")
    manifest = _validate_bundle(root)
    expected = _expected_state(
        root,
        manifest,
        server_ipv4,
        prefix_length,
        backend_port,
        worker_threads,
    )
    state_path = root / STATE_PATH
    if state_path.exists():
        existing = _load_json_object(state_path, "The LAN server state")
        if _state_identity(existing) != _state_identity(expected):
            raise LanSetupError(
                "This installation is already bound to a different LAN address, bundle, or "
                "server configuration. Preserve the complete recovery set and ask a technician "
                "to perform an explicit LAN reconfiguration."
            )
        expected = existing

    private_directory = root / PRIVATE_DIRECTORY
    private_directory.mkdir(parents=True, exist_ok=True)
    _assert_normal_local_tree(root, PRIVATE_DIRECTORY, "Private LAN state")
    (root / CADDY_DATA_DIRECTORY).mkdir(parents=True, exist_ok=True)
    (root / CADDY_CONFIG_DIRECTORY).mkdir(parents=True, exist_ok=True)

    caddyfile = build_caddyfile(
        expected["serverIpv4"], expected["backendPort"],
        prefix_length=expected["prefixLength"],
    )
    _atomic_write_text(root / CADDYFILE_PATH, caddyfile)
    if not state_path.exists():
        _atomic_write_text(
            state_path,
            json.dumps(expected, indent=2, sort_keys=True) + "\n",
        )
    return expected


def _certificate_details(certificate_path: Path) -> tuple[x509.Certificate, bytes]:
    try:
        pem = certificate_path.read_bytes()
        certificate = x509.load_pem_x509_certificate(pem)
    except (OSError, ValueError) as exc:
        raise LanSetupError("Caddy's internal root certificate is missing or invalid.") from exc
    try:
        constraints = certificate.extensions.get_extension_for_class(
            x509.BasicConstraints
        ).value
        key_usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound as exc:
        raise LanSetupError("Caddy's root certificate lacks required CA extensions.") from exc
    if not constraints.ca or not key_usage.key_cert_sign:
        raise LanSetupError("Caddy's generated certificate is not a certificate-authority root.")
    if certificate.subject != certificate.issuer:
        raise LanSetupError("Caddy's generated root certificate is not self-issued.")

    now = datetime.now(timezone.utc)
    not_before = certificate.not_valid_before_utc
    not_after = certificate.not_valid_after_utc
    if now < not_before or now >= not_after:
        raise LanSetupError("Caddy's generated root certificate is not currently valid.")
    der = certificate.public_bytes(serialization.Encoding.DER)
    return certificate, der


def _validate_private_key_matches_certificate(
    private_key_path: Path, certificate: x509.Certificate
) -> None:
    try:
        private_key = serialization.load_pem_private_key(
            private_key_path.read_bytes(), password=None
        )
        private_public_key = private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        certificate_public_key = certificate.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise LanSetupError("Caddy's private LAN root key is invalid.") from exc
    if private_public_key != certificate_public_key:
        raise LanSetupError(
            "Caddy's private LAN root key does not match its public root certificate."
        )


def _connection_document(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "product": "CSD Attendance LAN browser connection",
        "serverUrl": state["publicUrl"],
        "serverIpv4": state["serverIpv4"],
        "networkCidr": state["networkCidr"],
        "certificateFile": CLIENT_CERTIFICATE_NAME,
        "certificateSha256": state["rootCertificateSha256"],
        "certificateThumbprintSha1": state["rootCertificateThumbprintSha1"],
        "certificateSubject": state["rootCertificateSubject"],
        "sourceCommit": state["sourceCommit"],
        "bundleVersion": state["bundleVersion"],
    }


def _client_readme(state: Mapping[str, Any]) -> str:
    return f"""CSD ATTENDANCE - CONNECT THIS WINDOWS USER TO THE LAN SERVER
================================================================

Server address: {state['publicUrl']}
Allowed private network: {state['networkCidr']}
LAN CA SHA-256: {state['rootCertificateSha256']}

1. Connect this laptop to the client's approved private router/switch network.
2. Confirm the fingerprint above with the server operator using a separate,
   trusted view of the server screen.
3. Double-click CONNECT_TO_CSD.cmd.
4. Compare the displayed fingerprint with the server owner's copy. Select
   "The fingerprints match - connect" only when they match. Closing cancels.
5. The script trusts this exact private CA for the current Windows user and
   opens the server in the default browser. If the server is unavailable, use
   "Check connection again" after checking the server and network.

Use Microsoft Edge or Google Chrome. Never continue through a browser
certificate warning. This kit contains no application database, account
password, camera credential, biometric material, or private CA key.

This connection works only while the server laptop is running
START_CSD_LAN_SERVER.cmd on the same private LAN. There must be no router port
forwarding or public Wi-Fi exposure.

When this laptop is no longer authorized, double-click
REMOVE_CSD_TRUST.cmd. It removes only the exact certificate named in this kit
from the current Windows user's trusted-root store.
"""


def _write_client_kit(root: Path, state: Mapping[str, Any], certificate_der: bytes) -> None:
    kit = root / CLIENT_KIT_DIRECTORY
    _assert_normal_local_tree(root, CLIENT_KIT_DIRECTORY, "The LAN browser connection kit")
    if kit.exists() and not kit.is_dir():
        raise LanSetupError("LAN_CLIENT_KIT exists but is not a normal directory.")
    kit.mkdir(parents=True, exist_ok=True)
    _assert_normal_local_tree(root, CLIENT_KIT_DIRECTORY, "The LAN browser connection kit")
    existing_directories = [path for path in kit.rglob("*") if path.is_dir()]
    if existing_directories:
        raise LanSetupError(
            "LAN_CLIENT_KIT contains an unexpected directory; preserve it for review before "
            f"rerunning: {existing_directories[0].relative_to(kit).as_posix()}"
        )
    existing_files = {
        path.relative_to(kit).as_posix()
        for path in kit.rglob("*")
        if path.is_file()
    }
    unexpected = existing_files - CLIENT_KIT_EXPECTED_FILES
    if unexpected:
        raise LanSetupError(
            "LAN_CLIENT_KIT contains unexpected files; preserve it for review before rerunning: "
            + ", ".join(sorted(unexpected))
        )

    for destination_name, source_relative in TRACKED_CLIENT_KIT_FILES.items():
        source = _root_file(root, source_relative)
        _atomic_write_bytes(kit / destination_name, source.read_bytes())
    _atomic_write_bytes(kit / CLIENT_CERTIFICATE_NAME, certificate_der)
    _atomic_write_text(
        kit / "CONNECTION.json",
        json.dumps(_connection_document(state), indent=2, sort_keys=True) + "\n",
    )
    _atomic_write_text(kit / "README.txt", _client_readme(state))

    manifest_lines = []
    for path in sorted(kit.iterdir(), key=lambda item: item.name.lower()):
        if path.is_file() and path.name != "SHA256SUMS":
            manifest_lines.append(f"{sha256_file(path)} *{path.name}")
    _atomic_write_text(kit / "SHA256SUMS", "\n".join(manifest_lines) + "\n")


def finalize(root: Path) -> dict[str, Any]:
    root = _canonical_root(root)
    _assert_no_pending_reconfiguration(root)
    _assert_normal_local_tree(root, PRIVATE_DIRECTORY, "Private LAN state")
    _assert_normal_local_tree(root, CLIENT_KIT_DIRECTORY, "The LAN browser connection kit")
    state_path = root / STATE_PATH
    state = _load_json_object(state_path, "The LAN server state")
    _validate_ready_or_prepared_state(root, state)
    certificate_path = _root_file(root, CADDY_ROOT_CERTIFICATE_PATH)
    key_path = _root_file(root, CADDY_ROOT_KEY_PATH)
    if not key_path.is_file() or key_path.stat().st_size < 32:
        raise LanSetupError("Caddy's private root key is missing from private server state.")
    certificate, der = _certificate_details(certificate_path)
    _validate_private_key_matches_certificate(key_path, certificate)

    state = dict(state)
    state.update(
        {
            "status": "ready",
            "rootCertificateSha256": _sha256_bytes(der),
            "rootCertificateThumbprintSha1": certificate.fingerprint(
                hashes.SHA1()
            ).hex().upper(),
            "rootCertificateSubject": certificate.subject.rfc4514_string(),
            "readyAtUtc": state.get("readyAtUtc")
            or datetime.now(timezone.utc).isoformat(),
        }
    )
    _write_client_kit(root, state, der)
    _atomic_write_text(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
    return state


def _validate_ready_or_prepared_state(root: Path, state: Mapping[str, Any]) -> None:
    manifest = _validate_bundle(root)
    try:
        expected = _expected_state(
            root,
            manifest,
            str(state["serverIpv4"]),
            int(state["prefixLength"]),
            int(state["backendPort"]),
            int(state["workerThreads"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LanSetupError("The LAN server state is incomplete or invalid.") from exc
    if _state_identity(state) != _state_identity(expected):
        raise LanSetupError("The LAN server state does not match this verified bundle or address.")
    if state.get("status") not in {"prepared", "ready"}:
        raise LanSetupError("The LAN server state has an unsupported lifecycle status.")
    expected_caddyfile = build_caddyfile(
        expected["serverIpv4"], expected["backendPort"],
        prefix_length=expected["prefixLength"],
    )
    try:
        actual_caddyfile = (root / CADDYFILE_PATH).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise LanSetupError("The generated Caddyfile is missing or unreadable.") from exc
    if actual_caddyfile != expected_caddyfile:
        raise LanSetupError("The generated Caddyfile was changed after LAN setup.")


def _validate_client_kit(root: Path, state: Mapping[str, Any], root_der: bytes) -> None:
    kit = root / CLIENT_KIT_DIRECTORY
    if not kit.is_dir():
        raise LanSetupError("The LAN browser connection kit is missing.")
    actual_files = {
        path.relative_to(kit).as_posix()
        for path in kit.rglob("*")
        if path.is_file()
    }
    actual_directories = [path for path in kit.rglob("*") if path.is_dir()]
    if actual_directories or actual_files != CLIENT_KIT_EXPECTED_FILES:
        raise LanSetupError("The LAN browser connection kit has missing or unexpected files.")

    connection = _load_json_object(kit / "CONNECTION.json", "The LAN connection document")
    if connection != _connection_document(state):
        raise LanSetupError("The LAN connection document does not match server state.")
    if (kit / CLIENT_CERTIFICATE_NAME).read_bytes() != root_der:
        raise LanSetupError(
            "The public LAN certificate does not match the server's private CA state."
        )

    manifest_entries: dict[str, str] = {}
    try:
        manifest_lines = (kit / "SHA256SUMS").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise LanSetupError("The LAN client-kit checksum manifest is invalid.") from exc
    for line in manifest_lines:
        match = re.fullmatch(r"([0-9a-f]{64}) \*([^\\/]+)", line)
        if not match or match.group(2) in manifest_entries:
            raise LanSetupError("The LAN client-kit checksum manifest is malformed.")
        manifest_entries[match.group(2)] = match.group(1)
    expected_manifest_files = CLIENT_KIT_EXPECTED_FILES - {"SHA256SUMS"}
    if set(manifest_entries) != expected_manifest_files:
        raise LanSetupError("The LAN client-kit checksum manifest is incomplete.")
    for filename, expected_hash in manifest_entries.items():
        if sha256_file(kit / filename) != expected_hash:
            raise LanSetupError(f"The LAN client-kit file failed validation: {filename}")


def check_ready(
    root: Path,
    *,
    current_server_ipv4: str | None = None,
    current_prefix_length: int | None = None,
    _allow_reconfiguration: bool = False,
) -> dict[str, Any]:
    root = _canonical_root(root)
    if not _allow_reconfiguration:
        _assert_no_pending_reconfiguration(root)
    _assert_normal_local_tree(root, PRIVATE_DIRECTORY, "Private LAN state")
    _assert_normal_local_tree(root, CLIENT_KIT_DIRECTORY, "The LAN browser connection kit")
    state = _load_json_object(root / STATE_PATH, "The LAN server state")
    _validate_ready_or_prepared_state(root, state)
    if state.get("status") != "ready":
        raise LanSetupError("LAN setup is not complete. Run LAN_SERVER_FIRST_RUN.cmd.")

    if current_server_ipv4 is not None or current_prefix_length is not None:
        if current_server_ipv4 is None or current_prefix_length is None:
            raise LanSetupError("Both the current LAN address and prefix length are required.")
        current_address, current_network = parse_private_server_network(
            current_server_ipv4, current_prefix_length
        )
        if (
            str(current_address) != state["serverIpv4"]
            or str(current_network) != state["networkCidr"]
        ):
            raise LanSetupError(
                "This laptop's active private address changed after LAN setup. Do not bypass "
                "the check; restore its router reservation or perform an explicit reconfiguration."
            )

    certificate_path = _root_file(root, CADDY_ROOT_CERTIFICATE_PATH)
    key_path = _root_file(root, CADDY_ROOT_KEY_PATH)
    if not key_path.is_file() or key_path.stat().st_size < 32:
        raise LanSetupError("Caddy's private LAN root key is missing.")
    certificate, der = _certificate_details(certificate_path)
    _validate_private_key_matches_certificate(key_path, certificate)
    if _sha256_bytes(der) != state.get("rootCertificateSha256"):
        raise LanSetupError("Caddy's private LAN root certificate changed unexpectedly.")
    if certificate.fingerprint(hashes.SHA1()).hex().upper() != state.get(
        "rootCertificateThumbprintSha1"
    ):
        raise LanSetupError("Caddy's LAN root certificate thumbprint changed unexpectedly.")
    if certificate.subject.rfc4514_string() != state.get("rootCertificateSubject"):
        raise LanSetupError("Caddy's LAN root certificate subject changed unexpectedly.")
    _validate_client_kit(root, state, der)
    return dict(state)


def _assert_no_pending_reconfiguration(root: Path) -> None:
    """Ordinary LAN startup never consumes a partially published replacement."""
    _assert_normal_local_tree(root, PRIVATE_DIRECTORY, "Private LAN state")
    journal_path = root / RECONFIGURATION_JOURNAL
    if not journal_path.exists():
        return
    try:
        with journal_path.open("rb") as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError("Oversized LAN reconfiguration receipt")
        journal = json.loads(raw)
    except (OSError, ValueError, UnicodeError) as exc:
        raise LanSetupError("The LAN recovery receipt is invalid; preserve its recovery set.") from exc
    if (not isinstance(journal, dict) or type(journal.get("schemaVersion")) is not int or journal.get("schemaVersion") != 1
            or not re.fullmatch(r"[0-9a-f]{32}", str(journal.get("id", "")))
            or journal.get("phase") not in {"committed", "rolled_back"}):
        raise LanSetupError(
            "LAN address recovery is incomplete. Keep its recovery files and reopen "
            "Refresh LAN connection to finish or restore the preceding configuration. "
            "Local-only mode remains available."
        )


def caddy_environment(root: Path) -> dict[str, str]:
    root = _canonical_root(root)
    return {
        "XDG_DATA_HOME": str(root / CADDY_DATA_DIRECTORY),
        "XDG_CONFIG_HOME": str(root / CADDY_CONFIG_DIRECTORY),
    }


def _command_prepare(arguments: argparse.Namespace) -> dict[str, Any]:
    return prepare(
        arguments.root,
        arguments.server_ip,
        arguments.prefix_length,
        arguments.backend_port,
        arguments.threads,
    )


def _command_finalize(arguments: argparse.Namespace) -> dict[str, Any]:
    return finalize(arguments.root)


def _command_check(arguments: argparse.Namespace) -> dict[str, Any]:
    return check_ready(
        arguments.root,
        current_server_ipv4=arguments.server_ip,
        current_prefix_length=arguments.prefix_length,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare or check the CSD LAN server.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--root", required=True, type=Path)
    prepare_parser.add_argument("--server-ip", required=True)
    prepare_parser.add_argument("--prefix-length", required=True, type=int)
    prepare_parser.add_argument("--backend-port", required=True, type=int)
    prepare_parser.add_argument("--threads", type=int, default=DEFAULT_WORKER_THREADS)
    prepare_parser.set_defaults(handler=_command_prepare)

    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--root", required=True, type=Path)
    finalize_parser.set_defaults(handler=_command_finalize)

    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--root", required=True, type=Path)
    check_parser.add_argument("--server-ip", required=True)
    check_parser.add_argument("--prefix-length", required=True, type=int)
    check_parser.set_defaults(handler=_command_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        result = arguments.handler(arguments)
    except LanSetupError as exc:
        print(f"LAN SETUP FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
