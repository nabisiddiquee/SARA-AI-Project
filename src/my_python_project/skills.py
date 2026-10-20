"""Everything Sara can do: the catalog behind the dashboard's ⚡ Skills window.

Each example is a phrase Sara really understands. ``run`` marks examples that
are safe to run straight from a click (they only show or play something);
the rest (power, messages, files) are put in the command box for the user to
check and send. ``edit`` marks templates with a part to replace (a number...).
Keep this in step with actions.py when a feature is added or changed.
"""

from __future__ import annotations

import re


def _ex(text: str, *, run: bool = True, edit: bool = False) -> dict:
    return {"text": text, "run": run and not edit, "edit": edit}


CATEGORIES: list[dict] = [
    {
        "id": "chat", "icon": "💬", "title": "Baat-cheet & AI",
        "skills": [
            {"title": "Kuch bhi poochhiye", "desc": "Sawal, jaankari, likhai, idea: Claude AI se jawab, Hindi ya English mein.",
             "examples": [_ex("Taj Mahal kisne banwaya?"), _ex("mujhe ek chhutti ki application likh do"),
                          _ex("Python mein list aur tuple ka farq batao")]},
            {"title": "Bhasha badlein", "desc": "Sara Hindi ya English mein jawab de. Header ke 'Sara ki bhasha' se bhi.",
             "examples": [_ex("hindi mein baat karo"), _ex("english mein baat karo")]},
            {"title": "Pehchaan", "desc": "Sara se milna, naam batana.",
             "examples": [_ex("hello sara"), _ex("tum kaun ho"), _ex("mera naam Nabi hai")]},
        ],
    },
    {
        "id": "quran", "icon": "🕌", "title": "Quran",
        "skills": [
            {"title": "Quran window", "desc": "114 surahs, 8 qari, search, repeat, mini player. Bismillah se shuruaat.",
             "examples": [_ex("quran sunao"), _ex("tilawat chalao")]},
            {"title": "Koi bhi surah", "desc": "Naam ya number se.",
             "examples": [_ex("surah yaseen sunao"), _ex("surah rahman sunao"), _ex("surah 67 chalao"),
                          _ex("सूरह कहफ़ सुनाओ")]},
            {"title": "Band karna", "desc": "Tilawat rokna.", "examples": [_ex("quran band karo")]},
        ],
    },
    {
        "id": "media", "icon": "🎵", "title": "Music, YouTube & Radio",
        "skills": [
            {"title": "Gaane & naat", "desc": "Playlist chunne ka menu, ya seedha koi playlist.",
             "examples": [_ex("gaana chalao"), _ex("90s songs chalao"), _ex("naat chalao"), _ex("song band karo")]},
            {"title": "YouTube par chalao", "desc": "Koi bhi video dashboard ke bade panel mein.",
             "examples": [_ex("youtube par arijit singh chalao"), _ex("play lofi music on youtube")]},
            {"title": "FM Radio", "desc": "Live stations: Mirchi, Vividh Bharati, aur bahut. Sara bolte waqt awaaz dheemi.",
             "examples": [_ex("radio chalao"), _ex("radio mirchi lagao"), _ex("radio band karo")]},
        ],
    },
    {
        "id": "web", "icon": "🌐", "title": "Web",
        "skills": [
            {"title": "Google search", "desc": "Natije bade panel mein.",
             "examples": [_ex("google par python tutorial search karo"), _ex("search best laptop under 50000")]},
            {"title": "Map & Wikipedia", "desc": "Jagah naqshe par, ya Wikipedia ka lekh.",
             "examples": [_ex("map of India Gate"), _ex("wikipedia par Taj Mahal dikhao")]},
            {"title": "Website kholo", "desc": "Koi bhi site ka address.", "examples": [_ex("open github.com"), _ex("youtube kholo")]},
            {"title": "Panel control", "desc": "Bada panel full screen, chhota ya band.",
             "examples": [_ex("full screen"), _ex("chhota karo"), _ex("screen band karo")]},
        ],
    },
    {
        "id": "live", "icon": "📰", "title": "Live jaankari",
        "skills": [
            {"title": "Khabrein", "desc": "Taaza headlines, kisi bhi vishay par.", "examples": [_ex("aaj ki news"), _ex("cricket news")]},
            {"title": "Mausam", "desc": "Kisi bhi shehar ka abhi ka mausam.",
             "examples": [_ex("Delhi ka mausam"), _ex("sab shahron ka mausam"), _ex("weather in Dubai")]},
            {"title": "Samay & tareekh", "desc": "Yahan ya duniya ke kisi shehar ka time.",
             "examples": [_ex("abhi kitne baje hain"), _ex("Dubai mein time kya hai"), _ex("aaj ki tareekh")]},
            {"title": "Share bazaar", "desc": "Nifty, Sensex, ya kisi company ka bhav.",
             "examples": [_ex("nifty sensex batao"), _ex("Reliance share price")]},
        ],
    },
    {
        "id": "pc", "icon": "💻", "title": "Apps & Files",
        "skills": [
            {"title": "App kholo / band karo", "desc": "Computer mein install koi bhi app.",
             "examples": [_ex("chrome kholo", run=False), _ex("VS Code open karo", run=False), _ex("chrome band karo", run=False)]},
            {"title": "Folder & file", "desc": "Naya folder/file banana, ya kisi drive/folder ki list.",
             "examples": [_ex("Desktop pe Projects naam ka folder banao", run=False), _ex("D drive ke folders dikhao", run=False)]},
            {"title": "Matrix animation", "desc": "Full-screen SARA / binary rain. Band karne ke liye Esc.",
             "examples": [_ex("matrix animation chalao", run=False), _ex("binary animation", run=False)]},
        ],
    },
    {
        "id": "power", "icon": "⚡", "title": "System power",
        "skills": [
            {"title": "Sleep & lock", "desc": "Turant sleep (hibernate nahi) ya screen lock.",
             "examples": [_ex("system sleep karo", run=False), _ex("system lock karo", run=False)]},
            {"title": "Time par sleep", "desc": "Tay samay par sleep; 1 minute pehle chetavni.",
             "examples": [_ex("30 minute baad system sleep kar do", run=False), _ex("raat 11 baje system sleep kar dena", run=False)]},
            {"title": "Time par jagana", "desc": "Sleep se tay samay par system on (charger par).",
             "examples": [_ex("subah 6 baje system on kar dena", run=False),
                          _ex("raat 11 baje sleep aur subah 6 baje on kar dena", run=False)]},
            {"title": "Restart / shutdown", "desc": "Pehle 'haan' se pakka karti hai; baad ke liye bhi.",
             "examples": [_ex("1 ghante baad shutdown", run=False), _ex("system restart karo", run=False)]},
            {"title": "Timer cancel", "desc": "Sleep, jagana ya shutdown ka timer hatana.",
             "examples": [_ex("sleep cancel karo", run=False), _ex("wake cancel karo", run=False), _ex("shutdown cancel karo", run=False)]},
        ],
    },
    {
        "id": "notes", "icon": "📝", "title": "Notebook & Calculator",
        "skills": [
            {"title": "Notebook", "desc": "Auto-save, tareekh wala naam. Bolkar likhwaiye.",
             "examples": [_ex("notebook kholo"), _ex("notebook me likho: client call done", run=False),
                          _ex("aaj jo kaam kiye hai woh save kar lo", run=False)]},
            {"title": "Roz WhatsApp par note", "desc": "Har din tay samay par aaj ka note WhatsApp par.",
             "examples": [_ex("whatsapp 98XXXXXXXX send karna every 11:59:58 PM", edit=True), _ex("WhatsApp schedule band karo", run=False)]},
            {"title": "Calculator", "desc": "Hisaab bolkar ya type karke.", "examples": [_ex("calculator kholo"), _ex("12 plus 5 kitna hai")]},
        ],
    },
    {
        "id": "msg", "icon": "✉️", "title": "WhatsApp & Email",
        "skills": [
            {"title": "WhatsApp", "desc": "App kholna, ya kisi number par message.",
             "examples": [_ex("whatsapp kholo", run=False), _ex("98XXXXXXXX pe whatsapp message bhejo: main 5 baje aaunga", edit=True)]},
            {"title": "Email", "desc": "AI draft likhti hai, aapke 'haan' ke baad bhejti hai.",
             "examples": [_ex("name@example.com ko email bhejo ki meeting kal 10 baje hai", edit=True)]},
        ],
    },
    {
        "id": "camera", "icon": "📷", "title": "Camera & Scan",
        "skills": [
            {"title": "Camera", "desc": "Face detection, scan quality gauge, objects, achhe scan par photo save.",
             "examples": [_ex("camera kholo"), _ex("camera band karo")]},
        ],
    },
    {
        "id": "devices", "icon": "🎧", "title": "Bluetooth, AC & Phone",
        "skills": [
            {"title": "Bluetooth", "desc": "Devices ki screen, radio on/off, earbuds/speaker jodna. Jud-ne par awaaz se suchna.",
             "examples": [_ex("bluetooth kholo"), _ex("bluetooth on karo", run=False), _ex("realme buds connect karo", edit=True)]},
            {"title": "AC control", "desc": "Blue Star AC: on/off, temperature, mode, fan, swing, turbo/sleep. Live room temperature aur AI tips.",
             "examples": [_ex("ac kholo"), _ex("ac 24 pe chalao", run=False), _ex("ac 2 degree kam karo", run=False), _ex("ac band karo", run=False)]},
            {"title": "Phone se command", "desc": "Phone ke browser mein /remote kholkar bolkar ya likhkar command.",
             "examples": []},
        ],
    },
    {
        "id": "games", "icon": "🎮", "title": "Games",
        "skills": [
            {"title": "Number guessing", "desc": "1 se 100 ke beech number socho.", "examples": [_ex("game khelo")]},
            {"title": "Quiz", "desc": "5 sawaalon ka round.", "examples": [_ex("quiz khelo")]},
            {"title": "Tic-tac-toe", "desc": "Aap X, Sara O. Khaane 1–9.", "examples": [_ex("tic tac toe")]},
            {"title": "Chhote khel", "desc": "Paasa, sikka, patthar-kagaz-kainchi.",
             "examples": [_ex("roll dice"), _ex("toss karo"), _ex("rock paper scissors")]},
        ],
    },
]

# What the user can say to open this list.
_HELP = re.compile(
    r"(kya\s*kya\s*kar\s*(sakti|sakte|skti)|kya\s*kar\s*(sakti|skti)\s*ho|what\s+can\s+you\s+do|"
    r"^(sara\s+)?(show|open)?\s*(your\s+|sara\s+)?(skills|commands?|features)(\s+(list|dikhao|kholo|batao))?$|"
    r"^(sara\s+)?help$|help\s+(menu|karo)|क्या\s*क्या\s*कर\s*सकती|स्किल्स)",
    re.IGNORECASE,
)


def is_help_request(text: str) -> bool:
    return bool(_HELP.search(" ".join(text.strip().lower().split())))


def catalog() -> dict:
    count = sum(len(c["skills"]) for c in CATEGORIES)
    examples = sum(len(s["examples"]) for c in CATEGORIES for s in c["skills"])
    return {"categories": CATEGORIES, "count": count, "examples": examples}
