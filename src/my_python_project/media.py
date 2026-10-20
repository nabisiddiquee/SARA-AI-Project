"""Songs, naats and Quran recitations Sara can play, and the phrases that start/stop them.

The web dashboard plays them in its big panel; the CLI opens them in the
default browser instead. A general request ("gaana chalao", "naat sunao")
shows a menu of that category to pick from; a specific phrase ("90s songs",
"aya hai bulawa") plays that one straight away.

To add one, append an entry to ``SONGS`` or ``NAATS``. ``list_id``
"RD<video_id>" is YouTube's auto-mix for that video, so playback continues.
"""

from __future__ import annotations

import re

from . import quran

SONGS = [
    {
        "title": "Top 30 Romantic Hindi Songs",
        "channel": "Sony Music India",
        "video_id": "zeVWTY31Vn8",
        "list_id": "RDzeVWTY31Vn8",
        "phrases": ("top 30", "sony music", "evergreen"),
    },
    {
        "title": "90s Old Hindi Love Songs",
        "channel": "Udit Narayan · Alka Yagnik · Kumar Sanu",
        "video_id": "SaM8ZFCOsEQ",
        "list_id": "RDSaM8ZFCOsEQ",
        "phrases": (
            "90s", "90's", "nineties", "old song", "old songs", "purane gane",
            "purane gaane", "purana gana", "kumar sanu", "udit narayan", "alka yagnik",
            "पुराने गाने", "पुराना गाना",
        ),
    },
    {
        "title": "Top 20 Romantic Songs",
        "channel": "Saregama Music",
        "video_id": "N_9RNX12X8g",
        "list_id": "RDN_9RNX12X8g",
        "phrases": ("top 20", "saregama", "gehra hua", "tumhare hi rahenge"),
    },
    {
        "title": "Rishta Tera Mera · Dard Bhare Gaane",
        "channel": "Pankaj Udhas · Jai Vikranta (1995)",
        "video_id": "3rkxFDzf1do",
        "list_id": "RD3rkxFDzf1do",
        "phrases": (
            "sad song", "sad songs", "dard bhare", "dard bhara", "rishta tera mera",
            "pankaj udhas", "दर्द भरे", "सैड सॉन्ग",
        ),
    },
]

# General music requests: show the menu so the user can choose.
_MENU_PHRASES = (
    "love song", "love songs", "romantic song", "romantic songs",
    "play song", "play songs", "play a song", "play music", "play some music",
    "song chalao", "songs chalao", "song lagao", "song sunao", "songs sunao",
    "gana chalao", "gaana chalao", "gane chalao", "gaane chalao", "gana lagao",
    "gaana lagao", "gana sunao", "gaana sunao", "gane sunao", "gaane sunao",
    "music chalao", "music lagao", "playlist", "song list", "songs list",
    "pyar ka gana", "pyaar ka gaana", "pyar ka gaana", "pyaar ka gana",
    "pyar wala gana", "prem geet",
    "गाना चलाओ", "गाने चलाओ", "गाना सुनाओ", "गाने सुनाओ", "गाना लगाओ",
    "लव सॉन्ग", "लव सोंग", "प्यार का गाना", "रोमांटिक गाना", "प्रेम गीत", "प्लेलिस्ट",
)

NAATS = [
    {
        "title": "Humne Aankhon Se Dekha Nahi Hai Magar",
        "channel": "Mehmood J · B2 Islamic",
        "video_id": "JH4Yy5twNyI",
        "list_id": "RDJH4Yy5twNyI",
        "phrases": ("humne aankhon se", "humne ankhon se", "hamne aankhon se", "mehmood j"),
    },
    {
        "title": "Aya Hai Bulawa Mujhe Darbar-e-Nabi Se",
        "channel": "Owais Raza Qadri · OSA Islamic",
        "video_id": "XPUCHjrjtII",
        "list_id": "RDXPUCHjrjtII",
        "phrases": ("aya hai bulawa", "aaya hai bulawa", "aya hae bulawa", "darbar e nabi",
                    "owais raza", "owais qadri"),
    },
    {
        "title": "Kash Main Aqsa Ka Basi Hota",
        "channel": "Anas Nazeer · Nusrat Maheera",
        "video_id": "pBGpNjdjxKg",
        "list_id": "RDpBGpNjdjxKg",
        "phrases": ("kash main aqsa", "kaash main aqsa", "aqsa ka basi", "anas nazeer"),
    },
]

# Quran recitations (Sheikh Yasser Al-Dosari). No "list_id": a recitation
# plays on its own, without YouTube's auto-mix running on into music.
QURAN = [
    {
        "title": "Surah Al-Mu'minun",
        "channel": "Sheikh Yasser Al-Dosari · Al Hamed",
        "video_id": "_nx76MlMzFE",
        "surah": 23,
        "list_id": None,
        "phrases": ("muminun", "mominun", "muminoon", "mominoon", "al muminun", "al-muminun", "मोमिनून", "मुमिनून"),
    },
    {
        "title": "Surah Al-Mu'minun (recitation 2)",
        "channel": "Sheikh Yasser Al-Dosari · Qur4nt",
        "video_id": "1idsv_BY7Uw",
        "surah": 23,
        "list_id": None,
        "phrases": ("muminun 2", "mominun 2", "muminoon 2", "mominoon do", "doosri muminun"),
    },
    {
        "title": "Surah Maryam",
        "channel": "Sheikh Yasser Al-Dosari · 1432 AH",
        "video_id": "sdO8dCBpzv4",
        "surah": 19,
        "list_id": None,
        "phrases": ("maryam", "mariyam", "mariam", "surah mary", "मरियम"),
    },
    {
        "title": "Surah Ar-Rahman",
        "channel": "Sheikh Yasser Al-Dosari",
        "video_id": "_GsKIvd99YQ",
        "surah": 55,
        "list_id": None,
        "phrases": ("rahman", "rehman", "ar rahman", "ar-rahman", "surah rahman", "रहमान", "الرحمن"),
    },
    {
        "title": "Surah Al-Isra",
        "channel": "Sheikh Yasser Al-Dosari · Qurani My Life",
        "video_id": "TGHBaWbo7v8",
        "surah": 17,
        "list_id": None,
        "phrases": ("isra", "al isra", "al-isra", "bani israil", "bani israel", "इसरा", "الإسراء"),
    },
]

