"""System power controls for Sara: sleep, lock, restart, and shutdown.

Sleep and lock are reversible, so they run immediately. Restart and shutdown
are destructive, so Sara asks for one confirmation first: the request sets a
pending action, and only a "yes"/"haan" on the next command carries it out.
Anything else cancels — a safety net so the machine never powers off by
accident.

Restart and shutdown may be scheduled for later ("shutdown after 2 hours",
"1.5 ghante baad shutdown", "shutdown in 30 minutes"): the spoken duration is
parsed to seconds and passed to ``shutdown /t``. A scheduled power-off can be
called back off with "cancel shutdown" (Windows ``shutdown /a``).
"""

from __future__ import annotations

import re
import subprocess
import threading

# Words that name the machine, so "computer off karo" is understood as a power
# command rather than, say, closing an app.
SYSTEM_NOUNS = ("system", "computer", "pc", "laptop", "machine",
                "सिस्टम", "कंप्यूटर", "कम्प्यूटर", "लैपटॉप")

# Confirmation vocabulary for the shutdown/restart reconfirm step.
_YES = ("yes", "yeah", "yep", "yup", "haan", "han", "ha", "haanji", "ji",
        "confirm", "ok", "okay", "sure", "bilkul", "kardo", "karo",
        "हाँ", "हां", "ठीक", "करो", "कर दो")
_NO = ("no", "nope", "nahi", "nhi", "mat", "cancel", "ruko", "rehne do",
       "नहीं", "मत", "रुको", "रहने दो")

# The power action awaiting confirmation, as ``(kind, delay_seconds)`` where
# kind is "shutdown" or "restart" and delay_seconds >= 0, or None when idle.
_pending: tuple[str, int] | None = None

# Words that cancel a scheduled (or pending) power-off: "cancel shutdown",
# "shutdown roko", "restart mat karo", "abort shutdown"...
_CANCEL_WORDS = (
    "cancel", "abort", "stop", "roko", "ruko", "rok do", "rehne do", "mat karo",
    "रद्द", "रोको", "रुको", "मत", "कैंसिल", "अबॉर्ट",
)

# Duration vocabulary for scheduled power-offs. Units map to seconds; number
# words cover the common Hindi/Hinglish forms alongside plain digits/fractions.
_HOUR_UNITS = (
    "hour", "hours", "hr", "hrs", "h", "ghanta", "ghante", "ghanton", "ghnta",
    "घंटा", "घंटे", "घंटों", "घण्टा", "घण्टे",
)
_MIN_UNITS = (
    "minute", "minutes", "min", "mins", "m", "minat", "minit", "mint", "मिनट",
)
_SEC_UNITS = ("second", "seconds", "sec", "secs", "s", "सेकंड", "सेकेंड")

