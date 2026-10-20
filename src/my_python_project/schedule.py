"""Daily scheduled jobs (currently: send today's note on WhatsApp).

Jobs live in ``.sara_schedule.json`` in the project root so they survive a
restart. A background thread checks twice a second and runs each job once per
day at its time (to the second). The PC must be on, unlocked and running
Sara at that moment; a job missed by more than 2 minutes waits for the next day.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

FILE = Path(__file__).resolve().parents[2] / ".sara_schedule.json"
GRACE_SECONDS = 120
_lock = threading.Lock()
_started = False


def _load() -> list[dict]:
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _store(jobs: list[dict]) -> None:
    FILE.write_text(json.dumps(jobs, indent=2), encoding="utf-8")


def jobs() -> list[dict]:
    with _lock:
        return _load()


def set_whatsapp_note(number: str, at: str) -> dict:
    """Send that day's note to ``number`` daily at ``at`` ("HH:MM:SS").
    Replaces any existing WhatsApp-note job."""
    from .whatsapp import _normalize_number

    num = _normalize_number(number)
    job = {
        "kind": "whatsapp_note", "number": num,
        "number_display": num[-10:] if len(num) > 10 else num,
        "time": at, "last_run": "",
    }
    with _lock:
        items = [j for j in _load() if j.get("kind") != "whatsapp_note"]
        items.append(job)
        _store(items)
    return job


def clear(kind: str) -> int:
    with _lock:
        items = _load()
        keep = [j for j in items if j.get("kind") != kind]
        _store(keep)
        return len(items) - len(keep)


def scheduled_number() -> str:
    """Number of the daily WhatsApp-note job ("" if none)."""
    job = next((j for j in jobs() if j.get("kind") == "whatsapp_note"), None)
    return job["number"] if job else ""


def send_note(name: str, number: str = "") -> tuple[bool, str]:
    """Send note ``name`` on WhatsApp now (to ``number``, else the scheduled one)."""
    from . import notes
    from .whatsapp import send_message

    number = number or scheduled_number()
    if not number:
        return False, "No WhatsApp number set. Add one, e.g. say 'whatsapp 98765 43210 send karna every 11:59 PM'."
    note = notes.read(name)
    if not note.get("ok"):
        return False, f"Note '{name}' not found."
    body = (note.get("text") or "").strip()
    if not body:
        return False, f"'{name}' is empty, so nothing was sent. Write something in it first."
    stamp = datetime.now().strftime("%d-%b-%Y %I:%M %p")
    return send_message(number, f"📒 {note['name']} ({stamp})\n\n{body}")


def _run_whatsapp_note(job: dict, emit) -> None:
    from . import notes

    name = notes.default_name()
    ok, msg = send_note(name, job["number"])
    emit("schedule", ("Daily WhatsApp: " + msg) if ok else ("Daily WhatsApp not sent: " + msg))


def start(emit) -> None:
    """Start the background scheduler (once)."""
    global _started
    if _started:
        return
    _started = True

    def loop() -> None:
        while True:
            try:
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                due = []
                with _lock:              # read, mark as run and save in one step
                    items = _load()
                    for j in items:
                        h, m, s = (int(x) for x in j.get("time", "23:59:58").split(":"))
                        target = now.replace(hour=h, minute=m, second=s, microsecond=0)
                        late = (now - target).total_seconds()
                        if j.get("last_run") != today and 0 <= late <= GRACE_SECONDS:
                            j["last_run"] = today
                            due.append(dict(j))
                    if due:
                        _store(items)
                for j in due:
                    if j.get("kind") == "whatsapp_note":
                        threading.Thread(target=_run_whatsapp_note, args=(j, emit), daemon=True).start()
            except Exception:  # noqa: BLE001 - keep the scheduler alive
                pass
            time.sleep(0.5)

    threading.Thread(target=loop, name="sara-schedule", daemon=True).start()
