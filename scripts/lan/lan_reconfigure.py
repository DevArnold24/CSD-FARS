"""Recover a changed address within one installation without importing old data.

The Windows first-run caller holds the installation lifecycle mutex and verifies
private ACLs. This module only stages/publishes local configuration; it never
changes an adapter, trust store or firewall. An unfinished journal blocks ordinary
LAN startup. Every copied byte is allowlisted and hashed, and retries refuse
unrecognized changes instead of overwriting them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from uuid import uuid4

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).absolute().parents[2]))

from scripts.lan import lan_setup as lan

MAX_FILE_BYTES = 2 * 1024 * 1024
TARGETS = (lan.STATE_PATH.as_posix(), lan.CADDYFILE_PATH.as_posix(), *(
    (lan.CLIENT_KIT_DIRECTORY / name).as_posix()
    for name in sorted(lan.CLIENT_KIT_EXPECTED_FILES)
))


def _binding(root: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()


def _read(path: Path) -> bytes:
    try:
        with path.open("rb") as stream:
            value = stream.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise lan.LanSetupError("A LAN recovery file is unavailable; preserve the recovery set.") from exc
    if len(value) > MAX_FILE_BYTES:
        raise lan.LanSetupError("A LAN recovery file exceeds its size limit.")
    return value


def _json(value: dict) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _paths(root: Path) -> None:
    lan._assert_normal_local_tree(root, lan.PRIVATE_DIRECTORY, "Private LAN state")
    lan._assert_normal_local_tree(root, lan.CLIENT_KIT_DIRECTORY, "LAN connection kit")


def _load(root: Path) -> tuple[dict, Path]:
    _paths(root)
    try:
        journal = json.loads(_read(root / lan.RECONFIGURATION_JOURNAL))
    except (UnicodeError, ValueError) as exc:
        raise lan.LanSetupError("The LAN recovery journal is invalid.") from exc
    if not isinstance(journal, dict):
        raise lan.LanSetupError("The LAN recovery journal is invalid.")
    if (type(journal.get("schemaVersion")) is not int or journal.get("schemaVersion") != 1 or journal.get("rootBinding") != _binding(root)
            or not re.fullmatch(r"[0-9a-f]{32}", str(journal.get("id", "")))
            or journal.get("phase") not in {"prepared", "applying", "published", "committed", "rolling_back", "rolled_back"}
            or not isinstance(journal.get("files"), dict)
            or set(journal["files"]) != set(TARGETS)):
        raise lan.LanSetupError("The LAN recovery journal does not belong to this installation.")
    directory = root / lan.PRIVATE_DIRECTORY / "reconfiguration" / journal["id"]
    for name in TARGETS:
        entry = journal["files"][name]
        if not isinstance(entry, dict) or set(entry) != {"before", "after"}:
            raise lan.LanSetupError("The LAN recovery inventory is invalid.")
        for side in ("before", "after"):
            expected = entry[side]
            if (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)
                    or _hash(_read(directory / side / name)) != expected):
                raise lan.LanSetupError("A LAN recovery file failed its checksum; no state was changed.")
    return journal, directory


def _save(root: Path, journal: dict, phase: str) -> None:
    journal = {**journal, "phase": phase}
    lan._atomic_write_bytes(root / lan.RECONFIGURATION_JOURNAL, _json(journal))


def _kit_payload(root: Path, state: dict, der: bytes) -> dict[str, bytes]:
    files = {name: _read(lan._root_file(root, source))
             for name, source in lan.TRACKED_CLIENT_KIT_FILES.items()}
    files[lan.CLIENT_CERTIFICATE_NAME] = der
    files["CONNECTION.json"] = _json(lan._connection_document(state))
    files["README.txt"] = lan._client_readme(state).encode("utf-8")
    files["SHA256SUMS"] = "".join(
        f"{_hash(value)} *{name}\n" for name, value in sorted(files.items(), key=lambda item: item[0].lower())
    ).encode("ascii")
    return {(lan.CLIENT_KIT_DIRECTORY / name).as_posix(): value for name, value in files.items()}


def stage(root: Path, address: str, prefix: int, backend_port: int, threads: int = lan.DEFAULT_WORKER_THREADS) -> dict:
    root = lan._canonical_root(root)
    _paths(root)
    manifest = lan._validate_bundle(root)
    expected = lan._expected_state(root, manifest, address, prefix, backend_port, threads)
    if (root / lan.RECONFIGURATION_JOURNAL).exists():
        prior, directory = _load(root)
        if prior["phase"] not in {"committed", "rolled_back"}:
            candidate = json.loads(_read(directory / "after" / lan.STATE_PATH))
            if lan._state_identity(candidate) != lan._state_identity(expected):
                raise lan.LanSetupError("Finish or restore the pending LAN address before choosing another configuration.")
            return prior
    previous = lan.check_ready(root)
    if lan._state_identity(previous) == lan._state_identity(expected):
        raise lan.LanSetupError("This LAN configuration is unchanged. Use the ordinary LAN check.")
    _, der = lan._certificate_details(lan._root_file(root, lan.CADDY_ROOT_CERTIFICATE_PATH))
    expected.update({key: previous[key] for key in (
        "rootCertificateSha256", "rootCertificateThumbprintSha1", "rootCertificateSubject")})
    expected.update(status="ready", readyAtUtc=expected["preparedAtUtc"])
    after = _kit_payload(root, expected, der)
    after[lan.STATE_PATH.as_posix()] = _json(expected)
    after[lan.CADDYFILE_PATH.as_posix()] = lan.build_caddyfile(
        expected["serverIpv4"], expected["backendPort"], prefix_length=expected["prefixLength"]
    ).encode("utf-8")
    transaction_id = uuid4().hex
    directory = root / lan.PRIVATE_DIRECTORY / "reconfiguration" / transaction_id
    directory.mkdir(parents=True, exist_ok=False)
    inventory = {}
    for name in TARGETS:
        before = _read(lan._root_file(root, Path(name)))
        for side, value in (("before", before), ("after", after[name])):
            lan._atomic_write_bytes(directory / side / name, value)
        inventory[name] = {"before": _hash(before), "after": _hash(after[name])}
    journal = {"schemaVersion": 1, "id": transaction_id, "rootBinding": _binding(root),
               "phase": "prepared", "files": inventory}
    # Recovery bytes are durable before the guard is published; live files have
    # not changed. Unreferenced staging left by a crash is never adopted/deleted.
    lan._atomic_write_bytes(root / lan.RECONFIGURATION_JOURNAL, _json(journal))
    return journal


def _publish(root: Path, side: str) -> dict:
    root = lan._canonical_root(root)
    journal, directory = _load(root)
    allowed = {"prepared", "applying", "published"} if side == "after" else {"prepared", "applying", "published", "rolling_back"}
    if journal["phase"] not in allowed:
        raise lan.LanSetupError("This LAN recovery action is already complete; use its ordinary readiness check.")
    for name in TARGETS:
        current = _hash(_read(lan._root_file(root, Path(name))))
        if current not in journal["files"][name].values():
            raise lan.LanSetupError("LAN files changed outside recovery. Preserve both sets for technician review.")
    _save(root, journal, "applying" if side == "after" else "rolling_back")
    # State goes last. The pending guard blocks every ordinary LAN check even
    # if a crash exposes a mixture of files before the final state write.
    for name in (*TARGETS[1:], TARGETS[0]):
        lan._atomic_write_bytes(root / name, _read(directory / side / name))
    _save(root, journal, "published" if side == "after" else "rolled_back")
    return {"phase": "published" if side == "after" else "rolled_back", "id": journal["id"]}


def apply(root: Path) -> dict:
    return _publish(root, "after")


def rollback(root: Path) -> dict:
    # Only the configuration/kit are restored. OS trust/firewall never change in
    # this module; ordinary full readiness must still verify their actual state.
    root = lan._canonical_root(root)
    journal, _ = _load(root)
    if journal["phase"] == "rolled_back":
        if any(_hash(_read(lan._root_file(root, Path(name)))) != journal["files"][name]["before"] for name in TARGETS):
            raise lan.LanSetupError("Restored LAN files changed; preserve the recovery set for review.")
        return {"phase": "rolled_back", "id": journal["id"]}
    return _publish(root, "before")


def verify(root: Path, address: str, prefix: int) -> dict:
    root = lan._canonical_root(root)
    journal, directory = _load(root)
    if journal["phase"] != "published":
        raise lan.LanSetupError("Publish the complete pending LAN configuration before verification.")
    for name in TARGETS:
        if _hash(_read(lan._root_file(root, Path(name)))) != journal["files"][name]["after"]:
            raise lan.LanSetupError("Published LAN state differs from the staged configuration.")
    lan.check_ready(root, current_server_ipv4=address, current_prefix_length=prefix, _allow_reconfiguration=True)
    return {"phase": "verified", "id": journal["id"]}


def commit(root: Path, address: str, prefix: int) -> dict:
    # The secured caller checks selected adapter, Caddy config, OS trust and the
    # exact firewall rule before committing. This final local check is repeated.
    verify(root, address, prefix)
    root = lan._canonical_root(root)
    journal, _ = _load(root)
    _save(root, journal, "committed")
    return {"phase": "committed", "id": journal["id"]}


def previous_state(root: Path) -> dict:
    root = lan._canonical_root(root)
    _, directory = _load(root)
    return json.loads(_read(directory / "before" / lan.STATE_PATH))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("stage", "apply", "verify", "commit", "rollback"))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--server-ip")
    parser.add_argument("--prefix-length", type=int)
    parser.add_argument("--backend-port", type=int, default=5000)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command in {"stage", "verify", "commit"}:
            if arguments.server_ip is None or arguments.prefix_length is None:
                raise lan.LanSetupError("The confirmed LAN address and prefix are required.")
            if arguments.command == "stage":
                result = stage(arguments.root, arguments.server_ip, arguments.prefix_length, arguments.backend_port)
            else:
                result = globals()[arguments.command](arguments.root, arguments.server_ip, arguments.prefix_length)
        else:
            result = globals()[arguments.command](arguments.root)
    except (lan.LanSetupError, OSError, ValueError):
        print("LAN address recovery could not finish. Keep its recovery set and use the LAN recovery choices; local-only mode remains available.", file=sys.stderr)
        return 1
    print(json.dumps({key: result[key] for key in ("phase", "id")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
