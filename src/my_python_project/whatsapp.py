"""Open WhatsApp and send messages to a phone number.

Messaging prefers the WhatsApp **desktop app** via its deep link
(``whatsapp://send?phone=<num>&text=<text>``): this opens the native app
directly on the chat, focused, with the text pre-filled — which makes the
automated Send reliable. If the desktop app isn't available it falls back to the
web click-to-chat link (``https://wa.me/<num>``). To actually press Send
automatically, the optional ``pyautogui`` package hits Enter once the chat loads
(install with ``pip install -e ".[whatsapp]"``); without it, the chat opens
pre-filled and the user presses Enter themselves.

Phone numbers without a country code default to India (91); override with the
``WHATSAPP_COUNTRY_CODE`` env var. Names are resolved from an optional
``contacts.json`` ({"name": "919876543210", ...}) in the project root or CWD.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import webbrowser
from importlib.util import find_spec
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# A run of digits (with optional spaces/dashes) that looks like a phone number.
_NUMBER_RE = re.compile(r"\+?\d[\d\s\-]{6,}\d")

# WhatsApp app names to strip while parsing the message text.
_WA_WORDS = ("whatsapp", "whatsap", "watsapp", "watsap", "वाट्सएप", "व्हाट्सएप")

# Logistics words removed from the start of a message ("9876.. ko message karo X").
_CONNECTORS = {
    "to", "ko", "pe", "par", "pr", "ka", "ki", "ke", "number", "no", "nbr",
    "message", "msg", "msge", "text", "send", "bhejo", "bhej", "bhejna",
    "bhejdo", "likho", "likh", "karo", "kar", "kardo", "do", "de", "dena",
    "open", "start", "kholo", "khol", "chalu", "on", "a", "an", "the",
    "saying", "that", "please", "pls", "contact", "wala", "wale", "name", "sms",
}


def _digits(raw: str) -> str:
    return re.sub(r"\D", "", raw or "")


def _normalize_number(raw: str) -> str:
    """Return a WhatsApp-ready number (country code + digits), best effort."""
    d = _digits(raw)
    cc = os.environ.get("WHATSAPP_COUNTRY_CODE", "91")
    if len(d) == 10:  # local 10-digit number
        d = cc + d
    elif d.startswith("0"):
        d = cc + d.lstrip("0")
    return d


def _load_contacts() -> dict[str, str]:
    for path in (Path.cwd() / "contacts.json", _PROJECT_ROOT / "contacts.json"):
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                return {str(k).lower(): str(v) for k, v in data.items()}
        except (OSError, ValueError):
            continue
    return {}


def _match_contact(text: str) -> str:
    low = text.lower()
    for name, number in _load_contacts().items():
        if name and name in low:
            return number
    return ""


def _parse(text: str) -> tuple[str, str]:
    """Split free-form text into (number, message)."""
    match = _NUMBER_RE.search(text)
    number = match.group(0) if match else ""

    rest = text.replace(number, " ") if number else text
    for word in _WA_WORDS:
        rest = re.sub(re.escape(word), " ", rest, flags=re.IGNORECASE)

    tokens = rest.split()
    while tokens and tokens[0].lower().strip(".,:;") in _CONNECTORS:
        tokens.pop(0)
    message = " ".join(tokens).strip(" :.-")
    return number, message


def _autosend_available() -> bool:
    return find_spec("pyautogui") is not None


def _open_deeplink(uri: str) -> bool:
    """Open a ``whatsapp://`` URI in the desktop app. Returns True on success."""
    if os.name != "nt":
        return False
    try:
        os.startfile(uri)  # type: ignore[attr-defined]
        return True
    except Exception:  # noqa: BLE001
        return False


def _open_chat(num: str, text: str) -> bool:
    """Open the chat for ``num`` with ``text`` pre-filled. Returns True if the
    desktop app was used (more reliable for auto-send), False if the web link
    was used as a fallback."""
    quoted = urllib.parse.quote(text or "")
    deeplink = f"whatsapp://send?phone={num}"
    if text:
        deeplink += f"&text={quoted}"
    if _open_deeplink(deeplink):
        return True
    web = "https://wa.me/" + num + (("?text=" + quoted) if text else "")
    webbrowser.open(web)
    return False


