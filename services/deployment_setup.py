"""Non-secret, declarative preference for the next interactive local launch.

This module never starts a process, inspects private installation state, or
changes the active listener, firewall, certificate trust, or scheduled task.
"""

import json
import os
from pathlib import Path
import stat
from uuid import uuid4


PREFERENCE_FILENAME = ".deployment-preference.json"
MAX_PREFERENCE_BYTES = 1024
MODES = frozenset({"local", "lan"})


class PreferenceError(ValueError):
    """A fixed, operator-safe preference error without filesystem details."""


def _normal(metadata, *, directory=False):
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if getattr(metadata, "st_file_attributes", 0) & reparse:
        return False
    if directory:
        return stat.S_ISDIR(metadata.st_mode)
    return stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1


def _preference_path(root):
    root = Path(os.path.abspath(root))
    try:
        for ancestor in (*reversed(root.parents), root):
            if not _normal(ancestor.lstat(), directory=True):
                raise PreferenceError("The installation folder must not use links or junctions.")
        path = root / PREFERENCE_FILENAME
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return path
        if not _normal(metadata):
            raise PreferenceError("The saved setup choice is not a normal local file.")
        return path
    except OSError as exc:
        raise PreferenceError("The installation folder is unavailable or not writable.") from exc


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def load_preference(root):
    """Return local/lan or None; never treat invalid content as a LAN choice."""
    path = _preference_path(root)
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PreferenceError("The saved setup choice could not be read.") from exc
    try:
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not _normal(metadata) or metadata.st_size > MAX_PREFERENCE_BYTES:
                raise PreferenceError("The saved setup choice is invalid. Choose a mode again.")
            raw = source.read(MAX_PREFERENCE_BYTES + 1)
        if len(raw) > MAX_PREFERENCE_BYTES:
            raise ValueError("too large")
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if (
            not isinstance(data, dict)
            or set(data) != {"version", "mode"}
            or type(data["version"]) is not int
            or data["version"] != 1
            or not isinstance(data["mode"], str)
            or data["mode"] not in MODES
        ):
            raise ValueError("invalid preference")
        return data["mode"]
    except (OSError, UnicodeError, ValueError) as exc:
        raise PreferenceError("The saved setup choice is invalid. Choose a mode again.") from exc


def save_preference(root, mode):
    """Atomically save an allowlisted choice; the owner still confirms locally."""
    if not isinstance(mode, str) or mode not in MODES:
        raise PreferenceError("Choose laptop-only or shared LAN.")
    path = _preference_path(root)
    temporary = path.with_name(f"{PREFERENCE_FILENAME}.incomplete-{uuid4().hex}")
    created = False
    try:
        with temporary.open("xb") as destination:
            created = True
            destination.write((json.dumps({"version": 1, "mode": mode}) + "\n").encode("utf-8"))
            destination.flush()
            os.fsync(destination.fileno())
        # Refuse a link introduced while the new, non-secret file was written.
        _preference_path(root)
        os.replace(temporary, path)
    except OSError as exc:
        raise PreferenceError("The setup choice could not be saved. Keep the installation unchanged and ask its owner for help.") from exc
    finally:
        if created:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
