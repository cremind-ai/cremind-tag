"""The native setup window: a small tkinter view with no business logic (docs/connect-setup.md §8.1 step 3).

It shows what the person approves — the Cremind server, the profile, this
computer, what is being asked — the four-word verification phrase Cremind shows
too, and the attached gateways (a sole usable one preselected; unusable ones
listed, greyed, with the reason). The controller (``setup_flow``) drives it:

- every ``set_*``/``show_*``/``close`` call is thread-safe: it is queued and run
  on the Tk thread (polled with ``after``), so a network thread may call it;
- ``on_approve(callback(gateway_id))`` and ``on_cancel(callback())`` run on the
  Tk thread; after Approve both buttons stay disabled until the controller
  offers gateways again (``set_gateways``) or finishes (``show_done`` /
  ``show_error``);
- :meth:`SetupWindow.run` runs the Tk main loop (call it on the main thread) and
  returns when the window closes.

Wording is plain and calm: no COM ports or paths as identities (a gateway is
"Gateway …A1B2", the last four characters of its device id), no protocol
names. Keyboard: Tab/Shift-Tab move, Space selects, Enter approves, Escape
cancels, Alt+A / Alt+C.

``python -m cremind_tag.connect.window --demo`` shows it with sample data.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import queue
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

TITLE = "Cremind Connect"
OPERATION_TEXT = {
    "connect_gateway": "Connect a gateway to Cremind",
    "recover": "Move a gateway to this computer",
    "probe": "Check the gateways on this computer",
}
POLL_MS = 50


@dataclass(frozen=True)
class GatewayChoice:
    """One attached gateway as the person sees it."""

    id: str
    """The controller's id for it (e.g. the device id); never shown."""
    label: str
    """``Gateway …A1B2``."""
    detail: str = ""
    """A calm extra line: "Ready to connect", "Firmware 0.2.1"."""
    usable: bool = True
    reason: str = ""
    """Why it cannot be chosen: "Needs a firmware update", "Belongs to another Cremind"."""


def short_id(device_id: str) -> str:
    """``…A1B2``: the last four characters of a device id, upper case."""
    text = "".join(ch for ch in str(device_id) if ch.isalnum())
    return "…" + text[-4:].upper() if text else "…"


def gateway_label(device_id: str) -> str:
    return f"Gateway {short_id(device_id)}"


class ChoiceModel:
    """Which gateway is selected (pure logic, testable without a display)."""

    def __init__(self) -> None:
        self.choices: tuple[GatewayChoice, ...] = ()
        self.selected: str | None = None

    def set_choices(self, choices: Sequence[GatewayChoice]) -> None:
        """Keep a selection that is still usable; else preselect the only usable choice."""
        self.choices = tuple(choices)
        usable = [c.id for c in self.choices if c.usable]
        if self.selected not in usable:
            self.selected = usable[0] if len(usable) == 1 else None

    def select(self, choice_id: str) -> bool:
        if any(c.id == choice_id and c.usable for c in self.choices):
            self.selected = choice_id
            return True
        return False

    @property
    def usable_count(self) -> int:
        return sum(1 for c in self.choices if c.usable)


