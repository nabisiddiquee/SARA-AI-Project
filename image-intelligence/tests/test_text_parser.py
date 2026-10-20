"""Unit tests for the regex layer (no models needed).

    python -m pytest tests -q        (or)        python tests/test_text_parser.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.text_parser import classify_qr, parse  # noqa: E402


def lines(*texts, heights=None):
    heights = heights or [30] * len(texts)
    return [{"text": t, "confidence": 0.9, "height": h} for t, h in zip(texts, heights)]


def test_profile_card():
    info = parse(lines("John Smith", "Software Engineer", "@johnsmith", "linkedin.com/in/johnsmith",
                       "john@example.com", heights=[60, 30, 25, 25, 25]))
    assert info["possible_name"] == "John Smith"
    assert info["possible_job_title"] == "Software Engineer"
    assert info["username"] == "johnsmith"
    assert info["email"] == "john@example.com"
    assert info["linkedin_url"] == "https://linkedin.com/in/johnsmith"


def test_email_is_not_a_username():
    info = parse(lines("contact: priya.sharma@company.co.in"))
    assert info["email"] == "priya.sharma@company.co.in"
    assert "company" not in info["usernames"]


def test_phones_and_company():
    info = parse(lines("Infosys Technologies Ltd", "Mob: +91 98765 43210", "Tel (555) 123-4567"))
    assert info["possible_company"] == "Infosys Technologies Ltd"
    assert "+91 98765 43210" in info["phones"]
    assert any("555" in p for p in info["phones"])


def test_social_urls():
    info = parse(lines("instagram.com/john.smith", "https://www.facebook.com/johnsmith99", "x.com/jsmith"))
    assert info["instagram_url"] == "https://instagram.com/john.smith"
    assert info["facebook_url"] == "https://www.facebook.com/johnsmith99"
    assert info["twitter_url"] == "https://x.com/jsmith"
    assert "john.smith" in info["usernames"]


def test_qr_types():
    assert classify_qr("https://linkedin.com/in/example") == "url"
    assert classify_qr("mailto:a@b.com") == "email"
    assert classify_qr("+91 98765 43210") == "phone"
    assert classify_qr("WIFI:S:Home;T:WPA;P:secret;;") == "wifi"
    assert classify_qr("BEGIN:VCARD\nFN:John\nEND:VCARD") == "vcard"
    assert classify_qr("hello world") == "text"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
