"""Launch desktop applications by name.

Sara only knew how to open a few hard-coded apps (Notepad++, IntelliJ,
OpenVPN). This module adds a small registry of common Windows apps — including
Postman — so "postman open karo", "open chrome", "vlc chalu karo" all work.

Each entry lists the words that name the app, absolute candidate paths (globs
allowed) tried in order, and a shell fallback used when no path exists (so
apps registered on PATH or via the Windows app list still launch).
"""

from __future__ import annotations

import glob
import os
import subprocess

_USER = os.path.expanduser("~")

# Known applications. ``candidates`` are tried first (first existing one wins);
# ``fallback`` is a ``start`` command used when none are found on disk.
_APP_REGISTRY: tuple[dict, ...] = (
    {
        "label": "Postman",
        "aliases": ("postman",),
        "candidates": (rf"{_USER}\AppData\Local\Postman\Postman.exe",),
        "fallback": 'start "" postman',
        "exe": "Postman.exe",
    },
    {
        "label": "Google Chrome",
        "aliases": ("chrome", "google chrome"),
        "candidates": (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            rf"{_USER}\AppData\Local\Google\Chrome\Application\chrome.exe",
        ),
        "fallback": 'start "" chrome',
        "exe": "chrome.exe",
    },
    {
        "label": "Microsoft Edge",
        "aliases": ("edge", "microsoft edge", "msedge"),
        "candidates": (
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ),
        "fallback": 'start "" msedge',
        "exe": "msedge.exe",
    },
    {
        "label": "Visual Studio Code",
        "aliases": ("vs code", "vscode", "visual studio code", "code editor"),
        "candidates": (
            rf"{_USER}\AppData\Local\Programs\Microsoft VS Code\Code.exe",
            r"C:\Program Files\Microsoft VS Code\Code.exe",
        ),
        "fallback": 'start "" code',
        "exe": "Code.exe",
    },
    {
        "label": "Spotify",
        "aliases": ("spotify",),
        "candidates": (rf"{_USER}\AppData\Roaming\Spotify\Spotify.exe",),
        "fallback": 'start "" spotify',
        "exe": "Spotify.exe",
    },
    {
        "label": "VLC media player",
        "aliases": ("vlc", "vlc player", "vlc media player"),
        "candidates": (
            r"C:\Program Files\VideoLAN\VLC\vlc.exe",
            r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
        ),
        "fallback": 'start "" vlc',
        "exe": "vlc.exe",
    },
    {
        "label": "File Explorer",
        "aliases": ("file explorer", "explorer", "this pc", "my computer"),
        "candidates": (r"C:\Windows\explorer.exe",),
        "fallback": 'start "" explorer',
    },
    {
        "label": "Calculator",
        "aliases": ("calculator", "calc"),
        "candidates": (),
        "fallback": 'start "" calc',
    },
    {
        "label": "Command Prompt",
        "aliases": ("command prompt", "cmd"),
        "candidates": (),
        "fallback": 'start "" cmd',
    },
    {
        "label": "Settings",
        "aliases": ("settings", "windows settings"),
        "candidates": (),
        "fallback": "start ms-settings:",
    },
)


def _has(text: str, kws) -> bool:
    """Whole-word (ASCII) / substring (non-ASCII) keyword match.

    Mirrors the dispatcher's matching so app names behave like other triggers.
    """
    padded = " " + " ".join(text.lower().split()) + " "
    low = text.lower()
    for k in kws:
        k = k.lower()
        if k.isascii():
            if f" {k} " in padded:
                return True
        elif k in low:
            return True
    return False


def find_app(text: str) -> dict | None:
    """Return the registry entry whose alias appears in ``text``, else None."""
    for app in _APP_REGISTRY:
        if _has(text, app["aliases"]):
            return app
    return None


def _resolve_candidate(app: dict) -> str | None:
    """First existing path among an app's candidates (glob-aware), or None."""
    for pattern in app["candidates"]:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[-1]  # newest when patterns/versions sort by name
        if os.path.exists(pattern):
            return pattern
    return None


def launch_app(app: dict) -> bool:
    """Launch ``app`` via its resolved path, falling back to a shell command.

    Returns True if a launch was started, False if it failed outright.
    """
    path = _resolve_candidate(app)
    try:
        if path:
            subprocess.Popen([path])
        else:
            subprocess.Popen(app["fallback"], shell=True)
        return True
    except Exception:  # noqa: BLE001 - never let a bad launch crash Sara
        return False


# --- Generic launcher: open ANY installed app via its Start Menu shortcut. ---

# Where Windows keeps per-user and all-users Start Menu shortcuts.
_START_MENU_DIRS = (
    os.path.join(os.environ.get("APPDATA", ""),
                 r"Microsoft\Windows\Start Menu\Programs"),
    os.path.join(os.environ.get("PROGRAMDATA", ""),
                 r"Microsoft\Windows\Start Menu\Programs"),
)

