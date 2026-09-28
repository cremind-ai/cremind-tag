"""Plans: what registering Cremind Connect with the OS would do, and :func:`apply`, which does it.

:mod:`.startup`, :mod:`.urlhandler` and :mod:`.udev` only *plan* (render files,
list commands and registry values) — pure functions whose output tests assert
without touching the machine. :func:`apply` executes a plan in a fixed order:

1. write ``files`` (parent directories created, ``mode`` applied on POSIX),
2. set ``registry`` values (``HKEY_CURRENT_USER``, Windows),
3. run ``best_effort`` commands (failures are warnings: "not loaded", "not running"),
4. run ``commands`` (the first failure stops the plan),
5. delete ``remove_files`` and the ``remove_registry`` key trees,
6. run ``cleanup`` commands (best effort; e.g. ``systemctl --user daemon-reload``
   once a unit file is gone).

Required commands are checked up front, so a missing tool never leaves a
half-registered service behind. Nothing raises: the result says what failed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

TOOL_TIMEOUT_S = 60

Command = tuple[str, ...]
Runner = Callable[[Sequence[str]], tuple[int, str]]


@dataclass(frozen=True)
class PlanFile:
    """A file the plan writes before it runs anything."""

    path: Path
    content: str
    encoding: str = "utf-8"
    """``schtasks /XML`` wants UTF-16 (``utf-16`` writes the BOM it expects)."""
    mode: int | None = None


@dataclass(frozen=True)
class RegistryValue:
    """A string value under ``HKEY_CURRENT_USER\\<key>`` (``name`` ``""`` is the key's default value)."""

    key: str
    name: str
    value: str


@dataclass(frozen=True)
class Plan:
    """What one registration step does on this OS (see the module docstring for the order)."""

    kind: str
    description: str = ""
    supported: bool = True
    reason: str | None = None
    """Why ``supported`` is false, in words a person can act on."""
    files: tuple[PlanFile, ...] = ()
    registry: tuple[RegistryValue, ...] = ()
    best_effort: tuple[Command, ...] = ()
    commands: tuple[Command, ...] = ()
    remove_files: tuple[Path, ...] = ()
    remove_registry: tuple[str, ...] = ()
    cleanup: tuple[Command, ...] = ()

    def lines(self) -> list[str]:
        """A human-readable account of the plan (``--dry-run`` output, logs)."""
        if not self.supported:
            return [f"{self.kind}: not supported here: {self.reason}"]
        out = [f"{self.kind}: {self.description}" if self.description else self.kind]
        out += [f"  write {f.path}" for f in self.files]
        out += [f"  set HKCU\\{v.key} [{v.name or '(Default)'}] = {v.value}" for v in self.registry]
        out += [f"  run (best effort) {render_command(c)}" for c in self.best_effort]
        out += [f"  run {render_command(c)}" for c in self.commands]
        out += [f"  delete {p}" for p in self.remove_files]
        out += [f"  delete HKCU\\{k}" for k in self.remove_registry]
        out += [f"  run (best effort) {render_command(c)}" for c in self.cleanup]
        return out


@dataclass
class ApplyResult:
    ok: bool
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    ran: list[Command] = field(default_factory=list)


class Registry(Protocol):
    def set_value(self, key: str, name: str, value: str) -> None: ...

    def delete_tree(self, key: str) -> bool: ...


def render_command(argv: Sequence[str]) -> str:
    """argv as a copy-pasteable line."""
    return " ".join(f'"{a}"' if (" " in a or not a) else a for a in argv)


def _no_window_kwargs() -> dict[str, int]:
    if sys.platform == "win32":
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW: no console flashes under a GUI
    return {}


def run_command(argv: Sequence[str]) -> tuple[int, str]:
    """``(returncode, output)``; a tool that cannot run gives 127."""
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=TOOL_TIMEOUT_S,
                              stdin=subprocess.DEVNULL, **_no_window_kwargs())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, f"could not run {argv[0]}: {exc}"
    output = (proc.stdout or "").strip()
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return proc.returncode, f"{render_command(argv)} exited with {proc.returncode}" + (f": {detail}" if detail
                                                                                           else "")
    return 0, output


