# Backup and recovery readiness

The software can create encrypted SQLite snapshots, verify them, and restore a
verified database to a **new staging path**. It does not schedule backups, copy
them off the server, retain/delete generations, restore an installed machine,
or replace the active database. Those are operator-managed deployment tasks.

## Implemented and tested

- A SQLite online snapshot is integrity-checked before encryption. A missing
  source fails instead of becoming a new empty database.
- The encrypted v2 archive contains one database and its manifest; no `.env`
  or other private installation files are added. Verification authenticates
  encryption, checks the manifest identity and SHA-256, and checks SQLite.
- Same-second and concurrent backups receive distinct names. Complete bytes
  are published without overwriting existing paths. Failed calls clean up only
  their own staging files. Restore uses the same no-overwrite publication.
- Restore refuses the active database and existing output paths, including a
  destination created by another restore after validation starts.
- Legacy unencrypted ZIP restore requires an explicit option. A bundled `.env`
  is ignored and never installed. If a manifest is present, it is still checked.
- Synthetic tests cover concurrent publication, failure cleanup, destination
  races, malformed backups, encrypted biometric preservation, and login after
  database restoration. These tests do **not** prove complete-machine recovery.

Publication requires a local filesystem supporting atomic hard-link creation,
including the supported Windows NTFS installation. An unsupported destination
fails safely. Do not use a network share as the active SQLite database or as the
publication destination; copy a completed, verified encrypted archive through
the approved off-machine storage procedure. If interruption or permissions leave
an unlisted `.csd-recovery-*.tmp` file, preserve it for a technician to inspect;
do not remove files by wildcard while backup or restore is running.

## The matching recovery set

Preserve these together as one identified recovery set, even if key custody
requires separately controlled storage:

1. A verified encrypted database backup from this installation, with its
   creation time, release version/commit, checksum and verification result.
2. The original matching `.env`: `SECRET_KEY`, `BIOMETRIC_KEY`,
   `CAMERA_SOURCE_KEY`, `BACKUP_ARCHIVE_KEY`, `PURGE_ARCHIVE_KEY` and any other
   installation settings/secrets. The archive key decrypts the outer backup;
   the biometric/camera keys decrypt database fields. Newly generated keys are
   not substitutes. All keys must remain independent.
3. The original matching `.client-state` installation marker file. The
   supported checker binds this file, database and key set. Do not recreate the
   file or borrow it from another installation.
4. For LAN servers, the complete matching `.lan-server` directory, including
   private CA state, certificate identity, address and firewall identity.
5. The exact verified release bundle and the technician's installation/network
   record. The old virtual environment is not portable: a different machine or
   folder needs a rebuilt runtime and a checked recovery/reconfiguration plan.

Keep private material in approved encrypted offline storage with restricted
custody. Never place it in Git, reports, chat, browser-client kits or ordinary
email. Browser clients receive only the public `LAN_CLIENT_KIT`. Losing the
original keys can make an otherwise valid backup unusable. A successful archive
verification alone does not prove installation binding or all field decryption.

## Decisions required before live use

The system owner must record an owner and backup frequency, maximum acceptable
data loss (RPO), maximum recovery time (RTO), and how missed/failed backups will
be noticed. Choose approved encrypted off-machine storage, transfer verification,
retention generations/expiry, storage-capacity monitoring, and key custodians
with a documented handover process. Decide who may restore and who signs off.

Keep multiple independently verified generations and a copy isolated from the
server. Do not delete old archives or matching keys until the owner approves the
retention policy and recovery has been demonstrated. A backup on the server's
own disk does not cover disk loss. This change does not configure any schedule,
remote storage, key escrow or retention deletion.

## Technician-managed recovery rehearsal

Ask the system owner for the release's separate recovery qualification plan
and recorded software-test results. Development tests and their dependencies
are not included in the client bundle. A technician runs synthetic database
restore checks only in a separate isolated development checkout with Python
3.11 (64-bit), disposable keys, and synthetic records. Do not run development
initialization or test commands inside an installed client folder.

Record the exact release, Python version, result, elapsed time and isolated
staging destination. A staged database check must verify preserved attendance,
encrypted fields, login and that the source is unchanged. It does not establish
that a complete installation can be restored on a replacement computer.

Before production acceptance, a technician must separately rehearse a complete
installation recovery using a disposable installation and its matching set on
an isolated replacement machine. Stop the affected system; preserve the original
set; restore the database into a new staging location; verify keys, binding,
schema and representative encrypted fields; rebuild the pinned runtime; then
validate startup, roles, reports, HTTPS trust, LAN reachability and camera
configuration with test devices. Plan any address/certificate/firewall change
explicitly. Never regenerate keys or overwrite the live installation as a
shortcut. Measure recovery time and prove rollback before approving a cutover.

Actual school-machine restoration, off-machine backup delivery, production-size
capacity/restore timing and operator sign-off remain unperformed acceptance
steps. Passing these software tests must not be reported as completing them.
