# CSD Attendance: New Client Laptop

This is the handoff guide for a Windows 10 or Windows 11 laptop. The client
does not need Git, Python, Visual Studio, Node.js, or database software. The
Windows Client Bundle carries the application dependencies and creates a
private Python runtime plus a new SQLite database on that laptop.

The clean ZIP is the portable installer; an initialized folder is bound to its
installation location and Windows owner. Do not copy a live installation to
another computer. Each fresh server creates its own administrator and private
keys, without sample operational records. See
[PORTABLE_QUICK_START.md](PORTABLE_QUICK_START.md) for the short handoff guide.

## What the client needs

- A Windows 10 or Windows 11 **x64/AMD64** laptop (not ARM64 and not 32-bit)
- An internet connection for the first run
- At least 6 GB of free space
- Their chosen Dean username and a strong password they will remember

The bundle downloads its reviewed private Python runtime into the application
folder. It does not install Python or the Visual C++ runtime system-wide and it
does not require Administrator access in standalone mode. LAN mode requests
Administrator approval for one exact Windows Firewall rule and, only when the
client chooses the guided fix, for changing one unambiguous trusted router
connection from **Public** to **Private**.

The laptop must have the normal Windows media and camera components. Windows
N/KN editions need Microsoft's official Media Feature Pack. If First Run says
those components are unavailable, a technician or the organization's IT
administrator must enable the official optional feature and restart Windows.
Do not download individual DLL files from the web. Microsoft's current guidance
is: [Media Feature Pack for Windows N editions](https://support.microsoft.com/en-us/windows/experience/platform-variants/media-feature-pack-list-for-windows-n-editions).

The first-run password must contain at least eight characters, including an
uppercase letter, lowercase letter, number, and special character.

## What to send the client

Send the client the GitHub Release link for the approved version. Tell them to
download the asset named:

`CSD-Attendance-Windows-x64-<version>.zip`

Do **not** tell them to use GitHub's green **Code > Download ZIP** button. That
source archive does not contain the verified Windows dependency bundle.

This repository is private. A direct Release link works only for a signed-in
GitHub account that has been granted access. Otherwise, the system owner should
download the approved Release asset and its `.sha256` file, verify it, and pass
the unchanged ZIP to the client using an approved transfer method.

## Exact client instructions

1. Download the Windows Client Bundle ZIP from the link we sent you.
2. Open File Explorer and enter `%USERPROFILE%` in the address bar.
3. Create a folder named `CSD-Attendance` there. Keep it outside OneDrive.
4. Right-click the downloaded ZIP, choose **Extract All**, and extract its
   contents into that folder.
5. Open the extracted folder and double-click `START_HERE.cmd`.
6. Choose **Use on this computer** in the launcher window.
   This is the recommended test mode and works whether or not a LAN is present.
7. Leave the window open. On a fresh laptop, preparation starts automatically,
   can take several minutes, and needs internet access while it downloads the
   private Python runtime. Python packages are installed from the verified
   offline bundle.
8. Create the first Dean account in the setup window. Passwords are hidden by
   default; use **Show passwords** to check them. Correct a mismatch in the same
   window. Setup then starts the system and opens the sign-in page.
9. On later days, double-click `START_HERE.cmd` and choose the same mode.
10. Keep the black server window open while using the system. Closing it stops
   the local server. The browser opens automatically at the sign-in page.

`START_HERE.cmd` is the only file a normal client needs. `FIRST_RUN.cmd`,
`START_CSD.cmd`, `CHECK_CSD.cmd`, and the named LAN launchers remain available
as technician and backward-compatible recovery tools.

If setup shows a red error, leave the folder unchanged, take a photo showing
the complete window, and contact the system owner. Do not run files named
`reset`, `seed`, `migrate`, or `update`.

## One server laptop and multiple browser laptops

Use this mode when all users must share one attendance database. Only the new
server laptop receives the full Windows Client Bundle and owns the database.
Every other laptop is a browser client; do not install another server or copy a
database to it.

For a quick LAN test:

1. Connect the server laptop by Ethernet to the approved router/switch.
2. Let `START_HERE.cmd` check whether Windows labels the connection **Private**
   (or a managed **Domain** network). It gives a guided fix when safe, so a
   technician is not required just to perform a temporary test.
3. Before reliable daily use, ask the router owner or technician to reserve the
   displayed server address. The certificate and firewall rule are deliberately
   pinned to that address, but setup can proceed temporarily without a router
   login.
4. Ensure there is no Internet/router port forwarding, DMZ host, UPnP exposure,
   guest-Wi-Fi isolation bypass, or public inbound route to the server.
5. Keep the CCTV network reachable from the server only. Browser laptops do not
   need direct CCTV/RTSP access or camera credentials.

Then, on the server laptop:

1. Follow steps 1-4 in **Exact client instructions** to extract the approved
   Release into its permanent local folder.
2. Double-click `START_HERE.cmd` and choose **Share with computers on this
   network**. If several routed connections are active, select the one shared
   with the joining computers. Check its displayed name, address and profile.
   If Windows calls the trusted connection **Public**, choose **Make this
   connection Private** and approve the Windows prompt, or follow the displayed
   manual Settings steps. Setup creates or validates the private runtime,
   database, keys, and first Dean.
3. Choose **Use the current address** for a temporary test. For reliable daily
   use, have the router owner reserve the displayed address and then choose
   **The address is already reserved**. Setup does not reserve it for you.
4. Compare and record the displayed LAN CA SHA-256 fingerprint. Choose
   **Trust this installation** only for that exact certificate.
5. Choose **Continue to Windows approval** and approve the Windows
   administrator prompt for the narrowly scoped firewall
   rule. Do not approve a rule for another executable, port, or Public profile.
6. The LAN server starts and displays the HTTPS address used by authorized
   laptops. `CHECK_CSD_LAN_SERVER.cmd` remains available for technician checks.
7. Each day, double-click `START_HERE.cmd`, choose **Share with computers on
   this network**, and keep its server window open. Closing a setup choice
   cancels; reopen the same installation to retry without deleting files.

For optional automatic startup, finish LAN setup first, stop the foreground
server, then choose **Manage automatic startup** in `START_HERE.cmd`.
**Show status** is read-only. Enable requires explicit administrator approval
as the same Windows installation owner and that owner's Windows password entered locally (not the
CSD password or a Hello PIN). It schedules the next boot without starting now;
choose Start for an immediate test. Disable prevents future starts but does not
stop an active server; Stop ends the active server but does not disable future
starts. Keep the server awake and on AC power. Test an actual Windows restart
and trusted browser access before relying on unattended operation.

