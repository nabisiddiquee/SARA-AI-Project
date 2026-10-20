"""Full-screen "Matrix" binary rain animation (streams of 0s and 1s).

A black full-screen window fills with columns of green 0/1 characters falling
from top to bottom, brightest at the head and fading into a trail — the classic
digital-rain look. Press Esc or Q (or close the window) to exit; it also stops
on its own after ``max_seconds``.

The column/trail logic lives in :class:`_Rain` so it can be tested without ever
opening a window; :func:`play_matrix_rain` just paints that state with Tkinter.
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Green shades from brightest (head) to darkest (tail of the trail).
_TRAIL_COLORS = (
    "#CCFFCC", "#66FF66", "#33DD33", "#22AA22", "#158015",
    "#0E600E", "#0A480A", "#073507", "#052505", "#031803",
)
_HEAD_COLOR = "#EAFFEA"
_BG_COLOR = "#000000"


def _glyph(charset: str) -> str:
    """A random character drawn from ``charset`` (e.g. "SARA" or "01")."""
    return random.choice(charset or "01")


@dataclass
class _Rain:
    """State for the falling columns; one 'drop' head row per column."""

    cols: int
    rows: int

    def __post_init__(self) -> None:
        # Each column starts at a random height above/near the top so they
        # don't all fall in lockstep, with a per-column falling speed.
        self.heads = [random.randint(-self.rows, 0) for _ in range(self.cols)]
        self.speeds = [random.choice((1, 1, 2)) for _ in range(self.cols)]

    def advance(self) -> None:
        """Move every column's head down one step, recycling off-screen ones."""
        for c in range(self.cols):
            self.heads[c] += self.speeds[c]
            # Once the whole trail has passed the bottom, respawn at the top.
            if self.heads[c] - len(_TRAIL_COLORS) > self.rows and random.random() < 0.5:
                self.heads[c] = random.randint(-self.rows // 2, 0)
                self.speeds[c] = random.choice((1, 1, 2))


def play_matrix_rain(*, charset: str = "SARA", max_seconds: float = 60.0) -> bool:
    """Show the full-screen rain using characters from ``charset``.

    Defaults to raining the letters of "SARA"; pass ``charset="01"`` for the
    classic binary look. Returns True if it ran, False if Tk is unavailable.
    Blocks until the user exits or ``max_seconds`` elapses.
    """
    try:
        import tkinter as tk
        import tkinter.font as tkfont
    except Exception:  # noqa: BLE001 - headless/no-Tk environment
        return False

    from time import monotonic

    root = tk.Tk()
    root.title("Sara — Binary Rain")
    root.configure(bg=_BG_COLOR)
    try:
        root.attributes("-fullscreen", True)
    except tk.TclError:
        root.state("zoomed")
    root.update_idletasks()

    width = root.winfo_screenwidth()
    height = root.winfo_screenheight()
    canvas = tk.Canvas(root, width=width, height=height, bg=_BG_COLOR,
                       highlightthickness=0)
    canvas.pack(fill="both", expand=True)

    font = tkfont.Font(family="Consolas", size=14, weight="bold")
    cell_w = max(font.measure("0"), 1)
    cell_h = max(font.metrics("linespace"), 1)
    cols = max(width // cell_w, 1)
    rows = max(height // cell_h, 1)
    rain = _Rain(cols=cols, rows=rows)

    state = {"running": True, "start": monotonic()}

    def stop(_event=None) -> None:
        state["running"] = False
        try:
            root.destroy()
        except tk.TclError:
            pass

    root.bind("<Escape>", stop)
    root.bind("q", stop)
    root.bind("Q", stop)
    root.protocol("WM_DELETE_WINDOW", stop)

    def frame() -> None:
        if not state["running"]:
            return
        if monotonic() - state["start"] >= max_seconds:
            stop()
            return
        canvas.delete("all")
        for c in range(cols):
            head = rain.heads[c]
            x = c * cell_w
            # Draw the head plus a fading trail of characters above it.
            for depth, color in enumerate((_HEAD_COLOR, *_TRAIL_COLORS)):
                row = head - depth
                if 0 <= row < rows:
                    canvas.create_text(
                        x, row * cell_h, text=_glyph(charset), fill=color,
                        font=font, anchor="nw",
                    )
        rain.advance()
        root.after(60, frame)

    frame()
    try:
        root.mainloop()
    except tk.TclError:
        pass
    return True


def launch_matrix_rain(charset: str = "SARA") -> bool:
    """Open the rain in its own process.

    Tkinter must own the main thread, but Sara's web server dispatches commands
    on worker threads (and ``mainloop`` would block the HTTP request). Spawning a
    fresh interpreter sidesteps both problems — and because it imports this file
    from disk, it always runs the current code even if a long-lived server still
    has an older module cached. Returns True if the process was launched.
    """
    src_dir = str(Path(__file__).resolve().parents[1])  # .../src
    env = dict(os.environ)
    env["PYTHONPATH"] = src_dir + os.pathsep + env.get("PYTHONPATH", "")
    try:
        subprocess.Popen(
            [sys.executable, "-m", "my_python_project.animation", charset],
            env=env,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


if __name__ == "__main__":
    # Run directly (used by launch_matrix_rain): argv[1] = charset, argv[2] = seconds.
    _charset = sys.argv[1] if len(sys.argv) > 1 else "SARA"
    _seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
    play_matrix_rain(charset=_charset, max_seconds=_seconds)
