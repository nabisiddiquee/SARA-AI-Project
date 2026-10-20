"""Send emails by voice: Claude writes the email, you confirm, Sara sends it.

    "alammaruti@gmail.com ko email bhejo kal meeting 5 baje hai"
    "boss ko email karo ki main kal chhutti par rahunga"   (needs email_contacts.json)

Sara drafts a subject and body with the AI brain, shows the draft, and sends it
only after you say "haan"/"yes" — anything else cancels. Sending uses SMTP with
your own account; for Gmail create an App Password (Google Account -> Security
-> 2-Step Verification -> App passwords) and put it in ``.env``:

    SMTP_USER=you@gmail.com
    SMTP_PASSWORD=abcd efgh ijkl mnop

Names are resolved from an optional ``email_contacts.json`` in the project
folder: ``{"boss": "boss@company.com", "didi": "didi@gmail.com"}``.
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTACTS_FILE = "email_contacts.json"

EMAIL_KWS = ("email", "e-mail", "mail", "gmail", "ईमेल", "मेल", "ई-मेल")

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

_YES = ("yes", "yeah", "yep", "haan", "han", "ha", "haanji", "ji", "send", "bhejo",
        "bhej do", "confirm", "ok", "okay", "sure", "bilkul", "हाँ", "हां", "भेजो", "भेज दो")
_NO = ("no", "nope", "nahi", "nhi", "mat", "cancel", "ruko", "rehne do",
       "नहीं", "मत", "रुको", "रहने दो")

# Words stripped from the request before it is handed to the AI as the brief.
_FILLER = (
    "email", "e-mail", "mail", "gmail", "bhejo", "bhej do", "bhejna", "bhej", "send",
    "karo", "kar do", "kardo", "likho", "likh do", "write", "an", "a", "to", "ko",
    "please", "ईमेल", "मेल", "भेजो", "करो",
)

_DRAFT_SYSTEM = (
    "You write short, polite emails for the user. Reply in exactly this format:\n"
    "Subject: <subject line>\n\n<email body>\n\n"
    "Write in the language the user wrote in (English, or Hindi/Hinglish in the "
    "same script they used). Keep it brief and natural. Sign off with the "
    "sender's name {sender!r}. No markdown, no placeholders like [Name]."
)


@dataclass
class Draft:
    to: str
    subject: str
    body: str


_pending: Draft | None = None


def _has(text: str, words) -> bool:
    padded = " " + " ".join(re.sub(r"[?!.,]", " ", text.lower()).split()) + " "
    return any((f" {w} " in padded) if w.isascii() else (w in text) for w in words)


def is_email_request(text: str) -> bool:
    return _has(text, EMAIL_KWS)


def email_pending() -> bool:
    return _pending is not None


def cancel_pending() -> None:
    global _pending
    _pending = None


def load_contacts() -> dict[str, str]:
    for path in (Path.cwd() / CONTACTS_FILE, _PROJECT_ROOT / CONTACTS_FILE):
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return {k.lower(): v for k, v in data.items() if "@" in str(v)}
            except (OSError, ValueError):
                return {}
    return {}


def find_recipient(text: str) -> tuple[str | None, str]:
    """Return ``(address, text_without_it)`` from an address or a saved name."""
    m = _EMAIL_RE.search(text)
    if m:
        return m.group(0), (text[:m.start()] + text[m.end():])
    for name, address in load_contacts().items():
        pat = re.compile(r"\b" + re.escape(name) + r"\b", re.IGNORECASE)
        if pat.search(text):
            return address, pat.sub(" ", text, count=1)
    return None, text


def _brief(text: str) -> str:
    out = text
    for w in sorted(_FILLER, key=len, reverse=True):
        pat = r"\b" + re.escape(w) + r"\b" if w.isascii() else re.escape(w)
        out = re.sub(pat, " ", out, flags=re.IGNORECASE)
    return " ".join(out.split()).strip(" ,.-:")


def _sender_name() -> str:
    user = os.environ.get("SMTP_USER", "")
    return os.environ.get("SARA_USER_NAME") or (user.split("@")[0] if user else "Sara")


def parse_draft(reply: str, fallback_subject: str) -> tuple[str, str]:
    """Split an AI reply of the form 'Subject: ...\\n\\nbody'."""
    m = re.match(r"\s*subject\s*:\s*(.+?)\s*\n(.*)", reply, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return fallback_subject, reply.strip()


def start_email(text: str, *, lang: str = "en") -> tuple[bool, str]:
    """Draft an email from a spoken request and ask for confirmation."""
    global _pending
    to, rest = find_recipient(text)
    if not to:
        return False, (
            "किसे ईमेल भेजूँ? ईमेल पता बोलिए, जैसे 'abc@gmail.com ko email bhejo ...', "
            f"या नाम {CONTACTS_FILE} में सेव करें।"
            if lang == "hi" else
            "Who should I email? Say the address, e.g. 'email abc@gmail.com that ...', "
            f"or save names in {CONTACTS_FILE}."
        )
    brief = _brief(rest)
    if not brief:
        return False, (
            "ईमेल में क्या लिखूँ? पूरी बात एक साथ बोलिए।" if lang == "hi"
            else "What should the email say? Include it in the same sentence."
        )

    from .ai import ask_ai

    res = ask_ai(
        f"Write an email to {to}. What the user wants to say: {brief}",
        lang=lang, system=_DRAFT_SYSTEM.format(sender=_sender_name()), max_tokens=700,
    )
    fallback_subject = "Message from " + _sender_name()
    subject, body = parse_draft(res.text, fallback_subject) if res.ok else (
        fallback_subject, brief)
    _pending = Draft(to=to, subject=subject, body=body)
    ask = ("भेज दूँ? 'हाँ' या 'नहीं' कहिए।" if lang == "hi"
           else "Shall I send it? Say 'yes' or 'no'.")
    return True, f"To: {to}\nSubject: {subject}\n\n{body}\n\n{ask}"


def confirm_email(text: str, *, lang: str = "en") -> tuple[bool, str]:
    """Send the pending draft on an explicit yes; anything else cancels."""
    global _pending
    draft, _pending = _pending, None
    if draft is None or not _has(text, _YES) or _has(text, _NO):
        return True, ("ठीक है, ईमेल नहीं भेजा।" if lang == "hi" else "Okay, email not sent.")
    ok, err = send(draft)
    if ok:
        return True, (f"ईमेल भेज दिया: {draft.to}" if lang == "hi"
                      else f"Email sent to {draft.to}.")
    return False, err


def send(draft: Draft, attachments: tuple[tuple[str, bytes], ...] = ()) -> tuple[bool, str]:
    """Send ``draft``; ``attachments`` are (filename, data) pairs."""
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    if not user or not password:
        return False, (
            "Email isn't set up yet. Add to .env:\n"
            "  SMTP_USER=you@gmail.com\n"
            "  SMTP_PASSWORD=<Gmail App Password>\n"
            "(Google Account -> Security -> 2-Step Verification -> App passwords)"
        )
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = draft.to
    msg["Subject"] = draft.subject
    msg.set_content(draft.body)
    for filename, data in attachments:
        msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=filename)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(),
                                  timeout=30) as smtp:
                smtp.login(user, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(user, password)
                smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        return False, ("Email login failed. For Gmail, SMTP_PASSWORD must be an App "
                       "Password, not your normal password.")
    except (OSError, smtplib.SMTPException) as e:
        return False, f"Couldn't send the email: {e}"
    return True, ""
