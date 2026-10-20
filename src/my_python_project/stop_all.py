""""Sab roko": a bare stop command ("stop", "band karo", "rok do", "sab cancel
kar do") cancels everything Sara is busy with — games, pending shutdown /
email confirmations — and tells the web UI to stop speaking and playing.

Only a *bare* command counts: once filler words ("sara", "sab", "kaam",
"please") are dropped, nothing may remain but a stop word and verb endings,
so "chrome band karo" or "stop music" still reach their own handlers.
"""

from __future__ import annotations

import re

# The stop word itself.
_CORE = {
    "stop", "band", "cancel", "halt", "abort", "bas", "chup", "khatam", "khtm",
    "roko", "rok", "rokdo", "rokho", "roka", "ruko", "ruk", "rukjao", "rukja",
    "बंद", "रोको", "रोक", "रोकदो", "रुको", "रुक", "चुप", "बस", "कैंसिल", "स्टॉप",
    "खत्म", "ख़त्म",
}
# Verb endings around it ("band kar do", "ruk jao", "rok dijiye").
_VERB = {
    "karo", "kro", "kar", "kr", "krdo", "kardo", "krde", "karde", "do", "de",
    "dena", "dijiye", "dijie", "jao", "ja", "ho", "hojao", "jaiye", "kariye",
    "करो", "कर", "दो", "दे", "दीजिए", "जाओ", "जा", "हो", "करिए",
}
# Words that don't change the meaning ("Sara, saara kaam stop karo").
_FILLER = {
    "sara", "saara", "sarah", "sare", "saare", "sab", "sabhi", "sabkuch",
    "kaam", "kam", "kuch", "bhi", "ko", "abhi", "turant", "jaldi", "please",
    "plz", "pls", "ji", "yaar", "bhai", "everything", "all", "it", "now", "the",
    "सारा", "सारे", "सब", "सभी", "काम", "को", "अभी", "जी", "प्लीज़", "प्लीज",
}


def is_stop_all(text: str) -> bool:
    # Strip punctuation only: \W would also split Devanagari vowel signs.
    words = re.sub(r"[.,!?;:'\"()।|-]", " ", text.lower()).split()
    words = [w for w in words if w not in _FILLER]
    return (any(w in _CORE for w in words)
            and all(w in _CORE or w in _VERB for w in words))


def cancel_all() -> None:
    """Drop every in-progress game and pending confirmation."""
    from . import games, mailer, system

    games.stop_all()
    mailer.cancel_pending()
    system.clear_pending()
