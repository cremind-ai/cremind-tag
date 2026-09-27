"""Owner-only secret files and the file secret store under concurrent processes."""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest

from cremind_tag.private_files import IS_WINDOWS, InterProcessLock, access_problem, restrict_to_owner
from cremind_tag.secrets import FileBackend, SecretStore

pytestmark = pytest.mark.timeout(120)


def _open_to_users(path: Path) -> None:
    """Make ``path`` readable by other local users (what a folder under C:\\ passes on)."""
    if IS_WINDOWS:
        subprocess.run(["icacls", str(path), "/grant", "*S-1-5-32-545:(R)"], check=True, capture_output=True)
    else:
        path.chmod(0o644)


def test_restrict_to_owner(tmp_path: Path) -> None:
    path = tmp_path / "secret.bin"
    path.write_bytes(b"x")
    _open_to_users(path)
    assert access_problem(path) is not None
    restrict_to_owner(path)
    assert access_problem(path) is None
    assert path.read_bytes() == b"x"  # still ours


def test_the_secrets_file_is_owner_only_even_in_an_open_folder(tmp_path: Path) -> None:
    folder = tmp_path / "shared"
    folder.mkdir()
    if IS_WINDOWS:  # new files in this folder inherit "Users: read"
        subprocess.run(["icacls", str(folder), "/grant", "*S-1-5-32-545:(OI)(CI)(R)"], check=True,
                       capture_output=True)
    store = SecretStore(FileBackend(folder / "secrets.json"))
    store.set_tag_secret(1, bytes(32))
    assert access_problem(folder / "secrets.json") is None
    assert "owner-only ACL" in store.describe() if IS_WINDOWS else "0600" in store.describe()
    # A file an older version left readable is fixed when the store opens.
    _open_to_users(folder / "secrets.json")
    assert SecretStore.open(folder, "file").backend.path == folder / "secrets.json"  # type: ignore[attr-defined]
    assert access_problem(folder / "secrets.json") is None


def test_uicr_image_is_owner_only(tmp_path: Path) -> None:
    from cremind_tag.enroll.enroll import write_enrollment_hex
    from cremind_tag.protocol.ids import Board

    folder = tmp_path / "enroll"
    folder.mkdir()
    if IS_WINDOWS:
        subprocess.run(["icacls", str(folder), "/grant", "*S-1-5-32-545:(OI)(CI)(R)"], check=True,
                       capture_output=True)
    path = write_enrollment_hex(folder / "uicr.hex", bytes(48), Board.LAOWU_BW_NRF51822)
    assert access_problem(path) is None


def test_two_backends_on_one_file_lose_nothing(tmp_path: Path) -> None:
    """Two instances (as two processes would have) writing at the same time: every key survives."""
    path = tmp_path / "secrets.json"
    a, b = FileBackend(path), FileBackend(path)

    def writer(backend: FileBackend, prefix: str) -> None:
        for i in range(25):
            backend.set(f"{prefix}{i}", "v")

    threads = [threading.Thread(target=writer, args=(a, "a")), threading.Thread(target=writer, args=(b, "b"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    keys = set(FileBackend(path)._load())
    assert keys == {f"a{i}" for i in range(25)} | {f"b{i}" for i in range(25)}
    assert not list(tmp_path.glob("*.tmp"))


def test_concurrent_processes_lose_nothing(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    script = ("import sys\nfrom pathlib import Path\nfrom cremind_tag.secrets import FileBackend\n"
              "b = FileBackend(Path(sys.argv[1]))\n"
              "for i in range(15):\n    b.set(f'{sys.argv[2]}{i}', 'v')\n")
    procs = [subprocess.Popen([sys.executable, "-c", script, str(path), prefix]) for prefix in "pqr"]
    for proc in procs:
        assert proc.wait(90) == 0
    keys = set(FileBackend(path)._load())
    assert keys == {f"{p}{i}" for p in "pqr" for i in range(15)}


def test_interprocess_lock_times_out(tmp_path: Path) -> None:
    lock = InterProcessLock(tmp_path / "x.lock", timeout=0.2)
    other = InterProcessLock(tmp_path / "x.lock", timeout=0.2)
    with lock.held(), pytest.raises(TimeoutError), other.held():
        pass
    with other.held():
        pass