If Windows labels the trusted router connection **Public**, `START_HERE.cmd`
explains why LAN sharing cannot continue and offers a one-click repair when it
can identify one unambiguous routed private connection. Choose **Make this
connection Private** only on a trusted school/home router and approve the Windows administrator prompt. If no
safe automatic candidate can be identified, follow its numbered manual guide:
choose the trusted connection in **Settings > Network & internet > Ethernet or
Wi-Fi > Network profile type**, mark it **Private**, and retry.
**Use on this computer** remains available when its local checks pass.

The server setup creates `LAN_CLIENT_KIT`. Copy only that folder to each
authorized Windows browser laptop using an approved local transfer. On each
Windows user account:

1. Connect to the same approved router/switch network.
2. Open `LAN_CLIENT_KIT\README.txt` and compare its SHA-256 fingerprint with the
   one physically shown or separately confirmed by the server operator.
3. Double-click `CONNECT_TO_CSD.cmd` and choose **The fingerprints match -
   connect** only when they match. Use the normal Windows browser opened by the
   kit, such as Microsoft Edge or Google Chrome. Closing the question cancels.
4. Bookmark the opened `https://<reserved-private-address>/` page. Never bypass
   a browser certificate warning.

The client kit makes the private HTTPS certificate trusted; it does not grant a
role or log anyone in. The server's existing usernames, passwords, and role
checks remain the authorization boundary. The firewall admits the approved
private subnet to the login page. Disable an application account to revoke that
person's access; `REMOVE_CSD_TRUST.cmd` only removes certificate trust for that
Windows user.

More than two browser laptops may connect to the one server. The initial server
configuration has 32 request threads because every open MJPEG camera view holds
one thread. The technician must perform an on-site concurrent-use test for the
expected number of Teachers, Attendance Displays, and administrative users;
the bundle does not claim unlimited clients.

`DISABLE_CSD_LAN_ACCESS.cmd` removes only this installation's exact firewall
rule and leaves the recovery state intact. On a browser laptop that is no
longer authorized, run `REMOVE_CSD_TRUST.cmd` from its original client kit.

## Windows security and managed laptops

