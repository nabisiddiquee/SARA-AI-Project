from __future__ import annotations

import glob
import os
import re
import subprocess
import threading
import webbrowser
from dataclasses import dataclass
from time import monotonic

from .events import emit
from .notify import notify
from .tts import speak

# Common romanized-Hindi (Hinglish) words. If any appears as a whole word, the
# input is treated as Hindi so Sara replies in Hindi. Kept to fairly distinctive
# words to avoid misreading plain English (e.g. "name", "the", "is").
_HINGLISH_WORDS = {
    "kya", "kyu", "kyun", "kyunki", "kaise", "kaisa", "kaisi", "kaun", "kahan",
    "kaha", "kab", "kitna", "kitne", "kitni", "hai", "hain", "hoon", "hu",
    "tum", "tumhara", "tumhari", "tera", "teri", "mera", "meri", "aap", "aapka",
    "aapki", "mujhe", "tujhe", "hum", "humein", "nahi", "nhi", "mat", "acha",
    "accha", "achha", "theek", "thik", "sahi", "bilkul", "batao", "bata",
    "bataye", "karo", "kardo", "kijiye", "chahiye", "raha", "rahi",
    "rha", "rhi", "naam", "namaste", "namaskar", "dhanyavaad", "shukriya",
    "swagat", "kholo", "khol", "banao", "chalu", "mausam", "mosam", "samay",
    "waqt", "abhi", "aaj", "yaar", "bhai", "matlab", "thoda", "zyada", "bahut",
    "kuch", "kaam", "paani", "khana", "dost", "suno", "bolo", "dikhao",
    "khabar", "khabren", "khabrein", "samachar", "tareekh", "baje",
}

# Multi-word Hinglish phrases worth matching as a unit.
_HINGLISH_PHRASES = (
    "mera naam", "tumhara naam", "kaun ho", "kaisa hai", "kya hai", "naam kya",
    "kaise ho", "kaise hain",
)


def is_hindi(text: str) -> bool:
    """Detect Devanagari or common romanized-Hindi (Hinglish) input."""
    if any("ऀ" <= c <= "ॿ" for c in text):
        return True
    low = text.lower()
    tokens = {t.strip("?.,!\"'") for t in low.split()}
    if tokens & _HINGLISH_WORDS:
        return True
    return any(p in low for p in _HINGLISH_PHRASES)


def _has_kw(text: str, kws) -> bool:
    """Whole-word keyword match.

    ASCII keywords must appear as standalone, space-delimited tokens so that
    short words like ``hi`` do not match inside ``this``/``machine``.
    Non-ASCII keywords (Devanagari/Urdu) fall back to substring matching
    because those scripts rarely collide with surrounding English text and
    inflected forms make strict boundaries unreliable.
    """
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


def _extract_name(text: str, kws) -> str:
    """Strip the first matching keyword from ``text`` and return the remainder.

    Operates on the original (case-preserving) text so names like
    ``Report.txt`` are not lower-cased.
    """
    low = text.lower()
    for k in kws:
        idx = low.find(k.lower())
        if idx != -1:
            return (text[:idx] + text[idx + len(k):]).strip()
    return ""


# Filler words dropped when pulling a city/topic out of a question, so what
# remains is the place name (weather/time) or search topic (news).
_CITY_STOPWORDS = {
    # Hinglish filler / question words
    "aaj", "kal", "abhi", "ka", "ki", "ke", "ko", "par", "pe", "mein", "me",
    "kya", "kaisa", "kaisi", "kaise", "kaun", "kaunsa", "konsa", "kitna",
    "kitne", "kitni", "hai", "hain", "ho", "hua", "huyi", "raha", "rahi",
    "rha", "rhi", "batao", "bata", "sunao", "dikhao", "do", "main", "yaar",
    "taza", "taaza", "latest", "breaking", "update", "updates",
    # English filler
    "today", "in", "on", "for", "the", "of", "about", "what", "whats",
    "is", "how", "at", "right", "now", "tell", "please", "like", "s",
    "current", "currently", "there", "information", "info", "report", "details",
    "detail", "nhi", "nahi", "mujhe", "chahiye", "dena", "city", "shahar",
    # Devanagari filler
    "आज", "कल", "का", "की", "के", "कैसा", "कैसी", "है", "में", "क्या",
    "अभी", "बताओ", "कितना", "कितने", "ताज़ा",
}


def _extract_subject(text: str, keywords, *, keep_commas: bool = False) -> str:
    """Pull the subject (city or news topic) out of a question.

    Removes the intent keywords and common filler words; whatever remains is the
    subject (e.g. "aaj ka mausam Darbhanga ka" -> "darbhanga"). With
    ``keep_commas`` a list like "delhi, mumbai" keeps its separators.
    """
    low = text.lower()
    for k in keywords:
        low = low.replace(k.lower(), " ")
    low = low.replace(",", " , " if keep_commas else " ")
    tokens = [t for t in low.split() if t and t not in _CITY_STOPWORDS]
    return " ".join(tokens).strip(" ,")


def _on_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    message: str
    # "task" = Sara carried out an action (the web UI announces completion);
    # "chat" = a conversational or informational answer, spoken as-is;
    # "camera_open" / "camera_close" = the web UI switches its own camera;
    # "media_play" / "media_stop" = the web UI plays/stops a song (``data``);
    # "media_menu" = the web UI offers the playlists in ``data`` to pick from;
    # "stage" / "stage_close" / "stage_max" / "stage_restore" = the web UI shows
    # the news board in ``data`` in (or closes / resizes) its big panel;
    # "bt_open" = the web UI opens its Bluetooth screen;
    # "ac_open" = the web UI opens its AC control screen;
    # "notebook_open" = the web UI opens the Notebook (on note ``data.name``);
    # "calc_open" = the web UI opens the Calculator (showing ``data.expr``);
    # "stop_all" = the web UI stops speaking and all music / radio / Quran.
    kind: str = "chat"
    data: dict | None = None


_stop_camera_event = threading.Event()


