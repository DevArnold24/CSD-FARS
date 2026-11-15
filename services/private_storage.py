"""Operation-owned temporary storage under a verified private destination."""
from contextlib import contextmanager
import os
from pathlib import Path
import secrets
import shutil

from scripts.client.windows_storage import StorageError, assert_private_directory, normal_path


DIRECTORY_MODE = 0o777 if os.name == "nt" else 0o700


def ensure_private_directory(directory):
    """Create missing descendants only after checking the existing parent.

    Existing directories are never repaired. Windows children inherit the
    verified owner/SYSTEM/Administrators DACL; POSIX children use mode 0700.
    """
    path = normal_path(directory)
    pending = []
    current = path
    while True:
        try:
            current.lstat()
        except FileNotFoundError:
            if current == current.parent:
                raise StorageError("A private local destination is required.")
            pending.append(current)
            current = current.parent
        else:
            assert_private_directory(current)
            break
    for child in reversed(pending):
        assert_private_directory(child.parent)
        try:
            os.mkdir(child, DIRECTORY_MODE)
        except FileExistsError:
            pass  # A competing creator must satisfy the same independent check.
        assert_private_directory(child)
    return path


def assert_private_output(output_path):
    """Verify a new plaintext output's parent without replacing an existing path."""
    output = normal_path(output_path)
    try:
        output.lstat()
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError("The private output already exists.")
    ensure_private_directory(output.parent)
    return output


@contextmanager
def private_temporary_directory(parent):
    """Never use global TEMP; clean only this call's unchanged operation folder."""
    parent = ensure_private_directory(parent)
    directory = None
    for _ in range(10):
        candidate = parent / (".csd-private-" + secrets.token_hex(16))
        assert_private_directory(parent)
        try:
            os.mkdir(candidate, DIRECTORY_MODE)
        except FileExistsError:
            continue
        directory = candidate
        break
    if directory is None:
        raise StorageError("A unique private temporary directory could not be created.")
    info = directory.lstat()
    identity = (info.st_dev, info.st_ino)
    try:
        assert_private_directory(directory)
        yield str(directory)
    finally:
        # Recheck the exact owned path before recursive cleanup. On uncertainty
        # retain private residue; never sweep or repair another operation's files.
        try:
            current = directory.lstat()
            if (directory.parent != parent or not directory.name.startswith(".csd-private-")
                    or (current.st_dev, current.st_ino) != identity):
                raise StorageError("The private temporary directory changed; its files were retained.")
            assert_private_directory(directory)
            shutil.rmtree(directory)
        except (OSError, StorageError):
            raise StorageError("Private temporary cleanup could not be confirmed. Keep the backup location private and ask your technician to check it.") from None


def use_memory_sqlite_temp(connection):
    """Keep native SQLite sorter/temp databases out of the operating-system TEMP.

    This is connection-local and does not change the database's persistent
    settings. Refuse runtimes that cannot confirm the requested policy.
    """
    options = connection.execute("PRAGMA compile_options").fetchall()
    settings = [row[0] for row in options if isinstance(row[0], str) and row[0].startswith("TEMP_STORE=")]
    if len(settings) != 1 or settings[0] not in {"TEMP_STORE=1", "TEMP_STORE=2", "TEMP_STORE=3"}:
        raise StorageError("SQLite private temporary storage could not be confirmed.")
    connection.execute("PRAGMA temp_store=MEMORY")
    row = connection.execute("PRAGMA temp_store").fetchone()
    if row is None or row[0] != 2:
        raise StorageError("SQLite private temporary storage could not be confirmed.")
