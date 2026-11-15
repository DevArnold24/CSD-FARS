CSD FARS / CSD Attendance source code
Version: 14 November 2026, final adviser release
Source revision: 4f1d20964ab0ccb693b41d70dfd0fdbfdb58ea0f

This archive contains all 249 application and setup source files shipped
in the matching Windows application, unchanged, plus the dependency lock and
third-party licenses. It contains no accounts or school data.

The About page credits Sir Leo Marcha and includes his supplied portrait without
generation or retouching. The original researcher portraits remain unchanged.

For normal installation, use CSD-FARS-Windows-x64-20261114-r3.zip.
Use Extract All, then open START_HERE.cmd from the extracted folder. The complete
installer includes dependencies and deployment tools; this source-only archive
does not. Installer wrapper source and regression tools are supplied separately.
The application uses Python 3.11 (64-bit), Flask and SQLite. Do not initialize
development data in an existing installation.

Application: app.py, core, routes, services, workers, security, database.
Interface: templates and static. Setup: scripts/client, scripts/lan,
scripts/windows and root launchers. Licenses: THIRD_PARTY_LICENSES and notices.

Source-control history, editor settings, internal notes, test output and private
configuration are excluded. The optional standalone viewer is a separate project.
