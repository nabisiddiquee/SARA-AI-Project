"""The Quran: 114 surahs, audio reciters, and surah lookup for voice commands.

Audio comes from mp3quran.net (public, CORS-enabled MP3s, one file per surah:
<server>/<NNN>.mp3). Unlike YouTube embeds, plain audio also plays on networks
whose web filter blocks YouTube.
"""

from __future__ import annotations

import re

# (number, transliteration, Arabic, English meaning, ayahs)
SURAHS: list[tuple[int, str, str, str, int]] = [
    (1, "Al-Fatiha", "الفاتحة", "The Opening", 7),
    (2, "Al-Baqarah", "البقرة", "The Cow", 286),
    (3, "Aal-e-Imran", "آل عمران", "The Family of Imran", 200),
    (4, "An-Nisa", "النساء", "The Women", 176),
    (5, "Al-Ma'idah", "المائدة", "The Table Spread", 120),
    (6, "Al-An'am", "الأنعام", "The Cattle", 165),
    (7, "Al-A'raf", "الأعراف", "The Heights", 206),
    (8, "Al-Anfal", "الأنفال", "The Spoils of War", 75),
    (9, "At-Tawbah", "التوبة", "The Repentance", 129),
    (10, "Yunus", "يونس", "Jonah", 109),
    (11, "Hud", "هود", "Hud", 123),
    (12, "Yusuf", "يوسف", "Joseph", 111),
    (13, "Ar-Ra'd", "الرعد", "The Thunder", 43),
    (14, "Ibrahim", "إبراهيم", "Abraham", 52),
    (15, "Al-Hijr", "الحجر", "The Rocky Tract", 99),
    (16, "An-Nahl", "النحل", "The Bee", 128),
    (17, "Al-Isra", "الإسراء", "The Night Journey", 111),
    (18, "Al-Kahf", "الكهف", "The Cave", 110),
    (19, "Maryam", "مريم", "Mary", 98),
    (20, "Ta-Ha", "طه", "Ta-Ha", 135),
    (21, "Al-Anbiya", "الأنبياء", "The Prophets", 112),
    (22, "Al-Hajj", "الحج", "The Pilgrimage", 78),
    (23, "Al-Mu'minun", "المؤمنون", "The Believers", 118),
    (24, "An-Nur", "النور", "The Light", 64),
    (25, "Al-Furqan", "الفرقان", "The Criterion", 77),
    (26, "Ash-Shu'ara", "الشعراء", "The Poets", 227),
    (27, "An-Naml", "النمل", "The Ant", 93),
    (28, "Al-Qasas", "القصص", "The Stories", 88),
    (29, "Al-Ankabut", "العنكبوت", "The Spider", 69),
    (30, "Ar-Rum", "الروم", "The Romans", 60),
    (31, "Luqman", "لقمان", "Luqman", 34),
    (32, "As-Sajdah", "السجدة", "The Prostration", 30),
    (33, "Al-Ahzab", "الأحزاب", "The Combined Forces", 73),
    (34, "Saba", "سبأ", "Sheba", 54),
    (35, "Fatir", "فاطر", "The Originator", 45),
    (36, "Ya-Sin", "يس", "Ya-Sin", 83),
    (37, "As-Saffat", "الصافات", "Those Ranged in Ranks", 182),
    (38, "Sad", "ص", "Sad", 88),
    (39, "Az-Zumar", "الزمر", "The Groups", 75),
    (40, "Ghafir", "غافر", "The Forgiver", 85),
    (41, "Fussilat", "فصلت", "Explained in Detail", 54),
    (42, "Ash-Shura", "الشورى", "The Consultation", 53),
    (43, "Az-Zukhruf", "الزخرف", "The Ornaments of Gold", 89),
    (44, "Ad-Dukhan", "الدخان", "The Smoke", 59),
    (45, "Al-Jathiyah", "الجاثية", "The Kneeling", 37),
    (46, "Al-Ahqaf", "الأحقاف", "The Wind-Curved Sandhills", 35),
    (47, "Muhammad", "محمد", "Muhammad", 38),
    (48, "Al-Fath", "الفتح", "The Victory", 29),
    (49, "Al-Hujurat", "الحجرات", "The Rooms", 18),
    (50, "Qaf", "ق", "Qaf", 45),
    (51, "Adh-Dhariyat", "الذاريات", "The Winnowing Winds", 60),
    (52, "At-Tur", "الطور", "The Mount", 49),
    (53, "An-Najm", "النجم", "The Star", 62),
    (54, "Al-Qamar", "القمر", "The Moon", 55),
    (55, "Ar-Rahman", "الرحمن", "The Most Merciful", 78),
    (56, "Al-Waqi'ah", "الواقعة", "The Inevitable", 96),
    (57, "Al-Hadid", "الحديد", "The Iron", 29),
    (58, "Al-Mujadilah", "المجادلة", "The Pleading Woman", 22),
    (59, "Al-Hashr", "الحشر", "The Exile", 24),
    (60, "Al-Mumtahanah", "الممتحنة", "She That Is Examined", 13),
    (61, "As-Saff", "الصف", "The Ranks", 14),
    (62, "Al-Jumu'ah", "الجمعة", "Friday", 11),
    (63, "Al-Munafiqun", "المنافقون", "The Hypocrites", 11),
    (64, "At-Taghabun", "التغابن", "Mutual Disillusion", 18),
    (65, "At-Talaq", "الطلاق", "Divorce", 12),
    (66, "At-Tahrim", "التحريم", "The Prohibition", 12),
    (67, "Al-Mulk", "الملك", "The Sovereignty", 30),
    (68, "Al-Qalam", "القلم", "The Pen", 52),
    (69, "Al-Haqqah", "الحاقة", "The Reality", 52),
    (70, "Al-Ma'arij", "المعارج", "The Ascending Stairways", 44),
    (71, "Nuh", "نوح", "Noah", 28),
    (72, "Al-Jinn", "الجن", "The Jinn", 28),
    (73, "Al-Muzzammil", "المزمل", "The Enshrouded One", 20),
    (74, "Al-Muddaththir", "المدثر", "The Cloaked One", 56),
    (75, "Al-Qiyamah", "القيامة", "The Resurrection", 40),
    (76, "Al-Insan", "الإنسان", "Man", 31),
    (77, "Al-Mursalat", "المرسلات", "Those Sent Forth", 50),
    (78, "An-Naba", "النبأ", "The Tidings", 40),
    (79, "An-Nazi'at", "النازعات", "Those Who Drag Forth", 46),
    (80, "Abasa", "عبس", "He Frowned", 42),
    (81, "At-Takwir", "التكوير", "The Overthrowing", 29),
    (82, "Al-Infitar", "الانفطار", "The Cleaving", 19),
    (83, "Al-Mutaffifin", "المطففين", "The Defrauding", 36),
    (84, "Al-Inshiqaq", "الانشقاق", "The Splitting Open", 25),
    (85, "Al-Buruj", "البروج", "The Mansions of the Stars", 22),
    (86, "At-Tariq", "الطارق", "The Night-Comer", 17),
    (87, "Al-A'la", "الأعلى", "The Most High", 19),
    (88, "Al-Ghashiyah", "الغاشية", "The Overwhelming", 26),
    (89, "Al-Fajr", "الفجر", "The Dawn", 30),
    (90, "Al-Balad", "البلد", "The City", 20),
    (91, "Ash-Shams", "الشمس", "The Sun", 15),
    (92, "Al-Layl", "الليل", "The Night", 21),
    (93, "Ad-Duha", "الضحى", "The Morning Hours", 11),
    (94, "Ash-Sharh", "الشرح", "The Relief", 8),
    (95, "At-Tin", "التين", "The Fig", 8),
    (96, "Al-Alaq", "العلق", "The Clot", 19),
    (97, "Al-Qadr", "القدر", "The Power", 5),
    (98, "Al-Bayyinah", "البينة", "The Clear Proof", 8),
    (99, "Az-Zalzalah", "الزلزلة", "The Earthquake", 8),
    (100, "Al-Adiyat", "العاديات", "The Courser", 11),
    (101, "Al-Qari'ah", "القارعة", "The Calamity", 11),
    (102, "At-Takathur", "التكاثر", "The Rivalry in World Increase", 8),
    (103, "Al-Asr", "العصر", "The Declining Day", 3),
    (104, "Al-Humazah", "الهمزة", "The Traducer", 9),
    (105, "Al-Fil", "الفيل", "The Elephant", 5),
    (106, "Quraysh", "قريش", "Quraysh", 4),
    (107, "Al-Ma'un", "الماعون", "The Small Kindnesses", 7),
    (108, "Al-Kawthar", "الكوثر", "The Abundance", 3),
    (109, "Al-Kafirun", "الكافرون", "The Disbelievers", 6),
    (110, "An-Nasr", "النصر", "The Divine Support", 3),
    (111, "Al-Masad", "المسد", "The Palm Fiber", 5),
    (112, "Al-Ikhlas", "الإخلاص", "The Sincerity", 4),
    (113, "Al-Falaq", "الفلق", "The Daybreak", 5),
    (114, "An-Nas", "الناس", "Mankind", 6),
]
assert len(SURAHS) == 114 and sum(s[4] for s in SURAHS) == 6236

