"""Timed sleep and wake for Sara.

  "30 minute baad system sleep kar do"     -> sleep after a delay
  "raat 11 baje system sleep kar dena"      -> sleep at a clock time
  "subah 6 baje system on kar dena"         -> wake the PC from sleep at a time
  "sleep cancel karo" / "wake cancel karo"  -> call a timer off

Sleep uses the Win32 ``SetSuspendState`` call with hibernate = FALSE (the old
``rundll32 powrprof.dll,SetSuspendState`` trick passes bad arguments and
hibernates on PCs where hibernation is on).

Wake uses a Win32 waitable timer with "resume" set, the documented way to wake a
sleeping PC. It needs "Allow wake timers" on in the power plan (on battery it is
often off) and works from sleep, not from shutdown. Timers live in Sara's process:
if Sara is closed, they are gone.
"""

from __future__ import annotations

import ctypes
import re
import sys
import threading
from datetime import datetime, timedelta
from typing import Callable

Notify = Callable[[str, str], None]          # (message_hi, message_en)

WARN_BEFORE_S = 60                            # spoken warning before a timed sleep

# --- time parsing -------------------------------------------------------------------------

_MORNING = ("subah", "savere", "sawere", "morning", "सुबह", "सवेरे")
_NOON = ("dopahar", "dopehar", "dopahr", "afternoon", "दोपहर")
_EVENING = ("shaam", "sham", "evening", "शाम")
_NIGHT = ("raat", "rat", "night", "रात")

_CLOCK = re.compile(
    r"(?<![\d.:])(?P<h>[0-2]?\d)(?:\s*[:.]\s*(?P<m>[0-5]\d))?\s*"
    r"(?P<ampm>[ap]\.?m\.?(?![a-z]))?\s*(?P<baje>baje|bje|बजे|o'?clock|oclock)?(?![\d])",
    re.IGNORECASE,
)


def _has_word(text: str, words) -> bool:
    low = " " + " ".join(text.lower().split()) + " "
    for w in words:
        if w.isascii():
            if re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", low):
                return True
        elif w in text:
            return True
    return False


def parse_clock(text: str, now: datetime | None = None) -> datetime | None:
    """A clock time in the request ("11 baje", "11:30 pm", "raat 11", "subah 6.15"),
    as the next future datetime. None if the text names no clock time."""
    now = now or datetime.now()
    t = text.lower()
    best = None
    for m in _CLOCK.finditer(t):
        has_min, ampm, baje = m.group("m"), m.group("ampm"), m.group("baje")
        if not (has_min or ampm or baje):
            continue                     # a bare number ("30 minute") is a duration, not a time
        h, mi = int(m.group("h")), int(m.group("m") or 0)
        if h > 23 or (ampm and not 1 <= h <= 12):
            continue
        best = (h, mi, (ampm or "").replace(".", ""))
        break
    if best is None:
        return None
    h, mi, ampm = best
    if ampm == "pm" and h < 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0
    elif not ampm and h <= 12:
        if _has_word(text, _MORNING):
            h = 0 if h == 12 else h
        elif _has_word(text, _NOON) or _has_word(text, _EVENING):
            h = h if h == 12 else h + 12
        elif _has_word(text, _NIGHT):
            h = 0 if h == 12 else (h + 12 if h >= 6 else h)   # raat 11 -> 23:00, raat 2 -> 02:00
        else:
            # No hint: the nearer upcoming of h:mm and (h+12):mm.
            cands = [now.replace(hour=hh % 24, minute=mi, second=0, microsecond=0) for hh in (h % 12, h % 12 + 12)]
            cands = [c if c > now else c + timedelta(days=1) for c in cands]
            return min(cands)
    at = now.replace(hour=h, minute=mi, second=0, microsecond=0)
    return at if at > now else at + timedelta(days=1)


def fmt_time(at: datetime, lang: str = "en") -> str:
    now = datetime.now()
    clock = at.strftime("%I:%M %p").lstrip("0")
    if at.date() == now.date():
        return f"आज {clock}" if lang == "hi" else f"today at {clock}"
    if at.date() == (now + timedelta(days=1)).date():
        return f"कल {clock}" if lang == "hi" else f"tomorrow at {clock}"
    return at.strftime("%d %b, ") + clock


# --- intent detection -------------------------------------------------------------------

_SYSTEM = ("system", "computer", "pc", "laptop", "machine", "सिस्टम", "कंप्यूटर", "कम्प्यूटर", "लैपटॉप")
_WAKE = ("wake", "wake up", "jagana", "jaga dena", "jaga do", "uthana", "utha dena", "on kar", "on karna",
         "on kardena", "on kar dena", "chalu kar", "chalu karna", "जगा", "उठा", "ऑन कर", "चालू कर")
_SLEEP = ("sleep", "so jao", "sula", "suspend", "स्लीप", "सुला")
_CANCEL = ("cancel", "mat karo", "mat karna", "roko", "rok do", "band karo timer", "hatao", "रद्द", "मत करो", "रोको", "कैंसिल")


def is_wake_request(text: str) -> bool:
    return _has_word(text, _WAKE) and (_has_word(text, _SYSTEM) or _has_word(text, ("wake", "जगा")))


def is_sleep_word(text: str) -> bool:
    return _has_word(text, _SLEEP)


def is_timer_cancel(text: str) -> str | None:
    """'sleep' / 'wake' / 'all' when the user calls a timer off."""
    if not _has_word(text, _CANCEL):
        return None
    s, w = _has_word(text, _SLEEP), _has_word(text, _WAKE + ("wake",))
    if s and w:
        return "all"
    if s:
        return "sleep"
    if w or _has_word(text, ("alarm", "timer")):
        return "wake"
    return None


