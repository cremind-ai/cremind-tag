# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Cremind Connect: a ONE-DIRECTORY bundle (docs/connect-packaging.md).

Run it through ``packaging/build_connect.py``, which writes ``connect.json`` and
passes these environment variables:

- ``CONNECT_BUILD_INFO`` — path of ``connect.json`` (version, executable, platform);
  it lands in the bundle's resource directory (``_internal/``, ``Contents/Resources``);
- ``CONNECT_ASSETS`` — optional font asset root (``<root>/fonts/<pack_id>/``), embedded
  as ``assets/`` (``cremind_tag.resources`` finds it there).

Windows and macOS executables are windowed (no console window at logon or for
links). macOS gets ``Cremind Connect.app`` (``LSUIElement``: no Dock icon) whose
``Info.plist`` declares the ``cremind-connect:`` URL scheme; argv emulation turns
the link's Apple Event into ``argv[1]``.
"""

import json
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules, copy_metadata

SPEC_DIR = Path(SPECPATH)  # noqa: F821 - provided by PyInstaller
COMPANION = SPEC_DIR.parent
BUILD_INFO = Path(os.environ["CONNECT_BUILD_INFO"])
ASSETS = os.environ.get("CONNECT_ASSETS") or None
INFO = json.loads(BUILD_INFO.read_text(encoding="utf-8"))
VERSION = INFO["version"]
IS_MAC = sys.platform == "darwin"


def optional_metadata(*names):
    out = []
    for name in names:
        try:
            out += copy_metadata(name)
        except Exception:  # noqa: BLE001 - a package this platform does not install
            pass
    return out


hiddenimports = [
    # Every module of the companion: the worker and the setup flow import much of it lazily.
    *collect_submodules("cremind_tag"),
    # serial_for_url("socket://…") imports its protocol handler by name.
    *collect_submodules("serial.urlhandler"),
    "serial.tools.list_ports",
    # keyring finds its backends through entry points (metadata below) and imports them by name.
    *collect_submodules("keyring.backends"),
    *collect_submodules("secretstorage"),
    *collect_submodules("jeepney"),
    "win32ctypes.core",
    # Text layout and rendering (compiled extensions; freetype-py ships its own PyInstaller hook).
    "icu", "uharfbuzz", "freetype", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont",
    "cbor2", "cryptography", "yaml", "httpx", "platformdirs",
    "tkinter", "tkinter.ttk", "tkinter.font",
]

datas = [
    (str(BUILD_INFO), "."),
    *optional_metadata("keyring", "freetype-py", "cremind-tag", "pyserial", "httpx", "cryptography", "cbor2",
                       "uharfbuzz", "pyicu-wheels", "pillow"),
]
if ASSETS:
    datas.append((ASSETS, "assets"))

binaries = [*collect_dynamic_libs("icu")]  # ICU's own DLLs/dylibs beside _icu_

a = Analysis(  # noqa: F821
    [str(SPEC_DIR / "connect_entry.py")],
    pathex=[str(COMPANION / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "_pytest", "pluggy", "iniconfig", "IPython", "numpy", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="cremind-connect",
    console=False,
    argv_emulation=IS_MAC,
    upx=False,
    codesign_identity=None,  # signed afterwards (docs/connect-packaging.md)
    entitlements_file=None,
)

coll = COLLECT(exe, a.binaries, a.datas, name="cremind-connect", upx=False)  # noqa: F821

if IS_MAC:
    import re

    # CFBundle versions are dot-separated integers: 0.2.0rc1 -> 0.2.0 (the full one goes in CremindConnectVersion)
    release = re.match(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", VERSION)
    numeric = ".".join(str(int(g or 0)) for g in release.groups()) if release else "0.0.0"
    app = BUNDLE(  # noqa: F821
        coll,
        name="Cremind Connect.app",
        bundle_identifier="io.cremind.connect",
        version=numeric,
        info_plist={
            "CFBundleName": "Cremind Connect",
            "CFBundleDisplayName": "Cremind Connect",
            "CFBundleShortVersionString": numeric,
            "CFBundleVersion": numeric,
            "CremindConnectVersion": VERSION,
            "CFBundleURLTypes": [{"CFBundleURLName": "io.cremind.connect.setup",
                                  "CFBundleURLSchemes": ["cremind-connect"]}],
            "LSUIElement": True,
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
        },
    )
