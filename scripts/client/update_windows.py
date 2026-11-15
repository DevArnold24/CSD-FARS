"""Windows boundary for the installer-owned updater, never an app import.

The PowerShell parent owns the existing secured lifecycle mutex and contains
this process in a kill-on-close job. These checks detect a missing/reused parent
or released lock; they are not a boundary against the same Windows owner.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys

import psutil


class WindowsUpdateError(RuntimeError):
    pass


def powershell() -> Path:
    if os.name != 'nt':
        raise WindowsUpdateError('The updater requires 64-bit Windows and Python 3.11.')
    # Do not resolve an executable through PATH or a current-directory alias.
    buffer = ctypes.create_unicode_buffer(32768)
    get = ctypes.WinDLL('kernel32', use_last_error=True).GetSystemDirectoryW
    get.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    get.restype = wintypes.UINT
    size = get(buffer, len(buffer))
    if not 0 < size < len(buffer):
        raise WindowsUpdateError('The Windows runtime could not be located.')
    return Path(buffer.value)/'WindowsPowerShell/v1.0/powershell.exe'


def process_created(pid: int) -> int:
    """Use integer FILETIME throughout; .NET/psutil float conversions can differ."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)]*4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise WindowsUpdateError('The update controller is no longer available.')
    try:
        values = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
            raise WindowsUpdateError('The update controller identity could not be checked.')
        return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


class WindowsGuard:
    def __init__(self, root: Path, parent_pid: int, parent_created: int):
        if sys.version_info[:2] != (3, 11) or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise WindowsUpdateError('The updater requires 64-bit Windows and Python 3.11.')
        self.root = Path(os.path.abspath(root))
        self.parent_pid, self.parent_created = parent_pid, parent_created
        self.redirector = None
        self.shell = powershell()
        self._parent()
        access = self._access()
        self.owner_sid, self.mutex_name = access['owner_sid'], access['mutex']
        self.assert_exclusive_access()

    def _parent(self):
        try:
            parent = psutil.Process(self.parent_pid)
            command = parent.cmdline()
            file_index = [value.lower() for value in command].index('-file')
            script = Path(os.path.abspath(command[file_index+1]))
            immediate = os.getppid()
            if immediate != self.parent_pid:
                # Windows venv python.exe is a redirector: the interpreter is
                # its child. Accept only this installation's exact redirector
                # with identical interpreter arguments and the secured parent.
                redirector = psutil.Process(immediate)
                identity = (immediate, redirector.create_time())
                if (Path(redirector.exe()) != self.root/'.venv/Scripts/python.exe'
                        or redirector.ppid() != self.parent_pid
                        or redirector.cmdline()[1:] != sys.orig_argv[1:]
                        or self.redirector not in (None, identity)):
                    raise ValueError('Wrong Python redirector')
                self.redirector = identity
            elif self.redirector is not None:
                raise ValueError('Python redirector changed')
            if (process_created(self.parent_pid) != self.parent_created
                    or Path(parent.exe()) != self.shell
                    or script != self.root/'scripts/client/Update-CSD.ps1'):
                raise ValueError('Wrong controller')
        except (psutil.Error, ValueError, IndexError, OSError):
            raise WindowsUpdateError('Run UPDATE_CSD.cmd from this installation. Its update controller is not available.') from None

    def _access(self):
        environment = os.environ.copy()
        environment['PSModulePath'] = str(self.shell.parent/'Modules')
        try:
            result = subprocess.run([str(self.shell), '-NoLogo', '-NoProfile', '-NonInteractive',
                '-ExecutionPolicy', 'Bypass', '-File', str(self.root/'scripts/windows/Update-Access.ps1'),
                '-Root', str(self.root)], stdin=subprocess.DEVNULL, capture_output=True,
                timeout=45, env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode or len(result.stdout) > 4096:
                raise ValueError('Access check failed')
            access = json.loads(result.stdout.decode('utf-8-sig'))
            if not isinstance(access, dict) or set(access) != {'owner_sid', 'mutex'}:
                raise ValueError('Invalid access reply')
            return access
        except (OSError, ValueError, subprocess.SubprocessError):
            raise WindowsUpdateError('Update storage ownership or access could not be verified. No permissions were changed.') from None

    def assert_private_access(self, path=None):
        if path is not None:
            path = Path(os.path.abspath(path))
            if not any(path == self.root/name or path.is_relative_to(self.root/name)
                       for name in ('.updates', '.update-trust')):
                raise WindowsUpdateError('Recovery must stay in this installation\'s private storage.')
        self.assert_exclusive_access()
        access = self._access()
        if access != {'owner_sid': self.owner_sid, 'mutex': self.mutex_name}:
            raise WindowsUpdateError('The installation owner or lifecycle lock changed.')

    def assert_exclusive_access(self):
        self._parent()
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.OpenMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel.ReleaseMutex.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenMutexW(0x00100001, False, self.mutex_name)  # SYNCHRONIZE | MODIFY_STATE
        if not handle:
            raise WindowsUpdateError('The installation lifecycle lock is unavailable.')
        try:
            status = kernel.WaitForSingleObject(handle, 0)
            # WAIT_ABANDONED also grants ownership; release it before refusing.
            # https://learn.microsoft.com/windows/win32/api/synchapi/nf-synchapi-waitforsingleobject
            if status in (0, 0x80):
                kernel.ReleaseMutex(handle)
            if status != 258:  # Only WAIT_TIMEOUT proves another thread holds it.
                raise WindowsUpdateError('The update controller no longer holds the installation lock.')
        finally:
            kernel.CloseHandle(handle)
        self._parent()