def _foreground_title() -> str:
    """Return the title of the active foreground window (Windows only)."""
    if os.name != "nt":
        return ""
    try:
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        hwnd = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value or ""
    except Exception:  # noqa: BLE001
        return ""


def _whatsapp_focused() -> bool:
    """True if WhatsApp (desktop app or a browser tab) is the active window."""
    return "whatsapp" in _foreground_title().lower()


def _schedule_autosend(default_delay: float = 8.0) -> None:
    """Press Enter once WhatsApp has loaded, in a background thread.

    For safety the Enter is only sent while WhatsApp is the foreground window, so
    the message can never be typed into whatever app the user switched to. Set
    ``WHATSAPP_REQUIRE_FOCUS=0`` to skip that guard.
    """

    def _job() -> None:
        try:
            import pyautogui  # type: ignore
        except Exception:  # noqa: BLE001
            return
        try:
            delay = float(os.environ.get("WHATSAPP_SEND_DELAY", default_delay))
        except ValueError:
            delay = default_delay
        time.sleep(delay)  # let the chat load and the text fill in

        require_focus = os.environ.get("WHATSAPP_REQUIRE_FOCUS", "1") != "0"
        if require_focus and os.name == "nt":
            # Wait briefly for WhatsApp to be the active window; if it never is
            # (user clicked away), abort instead of sending into the wrong app.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if _whatsapp_focused():
                    break
                time.sleep(0.3)
            else:
                return

        try:
            pyautogui.press("enter")
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_job, daemon=True).start()


def open_whatsapp(*, hi: bool = False) -> tuple[bool, str]:
    """Open the WhatsApp desktop app (or WhatsApp Web as a fallback)."""
    try:
        if os.name == "nt":
            os.startfile("whatsapp:")  # type: ignore[attr-defined]  # desktop app
        else:
            webbrowser.open("https://web.whatsapp.com")
    except Exception:  # noqa: BLE001
        webbrowser.open("https://web.whatsapp.com")
    return True, ("व्हाट्सएप खोल रही हूँ..." if hi else "Opening WhatsApp...")


def send_message(number: str, text: str, *, hi: bool = False) -> tuple[bool, str]:
    """Open a WhatsApp chat for ``number`` with ``text`` pre-filled, and (if
    automation is available) press Send."""
    num = _normalize_number(number)
    if len(num) < 11:
        return False, (
            "मुझे सही फ़ोन नंबर नहीं मिला।" if hi
            else "I couldn't read a valid phone number."
        )

    used_app = _open_chat(num, text)

    auto = os.environ.get("WHATSAPP_AUTOSEND", "1") != "0"
    if text and auto and _autosend_available():
        # The desktop app focuses reliably and loads fast; the web fallback
        # (WhatsApp Web) needs longer before the message box is ready.
        _schedule_autosend(default_delay=6.0 if used_app else 12.0)
        return True, (
            f"{num} पर मैसेज भेज रही हूँ..." if hi else f"Sending your message to {num}..."
        )
    if text:
        return True, (
            f"{num} के लिए चैट खोल दी — भेजने के लिए Enter दबाएँ।" if hi
            else f"Opened the chat for {num} — press Enter to send."
        )
    return True, (
        f"{num} के लिए व्हाट्सएप चैट खोल दी।" if hi
        else f"Opened the WhatsApp chat for {num}."
    )


def handle_whatsapp(text: str, *, hi: bool = False, lang: str = "en") -> tuple[bool, str]:
    """Decide between opening WhatsApp and sending a message, then do it."""
    number, message = _parse(text)
    if not number:
        number = _match_contact(text)
    if number:
        return send_message(number, message, hi=hi)
    return open_whatsapp(hi=hi)