# --- Win32 ---------------------------------------------------------------------------------

def _win() -> bool:
    return sys.platform == "win32"


def sleep_now() -> bool:
    """Sleep (never hibernate): SetSuspendState(hibernate=FALSE, force=FALSE, disableWake=FALSE)."""
    if not _win():
        return False
    try:
        return bool(ctypes.WinDLL("powrprof").SetSuspendState(False, False, False))
    except Exception:  # noqa: BLE001
        return False


_ES_CONTINUOUS, _ES_SYSTEM_REQUIRED, _ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002


def _keep_awake(seconds: int) -> None:
    """After a timed wake Windows goes back to sleep in ~2 minutes unless someone
    is there; hold the system and the screen on for a while so the user finds it on."""
    k = ctypes.windll.kernel32
    k.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED)
    try:
        threading.Event().wait(seconds)
    finally:
        k.SetThreadExecutionState(_ES_CONTINUOUS)


# --- timers ------------------------------------------------------------------------------

class _Timers:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sleep_at: datetime | None = None
        self.sleep_cancel = threading.Event()
        self.wake_at: datetime | None = None
        self.wake_handle = None
        self.wake_cancel = threading.Event()


T = _Timers()


def schedule_sleep(at: datetime, notify: Notify) -> None:
    cancel_sleep()
    ev = threading.Event()
    with T.lock:
        T.sleep_at, T.sleep_cancel = at, ev

    def run() -> None:
        warn_at = at - timedelta(seconds=WARN_BEFORE_S)
        if ev.wait(max(0.0, (warn_at - datetime.now()).total_seconds())):
            return
        if (at - datetime.now()).total_seconds() > 5:
            notify("1 मिनट में सिस्टम स्लीप हो जाएगा। रोकने के लिए 'sleep cancel karo' कहिए।",
                   "The system will sleep in 1 minute. Say 'cancel sleep' to stop it.")
        if ev.wait(max(0.0, (at - datetime.now()).total_seconds())):
            return
        with T.lock:
            if T.sleep_cancel is ev:
                T.sleep_at = None
        notify("अब सिस्टम स्लीप कर रही हूँ।", "Putting the system to sleep now.")
        threading.Event().wait(3)          # let the voice line finish
        if not sleep_now():
            notify("माफ़ कीजिए, सिस्टम स्लीप नहीं हो पाया।", "Sorry, the system did not go to sleep.")

    threading.Thread(target=run, name="sara-sleep-timer", daemon=True).start()


def cancel_sleep() -> bool:
    with T.lock:
        had = T.sleep_at is not None
        T.sleep_cancel.set()
        T.sleep_at = None
    return had


def schedule_wake(at: datetime, notify: Notify) -> bool:
    """Arm a resume-capable waitable timer for `at`. False if Windows refused."""
    if not _win():
        return False
    cancel_wake()
    k = ctypes.windll.kernel32
    k.CreateWaitableTimerW.restype = ctypes.c_void_p
    handle = k.CreateWaitableTimerW(None, True, None)
    if not handle:
        return False
    # Absolute FILETIME (100 ns since 1601-01-01, UTC).
    due = ctypes.c_longlong(int((at.timestamp() + 11644473600) * 10_000_000))   # naive = local time
    ok = k.SetWaitableTimer(ctypes.c_void_p(handle), ctypes.byref(due), 0, None, None, True)
    if not ok:
        k.CloseHandle(ctypes.c_void_p(handle))
        return False
    ev = threading.Event()
    with T.lock:
        T.wake_at, T.wake_handle, T.wake_cancel = at, handle, ev

    def run() -> None:
        while not ev.is_set():
            # Short waits so a cancel is noticed; the timer itself wakes the PC.
            if k.WaitForSingleObject(ctypes.c_void_p(handle), 1000) == 0:
                break
        if ev.is_set():
            return
        with T.lock:
            T.wake_at, T.wake_handle = None, None
        k.CloseHandle(ctypes.c_void_p(handle))
        threading.Thread(target=_keep_awake, args=(10 * 60,), daemon=True).start()
        notify("सुप्रभात! तय समय पर सिस्टम चालू कर दिया है।", "Good morning! The system is awake as scheduled.")

    threading.Thread(target=run, name="sara-wake-timer", daemon=True).start()
    return True


def cancel_wake() -> bool:
    with T.lock:
        had = T.wake_at is not None
        T.wake_cancel.set()
        h, T.wake_handle, T.wake_at = T.wake_handle, None, None
    if h and _win():
        k = ctypes.windll.kernel32
        k.CancelWaitableTimer(ctypes.c_void_p(h))
        k.CloseHandle(ctypes.c_void_p(h))
    return had


def status() -> dict:
    with T.lock:
        return {"sleep_at": T.sleep_at.isoformat() if T.sleep_at else None,
                "wake_at": T.wake_at.isoformat() if T.wake_at else None}


def wake_timers_allowed() -> dict:
    """Whether the active power plan allows wake timers on AC / battery (None if unknown)."""
    import subprocess
    try:
        out = subprocess.run(["powercfg", "/q", "SCHEME_CURRENT", "SUB_SLEEP", "BD3B718A-0680-4D9D-8AB2-E1D2B4AC806D"],
                             capture_output=True, text=True, timeout=10, creationflags=0x08000000).stdout
    except Exception:  # noqa: BLE001
        return {"ac": None, "dc": None}
    vals = re.findall(r"Current (AC|DC) Power Setting Index:\s*0x([0-9a-f]+)", out, re.I)
    d = {k.lower(): int(v, 16) != 0 for k, v in vals}
    return {"ac": d.get("ac"), "dc": d.get("dc")}