# Full-Quran Murattal (Hafs) recitations on mp3quran.net, checked 2026-10-02.
RECITERS = [
    {"id": "yasser", "name": "Yasser Al-Dosari", "server": "https://server11.mp3quran.net/yasser/"},
    {"id": "afs", "name": "Mishary Alafasy", "server": "https://server8.mp3quran.net/afs/"},
    {"id": "maher", "name": "Maher Al-Muaiqly", "server": "https://server12.mp3quran.net/maher/"},
    {"id": "basit", "name": "Abdul Basit Abdus Samad", "server": "https://server7.mp3quran.net/basit/"},
    {"id": "husr", "name": "Mahmoud Khalil Al-Hussary", "server": "https://server13.mp3quran.net/husr/"},
    {"id": "minsh", "name": "Mohammed Siddiq Al-Minshawi", "server": "https://server10.mp3quran.net/minsh/"},
    {"id": "s_gmd", "name": "Saad Al-Ghamdi", "server": "https://server7.mp3quran.net/s_gmd/"},
    {"id": "shur", "name": "Saud Al-Shuraim", "server": "https://server7.mp3quran.net/shur/"},
]
DEFAULT_RECITER = "yasser"

# Extra spoken / Hindi spellings for frequently requested surahs.
_EXTRA_ALIASES = {
    1: ("fatiha", "fateha", "alhamd", "alhamdu", "फातिहा", "फ़ातिहा"),
    2: ("baqara", "bakara", "baqra", "बक़रह", "बकरा"),
    3: ("imran", "al imran", "aal imran", "इमरान"),
    12: ("yusuf", "yousuf", "yusuf", "यूसुफ"),
    17: ("bani israil", "bani israel", "isra", "इसरा"),
    18: ("kahf", "kahaf", "kehf", "कहफ़", "कहफ"),
    19: ("maryam", "mariyam", "mariam", "मरियम"),
    23: ("muminun", "mominun", "muminoon", "mominoon", "मोमिनून", "मुमिनून"),
    32: ("sajda", "sajdah", "सजदा"),
    36: ("yasin", "yaseen", "yasin sharif", "yaseen sharif", "ya sin", "यासीन"),
    55: ("rahman", "rehman", "रहमान"),
    56: ("waqia", "waqiah", "waqiya", "vaqia", "वाक़िया", "वाकिया"),
    67: ("mulk", "tabarak", "मुल्क"),
    78: ("naba", "amma", "नबा"),
    94: ("alam nashrah", "inshirah", "sharh"),
    97: ("qadr", "kadr", "क़द्र", "कद्र"),
    108: ("kausar", "kawsar", "kosar", "कौसर"),
    109: ("kafirun", "kafiroon", "काफ़िरून"),
    112: ("ikhlas", "qul huwallah", "इख़लास", "इखलास"),
    113: ("falaq", "फ़लक़", "फलक"),
    114: ("naas", "nas", "नास"),
}


