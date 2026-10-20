"""Every 10 hours: zip the face snapshots in ``captures/``, email the zip, and
delete the snapshots once the email has gone out.

Uses the mailer's Gmail App Password (``SMTP_USER`` / ``SMTP_PASSWORD`` in
``.env``) and sends to ``CAPTURES_EMAIL_TO`` (default: ``SMTP_USER`` itself).
``CAPTURES_EMAIL_HOURS`` changes the interval; ``0`` turns it off.

Nothing is deleted unless its email was sent. Big batches are split into
several zips (Gmail refuses attachments over 25 MB). The ``captures`` folder
itself stays, since the dashboard keeps saving new snapshots into it.
"""

from __future__ import annotations

import io
import os
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

from .events import emit

_ROOT = Path(__file__).resolve().parents[2]
CAPTURES = _ROOT / "captures"
_STAMP = _ROOT / ".captures_mailed"   # time of the last run, survives restarts
MAX_ZIP = 20 * 1024 * 1024            # stay under Gmail's 25 MB limit
_CHECK_EVERY = 600.0                  # seconds between "is it time yet?" checks
_FRESH = 60.0                         # skip files still being written

_started = False
_lock = threading.Lock()


def _batches(files: list[Path]) -> list[list[Path]]:
    """Group files so each zip stays under MAX_ZIP (JPEGs barely compress)."""
    out: list[list[Path]] = [[]]
    size = 0
    for f in files:
        n = f.stat().st_size
        if out[-1] and size + n > MAX_ZIP:
            out.append([])
            size = 0
        out[-1].append(f)
        size += n
    return [b for b in out if b]


def _zip(files: list[Path]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f.relative_to(CAPTURES).as_posix())
    return buf.getvalue()


def send_now() -> tuple[bool, str]:
    """Zip, email, then delete what was sent. Returns ``(ok, message)``."""
    from .mailer import Draft, send

    to = os.environ.get("CAPTURES_EMAIL_TO", "").strip() or os.environ.get("SMTP_USER", "").strip()
    if not to:
        return False, "Captures email skipped: SMTP_USER is not set in .env."
    now = time.time()
    files = sorted(f for f in CAPTURES.rglob("*")
                   if f.is_file() and now - f.stat().st_mtime > _FRESH) if CAPTURES.is_dir() else []
    if not files:
        return True, "No new captures to email."
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    batches = _batches(files)
    sent = 0
    for i, batch in enumerate(batches, 1):
        part = f" (part {i} of {len(batches)})" if len(batches) > 1 else ""
        name = f"captures_{stamp}" + (f"_part{i}" if len(batches) > 1 else "") + ".zip"
        ok, err = send(Draft(to=to, subject=f"Sara captures {stamp}{part}",
                             body=f"{len(batch)} face snapshot(s) from Sara, zipped.{part}"),
                       attachments=((name, _zip(batch)),))
        if not ok:
            return False, f"Captures email failed, nothing deleted from that batch: {err}"
        for f in batch:
            f.unlink(missing_ok=True)
        sent += len(batch)
    # Remove the now-empty day folders; keep captures/ itself.
    for d in sorted((p for p in CAPTURES.rglob("*") if p.is_dir()), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass   # not empty (a new snapshot arrived meanwhile)
    return True, f"Emailed {sent} capture(s) to {to} and deleted them."


def _last_run() -> float:
    try:
        return float(_STAMP.read_text().strip())
    except (OSError, ValueError):
        return 0.0


def _loop(every: float) -> None:
    if not _last_run():
        _STAMP.write_text(str(time.time()))   # first start: count 10 h from now
    while True:
        if time.time() - _last_run() >= every:
            ok, msg = send_now()
            emit("captures", msg)
            if ok:
                _STAMP.write_text(str(time.time()))
        time.sleep(_CHECK_EVERY)


def start() -> None:
    """Start the timer once, when email is set up."""
    global _started
    try:
        hours = float(os.environ.get("CAPTURES_EMAIL_HOURS", "10"))
    except ValueError:
        hours = 10.0
    if hours <= 0 or not os.environ.get("SMTP_PASSWORD", "").strip():
        return
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, args=(hours * 3600,), name="sara-captures-mail", daemon=True).start()
