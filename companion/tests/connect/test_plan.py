"""Applying a plan: order, failures, preflight — with a fake runner and a fake registry."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from cremind_tag.connect.plan import Plan, PlanFile, RegistryValue, apply, render_command


class FakeRegistry:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.deleted: list[str] = []

    def set_value(self, key: str, name: str, value: str) -> None:
        self.values[(key, name)] = value

    def delete_tree(self, key: str) -> bool:
        self.deleted.append(key)
        return True


class Recorder:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail = fail or set()

    def __call__(self, argv: Sequence[str]) -> tuple[int, str]:
        self.calls.append(tuple(argv))
        return (1, f"{argv[0]} failed") if argv[0] in self.fail else (0, "")


def test_order_files_registry_best_effort_commands_removals_cleanup(tmp_path: Path) -> None:
    doomed = tmp_path / "old.txt"
    doomed.write_text("x", encoding="utf-8")
    plan = Plan("test", files=(PlanFile(tmp_path / "a" / "unit.txt", "hello\n"),
                               PlanFile(tmp_path / "task.xml", "<x/>", encoding="utf-16")),
                registry=(RegistryValue(r"Software\Classes\x", "", "URL:x"),),
                best_effort=(("pre",),), commands=(("one",), ("two",)), remove_files=(doomed, tmp_path / "missing"),
                remove_registry=(r"Software\Classes\y",), cleanup=(("post",),))
    runner, registry = Recorder(fail={"pre", "post"}), FakeRegistry()
    result = apply(plan, runner=runner, registry=registry)
    assert result.ok and result.error is None
    assert runner.calls == [("pre",), ("one",), ("two",), ("post",)]
    assert result.warnings == ["pre failed", "post failed"]
    assert (tmp_path / "a" / "unit.txt").read_text(encoding="utf-8") == "hello\n"
    assert (tmp_path / "task.xml").read_bytes().startswith(b"\xff\xfe")  # UTF-16 with its BOM
    assert registry.values == {(r"Software\Classes\x", ""): "URL:x"} and registry.deleted == [r"Software\Classes\y"]
    assert not doomed.exists()


def test_a_failing_command_stops_the_plan(tmp_path: Path) -> None:
    runner = Recorder(fail={"two"})
    result = apply(Plan("test", commands=(("one",), ("two",), ("three",)), remove_files=(tmp_path / "x",)),
                   runner=runner)
    assert not result.ok and result.error == "two failed" and runner.calls == [("one",), ("two",)]


def test_unsupported_plans_do_nothing() -> None:
    runner = Recorder()
    result = apply(Plan("bundle", supported=False, reason="no app bundle", commands=(("x",),)), runner=runner)
    assert not result.ok and result.error == "no app bundle" and runner.calls == []


def test_missing_required_tools_are_found_before_anything_is_written(tmp_path: Path) -> None:
    target = tmp_path / "never.txt"
    result = apply(Plan("test", files=(PlanFile(target, "x"),),
                        commands=(("definitely-not-a-real-tool-4711",),)))
    assert not result.ok and "not available" in (result.error or "") and not target.exists()


def test_lines_and_render_command(tmp_path: Path) -> None:
    plan = Plan("schtasks", "task", files=(PlanFile(tmp_path / "t.xml", ""),),
                commands=(("schtasks", "/TN", "Cremind Connect"),))
    lines = plan.lines()
    assert lines[0] == "schtasks: task" and any('"Cremind Connect"' in line for line in lines)
    assert render_command(["a", "b c", ""]) == 'a "b c" ""'