# Category -> its list. The first category a request matches wins.
CATEGORIES = {"quran": QURAN, "naat": NAATS, "song": SONGS}

# Any mention of the Quran / tilawat / a surah asks for the Quran menu.
_QURAN_WORD = re.compile(
    r"(?<![a-z])(?:quran|qur'?an|kuran|koran|quraan|tilawat|tilawah|tilaawat|surah|surat|sura)(?![a-z])"
    r"|क़ुरआन|कुरआन|कुरान|क़ुरान|तिलावत|सूरह|قرآن|تلاوت|سورة|سورہ")
# The Quran menu needs a "play / open" intent, so questions about the Quran
# ("quran mein kitni surah hain") still go to the AI.
_QURAN_PLAY = re.compile(
    r"(?<![a-z])(?:sunao|suna|sunna|chalao|chala|lagao|laga|play|bajao|kholo|khol|open|dikhao|"
    r"section|tilawat|tilawah|recitation|recite|padho|padh)(?![a-z])"
    r"|सुनाओ|चलाओ|लगाओ|खोलो|दिखाओ|तिलावत|पढ़ो")

# Any mention of naat asks for the naat menu ("naat chalao", "ek naat sunao").
# Whole words only, so "naatak" (drama) doesn't count.
_NAAT_WORD = re.compile(r"(?<![a-z])(?:naat|naath|naats|naat sharif|naat shareef)(?![a-z])|नात|نعت")

_STOP_PHRASES = (
    "stop song", "stop the song", "stop music", "stop the music", "pause song",
    "close song", "song band", "song stop", "gana band", "gaana band",
    "gana stop", "gaana stop", "music band", "music stop",
    "गाना बंद", "गाना रोको", "म्यूजिक बंद", "सॉन्ग बंद",
    "stop naat", "naat band", "naat stop", "naat roko", "नात बंद",
    "stop quran", "quran band", "quran stop", "quran roko", "tilawat band", "tilawat roko",
    "tilawat stop", "surah band", "कुरान बंद", "तिलावत बंद",
)


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


# A specific phrase only counts alongside a music word or play verb, so
# "shutdown after 90s" doesn't start the 90s playlist.
_MUSIC_WORDS = (
    "song", "gana", "gaana", "gane", "gaane", "music", "geet", "playlist",
    "jukebox", "play", "chalao", "lagao", "sunao", "bajao", "naat", "naath",
    "quran", "tilawat", "surah", "sura", "surat",
    "गाना", "गाने", "सॉन्ग", "गीत", "चलाओ", "लगाओ", "सुनाओ", "बजाओ", "कुरान", "तिलावत", "सूरह",
)


def find_song(text: str) -> dict | None:
    """The playlist a specific phrase asks for ("90s songs"), if any."""
    t = _norm(text)
    if not any(w in t for w in _MUSIC_WORDS):
        return None
    is_quran = bool(_QURAN_WORD.search(t)) and bool(_QURAN_PLAY.search(t))
    for category, items in CATEGORIES.items():
        # Surah names only count with a Quran word, so "A R Rahman songs"
        # (the composer) isn't Surah Ar-Rahman.
        if category == "quran" and not is_quran:
            continue
        # Longest phrase first, so "muminun 2" picks the second recitation, not the first.
        pairs = sorted(((p, song) for song in items for p in song["phrases"]), key=lambda x: -len(x[0]))
        for p, song in pairs:
            if p in t:
                return song
        if category == "quran":
            # Any of the 114 surahs ("surah yaseen sunao", "surah 67 chalao") plays as audio.
            n = quran.find_surah(t)
            if n:
                return quran_song(n)
    return None


def quran_song(n: int) -> dict:
    """A surah as a playable item (audio; no YouTube video)."""
    s = quran.surah(n)
    reciter = next(r for r in quran.RECITERS if r["id"] == quran.DEFAULT_RECITER)
    return {"title": f"Surah {s['name']}", "channel": f"Sheikh {reciter['name']}", "video_id": "",
            "list_id": None, "surah": n, "audio": f"{reciter['server']}{n:03d}.mp3", "phrases": ()}


def wants_menu(text: str) -> str | None:
    """The category ("quran" / "naat" / "song") whose menu a general request asks for."""
    t = _norm(text)
    if _QURAN_WORD.search(t):
        return "quran" if _QURAN_PLAY.search(t) else None
    if _NAAT_WORD.search(t):
        return "naat"
    if any(p in t for p in _MENU_PHRASES):
        return "song"
    return None


def is_music_stop(text: str) -> bool:
    t = _norm(text)
    return any(p in t for p in _STOP_PHRASES)


def watch_url(song: dict) -> str:
    if not song.get("video_id") and song.get("audio"):
        return song["audio"]
    url = f"https://www.youtube.com/watch?v={song['video_id']}"
    if song.get("list_id"):
        url += f"&list={song['list_id']}"
    return url


def payload(song: dict) -> dict:
    """What the web UI needs to show and play a playlist."""
    return {
        "title": song["title"], "channel": song.get("channel", ""),
        "video_id": song["video_id"], "list_id": song.get("list_id"),
        "url": watch_url(song), "surah": song.get("surah"),
    }
