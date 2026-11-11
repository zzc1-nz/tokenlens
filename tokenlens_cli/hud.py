"""The ai-token-hud style live view.

Structure
---------
``KeyReader``
    Non-blocking single character reader: ``msvcrt.getwch`` on Windows, ``termios`` +
    ``tty`` (raw mode) elsewhere, and a no-op stub when stdin is not a TTY so the HUD is
    importable in CI.
``run_hud``
    The refresh loop: fetch a frame, render it, poll the keyboard, sleep the remainder of
    ``--interval``. It survives the server being down by swapping to the ``OFFLINE``
    banner and retrying on the next tick — a monitoring tool that exits when the
    monitored service hiccups is useless.
``render_once``
    The non-TTY / ``--no-color`` path: print exactly one snapshot and return. This is what
    makes the HUD usable from a pipe, a cron job or a CI step.

Key bindings: ``p`` project, ``a`` agent, ``m`` model, ``b`` budget, ``q`` quit, plus
``r`` to force an immediate refresh and ``?`` to toggle the help line.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Callable, TextIO

from . import data, render
from .api import ApiError, TokenLensClient

#: Returned by :meth:`KeyReader.read_key` when no key is pending.
NO_KEY = ""
QUIT_KEYS = frozenset({"q", "Q", "\x03", "\x1b"})  # q, ctrl-c, ESC

INTERVAL_MIN = 0.2
INTERVAL_MAX = 60.0


class KeyReader:
    """Non-blocking keyboard reader with a graceful non-TTY fallback.

    Parameters
    ----------
    stream:
        Input stream; defaults to ``sys.stdin``. When it is not a TTY (pytest, pipes)
        :meth:`available` is ``False`` and :meth:`read_key` always returns ``""``, so
        callers need no special casing.
    """

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream if stream is not None else sys.stdin
        self._pending: list[str] = []
        self._closed = False
        self._saved_mode: Any = None
        self._impl = "none"
        self._init_impl()

    # -- backend selection ----------------------------------------------------------
    def _init_impl(self) -> None:
        if not self._isatty():
            return
        if os.name == "nt":  # pragma: no cover - exercised on Windows consoles only
            try:
                import msvcrt  # noqa: F401

                self._impl = "msvcrt"
                return
            except ImportError:
                pass
        try:  # pragma: no cover - POSIX consoles
            import termios
            import tty

            self._impl = "termios"
            self._termios = termios
            self._tty = tty
            return
        except ImportError:
            self._impl = "none"

    def _isatty(self) -> bool:
        try:
            return bool(self.stream.isatty())
        except (AttributeError, ValueError):
            return False

    @property
    def available(self) -> bool:
        """True when this reader can actually deliver keystrokes."""
        return self._impl != "none" and not self._closed

    @property
    def backend(self) -> str:
        """Name of the active backend (``msvcrt``/``termios``/``none``) — for tests."""
        return self._impl

    # -- lifecycle ------------------------------------------------------------------
    def __enter__(self) -> "KeyReader":
        self._enter_raw_mode()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def _enter_raw_mode(self) -> None:  # pragma: no cover - terminal specific
        if self._impl != "termios":
            return
        try:
            fd = self.stream.fileno()
            self._saved_mode = self._termios.tcgetattr(fd)
            self._tty.setcbreak(fd)
        except (OSError, ValueError):
            self._saved_mode = None

    def close(self) -> None:  # pragma: no cover - terminal specific
        """Restore the terminal mode. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        if self._impl == "termios" and self._saved_mode is not None:
            try:
                self._termios.tcsetattr(
                    self.stream.fileno(), self._termios.TCSADRAIN, self._saved_mode
                )
            except (OSError, ValueError):
                pass
            self._saved_mode = None

    # -- reading --------------------------------------------------------------------
    def read_key(self) -> str:
        """Return one pending character or ``""`` — never blocks."""
        if self._pending:
            return self._pending.pop(0)
        if not self.available:
            return NO_KEY
        if self._impl == "msvcrt":  # pragma: no cover - Windows console only
            try:
                import msvcrt

                while msvcrt.kbhit():
                    char = msvcrt.getwch()
                    if char in ("\x00", "\xe0"):  # arrow/function key prefix
                        if msvcrt.kbhit():
                            msvcrt.getwch()
                        continue
                    self._pending.append(char)
            except (ImportError, OSError):
                return NO_KEY
        elif self._impl == "termios":  # pragma: no cover - POSIX console only
            try:
                import select

                while select.select([self.stream], [], [], 0)[0]:
                    char = self.stream.read(1)
                    if not char:
                        break
                    self._pending.append(char)
            except (OSError, ValueError):
                return NO_KEY
        return self._pending.pop(0) if self._pending else NO_KEY

    def read_keys(self, limit: int = 8) -> list[str]:
        """Drain up to ``limit`` pending keys (handles key repeat without lag)."""
        keys: list[str] = []
        for _ in range(max(0, limit)):
            key = self.read_key()
            if not key:
                break
            keys.append(key)
        return keys

    def push(self, *keys: str) -> None:
        """Queue keys programmatically (used by the interactive test harness)."""
        self._pending.extend(keys)


def clamp_interval(interval: float | None) -> float:
    """Clamp ``--interval`` into a sane range so a typo cannot freeze the HUD."""
    try:
        value = float(interval if interval is not None else 1.0)
    except (TypeError, ValueError):
        return 1.0
    if value != value:  # NaN
        return 1.0
    return max(INTERVAL_MIN, min(INTERVAL_MAX, value))


