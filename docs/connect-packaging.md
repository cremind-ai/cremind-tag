# Cremind Connect: packaging, installers and releases

Cremind Connect is the per-OS-user background service that runs the companion
for people who never touch Git, Python or `uv` (design:
[connect-setup.md §11](connect-setup.md#11-cremind-connect)). It ships as a
**PyInstaller one-directory bundle** of the companion, wrapped in an installer
per OS. This document covers how it is built, what each installer does, how
copies are installed, upgraded, rolled back and removed, signing, and the
release steps.

Code: `companion/src/cremind_tag/connect/` (the program),
`companion/src/cremind_tag/resources.py` (resources of a frozen build),
`companion/packaging/` (spec, build script, installer sources),
`.github/workflows/connect.yml` (CI). Tests: `companion/tests/connect/`.

## 1. The program

One executable, several roles (`cremind_tag.connect.main`):

| Command | Who runs it | Does |
|---|---|---|
| `cremind-connect service` | the OS at logon (§5) | the supervisor: single instance, IPC, USB watcher, workers |
| `cremind-connect worker --dir D --port P` | the service | one worker (`cremind_tag.connect.worker.run_worker`) |
| `cremind-connect open <link>` | the browser (URL handler) | validates the `cremind-connect://setup?…` link, starts the service if needed, asks it to open the window |
| `cremind-connect window` | the service | the native setup window (`cremind_tag.connect.setup_flow.run_setup`); the link arrives in `$CREMIND_CONNECT_LINK`, never on a long-lived command line |
| `cremind-connect status [--json]` | people, support | this program, the installed copy, the service, workers, ports, registrations (read-only) |
| `cremind-connect install [--from DIR] [--register-only]` | installers, the desktop app | install/upgrade for this user and register (§4) |
| `cremind-connect uninstall [--keep-data \| --purge]` | installers, people | unregister, stop, remove the program (data kept by default) |
| `cremind-connect version [--json]` | CI, support | the version (`connect.json` of the bundle) |

A bare `cremind-connect:` link as the only argument means `open` (macOS hands
links to the app as an Apple Event; PyInstaller's argv emulation turns it into
`argv[1]`). Without arguments a packaged copy registers itself if nobody did
(the macOS `.dmg`), makes sure the service runs and exits.

`worker` and `setup_flow` are imported lazily; a build without them exits with
status 3 and a clear message (the service then backs off, 1 s → 60 s).

The Windows and macOS executables are **windowed**: no console window at logon
or when a link opens. `status`, `version`, `install` attach to the console they
were started from (or write to redirected output, which is how CI smoke-tests
them); with no console at all, output goes to `<data>/logs/<command>-console.log`.

### Directories

| OS | Data (`data_dir`) | Managed program (`app_root`) | Shown as / linked |
|---|---|---|---|
| Windows | `%LOCALAPPDATA%\Cremind\Connect` | `%LOCALAPPDATA%\Programs\Cremind Connect\versions\<v>` | `…\Cremind Connect\current` (junction) |
| macOS | `~/Library/Application Support/Cremind Connect` | `<data>/app/versions/<v>/Cremind Connect.app` | `~/Applications/Cremind Connect.app` (symlink) |
| Linux | `~/.local/share/cremind-connect` | `~/.local/lib/cremind-connect/versions/<v>` | `…/cremind-connect/current` (symlink) |

External copies are registered where an OS package put them:
`/Applications/Cremind Connect.app` (dragged from the `.dmg`) and
`/opt/cremind-connect` (the `.deb`).

`data_dir` holds `installation.json` + `installation.key` (the Ed25519
installation identity, owner-only), `ipc.key` (owner-only), `install.json` (the
active copy), `logs/` (`service.log`, `open.log`, `window.log`, `install.log`,
rotated), `assets/fonts/<pack_id>/` (verified font packs, read-only) and
`workers/<worker_id>/` (`worker.json`, the worker's own files and `logs/`). The
runtime directory (`$XDG_RUNTIME_DIR/cremind-connect` on Linux, else
`<data>/run`; mode `0700`) holds the instance lock and, on POSIX, the IPC
socket. `CREMIND_CONNECT_HOME=<dir>` moves all of it under one directory (tests,
side-by-side development).

### IPC

`multiprocessing.connection` over `\\.\pipe\cremind-connect-<SHA-256(user SID)[:16]>`
(Windows) or `<runtime>/cremind-connect.sock` (elsewhere), authenticated with
the 32-byte `ipc.key` (mutual HMAC challenge, with deadlines). Messages are
JSON objects with an `op` — never pickle. Ops: `ping`, `status`,
`list_gateways`, `open_link`, `add_worker`, `remove_worker`, `stop`
(`cremind_tag.connect.service`). No TCP, no HTTP.

## 2. Building

Prerequisites: [uv](https://docs.astral.sh/uv/) and Python 3.13 (uv installs
it). From `companion/`:

```bash
uv sync --group package                  # the companion + PyInstaller (dependency group "package")
uv run python packaging/build_connect.py --smoke
uv run python packaging/build_connect.py --version 0.2.0 --smoke --installer
```

`build_connect.py` writes `connect.json` (version, executable, platform, arch,
commit, build time), runs PyInstaller with `packaging/cremind-connect.spec`,
copies `connect.json` beside the executable, and with `--smoke` runs the
bundle's `version`, `version --json` and `status --json` with a private
`CREMIND_CONNECT_HOME` (it fails on any surprise). Outputs, under
`companion/dist/connect/` (git-ignored; work files in `companion/dist/.build/`):

| Output | |
|---|---|
| `cremind-connect/` | Windows/Linux bundle: `cremind-connect(.exe)`, `connect.json`, `_internal/` |
| `Cremind Connect.app` | macOS app (`LSUIElement`, `CFBundleURLTypes` → `cremind-connect`) |
| `installers/Cremind-Connect-<v>-windows-x64.exe` | Inno Setup (`--installer`, needs Inno Setup 6) |
| `installers/Cremind-Connect-<v>-macos-<arch>.dmg` | `hdiutil` disk image with an Applications link |
| `installers/cremind-connect_<v>_amd64.deb` | `dpkg-deb` (Debian version: `0.2.0rc1` → `0.2.0~rc1`) |
| `installers/cremind-connect-<v>-linux-x64.tar.gz` | per-user archive with `install.sh` |

The Windows bundle is about 112 MB in ~1100 files (ICU data is a third of it).
`--skip-build --installer` builds installers from an existing bundle — the
order CI needs to sign the program before packaging it (§6).

**Fonts.** Connect never builds fonts on a user's computer
([connect-setup.md §11.7](connect-setup.md#117-fonts)). A release embeds the
published **font asset bundle** — per pack `fonts/<pack_id>/` with
`fontpack.ctfp`, `fontpack.json`, the exact source fonts under `cache/`,
`NOTICE` and `LICENSES/`:

```bash
uv run cremind-tag fonts fetch && uv run cremind-tag fonts build --profile full
uv run python packaging/build_connect.py --fontpack ../fonts/out/full --font-cache ../fonts/cache --smoke
# or an already assembled root:  --assets <root containing fonts/<pack_id>/>
```

The bundle carries it as `assets/`; the service copies each verified pack to
`<data>/assets/fonts/<pack_id>/` (read-only) at start, so workers of every
version share it. `cremind_tag.resources` resolves asset roots in this order:
`$CREMIND_TAG_ASSETS`, the frozen bundle's `assets/`, the installed
`<data>/assets`. A frozen program never looks for a source checkout
(`fonts.manifest.repo_root` refuses to guess one; only `$CREMIND_TAG_REPO`
counts).

## 3. What each installer does

**Windows** (`packaging/windows/cremind-connect.iss`, Inno Setup 6): per user
(`PrivilegesRequired=lowest`, no UAC prompt), installs into
`%LOCALAPPDATA%\Programs\Cremind Connect\versions\<v>` and runs
`cremind-connect.exe install --register-only` from there, which switches the
`current` junction, stops an older service, creates the logon task, registers
the `cremind-connect:` handler under `HKCU\Software\Classes`, starts the
service and checks it answers (rolling back if not). Uninstall runs
`cremind-connect.exe uninstall --keep-data`. The `AppId` GUID must never change.

**macOS** (`.dmg`): the person drags `Cremind Connect.app` to Applications. The
first launch — by double-click or by the first link — registers the
LaunchAgent and asks LaunchServices to index the app (`lsregister -f`); the
`Info.plist` itself declares the URL scheme. `LSUIElement`: no Dock icon. The
app must be signed and notarized, or Gatekeeper blocks it (§6).

**Linux `.deb`**: `/opt/cremind-connect/…`, `/usr/bin/cremind-connect` (link),
`/lib/udev/rules.d/70-cremind-tag.rules` (`uaccess` for the gateway `1209:0002`
and the bridge maintenance port `1209:0001` — pid.codes **test** ids),
`/usr/lib/systemd/user/cremind-connect.service` and
`/usr/share/applications/cremind-connect.desktop`. `postinst` reloads udev,
re-triggers ttys, runs `systemctl --global enable cremind-connect.service` (every
user's session starts it at login) and `update-desktop-database`. `Depends:` the
build machine's glibc (CI builds on Ubuntu 22.04) and `libx11-6`.

**Linux `.tar.gz`**: `install.sh` runs `cremind-connect install --from <dir>`:
a managed per-user copy in `~/.local/lib/cremind-connect`, a systemd *user*
unit, a desktop file, and — with one administrator prompt through `pkexec` —
the udev rule in `/etc/udev/rules.d` (when `pkexec` is missing or refused, the
exact `sudo` command is printed). Remove with
`~/.local/lib/cremind-connect/current/cremind-connect uninstall`.

**Bundled with Cremind desktop**: the desktop app ships the bundle and runs
`cremind-connect install` from it at start (§4); in development it can talk to
the service over the same IPC.

## 4. Install, upgrade, rollback, convergence

`cremind_tag.connect.install`:

1. copy the bundle to `versions/<v>` (a hidden staging directory renamed into place);
2. stop the running service — through launchd/systemd first on macOS/Linux
   (`KeepAlive`/`Restart=always` would restart one that merely exits), then IPC
   `stop`; the instance lock tells when it is gone;
3. switch `current` (POSIX: a new symlink renamed over the old one, atomic;
   Windows: junctions cannot replace each other, so the old one is removed and
   the new one renamed in while the service is stopped);
4. register startup + URL handler (+ the udev rule on Linux) — always pointing at
   `<current>/<exe>`, so switching versions never re-registers anything;
5. start the service and wait up to **30 s** for `ping` to report the new version;
6. on failure: stop it, switch `current` back, start the previous version (a
   failed fresh install is unregistered and removed instead) — `install` then
   exits 1 with `rolled_back`;
7. write `install.json` and delete every version but the new and the previous one.

A real directory where the `current` link belongs (an app copied there by
hand) is adopted as a version first.

**The newer copy wins.** Before installing, `install` compares the bundle with
every copy it can find — `install.json`, the managed `current`, and the external
locations (`/opt/cremind-connect`, `/Applications/Cremind Connect.app`). An
older bundle never replaces a newer copy: `install` reports `kept_newer`, makes
sure the newer one is registered and running, and exits 0. The service itself
steps aside when it is a frozen copy older than the recorded active one, and
the per-user instance lock keeps a second service from ever running. So the
copy bundled with an older Cremind desktop and a newer standalone install (or
the reverse) converge on one service: the newest.

## 5. Startup and links

| OS | Startup (`startup.py`) | Links (`urlhandler.py`) |
|---|---|---|
| Windows | Scheduled Task **Cremind Connect** (`schtasks /Create /XML`): logon trigger for the user's SID, `InteractiveToken`, `LeastPrivilege`, restart on failure every minute ×999, no time limit, `IgnoreNew` | `HKCU\Software\Classes\cremind-connect`: `URL:Cremind Connect`, `URL Protocol`, `shell\open\command` = `"<exe>" open "%1"` |
| macOS | LaunchAgent `~/Library/LaunchAgents/io.cremind.connect.plist` (`RunAtLoad`, `KeepAlive`, `ThrottleInterval` 5, output to `<data>/logs/launchd.log`), `launchctl bootstrap gui/<uid>` | the app's `CFBundleURLTypes`; `lsregister -f` |
| Linux | `~/.config/systemd/user/cremind-connect.service` (`Restart=always`, `RestartSec=2`, `WantedBy=default.target`), `daemon-reload`, `enable --now` | `~/.local/share/applications/cremind-connect.desktop` (`Exec=<exe> open %u`, `MimeType=x-scheme-handler/cremind-connect;`, `NoDisplay=true`), `xdg-mime default`, `update-desktop-database` |

All of it is planned as data (`plan.Plan`) and applied by `plan.apply`; tests
assert the rendered task XML, plist, unit and desktop file and never register
anything on the test machine. On Linux the URL handler forwards the desktop's
display variables (`DISPLAY`, `WAYLAND_DISPLAY`, …) in `open_link`, so a service
started before the graphical session can still open the window.

## 6. Signing

CI signs only when the secrets exist; unsigned builds are fine for testing but
not for people: SmartScreen warns about an unsigned Windows installer, and
Gatekeeper refuses an unnotarized macOS app.

| Secret | Used for |
|---|---|
| `WINDOWS_CERT_PFX_BASE64`, `WINDOWS_CERT_PASSWORD` | `signtool sign /fd sha256 /tr <RFC 3161 timestamp>` of `cremind-connect.exe` (before Inno Setup packs it) and of the installer |
| `APPLE_CERT_P12_BASE64`, `APPLE_CERT_PASSWORD` | a "Developer ID Application" identity in a temporary keychain; `codesign --options runtime --timestamp` of every Mach-O file, then the executable and the app with `packaging/macos/entitlements.plist` (`allow-unsigned-executable-memory`, for ctypes/libffi) |
| `APPLE_ID`, `APPLE_TEAM_ID`, `APPLE_APP_PASSWORD` | `xcrun notarytool submit --wait` of the signed `.dmg`, then `xcrun stapler staple` |

Linux packages are not signed; every build publishes `SHA256SUMS-<target>.txt`.

## 7. CI (`.github/workflows/connect.yml`)

On pushes to `main` and pull requests touching `companion/`, on `connect-v*`
tags and by hand. Matrix: `windows-latest` (x64), `macos-14` (arm64), `macos-13`
(x64 — switch to the Intel successor label if GitHub retired it) and
`ubuntu-22.04` (x64; the oldest glibc the Linux bundle supports). Each job: `uv
sync --group package --locked`, `pytest tests/connect`, build + smoke test,
sign the program, build the installers, sign/notarize them, checksums, upload
the artifact `cremind-connect-<target>`. Branch builds are versioned
`<companion version>.dev<run number>`; a tag `connect-vX.Y.Z` builds version
`X.Y.Z` and creates (or updates) a **draft** GitHub release with every
installer and checksum file.

## 8. Release checklist

1. The Connect version is the tag: `connect-v0.2.0` → `0.2.0` (PEP 440 or
   `0.2.0-rc.1` style pre-releases). The companion's `__version__` is only the
   default for development builds.
2. Configure the signing secrets (§6) — once per repository.
3. Build the font asset bundle the release carries (`fonts build --profile full`;
   the bridges must run the same pack id) and embed it (`--fontpack`/`--assets`);
   CI currently builds without fonts unless the workflow is given them.
4. Push the tag; wait for the four jobs; review the draft release.
5. Test clean installs with no developer tools on Windows x64, macOS arm64 and
   x64, Linux x64 (systemd) — release gate 3 of
   [connect-setup.md §13](connect-setup.md#13-release-gates): install, open a
   setup link from Cremind, reboot/log out and in, check the service is back
   (`cremind-connect status`), upgrade from the previous release, uninstall.
6. Publish the draft, then point Cremind's installer links
   (`GET /api/tags/connect`, [connect-setup.md §9.1](connect-setup.md#91-profile-api-jwt-everything-scoped-to-the-callers-profile))
   and the desktop app's bundled copy at the new version.

## 9. Troubleshooting

| Look at | |
|---|---|
| `cremind-connect status` | service, workers (`running`, `backoff` with the next retry, `waiting_for_gateway`, `conflict`, `disabled`, `invalid`), ports with the device found on each |
| `<data>/logs/service.log` | the supervisor (ports appearing, probe results, worker starts/exits) |
| `<data>/workers/<id>/logs/` | a worker's own logs and its console output |
| Windows | Task Scheduler → "Cremind Connect"; `HKCU\Software\Classes\cremind-connect` |
| macOS | `launchctl print gui/$(id -u)/io.cremind.connect`; `<data>/logs/launchd.log` |
| Linux | `systemctl --user status cremind-connect`; `ls -l /dev/ttyACM*` (an ACL for you means the udev rule works) |

A probe reason `no_access` on Linux means the udev rule is missing; `busy`
means another program holds the port (a terminal, a companion daemon run by
hand); `v1_firmware` means the device runs protocol v1 and needs v2 firmware.
