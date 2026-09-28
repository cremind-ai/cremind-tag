"""Cremind Connect: the per-OS-user background service around the companion (docs/connect-setup.md §11).

``cremind-connect`` is one program with several roles (``service``, ``worker``,
``open``, ``window``, ``status``, ``install``, ``uninstall``, ``version``; see
:mod:`cremind_tag.connect.main`). It ships as a PyInstaller bundle, so end users
never need Git, Python or ``uv`` (docs/connect-packaging.md).

Modules: :mod:`.paths` (directories), :mod:`.runtime` (version, frozen bundle,
starting itself), :mod:`.installation` (the installation key),
:mod:`.instance` (single instance), :mod:`.ipc` (local IPC), :mod:`.links`
(the ``cremind-connect://`` launch link), :mod:`.usb` and :mod:`.probe`
(finding gateways), :mod:`.workerdir` (worker directories), :mod:`.service`
(the supervisor), :mod:`.plan` + :mod:`.startup` + :mod:`.urlhandler` +
:mod:`.udev` (OS registration), :mod:`.install` (install, upgrade, rollback),
:mod:`.window` (the native setup window).

This package imports nothing heavy at import time: the URL handler and
``status`` must start quickly from a cold bundle.
"""