def _plain(name: str) -> str:
    """'Al-Mu'minun' -> 'muminun' (no article, apostrophes or hyphens)."""
    s = name.lower().replace("'", "").replace("-", " ")
    s = re.sub(r"^(al|an|ar|as|ash|at|ad|adh|az|aal e)\s+", "", s)
    return " ".join(s.split())


def _aliases() -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for n, name, arabic, _meaning, _ayahs in SURAHS:
        plain = _plain(name)
        out += [(plain, n), (name.lower().replace("'", "").replace("-", " "), n), (arabic, n)]
        if " " in plain:
            out.append((plain.replace(" ", ""), n))
        out += [(a, n) for a in _EXTRA_ALIASES.get(n, ())]
    # Longest first, so "al imran" wins over "imran" and "nas" can't shadow "nasr".
    return sorted({a: n for a, n in out if len(a) >= 2}.items(), key=lambda x: -len(x[0]))


_ALIASES = _aliases()
_NUMBER = re.compile(r"(?:surah|surat|sura|सूरह|सूरा)\s*(?:no\.?|number|नंबर)?\s*(\d{1,3})(?!\d)")


def find_surah(text: str) -> int | None:
    """Surah number named in a request ("surah yaseen sunao", "surah 67"), else None."""
    t = " " + " ".join(text.lower().replace("'", "").replace("-", " ").split()) + " "
    m = _NUMBER.search(t)
    if m and 1 <= int(m.group(1)) <= 114:
        return int(m.group(1))
    for alias, n in _ALIASES:
        # Whole words only (Latin), so "nas" doesn't match inside "nasr" or "dinner".
        word = re.escape(alias)
        if alias.isascii() and len(alias) <= 3:
            # Short names are everyday words ("sad", "asr", "hud", "tin"): only right after "surah".
            word = r"(?:surah|surat|sura) " + word
        if re.search(r"(?<![a-z])" + word + r"(?![a-z])", t):
            return n
    return None


def surah(n: int) -> dict:
    num, name, arabic, meaning, ayahs = SURAHS[n - 1]
    return {"number": num, "name": name, "arabic": arabic, "meaning": meaning, "ayahs": ayahs}


def catalog() -> dict:
    return {
        "surahs": [surah(n) for n in range(1, 115)],
        "reciters": RECITERS,
        "default_reciter": DEFAULT_RECITER,
        "source": "mp3quran.net",
    }