def open_camera(*, max_seconds: float = 10.0) -> ActionResult:
    _stop_camera_event.clear()
    try:
        import cv2  # type: ignore
    except Exception as e:  # noqa: BLE001
        return ActionResult(
            ok=False,
            message=(
                "Camera support not installed. Install extras and try again:\n"
                "  python -m pip install -e \".[camera]\"\n"
                f"Import error: {e}"
            ),
        )

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        return ActionResult(ok=False, message="Could not open webcam (index 0).", kind="task")

    emit("camera", "Opening camera")
    notify("Camera", "Opening camera")
    speak("Opening camera")
    cv2.namedWindow("Camera (press q to quit)", cv2.WINDOW_NORMAL)
    start = monotonic()
    while not _stop_camera_event.is_set():
        if monotonic() - start >= max_seconds:
            break
        ok, frame = cap.read()
        if not ok:
            break
        cv2.imshow("Camera (press q to quit)", frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()
    emit("camera", "Camera closed")
    notify("Camera", "Camera closed")
    speak("Camera closed")
    return ActionResult(ok=True, message="Camera closed.", kind="task")


def _ask_string(title: str, prompt: str) -> str | None:
    """Prompt for a line of text via Tk. Returns None if cancelled/unavailable."""
    import tkinter as tk
    from tkinter import simpledialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return simpledialog.askstring(title, prompt)
    finally:
        root.destroy()


def _ask_directory(title: str) -> str | None:
    """Prompt for a directory via Tk. Returns None if cancelled/unavailable."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return filedialog.askdirectory(title=title)
    finally:
        root.destroy()


def _create_fs_entry(
    text: str,
    *,
    kws,
    hi: bool,
    lang: str,
    is_file: bool,
) -> ActionResult:
    """Shared handler for 'create folder' / 'create file' commands."""
    kind_hi = "फाइल" if is_file else "फोल्डर"
    kind_en = "file" if is_file else "folder"

    name = _extract_name(text, kws)

    # GUI dialogs only work safely on the process main thread (e.g. the CLI
    # `listen` path). When dispatched from a background/server thread, Tk
    # would crash, so require the name inline instead.
    if not name and not _on_main_thread():
        msg = (
            f"कृपया कमांड में {kind_hi} का नाम बताएँ।"
            if hi else
            f"Please include the {kind_en} name in the command "
            f"(GUI prompts aren't available from the server)."
        )
        speak(msg, lang=lang)
        return ActionResult(ok=False, message=msg)

    if not name:
        prompt = (
            f"{kind_hi} का नाम क्या रखें?" if hi
            else f"What should I name the {kind_en}?"
        )
        speak(prompt, lang=lang)
        name = _ask_string(
            f"{kind_en.capitalize()} Name",
            f"Enter {kind_en} name:",
        )

    if not name:
        msg = (
            f"{kind_hi} बनाना रद्द कर दिया गया।" if hi
            else f"{kind_en.capitalize()} creation cancelled."
        )
        speak(msg, lang=lang)
        return ActionResult(ok=False, message=msg)

    # Choose a location.
    if _on_main_thread():
        prompt_loc = "कहाँ बनाएँ?" if hi else "Where should I create it?"
        speak(prompt_loc, lang=lang)
        location = _ask_directory(f"Select Location to Create {kind_en.capitalize()}")
    else:
        # No GUI available off the main thread: default to the user's home dir.
        location = os.path.expanduser("~")

    if not location:
        msg = (
            f"{kind_hi} बनाना रद्द कर दिया गया।" if hi
            else f"{kind_en.capitalize()} creation cancelled."
        )
        speak(msg, lang=lang)
        return ActionResult(ok=False, message=msg)

    target = os.path.join(location, name)
    try:
        if is_file:
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write("Created by Sara\n")
        else:
            os.makedirs(target, exist_ok=True)
    except OSError as e:
        msg = f"Failed to create {kind_en}: {e}"
        emit("error", msg)
        speak(msg, lang=lang)
        return ActionResult(ok=False, message=msg, kind="task")

    msg = (
        f"{location} में {name} नाम का {kind_hi} बना दिया है।" if hi
        else f"Created {kind_en} {name!r} in {location}."
    )
    speak(msg, lang=lang)
    return ActionResult(ok=True, message=msg, kind="task")


_BT = r"(?:bluetooth|blue tooth|blutooth|bluetoth|bluethooth|ब्लूटूथ)"
_BT_ON = r"(?:^|\s)(?:on|chalu|chaalu|enable|turn on|start|चालू|ऑन)(?:\s|$)"
_BT_OFF = r"(?:^|\s)(?:off|band|bandh|disable|turn off|बंद|ऑफ)(?:\s|$)"
_BT_CONNECT = [
    r"^(?:connect|connect to|jodo)\s+(.+)$",
    r"^(.+?)\s+(?:ko\s+|se\s+)?(?:connect karo|connect kar do|connect kro|connect|jodo|कनेक्ट करो)$",
]
_BT_DISCONNECT = [
    r"^(?:disconnect|disconnect from)\s+(.+)$",
    r"^(.+?)\s+(?:ko\s+|se\s+)?(?:disconnect karo|disconnect kar do|disconnect kro|disconnect|डिस्कनेक्ट करो)$",
]


def _bluetooth_command(text: str, hi: bool, web_ui: bool) -> ActionResult | None:
    """Bluetooth voice commands: open the screen, radio on/off, (dis)connect a
    paired device by name. None when the command isn't about Bluetooth."""
    from . import bluetooth

    t = " ".join(text.lower().split()).rstrip(".!?।")
    mentions = re.search(_BT, t) is not None

    for patterns, on in ((_BT_DISCONNECT, False), (_BT_CONNECT, True)):
        name = next((m.group(1) for p in patterns if (m := re.match(p, t))), None)
        if not name:
            continue
        name = re.sub(rf"\b(?:{_BT[3:-1]}|my|mera|meri|mere|the|device|se|ko|par|pe|via|with|using|through|on)\b", " ", name)
        name = " ".join(name.split())
        if not name or not bluetooth.available():
            break
        dev = bluetooth.find_paired(name)
        if dev is None:
            if not mentions:
                return None  # e.g. "openvpn connect" — not a Bluetooth device
            msg = (f"'{name}' नाम का कोई पेयर्ड डिवाइस नहीं मिला।" if hi
                   else f"I couldn't find a paired Bluetooth device called {name!r}.")
            return ActionResult(ok=False, message=msg, kind="task")
        label = dev["name"]
        if not dev["audio"]:
            msg = (f"{label} पेयर्ड है, पर इसे उसी डिवाइस से कनेक्ट करना होगा।" if hi
                   else f"{label} is paired, but it has to connect from its own side (only headphones and speakers can be connected from here).")
            return ActionResult(ok=False, message=msg, kind="task")
        ok = bluetooth.connect(dev["mac"]) if on else bluetooth.disconnect(dev["mac"])
        if on:
            msg = (f"{label} से कनेक्ट कर रही हूँ। डिवाइस चालू और पास में रखें।" if hi
                   else f"Connecting to {label}. Make sure it's switched on and nearby.")
        else:
            msg = f"{label} डिस्कनेक्ट कर दिया है।" if hi else f"{label} has been disconnected."
        emit("bluetooth", msg)
        return ActionResult(ok=ok, message=msg, kind="task")

    if not mentions:
        return None
    if not bluetooth.available():
        msg = ("ब्लूटूथ सपोर्ट इंस्टॉल नहीं है।" if hi
               else 'Bluetooth support isn\'t installed. Run: pip install -e ".[bluetooth]"')
        return ActionResult(ok=False, message=msg, kind="task")

    want = True if re.search(_BT_ON, t) else False if re.search(_BT_OFF, t) else None
    if want is not None:
        try:
            state = bluetooth.radio(want)
        except Exception:  # noqa: BLE001
            state = None
        if state is None:
            msg = "ब्लूटूथ स्विच नहीं कर पाई।" if hi else "I couldn't switch Bluetooth."
            return ActionResult(ok=False, message=msg, kind="task")
        msg = (("ब्लूटूथ चालू कर दिया है।" if hi else "Bluetooth is now on.") if state
               else ("ब्लूटूथ बंद कर दिया है।" if hi else "Bluetooth is now off."))
        emit("bluetooth", msg)
        return ActionResult(ok=state == want, message=msg, kind="task")

    # Anything else about Bluetooth opens the screen and starts a search.
    if web_ui:
        bluetooth.start_scan()
        msg = ("ब्लूटूथ खोल रही हूँ और आस-पास के डिवाइस खोज रही हूँ।" if hi
               else "Opening Bluetooth and searching for nearby devices.")
        return ActionResult(ok=True, message=msg, kind="bt_open")
    os.startfile("ms-settings:bluetooth")  # noqa: S606 - CLI: Windows' own screen
    msg = "ब्लूटूथ सेटिंग्स खोल रही हूँ।" if hi else "Opening Bluetooth settings."
    return ActionResult(ok=True, message=msg, kind="task")


_AC_MODE_HI = {"cool": "कूल", "dry": "ड्राई", "fan": "फैन", "auto": "ऑटो"}
_AC_FAN_HI = {"low": "धीमी", "medium": "मीडियम", "high": "तेज़", "turbo": "टर्बो", "auto": "ऑटो"}
_AC_FEATURE = {"turbo": ("टर्बो", "Turbo"), "sleep": ("स्लीप मोड", "Sleep mode"),
               "display": ("डिस्प्ले", "The display"), "buzzer": ("बीप", "The beep"),
               "clean": ("सेल्फ़ क्लीन", "Self clean")}


def _ac_command(text: str, hi: bool, web_ui: bool) -> ActionResult | None:
    """Blue Star AC voice commands: on/off, temperature, mode, fan, swing,
    features, status; anything else opens the AC screen. None when the
    command isn't about the AC."""
    from . import ac

    cmd = ac.parse_command(text)
    if cmd is None:
        return None
    action = cmd["action"]
    if action == "open" and web_ui:
        msg = "एसी कंट्रोल खोल रही हूँ।" if hi else "Opening the AC controls."
        return ActionResult(ok=True, message=msg, kind="ac_open")
    if not ac.configured():
        msg = ("एसी अभी जुड़ा नहीं है। .env में BLUESTAR_USER और BLUESTAR_PASSWORD डालें।" if hi
               else "The AC isn't set up yet. Add BLUESTAR_USER and BLUESTAR_PASSWORD to .env.")
        return ActionResult(ok=False, message=msg, kind="ac_open" if web_ui else "task")
    try:
        if action == "report":
            msg = ac.energy_summary(cmd["days"])
            return ActionResult(ok=True, message=msg, kind="chat")
        if action in ("status", "open"):
            d = ac.find()
            room = f"{d['room']:.0f}°" if d["room"] is not None else ("पता नहीं" if hi else "unknown")
            if not d["online"]:
                msg = f"{d['name']} ऑफ़लाइन है।" if hi else f"{d['name']} is offline."
            elif d["power"]:
                msg = (f"एसी चालू है, {_AC_MODE_HI[d['mode']]} मोड, {d['target']:.0f} डिग्री पर। कमरे का तापमान {room} है।" if hi
                       else f"The AC is on in {d['mode']} mode at {d['target']:.0f}°C. The room is {room}.")
            else:
                msg = (f"एसी बंद है। कमरे का तापमान {room} है।" if hi
                       else f"The AC is off. The room is {room}.")
            return ActionResult(ok=True, message=msg, kind="ac_open" if web_ui else "chat")
        if action == "power":
            ac.power(cmd["on"])
            msg = (("एसी चालू कर दिया है।" if hi else "The AC is on.") if cmd["on"]
                   else ("एसी बंद कर दिया है।" if hi else "The AC is off."))
        elif action in ("temp", "delta"):
            cur = ac.find()
            t = ac.clamp_temp(cmd["value"] if action == "temp" else (cur["target"] or 24) + cmd["value"])
            ac.set_temp(t)
            msg = f"एसी का तापमान {t} डिग्री कर दिया है।" if hi else f"The AC is set to {t}°C."
        elif action == "mode":
            ac.set_mode(cmd["value"])
            msg = (f"एसी {_AC_MODE_HI[cmd['value']]} मोड में चला दिया है।" if hi
                   else f"The AC is now in {cmd['value']} mode.")
        elif action == "fan":
            ac.set_fan(cmd["value"])
            msg = (f"एसी की हवा {_AC_FAN_HI[cmd['value']]} कर दी है।" if hi
                   else f"Fan speed set to {cmd['value']}.")
        elif action == "swing":
            ac.set_swing(cmd["value"])
            on = cmd["value"] != "off"
            msg = (("स्विंग चालू कर दिया है।" if on else "स्विंग बंद कर दिया है।") if hi
                   else ("Swing is on." if on else "Swing is off."))
        elif action == "eco":
            ac.set_eco(cmd["value"])
            msg = (("इको मोड बंद कर दिया है।" if hi else "Eco mode is off.") if cmd["value"] == "off"
                   else (f"इको मोड {cmd['value']}% पर कर दिया है।" if hi else f"Eco mode set to {cmd['value']}%."))
        else:  # feature
            label_hi, label_en = _AC_FEATURE[cmd["name"]]
            msg = ((f"{label_hi} चालू कर दिया है।" if cmd["on"] else f"{label_hi} बंद कर दिया है।") if hi
                   else f"{label_en} is {'on' if cmd['on'] else 'off'}.")
            ac.set_feature(cmd["name"], cmd["on"])
    except ac.ACError as e:
        return ActionResult(ok=False, message=str(e), kind="task")
    except Exception as e:  # noqa: BLE001
        msg = f"एसी से बात नहीं हो पाई: {e}" if hi else f"I couldn't reach the AC: {e}"
        return ActionResult(ok=False, message=msg, kind="task")
    emit("ac", msg)
    return ActionResult(ok=True, message=msg, kind="task")


def _web_result(req: dict, hi: bool) -> ActionResult:
    """Handle a ``web.parse`` request from the dashboard.

    The big panel only shows news, so here it just closes / resizes. YouTube
    videos play in the AI output panel's player; everything else (Google,
    maps, Wikipedia, websites, YouTube's home page) opens in a browser tab.
    """
    from . import web

    action = req["action"]
    if action == "close":
        msg = "स्क्रीन बंद कर दी है।" if hi else "The screen has been closed."
        return ActionResult(ok=True, message=msg, kind="stage_close")
    if action == "maximize":
        msg = "स्क्रीन बड़ी कर दी है।" if hi else "The panel is now full screen."
        return ActionResult(ok=True, message=msg, kind="stage_max")
    if action == "restore":
        msg = "स्क्रीन सामान्य कर दी है।" if hi else "The panel is back to normal size."
        return ActionResult(ok=True, message=msg, kind="stage_restore")
    if action == "youtube_play":
        query = req["query"]
        try:
            results = web.youtube_search(query)
        except Exception:  # noqa: BLE001
            results = []
        if not results:
            msg = (f"माफ़ कीजिए, यूट्यूब पर '{query}' नहीं मिला।" if hi
                   else f"Sorry, I couldn't find {query!r} on YouTube.")
            return ActionResult(ok=False, message=msg, kind="task")
        top = results[0]
        # Speak a short title: YouTube titles often trail "| cast | label | 4K".
        name = top["title"].split("|")[0].strip() or top["title"]
        msg = (f"यूट्यूब पर चला रही हूँ: {name}" if hi
               else f"Playing {name} on YouTube.")
        emit("media", msg)
        return ActionResult(ok=True, message=msg, kind="media_play", data={
            "title": top["title"], "channel": top["channel"], "video_id": top["video_id"],
            "url": f"https://www.youtube.com/watch?v={top['video_id']}",
            "alternates": results[1:],  # tried in turn if this one won't embed
        })

    url, title = req["url"], req["title"]
    webbrowser.open(url)
    msg = {
        "YOUTUBE": ("यूट्यूब खोल रही हूँ।" if hi else "Opening YouTube in your browser."),
        "GOOGLE": ("गूगल पर खोज रही हूँ: {t}" if hi else "Opening the Google results for {t}."),
        "MAP": ("{t} का नक्शा खोल रही हूँ।" if hi else "Opening {t} on Google Maps."),
        "WIKI": ("विकिपीडिया पर {t} खोल रही हूँ।" if hi else "Opening {t} on Wikipedia."),
        "WEB": ("{t} खोल रही हूँ।" if hi else "Opening {t}."),
    }[req["kind"]].format(t=title)
    if req["kind"] == "GOOGLE" and title == "Google":
        msg = "गूगल खोल रही हूँ।" if hi else "Opening Google."
    emit("web", msg)
    return ActionResult(ok=True, message=msg, kind="task")


def _launch(label_en: str, label_hi: str, lang: str, hi: bool, candidates, fallback) -> None:
    """Launch the first existing executable from ``candidates``, else ``fallback``."""
    path = next((p for p in candidates if p and os.path.exists(p)), None)
    try:
        if path:
            subprocess.Popen([path])
        else:
            subprocess.Popen(fallback, shell=True)
    except Exception as e:  # noqa: BLE001
        emit("error", f"Failed to open {label_en}: {e}")


def dispatch_command(
    text: str, *, camera_seconds: float = 10.0, web_ui: bool = False, reply_lang: str = ""
) -> ActionResult:
    original = text.strip()
    # Reply language: the dashboard's "Sara ki bhasha" setting wins ("hi" /
    # "en"); otherwise it follows the command (Hindi / Hinglish -> Hindi).
    if reply_lang == "hi":
        hi = True
    elif reply_lang == "en":
        hi = False
    else:
        hi = is_hindi(text)
    lang = "hi" if hi else "en"

    def has(*kws) -> bool:
        return _has_kw(original, kws)

    # A bare "stop" / "band karo" / "rok do" / "sab cancel karo" stops
    # everything at once. Checked first so it also calls off a pending
    # shutdown or email confirmation and any running game.
    from . import stop_all

    if stop_all.is_stop_all(original):
        stop_all.cancel_all()
        msg = "ठीक है, सारे काम रोक दिए हैं।" if hi else "Okay, I have stopped everything."
        emit("system", msg)
        if not web_ui:
            speak(msg, lang=lang)
        return ActionResult(ok=True, message=msg, kind="stop_all" if web_ui else "task")

    # A pending shutdown/restart confirmation must be answered first — this is
    # the reconfirm gate, so nothing else runs until the user says yes or no.
    from . import system

    if system.power_pending():
        ok, msg = system.confirm_power(original, lang=lang)
        emit("system", msg)
        speak(msg, lang=lang)
        return ActionResult(ok=ok, message=msg)

    # Same for a drafted email waiting for "haan"/"yes" before it is sent.
    from . import mailer

    if mailer.email_pending():
        ok, msg = mailer.confirm_email(original, lang=lang)
        emit("email", msg)
        speak(msg, lang=lang)
        return ActionResult(ok=ok, message=msg)

    # "tum kya kya kar sakti ho" / "help" / "skills dikhao": the Skills window.
    from . import skills

    if skills.is_help_request(original):
        cat = skills.catalog()
        msg = (f"मैं {cat['count']} तरह के काम कर सकती हूँ। पूरी लिस्ट स्किल्स विंडो में खोल दी है, किसी भी उदाहरण पर क्लिक कीजिए।"
               if hi else f"I can do {cat['count']} kinds of things. The full list is open in the Skills window; click any example.")
        emit("skills", msg)
        if not web_ui:
            names = "; ".join(c["title"] for c in cat["categories"])
            msg += "\n" + names
            speak(msg.split("\n")[0], lang=lang)
        return ActionResult(ok=True, message=msg, kind="skills_open")

    # Notebook / calculator / daily-WhatsApp-note commands. Checked early so
    # "... whatsapp 85079... send karna every 11:59:58 PM" sets the schedule
    # instead of sending a WhatsApp message right away, and so the answer to
    # "what did you do today?" is saved to the note.
    from . import tools_voice

    tool = tools_voice.handle(original, hi=hi)
    if tool is not None:
        emit("notebook" if tool["kind"] == "notebook_open" else "tools", tool["message"])
        speak(tool["message"], lang=lang)
        return ActionResult(ok=tool["ok"], message=tool["message"], kind=tool["kind"], data=tool["data"])

    # Blue Star AC: "ac chalao", "ac 24 pe karo", "ac kholo".
    ac_result = _ac_command(original, hi, web_ui)
    if ac_result is not None:
        if not web_ui:
            speak(ac_result.message, lang=lang)
        return ac_result

    # Games. An in-progress guessing game must intercept the next input first
    # (a bare number like "50" matches no other keyword and would otherwise fall
    # through to the AI brain). Start/dice/coin triggers come before greetings.
    from . import games

    if games.is_game_active():
        ok, msg = games.play_number_game(original, lang=lang)
        emit("game", msg)
        speak(msg, lang=lang)
        return ActionResult(ok=ok, message=msg)

    if games.is_quiz_active():
        ok, msg = games.play_quiz(original, lang=lang)
        emit("game", msg)
        speak(msg, lang=lang)
        return ActionResult(ok=ok, message=msg)

    if games.is_ttt_active():
        ok, msg = games.play_tic_tac_toe(original, lang=lang)
        emit("game", msg)
        speak(msg.split("\n")[0], lang=lang)  # read status only, not the board
        return ActionResult(ok=ok, message=msg)

    if games.is_game_start(original):
        msg = games.start_number_game(lang=lang)
        emit("game", msg)
        speak(msg, lang=lang)
        return ActionResult(ok=True, message=msg)

    if games.is_quiz_start(original):
        msg = games.start_quiz(lang=lang)
        emit("game", msg)
        speak(msg, lang=lang)
        return ActionResult(ok=True, message=msg)

    if games.is_ttt_start(original):
        msg = games.start_tic_tac_toe(lang=lang)
        emit("game", msg)
        speak(msg.split("\n")[0], lang=lang)  # read intro only, not the board
        return ActionResult(ok=True, message=msg)

    if games.is_dice(original):
        ok, msg = games.roll_dice(lang=lang)
        emit("game", msg)
        speak(msg, lang=lang)
        return ActionResult(ok=ok, message=msg)

    if games.is_coin(original):
        ok, msg = games.flip_coin(lang=lang)
        emit("game", msg)
        speak(msg, lang=lang)
        return ActionResult(ok=ok, message=msg)

    if games.is_rps(original):
        ok, msg = games.play_rps(original, lang=lang)
        emit("game", msg)
        speak(msg, lang=lang)
        return ActionResult(ok=ok, message=msg)

    # "meri location kya hai" / "main kahan hoon": the place the dashboard HUD
    # shows (browser GPS), else the internet connection's city.
    from . import geo

    if geo.is_location_question(original):
        from .server import _netinfo

        ok, msg = geo.location_reply(hi=hi, ip_info=_netinfo())
        emit("location", msg)
        speak(msg, lang=lang)
        return ActionResult(ok=ok, message=msg)

    # Ask the trained intent model whether this is just conversation. Questions
    # that merely mention an action word ("exit code kya hai", "shutdown kaise
    # karte hain") skip the action branches below and go to the AI brain.
    # Live-data branches (news/time/weather) stay keyword-driven because the AI
    # has no real-time data.
    from .intent import is_chat

    chatty = is_chat(original)

    cam_kws = ("cam", "camera", "camra", "कैमरा", "کیمرہ")
    open_kws = ("open", "start", "on", "kholo", "chalu", "ओपन", "खोल", "चालू", "स्टार्ट", "کھول", "شروع")
    close_kws = ("close", "stop", "off", "band", "बंद", "क्लोज", "स्टॉप", "بند", "روک")

    is_cam = has(*cam_kws) and not chatty

    if is_cam and has(*open_kws):
        if web_ui:
            # The web dashboard opens the camera in its own video panel (with
            # the browser's permission) instead of a separate OpenCV window.
            res_msg = "कैमरा डैशबोर्ड में खोल रही हूँ।" if hi else "Opening the camera in the dashboard."
            emit("camera", res_msg)
            return ActionResult(ok=True, message=res_msg, kind="camera_open")
        res_msg = "कैमरा खोल रही हूँ..." if hi else "Opening camera..."
        speak(res_msg, lang=lang)
        return open_camera(max_seconds=camera_seconds)

    if is_cam and has(*close_kws):
        _stop_camera_event.set()
        res_msg = "कैमरा बंद कर दिया है।" if hi else "Camera stop signal sent."
        speak(res_msg, lang=lang)
        return ActionResult(ok=True, message=res_msg, kind="camera_close" if web_ui else "task")

    # FM radio ("radio chalao", "radio mirchi lagao", "radio band karo"): the
    # web dashboard plays it in its radio window.
    from . import radio

    radio_cmd = radio.parse_command(original) if web_ui and not chatty else None
    if radio_cmd:
        q = radio_cmd["query"]
        if radio_cmd["action"] == "stop":
            msg = "रेडियो बंद कर दिया है।" if hi else "The radio has been stopped."
        elif radio_cmd["action"] == "play":
            msg = f"रेडियो पर {q} लगा रही हूँ।" if hi else f"Tuning the radio to {q}."
        else:
            msg = "एफएम रेडियो खोल दिया है। कोई स्टेशन चुनिए।" if hi else "The FM radio is open. Please pick a station."
        emit("radio", msg)
        return ActionResult(ok=True, message=msg, kind="radio", data=radio_cmd)

    # Music ("love song chalao", "play romantic songs"). The web dashboard plays
    # it in its AI output panel; the CLI opens it in the default browser.
    from . import media

    if media.is_music_stop(original):
        stopped = media.wants_menu(original)
        if stopped == "quran":
            msg = "तिलावत बंद कर दी है।" if hi else "The Quran recitation has been stopped."
        elif stopped == "naat":
            msg = "नात बंद कर दी है।" if hi else "The naat has been stopped."
        else:
            msg = "गाना बंद कर दिया है।" if hi else "The song has been stopped."
        emit("media", msg)
        if not web_ui:
            speak(msg, lang=lang)
        return ActionResult(ok=True, message=msg, kind="media_stop" if web_ui else "task")

    # A specific playlist ("90s songs chalao") plays at once; a general request
    # ("gaana chalao", "love song") offers the menu on the web (the CLI, which
    # can't show one, plays the first playlist).
    song = media.find_song(original)
    category = media.wants_menu(original) if song is None else None
    if category:
        items = media.CATEGORIES[category]
        if web_ui:
            names = "\n".join(f"{i}. {s['title']}" for i, s in enumerate(items, 1))
            what_hi, what_en = {
                "quran": ("सूरह की तिलावत", "Quran recitations"),
                "naat": ("नात", "naats"),
            }.get(category, ("प्लेलिस्ट", "playlists"))
            msg = (
                f"मेरे पास {len(items)} {what_hi} हैं। कृपया कोई एक चुनें:\n{names}" if hi
                else f"I have {len(items)} {what_en} ready. Please select any one:\n{names}"
            )
            emit("media", msg)
            return ActionResult(ok=True, message=msg, kind="media_menu", data={
                "category": category, "songs": [media.payload(s) for s in items],
            })
        song = items[0]
    if song:
        if song.get("surah"):
            msg = (f"{song['title']} की तिलावत चला रही हूँ।" if hi
                   else f"Playing the recitation of {song['title']}.")
        else:
            msg = (
                f"आपके लिए चला रही हूँ: {song['title']}" if hi
                else f"Playing {song['title']} for you."
            )
        emit("media", msg)
        if web_ui:
            return ActionResult(ok=True, message=msg, kind="media_play", data=media.payload(song))
        speak(msg, lang=lang)
        webbrowser.open(media.watch_url(song))
        return ActionResult(ok=True, message=msg, kind="task")

    # Bluetooth: open the screen, radio on/off, connect "realme buds".
    bt_result = _bluetooth_command(original, hi, web_ui)
    if bt_result is not None:
        return bt_result

    # Web commands from the dashboard: YouTube videos, Google, maps,
    # Wikipedia, websites, plus close / full-screen for the big panel. Web UI
    # only — the CLI keeps opening things via the branches below.
    if web_ui:
        from . import web

        web_req = web.parse(original)
        if web_req:
            return _web_result(web_req, hi)

    # System power controls. Sleep and lock are reversible so they run at once;
    # restart and shutdown arm a one-step confirmation (answered at the top of
    # the next command) and may be scheduled for later ("shutdown after 2
    # hours"). Checked after the camera block so "camera off" still controls the
    # camera, not the machine.
    if not is_cam and not chatty:
        # Call off a scheduled power-off first, so "cancel shutdown" / "restart
        # roko" isn't mistaken for a fresh shutdown/restart request.
        if system.is_power_cancel(original):
            ok, msg = system.cancel_scheduled(lang=lang)
            emit("system", msg)
            speak(msg, lang=lang)
            return ActionResult(ok=ok, message=msg, kind="task")

        # Timed sleep / wake ("raat 11 baje sleep", "subah 6 baje system on kar dena").
        timer_result = _power_timer_command(original, hi, web_ui)
        if timer_result is not None:
            return timer_result

        if system.is_sleep(original):
            msg = "सिस्टम को स्लीप पर भेज रही हूँ..." if hi else "Putting the system to sleep..."
            emit("system", msg)
            speak(msg, lang=lang)
            system.do_sleep()
            return ActionResult(ok=True, message=msg, kind="task")

        if system.is_lock(original):
            msg = "सिस्टम लॉक कर रही हूँ..." if hi else "Locking the system..."
            emit("system", msg)
            speak(msg, lang=lang)
            system.do_lock()
            return ActionResult(ok=True, message=msg, kind="task")

        if system.is_restart(original):
            delay = system.parse_delay_seconds(original)
            msg = system.request_power("restart", delay=delay, lang=lang)
            emit("system", msg)
            speak(msg, lang=lang)
            return ActionResult(ok=True, message=msg)

        if system.is_shutdown(original):
            delay = system.parse_delay_seconds(original)
            msg = system.request_power("shutdown", delay=delay, lang=lang)
            emit("system", msg)
            speak(msg, lang=lang)
            return ActionResult(ok=True, message=msg)

    # Full-screen binary-rain ("Matrix") animation of 0s and 1s.
    anim_kws = ("animation", "matrix", "binary rain", "matrix rain",
                "binary animation", "एनिमेशन", "एनीमेशन", "मैट्रिक्स")
    if not chatty and has(*anim_kws):
        from . import animation

        msg = (
            "फुल-स्क्रीन एनिमेशन चला रही हूँ। बंद करने के लिए Esc दबाइए।"
            if hi else
            "Starting the full-screen animation. Press Esc to close it."
        )
        # Rain the letters of SARA by default; use 0/1 if the user asks for binary.
        charset = "01" if has("binary", "zero one", "0 1", "digits", "bit") else "SARA"
        # Launch in a separate process: Tkinter needs the main thread, and the web
        # server dispatches on worker threads, so an in-process window would fail
        # or block the request. A fresh process also always runs the latest code.
        ok = animation.launch_matrix_rain(charset=charset)
        if not ok:
            fail = (
                "एनिमेशन नहीं चला पाई।" if hi
                else "Couldn't start the animation."
            )
            return ActionResult(ok=False, message=fail, kind="task")
        emit("animation", msg)
        speak(msg, lang=lang)
        return ActionResult(ok=True, message=msg, kind="task")

    # Close a running app by name ("postman band karo", "close chrome"). After
    # the system block so "system band karo" powers off instead of matching here.
    from . import apps as _apps

    if not chatty and has(*_apps.APP_CLOSE_VERBS):
        close_name = _apps.extract_app_name(original, _apps.APP_CLOSE_VERBS)
        if close_name:
            ok, label = _apps.close_by_name(close_name)
            if ok:
                msg = f"{label} बंद कर दी।" if hi else f"Closed {label}."
                emit("app", msg)
                speak(msg, lang=lang)
                return ActionResult(ok=True, message=msg, kind="task")
            fail = (
                f"{label} बंद नहीं कर पाई — शायद यह चल नहीं रही।" if hi
                else f"Couldn't close {label} — it may not be running."
            )
            emit("app", fail)
            speak(fail, lang=lang)
            return ActionResult(ok=False, message=fail, kind="task")

    # Email: Claude drafts it, the user confirms, then it is sent over SMTP.
    # Needs a send-style verb or an address, so "gmail kholo" still opens Gmail.
    email_verbs = ("bhejo", "bhej", "send", "karo", "kar", "likho", "write",
                   "draft", "भेजो", "करो", "लिखो")
    if not chatty and mailer.is_email_request(original) and (
        has(*email_verbs) or mailer.find_recipient(original)[0]
    ):
        ok, msg = mailer.start_email(original, lang=lang)
        emit("email", msg)
        speak(msg.split("\n\n")[-1] if ok else msg, lang=lang)  # read the question
        return ActionResult(ok=ok, message=msg)

    # WhatsApp: open the app, or send a message to a number/contact. Checked
    # before greetings so a message body containing "hello" isn't mistaken for a
    # greeting. Triggers on "whatsapp ...", or "message/bhejo ..." with a number.
    wa_kws = ("whatsapp", "whatsap", "watsapp", "watsap", "वाट्सएप", "व्हाट्सएप")
    msg_kws = ("message", "msg", "msge", "text", "bhejo", "bhej", "sms")
    if not chatty and (
        has(*wa_kws) or (has(*msg_kws) and re.search(r"\d[\d\s-]{6,}\d", original))
    ):
        from .whatsapp import handle_whatsapp

        ok, msg = handle_whatsapp(original, hi=hi, lang=lang)
        emit("whatsapp", msg)
        speak(msg, lang=lang)
        return ActionResult(ok=ok, message=msg, kind="task")

    greet_kws = ("hello", "hi", "hey", "namaste", "नमस्ते", "हेलो", "हाय", "salam", "سلام")
    if not is_cam and not chatty and has(*greet_kws):
        msg = "नमस्ते! मैं सारा हूँ, आपकी एआई असिस्टेंट।" if hi else "Hello! I am Sara, your AI assistant."
        speak(msg, lang=lang)
        return ActionResult(ok=True, message=msg)

    if has("name is", "mera naam", "मेरा नाम", "میرا نام"):
        msg = "आपसे मिलकर खुशी हुई! मैं सारा हूँ। मैं आपकी कैसे मदद कर सकती हूँ?" if hi else "Nice to meet you! I am Sara. How can I help you today?"
        speak(msg, lang=lang)
        return ActionResult(ok=True, message=msg)

    identity_kws = (
        "who are you", "your name", "info", "about you",
        "tum kaun", "aap kaun", "तुम कौन", "आप कौन", "تم کون", "tumhara naam",
        "tum kon ho", "tumhara name kya hai", "tumhara naam kya hai",
        "aap kon ho", "aapka naam kya hai", "apna naam batao",
    )
    if has(*identity_kws):
        msg = "मैं सारा हूँ, एक शक्तिशाली एआई असिस्टेंट जो आपके सिस्टम और कार्यों को मैनेज करती है।" if hi else "I am Sara, a high-performance AI assistant built to manage your system and tasks."
        speak(msg, lang=lang)
        return ActionResult(ok=True, message=msg)

    if has("welcome", "swagat", "स्वागत", "خوش آمدید"):
        msg = "टर्मिनल में आपका स्वागत है! मैं सारा हूँ। सिस्टम ऑनलाइन हैं।" if hi else "Welcome to the Terminal! I am Sara. Systems are online."
        speak(msg, lang=lang)
        return ActionResult(ok=True, message=msg)

    youtube_kws = ("open youtube", "start youtube", "play youtube", "youtube kholo", "youtube chalu")
    if not chatty and has(*youtube_kws):
        msg = "आपके लिए यूट्यूब खोल रही हूँ..." if hi else "Opening YouTube for you..."
        speak(msg, lang=lang)
        webbrowser.open("https://www.youtube.com")
        return ActionResult(ok=True, message=msg, kind="task")

    # 0. List a folder's contents ("D drive ke folders dikhao", "list Desktop").
    list_verbs = ("list", "dikhao", "dikha", "contents", "kya hai", "kya h")
    fs_nouns = ("folder", "folders", "file", "files", "drive", "directory", "फ़ोल्डर", "फाइल")
    if not chatty and has(*list_verbs):
        from .filesystem import list_dir, resolve_location

        if resolve_location(original)[0] is not None or has(*fs_nouns):
            ok, msg = list_dir(original, lang=lang)
            emit("files", msg)
            speak(msg.split("\n")[0], lang=lang)  # read just the summary line aloud
            return ActionResult(ok=ok, message=msg)

    # 1 & 2. Create a folder / file. Flexible phrasing: a folder/file word plus
    # any create verb ("banao", "bana do", "banado", "create", "make", "new"...).
    folder_nouns = ("folder", "folders", "directory", "फ़ोल्डर", "फोल्डर")
    file_nouns = ("file", "फ़ाइल", "फाइल")
    create_verbs = (
        "create", "make", "new", "naya", "banao", "banaao", "banado", "banaado",
        "bana", "bna", "banade", "banaye", "banaiye", "banana",
        "बनाओ", "बना", "बनाना", "बनाइए", "नया",
    )
    if not chatty and has(*create_verbs) and has(*folder_nouns, *file_nouns):
        from .filesystem import create_entry, parse_create

        is_file = has(*file_nouns) and not has(*folder_nouns)
        strip_kws = folder_nouns + file_nouns + create_verbs
        location, name = parse_create(original, strip_kws)
        if name:  # create at the named location, or the Desktop by default
            ok, msg = create_entry(name, location, is_file=is_file, lang=lang)
            emit("files", msg)
            speak(msg, lang=lang)
            return ActionResult(ok=ok, message=msg, kind="task")
        # No name given -> prompt for it (GUI on the CLI, inline ask on the web).
        fallback_kws = file_nouns if is_file else folder_nouns
        return _create_fs_entry(
            original, kws=fallback_kws, hi=hi, lang=lang, is_file=is_file
        )

    # 3. Open Notepad++
    if not chatty and has("open notepad++", "start notepad++", "notepad++ kholo", "open notepad"):
        msg = "नोटपैड प्लस प्लस खोल रही हूँ..." if hi else "Opening Notepad plus plus..."
        speak(msg, lang=lang)
        _launch("Notepad++", msg, lang, hi, candidates=[], fallback="start notepad++")
        return ActionResult(ok=True, message=msg, kind="task")

    # 4. Open IntelliJ IDEA
    if not chatty and has("open intellij", "start intellij", "open idea"):
        msg = "इंटेलिजे आइडिया खोल रही हूँ..." if hi else "Opening IntelliJ IDEA..."
        speak(msg, lang=lang)
        user_profile = os.path.expanduser("~")
        idea_patterns = [
            r"C:\Program Files\JetBrains\IntelliJ IDEA*\bin\idea64.exe",
            r"C:\Program Files (x86)\JetBrains\IntelliJ IDEA*\bin\idea64.exe",
            rf"{user_profile}\AppData\Local\Programs\IntelliJ IDEA*\bin\idea64.exe",
            rf"{user_profile}\AppData\Local\JetBrains\Toolbox\apps\IDEA-U\ch-0\*\bin\idea64.exe",
            rf"{user_profile}\AppData\Local\JetBrains\Toolbox\apps\IDEA-C\ch-0\*\bin\idea64.exe",
        ]
        matches = [m for pattern in idea_patterns for m in glob.glob(pattern)]
        candidates = [sorted(matches)[-1]] if matches else []
        _launch("IntelliJ IDEA", msg, lang, hi, candidates=candidates, fallback="start idea64")
        return ActionResult(ok=True, message=msg, kind="task")

    # 5. Open OpenVPN Connect
    if not chatty and has("open openvpn", "start openvpn", "openvpn connect", "open vpn", "open the vpn"):
        msg = "ओपन वीपीएन कनेक्ट खोल रही हूँ..." if hi else "Opening Open VPN Connect..."
        speak(msg, lang=lang)
        openvpn_path = r"C:\Program Files\OpenVPN Connect\OpenVPNConnect.exe"
        _launch("OpenVPN Connect", msg, lang, hi, candidates=[openvpn_path], fallback="start openvpn-gui")
        return ActionResult(ok=True, message=msg, kind="task")

    # 6. Open any known desktop app by name (Postman, Chrome, VS Code, VLC...).
    # Checked after the specific launchers above so their exact phrasing wins,
    # and only when an "open"-style verb is present so a passing mention of an
    # app name doesn't launch it.
    if not chatty and has(*open_kws):
        from . import apps

        app = apps.find_app(original)
        if app:
            label = app["label"]
            msg = (
                f"{label} खोल रही हूँ..." if hi else f"Opening {label}..."
            )
            speak(msg, lang=lang)
            if apps.launch_app(app):
                emit("app", msg)
                return ActionResult(ok=True, message=msg, kind="task")
            fail = (
                f"माफ़ कीजिए, {label} खोल नहीं पाई। शायद यह इंस्टॉल नहीं है।"
                if hi else
                f"Sorry, I couldn't open {label}. It may not be installed."
            )
            emit("error", fail)
            speak(fail, lang=lang)
            return ActionResult(ok=False, message=fail, kind="task")

    # The LLM brain has no real-time data, so handle live-data intents (news,
    # time, weather) here before falling back to it.

    # Latest news headlines via Google News RSS (free, no key).
    news_kws = ("news", "khabar", "khabren", "khabrein", "khabaren", "samachar",
                "samachaar", "headlines", "समाचार", "खबर", "खबरें", "اخبار")
    if has(*news_kws):
        from .news import get_news, headlines

        topic = _extract_subject(original, news_kws)
        ok, msg = get_news(topic, lang=lang)
        emit("news", msg)
        if web_ui and ok:
            # The dashboard shows a full news board in its big panel.
            try:
                items = headlines(topic, lang=lang, count=12)
            except Exception:  # noqa: BLE001
                items = []
            if items:
                return ActionResult(ok=True, message=msg, kind="stage", data={
                    "type": "news", "topic": topic, "lang": lang, "items": items,
                })
        speak(msg, lang=lang)
        return ActionResult(ok=ok, message=msg)

    # Live stock-market prices via Yahoo Finance (free, no key).
    from .stocks import STOCK_KWS, get_stocks

    if not chatty and has(*STOCK_KWS):
        ok, msg = get_stocks(original, lang=lang)
        emit("stocks", msg)
        speak(msg.split("\n")[0], lang=lang)
        return ActionResult(ok=ok, message=msg)

    # Current date/time, optionally for a specific city's timezone.
    time_kws = ("time", "samay", "waqt", "baje", "clock", "date", "tareekh",
                "din", "समय", "तारीख", "बजे", "وقت")
    if has(*time_kws):
        from .datetime_info import get_datetime

        city = _extract_subject(original, time_kws)
        ok, msg = get_datetime(city, lang=lang)
        emit("time", msg)
        speak(msg, lang=lang)
        return ActionResult(ok=ok, message=msg)

    # Live weather via Open-Meteo (free, no API key).
    weather_kws = ("weather", "mausam", "mosam", "mausham", "मौसम", "موسم", "तापमान")
    if has(*weather_kws):
        from .weather import get_weather, wants_all_cities

        all_cities = wants_all_cities(original)
        city = "" if all_cities else _extract_subject(original, weather_kws, keep_commas=True)
        ok, msg = get_weather(city, lang=lang, all_cities=all_cities)
        emit("weather", msg)
        speak("\n".join(msg.split("\n")[:3]), lang=lang)  # read the first few cities
        return ActionResult(ok=ok, message=msg)

    # 7. Generic launcher: open ANY installed app by its Start Menu shortcut
    # (e.g. "requestly open karo", "open mongodb compass"). Uses narrower launch
    # verbs than the known-app branch above so ordinary questions containing
    # "start"/"open" aren't hijacked; if no app matches, falls through to the AI.
    from . import apps

    if not chatty and has(*apps.APP_OPEN_VERBS):
        name = apps.extract_app_name(original, apps.APP_OPEN_VERBS)
        if name:
            ok, label = apps.open_by_name(name)
            if ok:
                msg = f"{label} खोल रही हूँ..." if hi else f"Opening {label}..."
                emit("app", msg)
                speak(msg, lang=lang)
                return ActionResult(ok=True, message=msg, kind="task")

    # Catch-all: hand off to the real AI brain (Claude).
    from .ai import ask_ai

    res = ask_ai(original, lang=lang)
    emit("ai_task", res.text)
    if res.ok:
        speak(res.text, lang=lang)
    return ActionResult(ok=res.ok, message=res.text)


def _power_timer_command(original: str, hi: bool, web_ui: bool = False) -> "ActionResult | None":
    """Schedule / cancel a timed sleep or wake. None when the text isn't one."""
    import re as _re
    from datetime import datetime, timedelta

    from . import power_timer as pt
    from . import system

    lang = "hi" if hi else "en"

    def notify(msg_hi: str, msg_en: str) -> None:
        msg = msg_hi if hi else msg_en
        emit("power", msg)          # the web UI speaks "power" events itself
        if not web_ui:
            speak(msg, lang=lang)

    which = pt.is_timer_cancel(original)
    if which and (pt.status()["sleep_at"] or pt.status()["wake_at"] or pt.is_sleep_word(original)
                  or pt.is_wake_request(original) or "timer" in original.lower()):
        parts = []
        if which in ("sleep", "all") and pt.cancel_sleep():
            parts.append(("स्लीप", "sleep"))
        if which in ("wake", "all") and pt.cancel_wake():
            parts.append(("जगाने", "wake-up"))
        if parts:
            msg = (("ठीक है, " + " और ".join(p[0] for p in parts) + " का टाइमर रद्द कर दिया।") if hi
                   else "Okay, cancelled the " + " and ".join(p[1] for p in parts) + " timer.")
        else:
            msg = "कोई टाइमर सेट नहीं था।" if hi else "There was no timer set."
        emit("system", msg)
        return ActionResult(ok=True, message=msg, kind="task")

    replies: list[str] = []
    # "raat 11 baje sleep aur subah 6 baje on" -> handle each half.
    for part in _re.split(r"\s(?:aur|and|phir|then|और|फिर)\s|,", original):
        wants_wake = pt.is_wake_request(part)
        wants_sleep = not wants_wake and (system.is_sleep(part) or pt.is_sleep_word(part))
        if not (wants_wake or wants_sleep):
            continue
        at = pt.parse_clock(part)
        if at is None:
            delay = system.parse_delay_seconds(part)
            at = datetime.now() + timedelta(seconds=delay) if delay else None
        if at is None:
            if wants_wake:
                replies.append("किस समय जगाऊँ? जैसे: 'subah 6 baje system on kar dena'।" if hi
                               else "When should I wake it? For example: 'wake the system at 6 am'.")
            continue                 # a plain "system sleep karo" is handled by the caller
        when = pt.fmt_time(at, lang)
        if wants_sleep:
            pt.schedule_sleep(at, notify)
            replies.append(f"{when} सिस्टम स्लीप कर दूँगी। रोकने के लिए 'sleep cancel karo' कहिए।" if hi
                           else f"I'll put the system to sleep {when}. Say 'cancel sleep' to stop it.")
        else:
            if not pt.schedule_wake(at, notify):
                replies.append("माफ़ कीजिए, Windows ने जगाने का टाइमर सेट नहीं होने दिया।" if hi
                               else "Sorry, Windows did not allow the wake-up timer.")
                continue
            allowed = pt.wake_timers_allowed()
            note = ""
            if allowed.get("ac") is False or allowed.get("dc") is False:
                on = "चार्जर पर" if allowed.get("ac") else "बैटरी पर"
                note = (f" ध्यान दें: पावर सेटिंग में wake timers सिर्फ़ {on} चालू हैं।" if hi else
                        f" Note: wake timers are only allowed {'on AC power' if allowed.get('ac') else 'on battery'} in the power plan.")
            replies.append((f"{when} सिस्टम जगा दूँगी (सिर्फ़ स्लीप से, बंद सिस्टम से नहीं)।" if hi
                            else f"I'll wake the system {when} (from sleep, not from shutdown).") + note)
    if not replies:
        return None
    msg = " ".join(replies)
    emit("system", msg)
    return ActionResult(ok=True, message=msg, kind="task")
