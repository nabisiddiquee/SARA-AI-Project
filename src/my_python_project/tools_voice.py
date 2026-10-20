"""Voice commands for the Notebook and the Calculator (Hinglish / Hindi / English).

Examples
--------
* "Sara notebook open karo"                       -> opens the Notebook window
* "notebook me likho: client call done"           -> appends to today's note
* "aaj jo kaam kiye hai woh save kar lo"          -> asks what you did; the next
                                                     thing you say is saved
* "whatsapp 8507979430 send karna every 11:59:58 PM"
                                                  -> every day at that time,
                                                     today's note is sent there
* "WhatsApp schedule band karo"                   -> cancels the daily send
* "calculator kholo" / "12 plus 5 kitna hai"      -> opens / answers in the Calculator

``handle(text)`` returns None when the text isn't a notebook / calculator command.
"""

from __future__ import annotations

import re
from datetime import datetime

from . import notes, schedule

_NOTE_KWS = ("notebook", "note book", "notbook", "नोटबुक", "नोट बुक", "notes", "note", "नोट")
_CALC_KWS = ("calculator", "calculater", "calculate", "calc", "कैलकुलेटर", "hisab", "हिसाब")
_OPEN_KWS = ("open", "kholo", "khol", "kholna", "chalu", "start", "dikhao", "खोलो", "खोल", "ओपन", "दिखाओ")
_SAVE_KWS = ("save", "likho", "likh", "likh do", "add", "jod", "note karo", "note kar", "सेव", "लिखो", "लिख")
_WORK_KWS = ("aaj", "today", "kaam", "kam", "work", "आज", "काम")
_CANCEL_KWS = ("cancel", "band", "hatao", "stop", "delete", "बंद", "हटाओ", "रद्द")
_SCHED_KWS = ("every", "daily", "roz", "har din", "rozana", "रोज़", "रोज", "हर दिन", "schedule")
_WA_KWS = ("whatsapp", "whatsap", "watsapp", "watsap", "वाट्सएप", "व्हाट्सएप")

_TIME_RE = re.compile(r"\b(\d{1,2})[:.](\d{2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", re.I)
_PHONE_RE = re.compile(r"\+?\d[\d\s-]{8,}\d")

# Waiting for the user to dictate "what I did today" after asking for it.
_dictation_pending = False


def _has(text: str, kws) -> bool:
    low = text.lower()
    return any(re.search(r"(?<![a-z])" + re.escape(k) + r"(?![a-z])", low) for k in kws)


def _today_note() -> str:
    return notes.default_name()


def _append_today(entry: str) -> str:
    """Append a time-stamped line to today's note (created if missing)."""
    name = _today_note()
    cur = notes.read(name)
    text = cur["text"] if cur.get("ok") else ""
    stamp = datetime.now().strftime("%I:%M %p").lstrip("0")
    line = f"[{stamp}] {entry.strip()}"
    text = (text.rstrip() + "\n" + line).lstrip("\n") if text.strip() else line
    notes.save(name, text + "\n", old_name=name if cur.get("ok") else None, is_new=not cur.get("ok"))
    return name


def _dictation_text(text: str) -> str:
    """Content after "likho / save kar lo / :" ... or "" if none was given."""
    m = re.search(r"(?::|\b(?:likho|likh do|likh lo|write|note karo|add karo|save karo|save kar lo|save kr lo)\b)\s*(.+)$",
                  text, re.I)
    if not m:
        return ""
    body = m.group(1).strip(" .,:;-")
    # "save kar lo tum" / "likho na" carry no real content.
    if len(body.split()) < 2 or re.fullmatch(r"(tum|aap|na|please|pls|do|dena|isko|ise)[\s.!]*", body, re.I):
        return ""
    return body


def _parse_time(text: str) -> str | None:
    m = _TIME_RE.search(text)
    if not m:
        return None
    h, mi, s, ap = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0), (m.group(4) or "").lower()
    if ap.startswith("p") and h < 12:
        h += 12
    if ap.startswith("a") and h == 12:
        h = 0
    if not (0 <= h < 24 and 0 <= mi < 60 and 0 <= s < 60):
        return None
    return f"{h:02d}:{mi:02d}:{s:02d}"