def tool_available(program: str) -> bool:
    if os.path.isabs(program):
        return os.path.exists(program)
    return shutil.which(program) is not None


class WinRegistry:
    """``HKEY_CURRENT_USER`` through ``winreg`` (Windows only)."""

    def set_value(self, key: str, name: str, value: str) -> None:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as handle:
            winreg.SetValueEx(handle, name, 0, winreg.REG_SZ, value)

    def delete_tree(self, key: str) -> bool:
        import winreg

        def delete(parent: int, sub: str) -> None:
            with winreg.OpenKey(parent, sub, 0, winreg.KEY_READ | winreg.KEY_WRITE) as handle:
                while True:
                    try:
                        child = winreg.EnumKey(handle, 0)
                    except OSError:
                        break
                    delete(handle, child)
            winreg.DeleteKey(parent, sub)

        try:
            delete(winreg.HKEY_CURRENT_USER, key)
        except FileNotFoundError:
            return False
        return True


def read_registry_value(key: str, name: str = "") -> str | None:
    """A string value under ``HKEY_CURRENT_USER`` (``None`` when absent or not on Windows)."""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            value, _kind = winreg.QueryValueEx(handle, name)
    except OSError:
        return None
    return value if isinstance(value, str) else None


def apply(plan: Plan, *, runner: Runner | None = None, registry: Registry | None = None) -> ApplyResult:
    """Execute ``plan`` (see the module docstring); never raises for an ordinary failure."""
    run = runner or run_command
    result = ApplyResult(ok=True)
    if not plan.supported:
        return ApplyResult(ok=False, error=plan.reason or f"{plan.kind} is not supported here")
    if runner is None:
        for argv in plan.commands:
            if not tool_available(argv[0]):
                return ApplyResult(ok=False, error=f"{argv[0]} is not available on this system")

    def best_effort(commands: Sequence[Command]) -> None:
        for argv in commands:
            if runner is None and not tool_available(argv[0]):
                result.warnings.append(f"skipped {argv[0]}: not available")
                continue
            code, detail = run(argv)
            result.ran.append(argv)
            if code != 0:
                result.warnings.append(detail)

    for item in plan.files:
        try:
            item.path.parent.mkdir(parents=True, exist_ok=True)
            item.path.write_text(item.content, encoding=item.encoding, newline="")
            if item.mode is not None and sys.platform != "win32":
                os.chmod(item.path, item.mode)
        except OSError as exc:
            return ApplyResult(False, f"could not write {item.path}: {exc}", result.warnings, result.ran)
    if plan.registry:
        reg = registry or WinRegistry()
        for value in plan.registry:
            try:
                reg.set_value(value.key, value.name, value.value)
            except OSError as exc:
                return ApplyResult(False, f"could not set HKCU\\{value.key}: {exc}", result.warnings, result.ran)
    best_effort(plan.best_effort)
    for argv in plan.commands:
        code, detail = run(argv)
        result.ran.append(argv)
        if code != 0:
            return ApplyResult(False, detail, result.warnings, result.ran)
    for path in plan.remove_files:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            result.warnings.append(f"could not delete {path}: {exc}")
    if plan.remove_registry:
        reg = registry or WinRegistry()
        for key in plan.remove_registry:
            try:
                reg.delete_tree(key)
            except OSError as exc:
                result.warnings.append(f"could not delete HKCU\\{key}: {exc}")
    best_effort(plan.cleanup)
    return result


__all__ = ["ApplyResult", "Command", "Plan", "PlanFile", "Registry", "RegistryValue", "Runner", "WinRegistry",
           "apply", "read_registry_value", "render_command", "run_command", "tool_available"]
