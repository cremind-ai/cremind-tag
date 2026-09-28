"""Install, upgrade, rollback, convergence and uninstall with fake bundles in a temporary CREMIND_CONNECT_HOME.

The file operations (copies, the ``current`` junction/symlink, pruning) are real;
registration and the service are a fake (:class:`FakeHooks`), so nothing is
registered on this machine.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from cremind_tag.connect import install
from cremind_tag.connect.install import InstallError, Installer, read_bundle, read_record, switch_link
from cremind_tag.connect.paths import ConnectPaths
from cremind_tag.connect.runtime import exe_name


def make_bundle(root: Path, version: str, *, name: str | None = None) -> Path:
    directory = root / (name or f"bundle-{version}")
    (directory / "_internal").mkdir(parents=True)
    (directory / exe_name()).write_bytes(b"fake executable " + version.encode())
    (directory / "_internal" / "library.dat").write_text(version, encoding="utf-8")
    info = {"name": "cremind-connect", "version": version, "exe": exe_name()}
    (directory / "connect.json").write_text(json.dumps(info), encoding="utf-8")
    (directory / "_internal" / "connect.json").write_text(json.dumps(info), encoding="utf-8")
    return directory


def version_at(exe: Path) -> str | None:
    """What a started service would report: the bundle version where ``exe`` points."""
    try:
        return read_bundle(Path(os.path.realpath(exe.parent))).version
    except InstallError:
        return None


class FakeHooks:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.registered: list[str] | None = None
        self.running: str | None = None
        self.broken: set[str] = set()

    def register(self, command: list[str]) -> list[str]:
        self.calls.append("register")
        self.registered = command
        return []

    def unregister(self) -> list[str]:
        self.calls.append("unregister")
        self.registered = None
        return []

    def stop_service(self, timeout: float) -> bool:
        self.calls.append("stop")
        self.running = None
        return True

    def start_service(self, command: list[str]) -> None:
        self.calls.append("start")
        version = version_at(Path(command[0]))
        self.running = None if version in self.broken else version

    def service_version(self) -> str | None:
        return self.running


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def hooks() -> FakeHooks:
    return FakeHooks()


@pytest.fixture
def installer(paths: ConnectPaths, hooks: FakeHooks) -> Installer:
    fake = FakeTime()
    return Installer(paths, hooks, health_timeout=30.0, stop_timeout=1.0, sleep=fake.sleep, clock=fake.clock)


def current_version(paths: ConnectPaths) -> str | None:
    target = install.link_target(paths.current)
    return target.name if target else None


def test_read_bundle(tmp_path: Path) -> None:
    bundle = read_bundle(make_bundle(tmp_path, "0.1.0"))
    assert bundle.version == "0.1.0" and str(bundle.exe) == exe_name() and bundle.app_name is None
    assert bundle.executable.is_file()


@pytest.mark.parametrize("damage", ["no_info", "no_exe", "bad_version", "escaping_exe"])
def test_read_bundle_refuses_broken_bundles(tmp_path: Path, damage: str) -> None:
    directory = make_bundle(tmp_path, "0.1.0")
    info = json.loads((directory / "connect.json").read_text(encoding="utf-8"))
    if damage == "no_info":
        (directory / "connect.json").unlink()
        (directory / "_internal" / "connect.json").unlink()
    elif damage == "no_exe":
        (directory / exe_name()).unlink()
    else:
        info.update({"bad_version": {"version": "latest"}, "escaping_exe": {"exe": "../evil.exe"}}[damage])
        for path in (directory / "connect.json", directory / "_internal" / "connect.json"):
            path.write_text(json.dumps(info), encoding="utf-8")
    with pytest.raises(InstallError):
        read_bundle(directory)


def test_fresh_install(tmp_path: Path, paths: ConnectPaths, hooks: FakeHooks, installer: Installer) -> None:
    result = installer.install(read_bundle(make_bundle(tmp_path, "0.1.0")))
    assert result.ok and result.action == "installed" and result.version == "0.1.0"
    exe = paths.current / exe_name()
    assert result.exe == exe and hooks.registered == [str(exe)] and hooks.running == "0.1.0"
    assert current_version(paths) == "0.1.0"
    assert (paths.versions_dir / "0.1.0" / "_internal" / "library.dat").read_text(encoding="utf-8") == "0.1.0"
    assert os.path.realpath(exe) == os.path.realpath(paths.versions_dir / "0.1.0" / exe_name())
    record = read_record(paths)
    assert record is not None and record["version"] == "0.1.0" and record["layout"] == "managed"
    assert record["exe"] == str(exe)
    assert not [p for p in paths.versions_dir.iterdir() if p.name.startswith(".")]  # no staging left behind


def test_upgrade_keeps_the_last_two_versions(tmp_path: Path, paths: ConnectPaths, hooks: FakeHooks,
                                             installer: Installer) -> None:
    installer.install(read_bundle(make_bundle(tmp_path, "0.1.0")))
    hooks.calls.clear()
    installer.install(read_bundle(make_bundle(tmp_path, "0.2.0")))
    assert hooks.calls == ["stop", "register", "start"]
    assert installer.managed_versions() == ["0.1.0", "0.2.0"] and current_version(paths) == "0.2.0"
    record = read_record(paths)
    assert record is not None and record["previous"] == "0.1.0"
    result = installer.install(read_bundle(make_bundle(tmp_path, "0.3.0")))
    assert result.action == "upgraded" and hooks.running == "0.3.0"
    assert installer.managed_versions() == ["0.2.0", "0.3.0"]
    assert hooks.registered == [str(paths.current / exe_name())]  # the same path for every version


def test_the_service_is_stopped_before_current_moves(tmp_path: Path, paths: ConnectPaths, hooks: FakeHooks,
                                                     installer: Installer) -> None:
    installer.install(read_bundle(make_bundle(tmp_path, "0.1.0")))
    seen: list[str | None] = []
    original = hooks.stop_service

    def stop(timeout: float) -> bool:
        seen.append(current_version(paths))
        return original(timeout)

    hooks.stop_service = stop  # type: ignore[method-assign]
    installer.install(read_bundle(make_bundle(tmp_path, "0.2.0")))
    assert seen[0] == "0.1.0" and current_version(paths) == "0.2.0"


def test_a_failed_upgrade_rolls_back(tmp_path: Path, paths: ConnectPaths, hooks: FakeHooks,
                                     installer: Installer) -> None:
    installer.install(read_bundle(make_bundle(tmp_path, "0.1.0")))
    hooks.broken.add("0.2.0")
    result = installer.install(read_bundle(make_bundle(tmp_path, "0.2.0")))
    assert not result.ok and result.action == "rolled_back" and result.version == "0.1.0"
    assert "0.2.0 did not answer" in (result.error or "")
    assert current_version(paths) == "0.1.0" and hooks.running == "0.1.0"
    record = read_record(paths)
    assert record is not None and record["version"] == "0.1.0"
    hooks.broken.clear()  # a fixed build of the same version installs over the failed copy
    assert installer.install(read_bundle(make_bundle(tmp_path, "0.2.0", name="fixed"))).action == "upgraded"
    assert hooks.running == "0.2.0"


def test_a_failed_registration_rolls_back(tmp_path: Path, paths: ConnectPaths, hooks: FakeHooks,
                                          installer: Installer) -> None:
    installer.install(read_bundle(make_bundle(tmp_path, "0.1.0")))

    def refuse(command: list[str]) -> list[str]:
        raise InstallError("could not register Cremind Connect to start at logon: schtasks said no")

    hooks.register = refuse  # type: ignore[method-assign]
    result = installer.install(read_bundle(make_bundle(tmp_path, "0.2.0")))
    assert result.action == "rolled_back" and "schtasks said no" in (result.error or "")
    assert current_version(paths) == "0.1.0" and hooks.running == "0.1.0"


def test_a_failed_fresh_install_is_undone(tmp_path: Path, paths: ConnectPaths, hooks: FakeHooks,
                                          installer: Installer) -> None:
    hooks.broken.add("0.1.0")
    result = installer.install(read_bundle(make_bundle(tmp_path, "0.1.0")))
    assert result.action == "failed" and not result.ok
    assert hooks.registered is None and "unregister" in hooks.calls
    assert not paths.current.exists() and install.link_target(paths.current) is None


def test_a_newer_installed_copy_wins_over_an_older_bundle(tmp_path: Path, paths: ConnectPaths, hooks: FakeHooks,
                                                          installer: Installer) -> None:
    installer.install(read_bundle(make_bundle(tmp_path, "0.3.0")))
    hooks.running = None  # e.g. after a reboot, before logon
    result = installer.install(read_bundle(make_bundle(tmp_path, "0.2.0")))
    assert result.ok and result.action == "kept_newer" and result.version == "0.3.0"
    assert current_version(paths) == "0.3.0" and installer.managed_versions() == ["0.3.0"]
    assert hooks.running == "0.3.0"  # and it is made to run


def test_reinstalling_the_running_version_copies_nothing(tmp_path: Path, paths: ConnectPaths, hooks: FakeHooks,
                                                         installer: Installer) -> None:
    installer.install(read_bundle(make_bundle(tmp_path, "0.1.0")))
    marker = paths.versions_dir / "0.1.0" / "_internal" / "library.dat"
    marker.write_text("untouched", encoding="utf-8")
    hooks.calls.clear()
    result = installer.install(read_bundle(make_bundle(tmp_path, "0.1.0", name="again")))
    assert result.action == "reinstalled" and "stop" not in hooks.calls
    assert marker.read_text(encoding="utf-8") == "untouched"


def test_register_only_for_a_copy_an_os_installer_unpacked(tmp_path: Path, paths: ConnectPaths, hooks: FakeHooks,
                                                           installer: Installer) -> None:
    installer.install(read_bundle(make_bundle(tmp_path, "0.1.0")))
    placed = paths.versions_dir / "0.2.0"
    shutil.copytree(make_bundle(tmp_path, "0.2.0"), placed)  # what the Windows installer does
    result = installer.register_only(read_bundle(placed))
    assert result.ok and result.action == "registered" and current_version(paths) == "0.2.0"
    assert hooks.registered == [str(paths.current / exe_name())] and hooks.running == "0.2.0"
    record = read_record(paths)
    assert record is not None and record["layout"] == "managed" and record["version"] == "0.2.0"


def test_register_only_external_copy_and_convergence(tmp_path: Path, paths: ConnectPaths, hooks: FakeHooks,
                                                     installer: Installer) -> None:
    external = read_bundle(make_bundle(tmp_path / "opt", "0.5.0"))
    result = installer.register_only(external)
    assert result.action == "registered" and hooks.registered == [str(external.executable)]
    record = read_record(paths)
    assert record is not None and record["layout"] == "external" and record["exe"] == str(external.executable)
    # the desktop app now brings an older bundled copy: the external, newer one stays active
    kept = installer.install(read_bundle(make_bundle(tmp_path, "0.4.0")))
    assert kept.action == "kept_newer" and kept.version == "0.5.0" and hooks.registered == [str(external.executable)]
    assert installer.managed_versions() == []
    # and an older external copy does not displace a newer managed one
    installer.install(read_bundle(make_bundle(tmp_path, "0.6.0")))
    older = installer.register_only(read_bundle(make_bundle(tmp_path / "old", "0.5.5")))
    assert older.action == "kept_newer" and older.version == "0.6.0"


def test_register_only_rolls_back_to_the_previous_registration(tmp_path: Path, paths: ConnectPaths,
                                                               hooks: FakeHooks, installer: Installer) -> None:
    good = read_bundle(make_bundle(tmp_path / "a", "0.5.0"))
    installer.register_only(good)
    hooks.broken.add("0.6.0")
    result = installer.register_only(read_bundle(make_bundle(tmp_path / "b", "0.6.0")))
    assert result.action == "rolled_back" and hooks.registered == [str(good.executable)]
    assert hooks.running == "0.5.0"


def test_uninstall(tmp_path: Path, paths: ConnectPaths, hooks: FakeHooks, installer: Installer) -> None:
    for version in ("0.1.0", "0.2.0"):
        installer.install(read_bundle(make_bundle(tmp_path, version)))
    (paths.workers_dir / "w1").mkdir(parents=True)
    result = installer.uninstall()
    assert result.ok and hooks.registered is None and hooks.running is None
    assert not paths.current.exists() and not paths.app_root.exists()
    assert read_record(paths) is None and (paths.workers_dir / "w1").is_dir()  # data kept by default
    installer.install(read_bundle(make_bundle(tmp_path, "0.3.0")))
    installer.uninstall(keep_data=False)
    assert not paths.data_dir.exists()


def test_switch_link_replaces_links_and_refuses_real_directories(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (b / "marker").write_text("b", encoding="utf-8")
    link = tmp_path / "current"
    switch_link(link, a)
    switch_link(link, b)
    assert (link / "marker").read_text(encoding="utf-8") == "b" and (a.is_dir() and b.is_dir())
    assert install.link_target(link) == Path(os.path.realpath(b))
    real = tmp_path / "real"
    real.mkdir()
    with pytest.raises(InstallError):
        switch_link(real, a)
    with pytest.raises(InstallError):
        switch_link(link, tmp_path / "missing")


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS adopts an .app at ~/Applications; the fake is one-dir")
def test_a_real_directory_at_current_is_adopted_as_a_version(tmp_path: Path, paths: ConnectPaths, hooks: FakeHooks,
                                                            installer: Installer) -> None:
    shutil.copytree(make_bundle(tmp_path, "0.1.0"), paths.current)  # e.g. copied there by hand
    result = installer.install(read_bundle(make_bundle(tmp_path, "0.2.0")))
    assert result.action == "upgraded" and current_version(paths) == "0.2.0"
    assert install.link_target(paths.current) is not None  # a link now
    assert installer.managed_versions() == ["0.1.0", "0.2.0"]


@pytest.mark.skipif(sys.platform != "win32", reason="junctions")
def test_windows_current_is_a_junction(tmp_path: Path, paths: ConnectPaths, installer: Installer) -> None:
    installer.install(read_bundle(make_bundle(tmp_path, "0.1.0")))
    assert paths.current.is_junction()