def _pretty_time(hms: str) -> str:
    return datetime.strptime(hms, "%H:%M:%S").strftime("%I:%M:%S %p").lstrip("0")


# --- calculator ------------------------------------------------------------------

_OPS = [
    (r"\b(?:divided by|divide by|divide|bata|bhag|भाग)\b|÷", "/"),
    (r"\b(?:multiplied by|multiply by|multiply|times|into|guna|गुणा)\b|×", "*"),
    (r"\b(?:plus|add|jod|jodo|aur|और|प्लस)\b", "+"),
    (r"\b(?:minus|subtract|ghata|ghatao|घटा|माइनस)\b|−", "-"),
    (r"\b(?:percent|pratishat|प्रतिशत)\b", "%"),
]


def _to_expr(text: str) -> str:
    s = text.lower()
    for pat, op in _OPS:
        s = re.sub(pat, f" {op} ", s)
    s = s.replace("x", "*") if re.search(r"\d\s*x\s*\d", s) else s
    expr = "".join(re.findall(r"[\d.]+|[+\-*/%()]", s))
    return expr if re.search(r"\d", expr) and re.search(r"[+\-*/%]", expr) else ""


def _calc(expr: str) -> float:
    """Tiny safe evaluator: + - * / ( ) and postfix %."""
    pos = 0

    def peek():
        return expr[pos] if pos < len(expr) else ""

    def eat(c):
        nonlocal pos
        if peek() == c:
            pos += 1
            return True
        return False

    def number():
        nonlocal pos
        m = re.match(r"\d*\.?\d+", expr[pos:])
        if not m:
            raise ValueError("bad number")
        pos += len(m.group(0))
        return float(m.group(0))

    def factor():
        if eat("-"):
            return -factor()
        if eat("("):
            v = expression()
            eat(")")
        else:
            v = number()
        while eat("%"):
            v /= 100
        return v

    def term():
        v = factor()
        while True:
            if eat("*"):
                v *= factor()
            elif eat("/"):
                d = factor()
                if d == 0:
                    raise ZeroDivisionError
                v /= d
            else:
                return v

    def expression():
        v = term()
        while True:
            if eat("+"):
                v += term()
            elif eat("-"):
                v -= term()
            else:
                return v

    v = expression()
    if pos != len(expr):
        raise ValueError("bad expression")
    return round(v, 10)


def _fmt_num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


# --- entry point -----------------------------------------------------------------

