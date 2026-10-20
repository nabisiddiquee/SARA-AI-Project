"""Regex + rules that turn OCR lines into structured, *visible* information.

Nothing here looks anything up: it only classifies text that is written in
the image (emails, phones, @usernames, URLs, name / job-title / company lines).
"""

from __future__ import annotations

import re

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# International / Indian / US style numbers: +91 98765 43210, (555) 123-4567, 080-2345 6789
PHONE_RE = re.compile(r"(?<![\w@])(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{2,5}\)?[\s.-]?)?\d{3,5}[\s.-]?\d{3,5}(?![\w@])")
URL_RE = re.compile(
    r"\b(?:https?://)?(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|in|org|net|io|co|ai|dev|me|app|edu|gov|info|biz|uk|us|xyz|tech|site|online|store|page)"
    r"(?:/[^\s,;]*)?", re.I)
HANDLE_RE = re.compile(r"(?<![\w.@/])@([A-Za-z0-9_](?:[A-Za-z0-9_.]{0,28}[A-Za-z0-9_])?)\b")

SOCIAL = {
    "linkedin_url": re.compile(r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/(?:in|company|pub)/[^\s,;]+", re.I),
    "instagram_url": re.compile(r"(?:https?://)?(?:www\.)?(?:instagram\.com|instagr\.am)/[^\s,;]+", re.I),
    "facebook_url": re.compile(r"(?:https?://)?(?:www\.|m\.)?(?:facebook\.com|fb\.com|fb\.me)/[^\s,;]+", re.I),
    "twitter_url": re.compile(r"(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/[^\s,;]+", re.I),
    "youtube_url": re.compile(r"(?:https?://)?(?:www\.)?(?:youtube\.com/(?:@|c/|channel/|user/)[^\s,;]+|youtu\.be/[^\s,;]+)", re.I),
    "github_url": re.compile(r"(?:https?://)?(?:www\.)?github\.com/[^\s,;]+", re.I),
}

JOB_WORDS = (
    "engineer", "developer", "manager", "director", "designer", "analyst", "consultant", "officer",
    "founder", "co-founder", "ceo", "cto", "cfo", "coo", "president", "intern", "architect", "lead",
    "head of", "specialist", "executive", "administrator", "scientist", "researcher", "teacher",
    "professor", "doctor", "dr.", "lawyer", "advocate", "accountant", "marketing", "sales",
    "recruiter", "hr ", "human resources", "student", "associate", "partner", "owner", "chef",
    "photographer", "writer", "editor", "nurse", "technician", "programmer", "devops", "product",
)
COMPANY_WORDS = (
    "pvt", "private limited", "ltd", "limited", "inc", "inc.", "llc", "llp", "corp", "corporation",
    "company", "co.", "technologies", "technology", "solutions", "systems", "labs", "software",
    "services", "consulting", "group", "industries", "enterprises", "studio", "agency", "infotech",
    "global", "international", "bank", "university", "college", "school", "hospital",
)
_NOT_NAME = ("www", "http", ".com", "@", "phone", "email", "mobile", "tel", "fax", "address", "linkedin",
             "instagram", "facebook", "twitter", "github", "youtube", "follow", "subscribe", "contact")


def _norm_url(u: str) -> str:
    u = u.strip().rstrip(".,;:)!?'\"")
    return u if re.match(r"https?://", u, re.I) else "https://" + u


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _has_word(text: str, words) -> bool:
    low = f" {text.lower()} "
    return any((f" {w} " in low) if " " not in w.strip() and len(w) <= 4 else (w in low) for w in words)


def parse(lines: list[dict], qr_values: list[str] | None = None) -> dict:
    """lines: OCR lines [{text, confidence, height}] top-to-bottom."""
    texts = [l["text"] for l in lines]
    blob = "\n".join(texts + list(qr_values or []))
    info: dict = {"usernames": [], "emails": [], "phones": [], "websites": []}

    info["emails"] = list(dict.fromkeys(m.group(0) for m in EMAIL_RE.finditer(blob)))
    email_spans = " ".join(info["emails"])

    for key, rx in SOCIAL.items():
        m = rx.search(blob)
        if m:
            info[key] = _norm_url(m.group(0))

    social_hosts = ("linkedin.", "instagram.", "instagr.am", "facebook.", "fb.com", "fb.me", "twitter.", "x.com",
                    "youtube.", "youtu.be", "github.")
    for m in URL_RE.finditer(blob):
        u = m.group(0)
        if u in email_spans or any(h in u.lower() for h in social_hosts):
            continue
        u = _norm_url(u)
        if u not in info["websites"]:
            info["websites"].append(u)

    for m in HANDLE_RE.finditer(blob):
        handle = m.group(1)
        if any(handle in e for e in info["emails"]):
            continue
        if handle not in info["usernames"]:
            info["usernames"].append(handle)
    # Usernames from social URLs ("instagram.com/john.smith" -> john.smith).
    for key in ("instagram_url", "twitter_url", "github_url", "linkedin_url"):
        if info.get(key):
            tail = info[key].rstrip("/").rsplit("/", 1)[-1].split("?")[0]
            if tail and tail.lower() not in ("in", "company", "pub") and tail not in info["usernames"]:
                info["usernames"].append(tail.lstrip("@"))

    for m in PHONE_RE.finditer(blob):
        raw = m.group(0).strip()
        d = _digits(raw)
        if 10 <= len(d) <= 13 and raw not in info["phones"] and not re.fullmatch(r"(19|20)\d{6}", d):
            info["phones"].append(raw)

    # Name / job title / company from plain lines (no contact data on them).
    plain = []
    for l in lines:
        t = l["text"].strip()
        low = t.lower()
        if EMAIL_RE.search(t) or URL_RE.search(t) or HANDLE_RE.search(t) or any(x in low for x in _NOT_NAME):
            continue
        if len(_digits(t)) >= 6:
            continue
        plain.append(l)

    job = next((l["text"] for l in plain if _has_word(l["text"], JOB_WORDS) and len(l["text"]) <= 60), None)
    company = next((l["text"] for l in plain if _has_word(l["text"], COMPANY_WORDS) and l["text"] != job
                    and len(l["text"]) <= 60), None)

    def name_like(t: str) -> bool:
        words = t.replace(".", ". ").split()
        if not 2 <= len(words) <= 4 or t in (job, company):
            return False
        if _has_word(t, JOB_WORDS) or _has_word(t, COMPANY_WORDS):
            return False
        return all(re.fullmatch(r"[A-Z][a-zA-Z'\-]*\.?|[A-Z]\.?|[A-Z]{2,}", w) for w in words)

    candidates = [l for l in plain if name_like(l["text"])]
    # The biggest text that looks like a name is usually the person's name.
    name = max(candidates, key=lambda l: l.get("height", 0))["text"] if candidates else None
    if name and name.isupper():
        name = name.title()

    info.update({
        "possible_name": name,
        "possible_job_title": job,
        "possible_company": company,
        "username": info["usernames"][0] if info["usernames"] else None,
        "email": info["emails"][0] if info["emails"] else None,
        "phone": info["phones"][0] if info["phones"] else None,
        "website": info["websites"][0] if info["websites"] else None,
    })
    return info


def classify_qr(value: str) -> str:
    v = value.strip()
    low = v.lower()
    if low.startswith("begin:vcard"):
        return "vcard"
    if low.startswith("mecard:"):
        return "mecard"
    if low.startswith("wifi:"):
        return "wifi"
    if low.startswith("mailto:") or EMAIL_RE.fullmatch(v):
        return "email"
    if low.startswith("tel:") or (len(_digits(v)) >= 8 and re.fullmatch(r"[\d\s+().-]+", v)):
        return "phone"
    if low.startswith("smsto:") or low.startswith("sms:"):
        return "sms"
    if low.startswith("geo:"):
        return "geo"
    if low.startswith(("http://", "https://", "www.")) or URL_RE.fullmatch(v):
        return "url"
    return "text"