class SetupWindow:
    """The view (see the module docstring)."""

    def __init__(self, *, title: str = TITLE) -> None:
        import tkinter as tk
        from tkinter import font as tkfont
        from tkinter import ttk

        _dpi_aware()
        self._tk = tk
        self._ttk = ttk
        self.root = tk.Tk()
        self.root.title(title)
        self.root.minsize(460, 360)
        self.root.protocol("WM_DELETE_WINDOW", self._cancel_clicked)
        self._queue: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._approve_cb: Callable[[str], None] | None = None
        self._cancel_cb: Callable[[], None] | None = None
        self._model = ChoiceModel()
        self._busy = False
        self._finished = False
        self._selected = tk.StringVar(value="")

        base = tkfont.nametofont("TkDefaultFont")
        size = max(10, int(base.cget("size")) if int(base.cget("size")) > 0 else 10)
        self._heading_font = tkfont.Font(family=base.cget("family"), size=size + 4, weight="bold")
        self._phrase_font = tkfont.Font(family=base.cget("family"), size=size + 6, weight="bold")
        self._small_font = tkfont.Font(family=base.cget("family"), size=max(9, size - 1))

        self.frame = ttk.Frame(self.root, padding=20)
        self.frame.pack(fill="both", expand=True)
        self._heading = ttk.Label(self.frame, text="Connect a gateway to Cremind", font=self._heading_font)
        self._heading.pack(anchor="w")
        self._info = ttk.Frame(self.frame)
        self._info.pack(fill="x", pady=(12, 0))
        self._info_values: dict[str, Any] = {}
        for row, (key, caption) in enumerate((("server", "Cremind"), ("profile", "Profile"),
                                              ("computer", "This computer"))):
            ttk.Label(self._info, text=caption + ":").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=1)
            value = ttk.Label(self._info, text="…")
            value.grid(row=row, column=1, sticky="w", pady=1)
            self._info_values[key] = value
        ttk.Separator(self.frame).pack(fill="x", pady=14)
        self._phrase_caption = ttk.Label(self.frame, text="Check that Cremind shows the same words:")
        self._phrase_caption.pack(anchor="w")
        self._phrase = ttk.Label(self.frame, text="…", font=self._phrase_font)
        self._phrase.pack(anchor="w", pady=(4, 14))
        ttk.Label(self.frame, text="Gateway:").pack(anchor="w")
        self._list = ttk.Frame(self.frame)
        self._list.pack(fill="x", pady=(4, 0))
        self._progress = ttk.Label(self.frame, text="", wraplength=420, justify="left")
        self._progress.pack(anchor="w", pady=(12, 0), fill="x")
        _wrap_to_width(self._progress)
        buttons = ttk.Frame(self.frame)
        buttons.pack(side="bottom", fill="x", pady=(16, 0))
        self._approve = ttk.Button(buttons, text="Approve", underline=0, command=self._approve_clicked,
                                   default="active")
        self._cancel = ttk.Button(buttons, text="Cancel", underline=0, command=self._cancel_clicked)
        self._approve.pack(side="right")
        self._cancel.pack(side="right", padx=(0, 8))
        self.root.bind("<Return>", lambda _e: self._approve_clicked())
        self.root.bind("<Escape>", lambda _e: self._cancel_clicked())
        self.root.bind("<Alt-a>", lambda _e: self._approve_clicked())
        self.root.bind("<Alt-c>", lambda _e: self._cancel_clicked())
        self._render_choices()
        self.root.after(POLL_MS, self._drain)
        self.root.after(0, self._bring_to_front)

    # -- thread-safe API ----------------------------------------------------------------

    def set_info(self, server_origin: str, profile_name: str, computer_name: str, operation: str) -> None:
        self._post(lambda: self._set_info(server_origin, profile_name, computer_name, operation))

    def set_phrase(self, words: Sequence[str]) -> None:
        text = "  ".join(str(w) for w in words)
        self._post(lambda: self._phrase.configure(text=text or "…"))

    def set_gateways(self, choices: Sequence[GatewayChoice]) -> None:
        items = tuple(choices)
        self._post(lambda: self._set_gateways(items))

    def set_progress(self, text: str) -> None:
        self._post(lambda: self._progress.configure(text=text))

    def show_error(self, title: str, message: str) -> None:
        self._post(lambda: self._finish(title, message, error=True))

    def show_done(self, message: str) -> None:
        self._post(lambda: self._finish("All set", message, error=False))

    def close(self) -> None:
        self._post(self._destroy)

    def on_approve(self, callback: Callable[[str], None]) -> None:
        self._approve_cb = callback

    def on_cancel(self, callback: Callable[[], None]) -> None:
        self._cancel_cb = callback

    def set_url_handler(self, callback: Callable[[str], None]) -> None:
        """macOS: a ``cremind-connect:`` link opened while this window runs arrives here (Apple Event)."""
        if sys.platform == "darwin":
            self._post(lambda: self.root.createcommand("::tk::mac::LaunchURL", callback))

    def run(self) -> None:
        self.root.mainloop()

    # -- Tk thread ------------------------------------------------------------------------

    def _post(self, action: Callable[[], None]) -> None:
        self._queue.put(action)

    def _drain(self) -> None:
        while True:
            try:
                action = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                action()
            except self._tk.TclError:
                return  # the window is gone
            except Exception:
                log.exception("window: update failed")
        with contextlib.suppress(self._tk.TclError):
            self.root.after(POLL_MS, self._drain)

    def _set_info(self, server: str, profile: str, computer: str, operation: str) -> None:
        self._heading.configure(text=OPERATION_TEXT.get(operation, "Approve a request from Cremind"))
        for key, value in (("server", server), ("profile", profile), ("computer", computer)):
            self._info_values[key].configure(text=value or "…")

    def _set_gateways(self, choices: tuple[GatewayChoice, ...]) -> None:
        self._model.set_choices(choices)
        self._busy = False
        self._render_choices()

    def _render_choices(self) -> None:
        ttk = self._ttk
        for child in self._list.winfo_children():
            child.destroy()
        self._selected.set(self._model.selected or "")
        if not self._model.choices:
            hint = ttk.Label(self._list, text="Plug in your gateway. It will appear here in a few seconds.",
                             wraplength=420, justify="left")
            hint.pack(anchor="w", fill="x")
            _wrap_to_width(hint)
        first: Any = None
        for choice in self._model.choices:
            row = ttk.Frame(self._list)
            row.pack(fill="x", pady=2)
            button = ttk.Radiobutton(row, text=choice.label, value=choice.id, variable=self._selected,
                                     command=lambda c=choice.id: self._choose(c))
            button.pack(anchor="w")
            note = choice.detail if choice.usable else choice.reason or "Cannot be used"
            if note:
                extra = {} if choice.usable else {"foreground": "gray40"}
                ttk.Label(row, text=note, font=self._small_font, **extra).pack(anchor="w", padx=(24, 0))
            if not choice.usable:
                button.state(["disabled"])
            elif first is None:
                first = button
        self._update_buttons()
        if first is not None and not self._busy:
            first.focus_set()

    def _choose(self, choice_id: str) -> None:
        self._model.select(choice_id)
        self._update_buttons()

    def _update_buttons(self) -> None:
        can_approve = self._model.selected is not None and not self._busy and not self._finished
        self._approve.state(["!disabled"] if can_approve else ["disabled"])
        self._cancel.state(["!disabled"] if not self._finished else ["disabled"])

    def _approve_clicked(self) -> None:
        if self._finished or self._busy or self._model.selected is None:
            return
        self._busy = True
        self._update_buttons()
        if self._approve_cb is not None:
            self._approve_cb(self._model.selected)

    def _cancel_clicked(self) -> None:
        if self._finished:
            self._destroy()
            return
        if self._cancel_cb is None:
            self._destroy()
            return
        self._busy = True
        self._update_buttons()
        self._cancel.state(["disabled"])
        self._progress.configure(text="Cancelling…")
        self._cancel_cb()

    def _finish(self, title: str, message: str, *, error: bool) -> None:
        ttk = self._ttk
        self._finished = True
        for child in self.frame.winfo_children():
            child.destroy()
        ttk.Label(self.frame, text=title, font=self._heading_font).pack(anchor="w")
        body = ttk.Label(self.frame, text=message, wraplength=420, justify="left")
        body.pack(anchor="w", pady=(12, 0), fill="x")
        _wrap_to_width(body)
        close = ttk.Button(self.frame, text="Close", command=self._destroy, default="active")
        close.pack(side="bottom", anchor="e", pady=(16, 0))
        close.focus_set()
        self.root.bind("<Return>", lambda _e: self._destroy())
        self.root.bind("<Escape>", lambda _e: self._destroy())
        if error:
            self.root.bell()
        self._bring_to_front()

    def _destroy(self) -> None:
        with contextlib.suppress(self._tk.TclError):
            self.root.destroy()

    def _bring_to_front(self) -> None:
        """Raise the window above the browser that opened the link (best effort on every OS)."""
        with contextlib.suppress(self._tk.TclError):
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(400, lambda: self.root.attributes("-topmost", False))
            self.root.focus_force()
        if sys.platform == "win32":
            with contextlib.suppress(Exception):
                import ctypes

                ctypes.windll.user32.SetForegroundWindow(int(self.root.wm_frame(), 16))


