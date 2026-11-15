# CSD Attendance: a fresh Windows computer

For a short visual walkthrough, open **CLIENT_QUICK_START.html** in a browser.

The **clean Windows Client Bundle ZIP** is the portable installer. Transfer that unchanged ZIP and its checksum to another computer, extract it into a new empty local destination, and let First Run create that computer's own installation. This delivery creates a new database and new keys; it does not require an old version, database, camera configuration or updater.

An **initialized application folder is not a copy-and-run portable app**. Its database, encryption keys, private runtime (`.runtime` and `.venv`), Windows permissions, LAN certificates, and scheduled-task configuration belong to the original installation and Windows owner. Do not copy or rename that folder to deploy another server. To move existing records, arrange a verified backup/restore and key/certificate migration with the system owner.

## Choose what the laptop will do

| You want to… | Use… |
| --- | --- |
| Run the system on this laptop only | The clean Windows Client Bundle and `START_HERE.cmd`, **Use on this computer** |
| Make this laptop the server for its own trusted LAN | The clean Windows Client Bundle and `START_CSD_LAN_SERVER.cmd` |
| View or operate an existing server from another laptop | That server's generated `LAN_CLIENT_KIT`; run `CONNECT_TO_CSD.cmd` |
| Update the existing server or finish an interrupted update | `START_HERE.cmd`, **Update or recover**, on the server computer |

A browser-client kit does not install the application, copy attendance data, or create another admin account. Users sign in with accounts created on the existing server. Each independently initialized server has a separate database; two server installations do not synchronize automatically.

## First setup on a fresh computer

1. Use a supported Windows x64/AMD64 computer with its normal media/camera components, at least 6 GB free, and internet access for first setup. ARM64 and 32-bit Windows are not supported. Windows N/KN may need the official Media Feature Pack installed by your IT administrator.
2. Obtain the complete verified Windows Client Bundle ZIP from the system owner. A repository **Code → Download ZIP** is source code and lacks the packaged runtime tools and dependency wheels.
3. Extract all files to a writable folder on a fixed local NTFS drive, outside OneDrive and other synchronized folders. Use `C:\CSD_Attendance` if the owner has prepared it for you, or a suitable folder under your Windows user directory. Do not run from inside the ZIP, a USB drive, a network share, or a symbolic link/junction.
4. Double-click `START_HERE.cmd`. Choose **Use on this computer** to start privately, or **Share with computers on this network** to set up a shared server. No LAN or administrator approval is needed for local setup. Closing the choice window cancels; pressing Enter does not approve a network or certificate change.
5. On first use, the launcher downloads its reviewed private Python runtime and installs the verified dependencies from the bundled offline wheelhouse. You do not need to install Python, Git, Node.js, Visual Studio, or database software yourself. First setup needs internet; later starts can run without internet.
6. Create this installation's Dean/admin username and strong password in the local setup window. Passwords are hidden by default; use **Show passwords** to check them. A mismatch stays in the window so you can correct it. Fresh setup generates new private encryption keys and creates only that administrator account, with no student, class, camera or attendance records. Interrupted setup is checked on the next launch; follow its recovery message rather than creating another installation.
7. Wait for the sign-in page. Keep the server window open while using the system. On later days, double-click `START_HERE.cmd` again and choose the same mode. Closing the foreground server window stops that foreground server.

Keep the installed folder in its chosen location and use the same Windows installation-owner account. Keep the clean installer ZIP separately for future fresh installations; do not replace it with a ZIP of your running installation.

## Share one server on this laptop's own LAN

Connect the server and browser devices to the same trusted router using Ethernet or that router's Wi-Fi. Double-click `START_CSD_LAN_SERVER.cmd` for guided LAN setup or daily LAN startup without the mode menu. `START_HERE.cmd` remains available for other modes and maintenance. Setup detects this computer's current network; there is no address from another installation to paste or reuse.

On first setup, if more than one routed connection is active, choose the connection shared with the joining computers. The window shows its adapter name, address and Windows profile; **More connections** shows additional choices. Choosing a connection does not change its address. Setup checks that the chosen connection is still present before creating LAN state. Later starts reuse this installation's saved connection and refuse to switch networks silently.

Follow the windows for the server address, certificate trust, and the narrow Windows Firewall rule. If Windows marks a trusted connection Public, choose **Make this connection Private** only on your trusted network; Windows then requests administrator approval. Choose **The address is already reserved** if the router owner has reserved it; otherwise choose **Use the current address**. An unreserved address may change later and require LAN setup to be refreshed. Setup does not log in to the router or reserve an address for you. **Trust this installation** applies only to the displayed private certificate, and **Continue to Windows approval** requests the firewall change. Use **Use on this computer** if LAN setup is not ready.

