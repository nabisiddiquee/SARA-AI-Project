"""New-message alerts: Sara pings you when a Gmail email or a WhatsApp
message arrives.

Gmail is checked over IMAP with the same App Password the mailer uses
(``SMTP_USER`` / ``SMTP_PASSWORD`` in ``.env``), read-only, so nothing is
marked as read. Gmail -> Settings -> "Forwarding and POP/IMAP" -> IMAP must be
enabled.

WhatsApp is read from the Windows notification centre (the WhatsApp desktop app
must be installed and allowed to show notifications; Windows asks once for
"notification access"). Needs the ``winrt-Windows.UI.Notifications*`` packages.

Each new message raises an ``inbox`` event (JSON: source / sender / text) that
the dashboard speaks, plus a Windows toast for email (WhatsApp shows its own).
"""

from __future__ import annotations

import asyncio
import email
import imaplib
import json
import os
import re
import threading
import time
from email.header import decode_header, make_header
from email.utils import parseaddr

from .events import emit
from .notify import notify

GMAIL_EVERY = 3.0       # seconds between IMAP checks (one login, kept open)
WHATSAPP_EVERY = 4.0    # seconds between notification-centre checks

_started = False
_lock = threading.Lock()


def _header(raw: str) -> str:
    try:
        return str(make_header(decode_header(raw or ""))).strip()
    except Exception:  # noqa: BLE001
        return (raw or "").strip()


def _alert(source: str, sender: str, text: str) -> None:
    text = re.sub(r"\s+", " ", text or "").strip()[:160]
    emit("inbox", json.dumps({"source": source, "sender": sender, "text": text}, ensure_ascii=False))


# --- Gmail --------------------------------------------------------------------

def _gmail_new(m: imaplib.IMAP4_SSL, last_uid: int) -> tuple[int, list[tuple[str, str]]]:
    """UIDs above ``last_uid`` as (sender, subject); returns the new high UID."""
    m.select("INBOX", readonly=True)
    _, data = m.uid("search", None, f"UID {last_uid + 1}:*")
    uids = [int(u) for u in (data[0] or b"").split() if int(u) > last_uid]
    found = []
    for uid in uids:
        _, msg = m.uid("fetch", str(uid), "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
        raw = next((p[1] for p in msg if isinstance(p, tuple)), b"")
        hdr = email.message_from_bytes(raw)
        name, addr = parseaddr(_header(hdr.get("From", "")))
        found.append((name or addr, _header(hdr.get("Subject", "")) or "(no subject)"))
    return max(uids, default=last_uid), found


def _gmail_loop(user: str, password: str) -> None:
    # One login, then a check every few seconds on the same connection:
    # logging in that often would get the account rate-limited by Gmail.
    host = os.environ.get("IMAP_HOST", "imap.gmail.com")
    last_uid = -1   # -1 = first check: only remember where the inbox is
    while True:
        try:
            with imaplib.IMAP4_SSL(host, timeout=30) as m:
                m.login(user, password)
                while True:
                    if last_uid < 0:
                        m.select("INBOX", readonly=True)
                        _, data = m.uid("search", None, "ALL")
                        last_uid = max((int(u) for u in (data[0] or b"").split()), default=0)
                    else:
                        last_uid, found = _gmail_new(m, last_uid)
                        for sender, subject in found:
                            notify(f"New email: {sender}", subject)
                            _alert("email", sender, subject)
                    time.sleep(GMAIL_EVERY)
        except imaplib.IMAP4.abort:
            pass     # Gmail closed the connection (it does so now and then): reconnect
        except imaplib.IMAP4.error as e:
            emit("inbox", json.dumps({"source": "error", "sender": "Gmail", "text": str(e)}))
            return   # wrong password / IMAP disabled: retrying won't help
        except OSError:
            pass     # offline for now; try again shortly
        time.sleep(GMAIL_EVERY * 5)


# --- WhatsApp (Windows notification centre) -----------------------------------

async def _whatsapp_toasts() -> list[tuple[int, str, str]]:
    from winrt.windows.ui.notifications import KnownNotificationBindings, NotificationKinds
    from winrt.windows.ui.notifications.management import UserNotificationListener

    out = []
    for n in await UserNotificationListener.current.get_notifications_async(NotificationKinds.TOAST):
        app = n.app_info.display_info.display_name if n.app_info else ""
        if "whatsapp" not in app.lower():
            continue
        b = n.notification.visual.get_binding(KnownNotificationBindings.toast_generic)
        texts = [t.text for t in b.get_text_elements()] if b else []
        out.append((n.id, texts[0] if texts else "WhatsApp", " ".join(texts[1:])))
    return out


def _whatsapp_loop() -> None:
    try:
        from winrt.windows.ui.notifications.management import (
            UserNotificationListener, UserNotificationListenerAccessStatus)
    except ImportError:
        return
    allowed = asyncio.run(_request_access(UserNotificationListener))
    if allowed != UserNotificationListenerAccessStatus.ALLOWED:
        emit("inbox", json.dumps({"source": "error", "sender": "WhatsApp",
                                  "text": "Windows notification access is off"}))
        return
    seen: set[int] | None = None   # None = first check: don't announce old ones
    while True:
        try:
            toasts = asyncio.run(_whatsapp_toasts())
            if seen is not None:
                for nid, sender, text in toasts:
                    if nid not in seen:
                        _alert("whatsapp", sender, text)
            seen = {nid for nid, _, _ in toasts} | (seen or set())
        except Exception:  # noqa: BLE001
            pass
        time.sleep(WHATSAPP_EVERY)


async def _request_access(listener):
    return await listener.current.request_access_async()


def start() -> None:
    """Start the watchers once (Gmail only when its App Password is set)."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    if user and password and os.environ.get("SARA_EMAIL_ALERTS", "1") != "0":
        threading.Thread(target=_gmail_loop, args=(user, password),
                         name="sara-gmail-watch", daemon=True).start()
    if os.name == "nt" and os.environ.get("SARA_WHATSAPP_ALERTS", "1") != "0":
        threading.Thread(target=_whatsapp_loop, name="sara-whatsapp-watch", daemon=True).start()