def make_console(*, no_color: bool = False, stream: Any = None, width: int | None = None) -> Any:
    """Build a ``rich`` console configured for the current output target.

    ``rich`` handles colour negotiation, but a legacy Windows code page (GBK/CP437) still
    cannot encode ``█``/``░``. When the stream declares such an encoding we tell ``rich``
    to fall back to its own ASCII-safe rendering, so the live view never dies with a
    ``UnicodeEncodeError`` mid-frame.
    """
    from rich.console import Console

    target = stream if stream is not None else sys.stdout
    encoding = (getattr(target, "encoding", None) or "utf-8").lower()
    utf8_ok = "utf" in encoding
    kwargs: dict[str, Any] = {
        "no_color": bool(no_color),
        "highlight": False,
        "legacy_windows": not utf8_ok,
        "soft_wrap": False,
    }
    if stream is not None:
        kwargs["file"] = stream
    if width is not None:
        kwargs["width"] = int(width)
    if not utf8_ok:
        # Force the ASCII box/bar rendering path for legacy code pages.
        kwargs["safe_box"] = True
        kwargs["emoji"] = False
    return Console(**kwargs)


def render_once(
    client: TokenLensClient,
    *,
    dimension: str = "project",
    project: str | None = None,
    as_json: bool = False,
    no_color: bool = False,
    stream: Any = None,
    width: int | None = None,
) -> int:
    """Print exactly one HUD frame and return an exit code (CI friendly).

    Returns ``0`` on success, ``1`` when the server is unreachable (the OFFLINE frame is
    still printed, so the caller sees *why*).
    """
    import json as _json

    out = stream if stream is not None else sys.stdout
    snapshot: render.HudSnapshot
    try:
        snapshot = data.build_snapshot(
            client, dimension=dimension, project=project, bar_width=_bar_width(width)
        )
    except ApiError as exc:
        snapshot = data.offline_snapshot(client.server, exc.friendly(client.server), dimension=dimension, project=project)
        if as_json:
            render.write_text(out, _json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2) + "\n")
        else:
            render.write_text(out, render.render_offline_plain(client.server, str(snapshot.error)) + "\n")
        return 1

    if as_json:
        render.write_text(out, _json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2) + "\n")
        return 0

    # A single snapshot is plain text even on a terminal: no need to pay for the rich
    # layout, and it keeps CI output byte-identical to --no-color.
    render.write_text(out, render.render_plain(snapshot, width=width or render.DEFAULT_WIDTH) + "\n")
    return 0


def _bar_width(width: int | None) -> int:
    if not width:
        return render.DEFAULT_BAR_WIDTH
    return max(12, min(40, int(width) - 52))


def run_hud(
    client: TokenLensClient,
    *,
    dimension: str = "project",
    project: str | None = None,
    interval: float = 1.0,
    no_color: bool = False,
    iterations: int | None = None,
    key_reader: KeyReader | None = None,
    console: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Run the live refresh loop.

    Parameters
    ----------
    iterations:
        Stop after N frames (tests use ``1``; ``None`` means "until quit").
    key_reader:
        Injectable key source. When omitted a real :class:`KeyReader` is created and only
        used if stdin is a TTY.
    console:
        Injectable ``rich`` console (tests pass a ``StringIO`` backed one).
    sleep / clock:
        Injected for deterministic tests — the loop never sleeps for real under test.

    The loop never propagates :class:`ApiError`: an unreachable server swaps the frame to
    the OFFLINE banner and the next tick retries, which is the whole point of a HUD.
    """
    interval = clamp_interval(interval)
    reader = key_reader if key_reader is not None else KeyReader()
    owns_reader = key_reader is None
    rich_console = console or make_console(no_color=no_color)
    current = dimension if dimension in render.DIMENSIONS else "project"
    frames = 0
    exit_code = 0
    last_error: str | None = None

    try:
        if owns_reader:
            reader.__enter__()
        while iterations is None or frames < iterations:
            frame_start = clock()
            try:
                snapshot = data.build_snapshot(client, dimension=current, project=project)
                last_error = None
            except ApiError as exc:
                last_error = exc.friendly(client.server)
                snapshot = data.offline_snapshot(
                    client.server, last_error, dimension=current, project=project
                )
                exit_code = 1
            _paint(rich_console, snapshot)
            frames += 1

            # Keys are drained after painting so a held key cannot starve rendering.
            quit_requested = False
            for key in reader.read_keys():
                pressed = key.lower()
                if pressed in QUIT_KEYS or key in QUIT_KEYS:
                    quit_requested = True
                    break
                target = data.dimension_for_key(pressed)
                if target:
                    current = target
                elif pressed == "r":
                    pass  # implicit: next loop iteration refreshes
            if quit_requested:
                break
            if iterations is not None and frames >= iterations:
                break
            remaining = interval - (clock() - frame_start)
            if remaining > 0:
                sleep(remaining)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        exit_code = 0
    finally:
        if owns_reader:
            reader.close()

    if last_error:
        rich_console.print(f"[red]OFFLINE[/red] {last_error}")
    return exit_code


def _paint(console: Any, snapshot: render.HudSnapshot) -> None:
    """Draw one frame, clearing the screen first when attached to a terminal.

    The renderable is printed through a text buffer first so the whole frame can be
    down-converted to ASCII when the console's code page cannot represent the block
    glyphs. Printing straight to a GBK terminal would raise mid-frame and leave a
    half-drawn HUD behind.
    """
    live_screen = bool(getattr(console, "is_terminal", False))
    if live_screen:
        console.clear()
    console.print(render.render_rich(snapshot))