_NUM_WORDS = {
    "aadha": 0.5, "adha": 0.5, "aadhe": 0.5, "half": 0.5, "आधा": 0.5, "आधे": 0.5,
    "dedh": 1.5, "derh": 1.5, "deodh": 1.5, "डेढ़": 1.5,
    "dhai": 2.5, "dhaai": 2.5, "ढाई": 2.5,
    "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5,
    "chha": 6, "chhe": 6, "che": 6, "saat": 7, "aath": 8, "nau": 9, "das": 10,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

# Longest supported delay (24h). Guards against a mis-parse scheduling a
# power-off years away.
_MAX_DELAY = 24 * 3600


def _has(text: str, kws) -> bool:
    """Whole-word (ASCII) / substring (non-ASCII) keyword match."""
    padded = " " + " ".join(text.lower().split()) + " "
    low = text.lower()
    for k in kws:
        k = k.lower()
        if k.isascii():
            if f" {k} " in padded:
                return True
        elif k in low:
            return True
    return False


def has_system_noun(text: str) -> bool:
    return _has(text, SYSTEM_NOUNS)


def is_sleep(text: str) -> bool:
    """True for a sleep/suspend request."""
    if _has(text, ("sleep mode", "suspend", "so jao", "sula do", "sula dijiye")):
        return True
    return has_system_noun(text) and _has(text, ("sleep", "so", "sula", "neend", "स्लीप", "सो"))


def is_lock(text: str) -> bool:
    """True for a lock-screen request."""
    if _has(text, ("lock screen", "screen lock", "lock karo", "lock kardo")):
        return True
    return has_system_noun(text) and _has(text, ("lock", "लॉक"))


def is_restart(text: str) -> bool:
    """True for a restart/reboot request."""
    return _has(text, ("restart", "reboot", "रीस्टार्ट", "रिबूट")) or (
        has_system_noun(text) and _has(text, ("dobara chalu", "दोबारा चालू"))
    )


def is_shutdown(text: str) -> bool:
    """True for a shutdown/power-off request."""
    if _has(text, ("shutdown", "shut down", "power off", "poweroff",
                   "power off karo", "switch off")):
        return True
    return has_system_noun(text) and _has(text, ("off", "band", "बंद", "ऑफ"))


def is_power_cancel(text: str) -> bool:
    """True for 'cancel/abort the (scheduled) shutdown/restart' requests.

    Requires both a cancel word and a power word, so an ordinary "stop the
    music" or a plain "shutdown" is not mistaken for a cancellation.
    """
    if not _has(text, _CANCEL_WORDS):
        return False
    if _has(text, ("shutdown", "shut down", "restart", "reboot", "power off",
                   "poweroff", "रीस्टार्ट", "रिबूट")):
        return True
    return has_system_noun(text) and _has(text, ("off", "band", "बंद", "ऑफ"))


def _word_to_num(tok: str) -> float | None:
    """Convert one token to a number: word ('do', 'half'), fraction ('1/2'),
    decimal ('1.5') or integer. Returns None if it isn't a number."""
    if tok in _NUM_WORDS:
        return _NUM_WORDS[tok]
    frac = re.fullmatch(r"(\d+)\s*/\s*(\d+)", tok)
    if frac:
        denom = int(frac.group(2))
        return int(frac.group(1)) / denom if denom else None
    try:
        return float(tok)
    except ValueError:
        return None


def _unit_seconds(tok: str) -> int | None:
    """Seconds-per-unit for a time-unit token, or None if it isn't one."""
    if tok in _HOUR_UNITS:
        return 3600
    if tok in _MIN_UNITS:
        return 60
    if tok in _SEC_UNITS:
        return 1
    return None


def parse_delay_seconds(text: str) -> int:
    """Extract a delay in seconds from natural speech, or 0 if none is given.

    Understands e.g. "2 hours", "1.5 hr", "90 min", "half an hour",
    "1 hour and a half", "dedh ghanta", "aadha ghanta", "2 ghante 30 minute".
    Returns 0 when no duration is mentioned, so callers fall back to an
    immediate power-off (matching the original behaviour).
    """
    low = text.lower().replace("½", " 0.5 ").replace("1/2", " 0.5 ")
    # Split a number glued to a unit ("2hr" -> "2 hr", "30min" -> "30 min").
    low = re.sub(r"(\d)([a-zऀ-ॿ])", r"\1 \2", low)
    low = re.sub(r"([a-zऀ-ॿ])(\d)", r"\1 \2", low)

    total = 0.0
    pending: float | None = None  # a number waiting for its unit
    last_unit: int | None = None  # seconds-per-unit of the most recent unit
    found = False

    for raw in re.split(r"[\s,]+", low):
        tok = raw.strip(".!?;:")
        if not tok:
            continue
        unit = _unit_seconds(tok)
        if unit is not None:
            num = pending
            if num is None:
                # Bare unit word ("an hour"/"a minute") means one; require an
                # explicit number for seconds and single-letter units (h/m/s).
                if unit in (3600, 60) and len(tok) > 1:
                    num = 1.0
                else:
                    continue
            total += num * unit
            last_unit = unit
            pending = None
            found = True
            continue
        val = _word_to_num(tok)
        if val is not None:
            pending = val if pending is None else pending + val

    # A trailing fraction ("... and a half") attaches to the last unit seen.
    if pending is not None and 0 < pending < 1 and last_unit is not None:
        total += pending * last_unit
        found = True

    if not found:
        return 0
    return int(round(max(0.0, min(total, _MAX_DELAY))))


# --- Executors. Split out so tests can run detection without real power ops. ---

def _run(cmd) -> bool:
    try:
        subprocess.Popen(cmd, shell=isinstance(cmd, str))
        return True
    except Exception:  # noqa: BLE001
        return False


def do_sleep() -> bool:
    # Win32 SetSuspendState(hibernate=False): the old "rundll32 powrprof.dll,
    # SetSuspendState 0,1,0" passes bad arguments and hibernates when hibernation is on.
    from .power_timer import sleep_now

    threading.Thread(target=sleep_now, daemon=True).start()   # returns only after waking
    return True


def do_lock() -> bool:
    return _run("rundll32.exe user32.dll,LockWorkStation")


def do_restart(seconds: int = 0) -> bool:
    return _run(f"shutdown /r /t {int(max(0, seconds))}")


def do_shutdown(seconds: int = 0) -> bool:
    return _run(f"shutdown /s /t {int(max(0, seconds))}")


def do_abort() -> bool:
    """Call off a shutdown/restart already scheduled with ``shutdown /t``."""
    return _run("shutdown /a")


# --- Reconfirm flow for destructive actions. ---

def power_pending() -> bool:
    """True while a shutdown/restart is waiting for confirmation."""
    return _pending is not None


def clear_pending() -> None:
    """Drop a shutdown/restart that is still waiting for confirmation."""
    global _pending
    _pending = None


def _format_delay(seconds: int, lang: str = "en") -> str:
    """Human-readable duration, e.g. 5400 -> '1 hour 30 minutes' / '1 घंटा 30 मिनट'."""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts: list[str] = []
    if lang == "hi":
        if h:
            parts.append("1 घंटा" if h == 1 else f"{h} घंटे")
        if m:
            parts.append(f"{m} मिनट")
        if s and not h and not m:
            parts.append(f"{s} सेकंड")
        return " ".join(parts) or "अभी"
    if h:
        parts.append(f"{h} hour" + ("" if h == 1 else "s"))
    if m:
        parts.append(f"{m} minute" + ("" if m == 1 else "s"))
    if s and not h and not m:
        parts.append(f"{s} second" + ("" if s == 1 else "s"))
    return " ".join(parts) or "now"


def request_power(kind: str, *, delay: int = 0, lang: str = "en") -> str:
    """Arm a destructive power action and return the confirmation prompt.

    ``delay`` (seconds) schedules the action for later; 0 means immediately.
    """
    global _pending
    delay = max(0, int(delay))
    _pending = (kind, delay)
    verb_hi = "बंद" if kind == "shutdown" else "रीस्टार्ट"
    verb_en = "shut down" if kind == "shutdown" else "restart"
    if lang == "hi":
        when = f"{_format_delay(delay, 'hi')} बाद " if delay > 0 else ""
        return (
            f"क्या आप वाकई {when}सिस्टम {verb_hi} करना चाहते हैं? पक्का करने के "
            "लिए 'हाँ' कहिए, या रद्द करने के लिए 'नहीं'।"
        )
    when = f" in {_format_delay(delay, 'en')}" if delay > 0 else ""
    return (
        f"Are you sure you want to {verb_en} the system{when}? Say 'yes' to "
        "confirm, or 'no' to cancel."
    )


def confirm_power(text: str, *, lang: str = "en") -> tuple[bool, str]:
    """Resolve a pending power action from the user's reply.

    Only an explicit yes carries it out; anything else cancels for safety.
    Returns ``(ok, message)``.
    """
    global _pending
    pending = _pending
    _pending = None  # clear either way — one confirmation, no lingering state
    kind, delay = pending if pending is not None else ("shutdown", 0)

    if not _has(text, _YES) or _has(text, _NO):
        return True, (
            "ठीक है, रद्द कर दिया।" if lang == "hi" else "Okay, cancelled."
        )

    verb = "shutdown" if kind == "shutdown" else "restart"
    if kind == "restart":
        started = do_restart(delay)
        act_hi, act_en = "रीस्टार्ट", "Restarting"
    else:
        started = do_shutdown(delay)
        act_hi, act_en = "बंद", "Shutting down"

    if not started:
        return False, (
            "माफ़ कीजिए, यह काम नहीं कर पाई।" if lang == "hi"
            else "Sorry, I couldn't run that command."
        )

    if delay > 0:
        if lang == "hi":
            return True, (
                f"सिस्टम {_format_delay(delay, 'hi')} बाद {act_hi} कर रही हूँ। "
                f"रोकने के लिए '{verb} cancel karo' कहिए।"
            )
        return True, (
            f"{act_en} the system in {_format_delay(delay, 'en')}. "
            f"Say 'cancel {verb}' to stop it."
        )
    return True, (
        f"सिस्टम {act_hi} कर रही हूँ..." if lang == "hi" else f"{act_en} the system..."
    )


def cancel_scheduled(*, lang: str = "en") -> tuple[bool, str]:
    """Call off a scheduled shutdown/restart (``shutdown /a``) and clear any
    pending confirmation. Returns ``(ok, message)``."""
    global _pending
    _pending = None
    ok = do_abort()
    return ok, (
        "ठीक है, शेड्यूल किया गया शटडाउन रद्द कर दिया।" if lang == "hi"
        else "Okay, cancelled the scheduled shutdown."
    )