# Filler words stripped when pulling an app name out of a command, so
# "postman open karo please" -> "postman".
_NAME_FILLERS = {
    "open", "launch", "start", "run", "kholo", "khol", "cholo", "chalu",
    "chalao", "chala", "chalado", "khol do", "kar", "karo", "kro", "kardo",
    "do", "de", "dena", "please", "pls", "app", "apps", "application",
    "program", "software", "the", "a", "an", "my", "mera", "meri", "mere",
    "system", "me", "in", "into", "par", "pe", "ko", "wala", "naam", "ki",
}

# Strong launch verbs that mark a command as "open something" (narrower than the
# dispatcher's open_kws so ordinary questions containing "start" aren't hijacked).
APP_OPEN_VERBS = (
    "open", "launch", "start", "run", "kholo", "khol", "chalu", "chalao",
    "chala", "chalado", "खोलो", "खोल", "चालू", "चलाओ", "स्टार्ट", "launch karo",
)


def _norm(s: str) -> str:
    """Lowercase and collapse separators so names compare cleanly."""
    return " ".join(s.lower().replace("-", " ").replace("_", " ").split())


def _iter_shortcuts():
    """Yield ``(path, display_name)`` for every Start Menu shortcut."""
    for base in _START_MENU_DIRS:
        if not base or not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            for f in files:
                if f.lower().endswith((".lnk", ".url")):
                    yield os.path.join(root, f), os.path.splitext(f)[0]


def extract_app_name(text: str, verbs) -> str:
    """Strip launch verbs and filler words, leaving the app name."""
    tokens = _norm(text).split()
    drop = _NAME_FILLERS | {_norm(v) for v in verbs}
    kept = [t for t in tokens if t not in drop]
    return " ".join(kept).strip()


def resolve_shortcut(query: str) -> tuple[str, str] | None:
    """Best Start Menu shortcut matching ``query``, or None if none is close.

    Scores exact > prefix > substring > all-query-words-present. Requires a
    reasonably strong match so a stray word doesn't launch a random app.
    """
    q = _norm(query)
    if not q:
        return None
    qtokens = set(q.split())
    best: tuple[str, str] | None = None
    best_score = 0
    for path, stem in _iter_shortcuts():
        s = _norm(stem)
        if not s:
            continue
        stokens = set(s.split())
        if s == q:
            score = 100
        elif s.startswith(q) or q.startswith(s):
            score = 80
        elif q in s:
            score = 60
        elif qtokens and qtokens <= stokens:
            score = 50
        elif qtokens & stokens:
            score = 20 * len(qtokens & stokens)
        else:
            continue
        # On ties, prefer the shorter (more exact) shortcut name.
        if score > best_score or (
            score == best_score and best is not None and len(s) < len(_norm(best[1]))
        ):
            best, best_score = (path, stem), score
    return best if best_score >= 50 else None


def open_by_name(query: str) -> tuple[bool, str]:
    """Open any installed app by name via its Start Menu shortcut.

    Returns ``(True, display_name)`` on success, or ``(False, "")`` when no
    matching app is found (so the caller can fall back to the AI brain).
    """
    hit = resolve_shortcut(query)
    if not hit:
        return False, ""
    path, stem = hit
    try:
        os.startfile(path)  # type: ignore[attr-defined]  # Windows-only
        return True, stem
    except Exception:  # noqa: BLE001
        return False, ""


# --- Closing apps. -----------------------------------------------------------

# Verbs that mean "close this app".
APP_CLOSE_VERBS = ("close", "quit", "exit", "kill", "band", "बंद", "بند",
                   "band karo", "close karo", "band kardo")

# Critical processes we must never kill, even if a name happens to match.
_CLOSE_BLOCKLIST = {
    "explorer.exe", "csrss.exe", "winlogon.exe", "services.exe",
    "svchost.exe", "system", "wininit.exe", "lsass.exe",
}


def close_by_name(name: str) -> tuple[bool, str]:
    """Close a running app by name using ``taskkill``.

    Tries the known executable name for registry apps first, then a couple of
    guesses derived from the spoken name. Returns ``(True, label)`` if a
    process was killed, else ``(False, label)``.
    """
    app = find_app(name)
    label = app["label"] if app else name.strip()

    guesses: list[str] = []
    if app and app.get("exe"):
        guesses.append(app["exe"])
    norm = _norm(name)
    if norm:
        guesses.append(norm.replace(" ", "") + ".exe")  # "mongodb compass" -> mongodbcompass.exe
        guesses.append(norm.split()[0] + ".exe")         # first word fallback

    for exe in dict.fromkeys(g.lower() for g in guesses):  # dedupe, keep order
        if exe in _CLOSE_BLOCKLIST:
            continue
        try:
            result = subprocess.run(
                ["taskkill", "/IM", exe, "/F"],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                return True, label
        except Exception:  # noqa: BLE001
            continue
    return False, label
