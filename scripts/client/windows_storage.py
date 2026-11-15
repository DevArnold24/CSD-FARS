"""Dependency-free, read-only Windows private-storage verification.

No PowerShell process, policy override, ACL repair or permission grant. The
installer-owned copy is also used by isolated update validation. POSIX checks
here support development backup fixtures, not Windows release qualification.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path, PureWindowsPath
import re
import stat

SYSTEM = "S-1-5-18"
ADMINISTRATORS = "S-1-5-32-544"
TRUSTED_INSTALLER = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
FULL_CONTROL = 0x001F01FF
REPARSE_POINT = 0x400
_PRIVATE_NAMES = re.compile(r"(?:\.env(?:\..*)?|\.client-state(?:\..*)?|\.client-publish-.*|\.client-setup\.log(?:\..*)?|\.client-audit-fallback\.log(?:\..*)?|system\.log(?:\..*)?|audit_fallback\.log(?:\..*)?)", re.I)


class StorageError(RuntimeError):
    """Storage could not be verified; existing permissions are never repaired."""


@dataclass(frozen=True)
class AccessRule:
    kind: int
    flags: int
    rights: int
    sid: str


@dataclass(frozen=True)
class Descriptor:
    owner: str
    rules: tuple[AccessRule, ...]


def check_live_descriptor(descriptor, owner, *, directory):
    allowed = {owner, SYSTEM, ADMINISTRATORS}
    if descriptor.owner not in allowed or (directory and descriptor.owner != owner):
        raise StorageError("Storage has an unexpected owner.")
    if len(descriptor.rules) != 3:
        raise StorageError("Storage requires owner, SYSTEM and Administrators only.")
    expected = 3 if directory else 0  # OBJECT_INHERIT | CONTAINER_INHERIT
    seen = set()
    for rule in descriptor.rules:
        if (rule.kind != 0 or rule.sid not in allowed or rule.sid in seen
                or rule.rights != FULL_CONTROL or rule.flags & ~0x10 != expected):
            raise StorageError("Storage has unexpected access or inheritance rules.")
        seen.add(rule.sid)


def check_ancestor_descriptor(descriptor, owner, *, volume_root=False):
    trusted = {owner, SYSTEM, ADMINISTRATORS, TRUSTED_INSTALLER}
    if descriptor.owner not in trusted:
        raise StorageError("An ancestor has an unexpected owner.")
    unsafe = 0x100C0040 if volume_root else 0x100D0040
    for rule in descriptor.rules:
        if rule.kind not in (0, 1) or rule.flags & ~0x1F:
            raise StorageError("An ancestor has unsupported access rules.")
        if (rule.kind == 0 and rule.sid not in trusted and not rule.flags & 0x08
                and rule.rights & unsafe):
            raise StorageError("An ancestor can be replaced or reconfigured.")


class _WindowsAPI:
    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self.kernel.GetCurrentProcess.argtypes = []
        self.kernel.GetCurrentProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel.LocalFree.argtypes = [ctypes.c_void_p]
        self.kernel.LocalFree.restype = ctypes.c_void_p
        self.kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        self.kernel.GetDriveTypeW.restype = wintypes.UINT
        self.kernel.GetVolumeInformationW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR,
            wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            wintypes.LPWSTR, wintypes.DWORD]
        self.kernel.GetVolumeInformationW.restype = wintypes.BOOL
        self.advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                ctypes.POINTER(wintypes.HANDLE)]
        self.advapi.OpenProcessToken.restype = wintypes.BOOL
        self.advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.advapi.GetTokenInformation.restype = wintypes.BOOL
        self.advapi.GetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.advapi.GetFileSecurityW.restype = wintypes.BOOL
        self.advapi.IsValidSecurityDescriptor.argtypes = [ctypes.c_void_p]
        self.advapi.IsValidSecurityDescriptor.restype = wintypes.BOOL
        self.advapi.GetSecurityDescriptorOwner.argtypes = [ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
        self.advapi.GetSecurityDescriptorOwner.restype = wintypes.BOOL
        self.advapi.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p,
            ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.BOOL)]
        self.advapi.GetSecurityDescriptorDacl.restype = wintypes.BOOL
        self.advapi.IsValidAcl.argtypes = [ctypes.c_void_p]
        self.advapi.IsValidAcl.restype = wintypes.BOOL
        self.advapi.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.POINTER(ctypes.c_void_p)]
        self.advapi.GetAce.restype = wintypes.BOOL
        self.advapi.IsValidSid.argtypes = [ctypes.c_void_p]
        self.advapi.IsValidSid.restype = wintypes.BOOL
        self.advapi.GetLengthSid.argtypes = [ctypes.c_void_p]
        self.advapi.GetLengthSid.restype = wintypes.DWORD
        self.advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                                     ctypes.POINTER(ctypes.c_void_p)]
        self.advapi.ConvertSidToStringSidW.restype = wintypes.BOOL

    def sid(self, pointer):
        if not pointer or not self.advapi.IsValidSid(pointer):
            raise StorageError("Windows returned an invalid security principal.")
        output = ctypes.c_void_p()
        if not self.advapi.ConvertSidToStringSidW(pointer, ctypes.byref(output)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.wstring_at(output)
        finally:
            self.kernel.LocalFree(output)

    def current_owner(self):
        handle = wintypes.HANDLE()
        if not self.advapi.OpenProcessToken(self.kernel.GetCurrentProcess(), 0x0008, ctypes.byref(handle)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            needed = wintypes.DWORD()
            ok = self.advapi.GetTokenInformation(handle, 1, None, 0, ctypes.byref(needed))
            if ok or ctypes.get_last_error() != 122 or not 0 < needed.value <= 65536:
                raise StorageError("The Windows account could not be verified.")
            buffer = ctypes.create_string_buffer(needed.value)
            if not self.advapi.GetTokenInformation(handle, 1, buffer, len(buffer), ctypes.byref(needed)):
                raise ctypes.WinError(ctypes.get_last_error())
            return self.sid(ctypes.c_void_p.from_buffer(buffer).value)
        finally:
            self.kernel.CloseHandle(handle)

    def fixed_ntfs(self, root):
        filesystem = ctypes.create_unicode_buffer(32)
        if self.kernel.GetDriveTypeW(root) != 3:
            raise StorageError("Private storage requires a fixed local drive.")
        if not self.kernel.GetVolumeInformationW(root, None, 0, None, None, None,
                                                filesystem, len(filesystem)):
            raise ctypes.WinError(ctypes.get_last_error())
        if filesystem.value.upper() != "NTFS":
            raise StorageError("Private storage requires NTFS.")

    def descriptor(self, path):
        needed = wintypes.DWORD()
        ok = self.advapi.GetFileSecurityW(str(path), 0x05, None, 0, ctypes.byref(needed))
        if ok or ctypes.get_last_error() != 122 or not 0 < needed.value <= 1024 * 1024:
            raise StorageError("Windows file permissions could not be read.")
        buffer = ctypes.create_string_buffer(needed.value)
        if not self.advapi.GetFileSecurityW(str(path), 0x05, buffer, len(buffer), ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        return self.decode_descriptor(buffer)

    def decode_descriptor(self, buffer):
        if not self.advapi.IsValidSecurityDescriptor(buffer):
            raise StorageError("Windows returned an invalid descriptor.")
        owner, acl = ctypes.c_void_p(), ctypes.c_void_p()
        defaulted, present = wintypes.BOOL(), wintypes.BOOL()
        if not self.advapi.GetSecurityDescriptorOwner(buffer, ctypes.byref(owner), ctypes.byref(defaulted)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not self.advapi.GetSecurityDescriptorDacl(buffer, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not present.value or not acl.value or not self.advapi.IsValidAcl(acl):
            raise StorageError("Storage has an absent or unrestricted DACL.")
        # ACL header: revision/reserved, WORD size, WORD ACE count, reserved.
        header = ctypes.string_at(acl, 8)
        count = int.from_bytes(header[4:6], "little")
        if count > 4096:
            raise StorageError("Storage has too many access rules.")
        rules = []
        for index in range(count):
            ace = ctypes.c_void_p()
            if not self.advapi.GetAce(acl, index, ctypes.byref(ace)):
                raise ctypes.WinError(ctypes.get_last_error())
            header = ctypes.string_at(ace, 4)
            kind, flags, size = header[0], header[1], int.from_bytes(header[2:4], "little")
            if kind not in (0, 1) or size < 16 or flags & ~0x1F:
                raise StorageError("Storage has unsupported access rules.")
            data = ctypes.string_at(ace, size)
            sid_pointer = ace.value + 8
            # Validate the SID's declared bounds before asking Win32 to decode it.
            if data[8] != 1 or 8 + 8 + 4 * data[9] != size:
                raise StorageError("Storage has an invalid principal entry.")
            if not self.advapi.IsValidSid(sid_pointer) or self.advapi.GetLengthSid(sid_pointer) != size - 8:
                raise StorageError("Storage has an invalid principal entry.")
            rules.append(AccessRule(kind, flags, int.from_bytes(data[4:8], "little"), self.sid(sid_pointer)))
        return Descriptor(self.sid(owner), tuple(rules))


@lru_cache(maxsize=1)
def _windows():
    return _WindowsAPI()


def _normal_entry(path, *, directory):
    info = path.lstat()  # Denied access is an error, never absence.
    if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & REPARSE_POINT
            or (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)) is False):
        raise StorageError("Private storage cannot use links, junctions or unexpected file types.")
    return info


def normal_path(path):
    """Reject Windows device/stream aliases before normalization and access."""
    expanded = os.path.expanduser(path)
    if os.name == "nt":
        reserved = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9\u00b9\u00b2\u00b3]|LPT[1-9\u00b9\u00b2\u00b3])(?:\..*)?", re.I)
        def validate_parts(candidate):
            parts = candidate.parts[1:] if candidate.anchor else candidate.parts
            for part in parts:
                if part in {".", ".."}:
                    continue
                if (part.endswith((".", " ")) or reserved.fullmatch(part)
                        or any(ord(char) < 32 or char in '<>:"|?*' for char in part)):
                    raise StorageError("Private storage cannot use device names or alternate data streams.")
        # Win32 abspath can erase final dots/spaces. Check user-supplied lexical
        # components before normalization, then check the actual absolute form.
        validate_parts(PureWindowsPath(expanded))
    path = Path(os.path.abspath(expanded))
    if os.name == "nt":
        if str(path).startswith("\\\\") or len(path.drive) != 2 or path.drive[1] != ":":
            raise StorageError("Private storage requires a normal local path.")
        validate_parts(path)
    return path


def _private_entry(path, owner, *, directory):
    info = _normal_entry(path, directory=directory)
    if os.name == "nt":
        check_live_descriptor(_windows().descriptor(path), owner, directory=directory)
    elif info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise StorageError("Private storage must be owned and accessible only by this account.")


def assert_private_directory(path):
    """Verify an existing private destination and its replacement boundary."""
    path = normal_path(path)
    owner = _windows().current_owner() if os.name == "nt" else os.geteuid()
    if os.name == "nt":
        if str(path).startswith("\\") or not path.drive or len(path.drive) != 2:
            raise StorageError("Private storage requires a normal local path.")
        _windows().fixed_ntfs(path.anchor)
    for ancestor in reversed(path.parents):
        info = _normal_entry(ancestor, directory=True)
        if os.name == "nt":
            check_ancestor_descriptor(_windows().descriptor(ancestor), owner,
                                      volume_root=ancestor.parent == ancestor)
        elif info.st_uid not in (0, owner) or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX):
            raise StorageError("A private storage ancestor is writable by another account.")
    _private_entry(path, owner, directory=True)
    return path


def assert_private_installation(root, maximum_entries=20000):
    """Equivalent to the Windows launcher gate, without launching PowerShell."""
    if os.name != "nt":
        return  # Headless application setup is not Windows qualification.
    root = assert_private_directory(root)
    owner = _windows().current_owner()
    database = root / "database"
    _private_entry(database, owner, directory=True)
    pending = [database]
    count = 0
    with os.scandir(root) as entries:
        for entry in entries:
            path = Path(entry.path)
            if entry.name.lower() == "backups":
                _private_entry(path, owner, directory=True)
                pending.append(path)
            elif _PRIVATE_NAMES.fullmatch(entry.name):
                _private_entry(path, owner, directory=False)
                count += 1
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                count += 1
                if count > maximum_entries:
                    raise StorageError("Private storage has too many entries to verify.")
                info = entry.stat(follow_symlinks=False)
                directory = stat.S_ISDIR(info.st_mode)
                path = Path(entry.path)
                _private_entry(path, owner, directory=directory)
                if directory:
                    pending.append(path)
