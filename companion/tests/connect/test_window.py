"""The setup window: selection logic without a display, and one Tk round trip when Tk can open a window."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from cremind_tag.connect.window import ChoiceModel, GatewayChoice, SetupWindow, gateway_label, short_id

USABLE = GatewayChoice("0f1e2d3c4b5a69788796a5b4c3d2a1b2", gateway_label("0f1e2d3c4b5a69788796a5b4c3d2a1b2"),
                       "Ready to connect")
OLD = GatewayChoice("aa55aa55aa55aa55aa55aa55aa55c0de", gateway_label("aa55aa55aa55aa55aa55aa55aa55c0de"),
                    usable=False, reason="Needs a firmware update")


def test_short_ids_never_show_ports_or_whole_ids() -> None:
    assert short_id("0f1e2d3c4b5a69788796a5b4c3d2a1b2") == "…A1B2"
    assert gateway_label("0f1e2d3c4b5a69788796a5b4c3d2a1b2") == "Gateway …A1B2"
    assert short_id("") == "…"


def test_a_sole_usable_gateway_is_preselected() -> None:
    model = ChoiceModel()
    model.set_choices([OLD, USABLE])
    assert model.selected == USABLE.id and model.usable_count == 1
    assert not model.select(OLD.id) and model.selected == USABLE.id


def test_several_usable_gateways_need_a_choice() -> None:
    other = GatewayChoice("11" * 16, gateway_label("11" * 16))
    model = ChoiceModel()
    model.set_choices([USABLE, other])
    assert model.selected is None
    assert model.select(other.id) and model.selected == other.id
    model.set_choices([USABLE, other, OLD])  # a new list keeps a selection that is still usable
    assert model.selected == other.id
    model.set_choices([USABLE])  # the chosen one was unplugged: the sole remaining one is preselected
    assert model.selected == USABLE.id
    model.set_choices([])
    assert model.selected is None


@pytest.fixture
def window(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(SetupWindow, "_bring_to_front", lambda self: None)
    try:
        view = SetupWindow()
    except Exception as exc:  # no display (headless CI) or no Tk
        pytest.skip(f"Tk cannot open a window here: {exc}")
    view.root.withdraw()
    yield view
    view.close()
    view.root.update()


def pump(view: SetupWindow) -> None:
    view._drain()
    view.root.update()


def test_thread_safe_updates_and_approve(window: SetupWindow) -> None:
    approved: list[str] = []
    window.on_approve(approved.append)
    worker = threading.Thread(target=lambda: (
        window.set_info("https://cremind.example.org", "Anna", "Anna's laptop", "connect_gateway"),
        window.set_phrase(["amber", "river", "candle", "orbit"]),
        window.set_gateways([USABLE, OLD]),
        window.set_progress("Approve here, then confirm in Cremind.")))
    worker.start()
    worker.join()
    pump(window)
    assert window._heading.cget("text") == "Connect a gateway to Cremind"
    assert window._phrase.cget("text") == "amber  river  candle  orbit"
    assert window._info_values["server"].cget("text") == "https://cremind.example.org"
    assert window._selected.get() == USABLE.id and window._approve.instate(["!disabled"])
    window._approve_clicked()
    assert approved == [USABLE.id] and window._approve.instate(["disabled"])
    window._approve_clicked()
    assert approved == [USABLE.id]  # no double approval
    window.show_done("Gateway …A1B2 is connected.")
    pump(window)
    assert window._finished


def test_cancel_goes_to_the_controller(window: SetupWindow) -> None:
    cancelled: list[bool] = []
    window.on_cancel(lambda: cancelled.append(True))
    window.set_gateways([])
    pump(window)
    assert window._approve.instate(["disabled"])
    window._cancel_clicked()
    assert cancelled == [True] and window._progress.cget("text") == "Cancelling…"