Windows may show a security prompt because the ZIP and command files were
downloaded. Continue only when the filename, approved GitHub Release link, and
checksum came from the system owner. If Windows reports an unexpected publisher,
blocked file, malware warning, or organization policy message, stop, leave the
folder unchanged, photograph the complete message, and contact the technician.

Do not disable Microsoft Defender or SmartScreen, change PowerShell execution
policy, unblock files with commands, or ask the client to bypass a warning.
School/company-managed laptops may intentionally block command files,
PowerShell, downloaded executables, optional Windows features, or local servers.
Only that organization's IT administrator can approve or change those policies.

## Where the client's data lives

In standalone mode the laptop creates and keeps its own independent set. In LAN
mode the one server laptop creates and keeps the shared deployment's only set:

- `.env` encryption keys and local settings
- `database\attendance.db` SQLite database
- users, attendance records, biometric templates, and camera settings
- `backups` and local logs

None of those files is downloaded from or uploaded to GitHub. Never copy the
desktop's or another client's `.env`, database, biometrics, logs, or camera
credentials into this folder. LAN browser laptops receive only
`LAN_CLIENT_KIT`, which contains the public LAN CA certificate and no private
application data.

Do not move the application folder after first run; its private environment
contains absolute paths. Do not place it in OneDrive, a shared/network folder,
an external drive, or inside the ZIP file.

## Owner checklist after setup

The system owner or technician should then:

1. Confirm `CHECK_CSD.cmd` reports a healthy local installation.
2. Sign in as the new Dean and create the required staff accounts.
3. Configure cameras locally; never put CCTV credentials in GitHub or chat.
4. Perform the on-premises camera, enrollment, attendance, role, export, and
   recovery acceptance checks.
5. Configure a secure backup routine. Recovery requires both a verified
   encrypted backup and the original matching client security state.

## Recovery is technician-managed

Do not attempt a self-service database-only restore. The attendance database
(or verified encrypted backup), `.env`, and `.client-state` belong to one
matching recovery set. A LAN server recovery set must also preserve its complete
`.lan-server` directory so the private LAN CA, certificate identity, pinned
address, and firewall identity remain matched. Keep the set together in the
approved encrypted offline recovery location. The `.env` and `.lan-server`
contain live private keys; never email, paste into chat, upload, commit, or copy
them to browser laptops. The `.client-state` contains the installation binding
and must not be invented or borrowed from another laptop.

For recovery, stop the system and give the unchanged matching recovery set to
the authorized technician. The technician must verify the backup, key/database
binding, and destination before replacing anything. Copying only a database,
only an `.env`, or another laptop's marker can make encrypted records unreadable
or attach the wrong keys to the wrong data.

Moving the installed folder also invalidates absolute paths in its private
Python environment. Preserve the original folder when possible. If the folder
or laptop must change, the technician should rebuild the private runtime at the
approved destination and validate the complete matching recovery set; the
client should not move files piecemeal.

Updates are technician-managed. Do not overwrite a working client folder with
a newer source ZIP or run `git pull` on the client's behalf without first
creating and verifying a backup and following the approved update procedure.

## Guided updates in publisher-enabled releases

The bundle includes a publisher public verification key for checking supported
update packages. This is not a Windows Authenticode signature and does not prove
that this candidate is an approved release or qualified on the target computer.
Use only the version and matching update package approved by the system owner.
The private signing key must never be included in a client bundle or connection
kit. Do not add or replace publisher-trust files yourself.

When an enabled release and its matching signed update are supplied:

1. Finish attendance, stop the server, and open `START_HERE.cmd` on the server
   computer using the Windows account that installed it. Choose **Update or
   recover**. For a scheduled server, use **Manage automatic startup** to stop it.
2. Choose the supplied update package. CSD checks its publisher and the exact
   installed version before showing **Back up and apply update**.
3. Keep the computer powered on while CSD saves recovery files and checks the
   updated application. Start the server again after the successful result.

If an update is interrupted, open the same updater and choose **Recover this
installation**. Do not delete `.updates` or `.update-trust`, move the folder,
replace individual files, or copy a database over the installation. An
unverifiable recovery set keeps startup blocked and needs technician review.

This first update format preserves dependencies, database schema, installer
recovery tools, and the network connection-kit protocol. An update that changes
those needs a separately validated upgrade procedure. Local recovery files are
kept after an update; they do not replace an encrypted off-machine backup.