def _wrap_to_width(label: Any) -> None:
    """Wrap a label's text at its current width (it is packed to fill the window's width)."""
    label.bind("<Configure>", lambda event: label.configure(wraplength=max(200, event.width - 4)))


def _dpi_aware() -> None:
    """Crisp text on scaled Windows displays (before the first window exists)."""
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)


def show_message(title: str, message: str) -> None:
    """A stand-alone message window (errors outside a setup, e.g. an invalid link); silent without a display."""
    try:
        window = SetupWindow()
    except Exception as exc:  # no display, no Tk
        log.warning("window: cannot show %r: %s", title, exc)
        return
    window.show_error(title, message)
    window.run()


def _demo() -> None:
    import threading
    import time

    window = SetupWindow()
    window.set_info("https://cremind.example.org", "Anna", "Anna's laptop", "connect_gateway")
    window.set_phrase(["amber", "river", "candle", "orbit"])
    window.set_progress("Looking for gateways…")

    def later() -> None:
        time.sleep(1.0)
        window.set_gateways([
            GatewayChoice("0f1e2d3c4b5a69788796a5b4c3d2a1b2", gateway_label("0f1e2d3c4b5a69788796a5b4c3d2a1b2"),
                          "Ready to connect"),
            GatewayChoice("aa55aa55aa55aa55aa55aa55aa55c0de", gateway_label("aa55aa55aa55aa55aa55aa55aa55c0de"),
                          usable=False, reason="Needs a firmware update before it can be used"),
        ])
        window.set_progress("Approve here, then confirm the same words in Cremind.")

    def approved(gateway_id: str) -> None:
        def work() -> None:
            for step in ("Waiting for Cremind to confirm…", "Setting up the gateway…"):
                window.set_progress(step)
                time.sleep(1.2)
            window.show_done(f"{gateway_label(gateway_id)} is connected. You can close this window; "
                             "Cremind Connect keeps running in the background.")

        threading.Thread(target=work, daemon=True).start()

    window.on_approve(approved)
    window.on_cancel(window.close)
    threading.Thread(target=later, daemon=True).start()
    window.run()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cremind_tag.connect.window")
    parser.add_argument("--demo", action="store_true", help="show the window with sample data")
    args = parser.parse_args(argv)
    if not args.demo:
        parser.print_help()
        return 2
    _demo()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["OPERATION_TEXT", "ChoiceModel", "GatewayChoice", "SetupWindow", "gateway_label", "short_id",
           "show_message"]
