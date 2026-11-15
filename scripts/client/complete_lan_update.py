"""Complete signed update metadata before the ordinary LAN validation.

The existing PowerShell launcher holds the secured lifecycle mutex and contains
this child in its Windows job. This boundary grants no setup or trust changes.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

if __package__ in (None, ''):
    # The launcher uses isolated Python: import only this installation's source.
    sys.path.insert(0, str(Path(__file__).absolute().parents[2]))

from scripts.client import update_windows as windows


class LanUpdateGuard(windows.WindowsGuard):
    """Retain the updater's owner/lock checks with exact LAN launcher parents."""

    def _parent(self):
        try:
            parent = windows.psutil.Process(self.parent_pid)
            command = parent.cmdline()
            indexes = [i for i, value in enumerate(command) if value.lower() == '-file']
            if len(indexes) != 1:
                raise ValueError('Ambiguous launcher')
            file_index = indexes[0]
            if any(value.lower() in ('-command', '-encodedcommand', '-c', '-ec')
                   for value in command[1:file_index]):
                raise ValueError('Wrong launcher mode')
            script = Path(command[file_index + 1])
            allowed = tuple(self.root / name for name in (
                'scripts/lan/Start-CSD-LAN.ps1',
                'scripts/lan/First-Run-LAN.ps1',
                'scripts/lan/Check-CSD-LAN.ps1',
                # START_HERE runs LAN Start in the same PowerShell host.
                'scripts/client/Start-Here.ps1',
            ))
            if (not script.is_absolute() or script not in allowed
                    or Path(parent.exe()) != self.shell
                    or windows.process_created(self.parent_pid) != self.parent_created):
                raise ValueError('Wrong launcher identity')
            immediate = os.getppid()
            if immediate != self.parent_pid:
                redirector = windows.psutil.Process(immediate)
                identity = (immediate, redirector.create_time())
                if (Path(redirector.exe()) != self.root / '.venv/Scripts/python.exe'
                        or redirector.ppid() != self.parent_pid
                        or redirector.cmdline()[1:] != sys.orig_argv[1:]
                        or self.redirector not in (None, identity)):
                    raise ValueError('Wrong Python redirector')
                self.redirector = identity
            elif self.redirector is not None:
                raise ValueError('Python redirector changed')
        except (windows.psutil.Error, ValueError, IndexError, OSError):
            raise windows.WindowsUpdateError(
                'Reopen the LAN launcher from this installation to finish its update.'
            ) from None


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--parent-pid', type=int, required=True)
    parser.add_argument('--parent-created', type=int, required=True)
    arguments = parser.parse_args(argv)
    try:
        root = Path(os.path.abspath(arguments.root))
        if root != Path(__file__).absolute().parents[2]:
            raise ValueError('Wrong installation')
        from scripts.client import update_lan_compat
        # A fresh setup or unchanged legacy LAN installation needs no updater
        # trust directory. Ordinary LAN validation still runs in the launcher.
        if not update_lan_compat.needs_reconciliation(root):
            return 0
        guard = LanUpdateGuard(root, arguments.parent_pid, arguments.parent_created)
        update_lan_compat.reconcile(root, guard)
        return 0
    except Exception:
        # Never expose saved addresses, private paths, or transaction contents.
        print('The LAN update check could not finish. Keep the installation and '
              'recovery files unchanged, then reopen the usual launcher.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