If you close a setup question, reopen `START_HERE.cmd` in the same folder to continue. Setup verifies the saved progress and reuses a completed administrator account. You may be asked for administrator details again if the first account was not yet saved. Correcting rejected input preserves any staged security keys. Do not delete files or initialize another copy to retry.

After successful LAN setup, give browser devices only this server's generated `LAN_CLIENT_KIT` folder. On each device, run `CONNECT_TO_CSD.cmd` and compare the displayed certificate fingerprint with the server owner's copy. Select **The fingerprints match - connect** only when they match; closing cancels. The kit opens the verified HTTPS server in the browser. If the server is unavailable, use **Check connection again** after checking its power and network connection. An already approved certificate does not need another approval. Use the normal Windows browser opened by the kit and bookmark the sign-in page for later visits while the server is running. Embedded app browsers may use a separate certificate store. Completed server setup also reuses certificate trust and a verified firewall rule, avoiding repeated administrator prompts. Do not bypass browser certificate warnings: trusted HTTPS is also needed for browser webcam enrollment on a laptop.

To remove that connection's trust from this Windows account later, run `REMOVE_CSD_TRUST.cmd` and choose **Remove connection trust** after checking the displayed fingerprint. Closing cancels. Always transfer the complete generated kit together, including its helper files; do not mix files from different kit versions or servers.

An isolated switch-only LAN is also supported when each device already has a unique private IPv4 address in the same subnet. Local sharing does not require a default gateway. A network owner must configure those addresses; setup does not assign IPs, add routes, or change Wi-Fi settings. Select the intended trusted interface explicitly when more than one is available.

The server must remain powered on and awake. For daily operation, ask the network owner to reserve its address on the router. If its trusted adapter or address changes, stop attendance and the server, then open `START_HERE.cmd` and choose **Review a changed server network**. Explicitly select the intended trusted connection. Recovery preserves the installation's database, keys and private certificate authority, stages a matching configuration and browser kit, and checks the exact Windows firewall rule before reporting completion. Windows approvals still happen locally. Give browser laptops the complete newly generated kit after a successful refresh.

An interrupted refresh keeps LAN startup blocked. Reopen the same recovery choice to finish that selected address, or choose **Restore preceding LAN configuration** to restore its recorded files and kit. If the firewall addresses already changed, Windows asks approval to restore that exact rule. Reconnect to the preceding trusted network before using the restored LAN configuration. Local-only mode remains available when its own checks pass. Do not delete the recovery journal, loosen permissions, or mix kit files.

Setup failures show a fixed code and safe next step. The latest owner-private `.support/latest-result.json` receipt is limited to 16 KiB, replaces its preceding receipt, and is never uploaded automatically. It contains no passwords, camera endpoints, database records or raw child output. Share its fixed code/correlation when requested by your technician; do not send `.env`, databases, `.lan-server`, raw logs or partial setup files. If cleanup is unconfirmed, keep the installation closed for technician review before starting another child.

## Choose modes inside the application

After signing in as the Dean/admin, open **Setup Center** in the sidebar. It shows the current launch mode separately from the saved choice for the next interactive launch. Choose **Local only** or **Shared LAN**, then select **Save next-launch choice**. Both modes keep the same installation, database, accounts and camera configuration; do not initialize a second installation to switch modes.

Saving is not an immediate network change. Finish active attendance sessions, stop the current server, and reopen `START_HERE.cmd` **on the server computer**, from the same installed folder. The window shows the saved choice; select the mode button you want. Closing it starts nothing. Missing or invalid preferences show guidance. The advanced console launcher still uses numbered choices and Enter to confirm its displayed default.

For a running, configured LAN server, Setup Center can download its verified **browser connection kit**. Transfer the complete extracted kit to the joining laptop and follow its certificate verification/trust prompts. This export contains only the reviewed public connection files, not the database, private keys, camera passwords or attendance data. An unavailable or modified kit cannot be downloaded. A displayed LAN address or an available kit does not prove another laptop can connect; test that separately.

Windows network, firewall and certificate approvals still happen through the trusted local setup tools, not a browser request. The page cannot join a router, approve Windows prompts on another computer, restart the server, or repair changed IP/certificate bindings automatically. A browser on another laptop must not be mistaken for the server computer.

The saved preference applies only to the interactive launcher. It does **not** change or disable a scheduled LAN startup task. To switch a scheduled server to local-only operation, choose **Manage automatic startup** on the server to **Stop** and **Disable** the existing LAN task first. Setup Center does not replace fresh-install initialization or the on-premises acceptance checks below.

## Optional automatic LAN startup

Automatic startup is **off unless you explicitly enable it**. Choosing local use or shared network use does not create a scheduled task.