def handle(text: str, *, hi: bool = False) -> dict | None:
    """Notebook / calculator / daily-WhatsApp commands. Returns
    {"ok", "message", "kind", "data"} or None if not ours."""
    global _dictation_pending
    t = text.strip()
    if not t:
        return None

    # 1. Answer to "what did you do today?" -> save it.
    if _dictation_pending:
        _dictation_pending = False
        if _has(t, ("cancel", "rehne do", "rahne do", "nahi", "no", "रहने दो", "नहीं")) and len(t.split()) <= 3:
            return {"ok": True, "kind": "chat", "data": None,
                    "message": "ठीक है, कुछ सेव नहीं किया।" if hi else "Okay, nothing was saved."}
        name = _append_today(t)
        return {"ok": True, "kind": "notebook_open", "data": {"name": name},
                "message": (f"सेव कर दिया, '{name}' में।" if hi else f"Saved to '{name}'.")}

    is_note = _has(t, _NOTE_KWS)
    is_wa_sched = _has(t, _WA_KWS) and _has(t, _SCHED_KWS) and bool(_TIME_RE.search(t))
    work_save = _has(t, _WORK_KWS) and _has(t, _SAVE_KWS)

    # 2. Cancel the daily WhatsApp send.
    if _has(t, _WA_KWS) and _has(t, ("schedule",)) and _has(t, _CANCEL_KWS):
        n = schedule.clear("whatsapp_note")
        msg = ("रोज़ का व्हाट्सएप शेड्यूल हटा दिया।" if hi else "The daily WhatsApp schedule is cancelled.") if n else \
              ("कोई शेड्यूल नहीं था।" if hi else "There was no schedule to cancel.")
        return {"ok": True, "kind": "task", "data": None, "message": msg}

    # 3. "notes whatsapp pe bhejo / send karo" -> send today's note right now.
    send_kws = ("send", "bhejo", "bhej", "bhejna", "bhej do", "share", "भेजो", "भेज")
    if (is_note or _has(t, _WORK_KWS)) and _has(t, _WA_KWS) and _has(t, send_kws) and not is_wa_sched:
        phone = _PHONE_RE.search(t)
        ok, msg = schedule.send_note(_today_note(), phone.group(0) if phone else "")
        if hi:
            msg = ("आज का नोट व्हाट्सएप पर भेज रही हूँ।" if ok else
                   "नोट नहीं भेजा: " + ("आज का नोट खाली है, पहले कुछ लिखिए।" if "empty" in msg else
                                       "व्हाट्सएप नंबर सेट नहीं है।" if "No WhatsApp number" in msg else msg))
        return {"ok": ok, "kind": "task", "data": None, "message": msg}

    if is_note or is_wa_sched or work_save:
        parts_en, parts_hi = [], []
        kind, data = "task", None

        if is_note or work_save:
            kind, data = "notebook_open", {"name": None}
            parts_en.append("The Notebook is open.")
            parts_hi.append("नोटबुक खोल दी है।")

        if work_save or (is_note and _has(t, _SAVE_KWS)):
            body = _dictation_text(t)
            if body and not is_wa_sched:
                name = _append_today(body)
                data = {"name": name}
                parts_en.append(f"Saved it to '{name}'.")
                parts_hi.append(f"'{name}' में सेव कर दिया।")
            else:
                _dictation_pending = True
                data = {"name": _today_note()}
                parts_en.append(f"Tell me what you worked on today, and I'll save it in '{_today_note()}'.")
                parts_hi.append(f"बताइए आज आपने क्या काम किया, मैं उसे '{_today_note()}' में सेव कर दूँगी।")

        if is_wa_sched:
            phone = _PHONE_RE.search(t)
            at = _parse_time(t)
            if phone and at:
                job = schedule.set_whatsapp_note(phone.group(0), at)
                parts_en.append(f"Every day at {_pretty_time(at)} I'll send that day's note on WhatsApp to {job['number_display']}.")
                parts_hi.append(f"हर रोज़ {_pretty_time(at)} पर उस दिन का नोट व्हाट्सएप से {job['number_display']} पर भेज दूँगी।")
            else:
                parts_en.append("I couldn't read the phone number or time for the WhatsApp schedule.")
                parts_hi.append("व्हाट्सएप शेड्यूल के लिए नंबर या समय समझ नहीं आया।")

        return {"ok": True, "kind": kind, "data": data, "message": " ".join(parts_hi if hi else parts_en)}

    # 4. Calculator.
    expr = _to_expr(t)
    asks_calc = _has(t, _CALC_KWS) or (expr and _has(t, ("kitna", "kitne", "kya hoga", "what is", "how much", "कितना", "कितने", "बराबर", "equals")))
    if asks_calc:
        if expr:
            try:
                v = _fmt_num(_calc(expr))
                pretty = expr.replace("*", " × ").replace("/", " ÷ ").replace("+", " + ").replace("-", " − ")
                return {"ok": True, "kind": "calc_open", "data": {"expr": expr, "result": v},
                        "message": (f"{pretty} = {v}" if not hi else f"{pretty} का जवाब है {v}")}
            except ZeroDivisionError:
                return {"ok": False, "kind": "calc_open", "data": {"expr": expr, "result": None},
                        "message": "शून्य से भाग नहीं हो सकता।" if hi else "You can't divide by zero."}
            except ValueError:
                pass
        if _has(t, _CALC_KWS):
            return {"ok": True, "kind": "calc_open", "data": None,
                    "message": "कैलकुलेटर खोल दिया है।" if hi else "The calculator is open."}
    return None