After LAN setup is complete, double-click `START_HERE.cmd` and choose **Manage automatic startup**. Choose **Show status** to inspect without changes. The window also offers:

- **Enable:** first stop the foreground server so the installation can be checked. This choice opens a separate Windows administrator window; keep the same installation-owner account. Follow its local credential prompt, then read the result before closing it. Enter the owner's actual Windows password, not the CSD Attendance password or a Windows Hello PIN; never send it in chat. Enable schedules the next Windows boot and does not start the server immediately. Use **Start** for an explicit test.
- **Disable:** prevent future automatic starts. This does not stop a server that is already running.
- **Start:** start the configured scheduled server now.
- **Stop:** stop the scheduled server now. This does not disable future automatic starts.

Close the window to leave the configuration unchanged. Keep only one server launch active for an installation. When a scheduled server is running, use its management controls and the generated browser kit instead of trying to launch a second foreground server. The scheduled task needs AC power to start; keep the server awake. Automatic startup does not make a sleeping or powered-off machine serve requests. After changing the installation owner's Windows password, update the stored credentials for this exact task in Windows Task Scheduler and retest it; selecting Enable again does not refresh an existing task's credentials. **Show status** reports task configuration/state in the server console; verify the HTTPS page separately to confirm the application is reachable.

Technician entry point: `scripts\lan\Manage-CSD-Autostart.ps1 -Action Status`. Other explicit actions are `Enable`, `Disable`, `Start`, and `Stop`. The script derives the installation folder itself; do not copy a scheduled task from another computer.

## Update the existing installation

Use **Update or recover** in `START_HERE.cmd` on the server, using the same Windows installation-owner account and installed folder. This guided updater accepts an approved update ZIP supplied for the installed version. A fresh-install bundle and a repository download are different packages; do not extract either over your working installation.

1. Finish or cancel open attendance sessions in the application. Stop the foreground server, or use **Manage automatic startup → Stop** for the scheduled server. The updater refuses while the server or another setup window holds the installation lock; it does not stop attendance for you.
2. Open `START_HERE.cmd` and choose **Update or recover**. Keep the computer awake and connected to power. The updater checks this installation's update support before showing a package picker.
3. Select **Choose update package** and select the supplied ZIP without extracting it. CSD verifies its publisher, supported installed version and files. If a package is rejected, use **Choose another package** or close the window and ask the system owner for the correct package. A rejected package is not applied.
4. Review the version and select **Back up and apply update**. Closing the selection or review window cancels. Once application begins, keep its progress window open while it creates a matching recovery copy, applies the checked package and verifies the application.
5. Read the result. When the new or restored version passes its application check, reopen `START_HERE.cmd` and choose your usual mode, or use **Manage automatic startup → Start** for the configured scheduled server. Browser-only laptops normally refresh the page; they do not install another server.

An interrupted update shows **Recover this installation** before another package can be selected. Follow that window and keep the recovery files. If recovery cannot verify the saved files, leave the folder unchanged and give the system owner the displayed message. Do not delete the pending record or copy another installation over it.

Accounts, students, face enrollments, attendance and settings remain in the existing installation. The updater keeps its matching local recovery files; these do not replace your off-machine backups.

**Update availability:** this bundle includes its publisher's public verification key. That key verifies update packages; it is not a Windows Authenticode signature or evidence of release approval or target-computer qualification. Future updates must be signed by that publisher and supplied for your installed version; updates are not downloaded automatically. The private signing key stays with the system owner and is never part of your installer or connection kit. Do not add or replace publisher-trust files yourself. Version 1 supports compatible application changes; dependency/runtime, database-schema and network-connection changes require a separately tested upgrade procedure and are refused by this updater.

## Before using real attendance records

A packaged ZIP and a successful login do not establish production readiness or classroom recognition accuracy. First test registration and approval, class schedules and rosters, manual attendance corrections, reports, backup/restore, and the intended roles on a disposable installation. Configure programs, locations, cameras, staff, and students deliberately when the system owner is ready; fresh setup starts with no student, class, camera or attendance records.

Ask the system owner for the separate LAN deployment acceptance checklist and complete its on-premises checks: trusted laptop access, expected concurrent viewers with responsive attendance controls, camera reconnect behavior, lighting/distance/occlusion, and measured recognition results. If automatic startup is enabled, verify a real restart and subsequent browser access on that machine. Measure the supported camera and viewer count on the actual server and school network.

For a failed setup, keep the folder unchanged and give the owner the complete error message without passwords, encryption keys, student data, or camera URLs. Use `CHECK_CSD.cmd` for local installation checks or `CHECK_CSD_LAN_SERVER.cmd` for an initialized LAN installation when directed. Do not disable Windows security, rerun reset/seed tools, or overwrite an existing database to get past an error.
