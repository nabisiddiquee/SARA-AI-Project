"""Look at and create files/folders anywhere on the machine.

Sara can list a folder's contents and create folders/files at a named location
(Desktop, Documents, "D drive", or an explicit ``C:\\path``). Everything is
plain stdlib — no extra dependencies.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_HOME = Path.home()


def _win_shell_folder(reg_name: str) -> Path | None:
    """Real path of a Windows known folder (handles OneDrive redirection)."""
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            val, _ = winreg.QueryValueEx(key, reg_name)
            path = Path(os.path.expandvars(val))
            return path if path.exists() else None
    except OSError:
        return None


# Friendly name -> (registry value name, fallback subfolder under home).
_KNOWN_SPEC: dict[str, tuple[str | None, str]] = {
    "desktop": ("Desktop", "Desktop"),
    "documents": ("Personal", "Documents"),
    "document": ("Personal", "Documents"),
    "downloads": ("{374DE290-123F-4565-9164-39C4925E467B}", "Downloads"),
    "download": ("{374DE290-123F-4565-9164-39C4925E467B}", "Downloads"),
    "pictures": ("My Pictures", "Pictures"),
    "music": ("My Music", "Music"),
    "videos": ("My Video", "Videos"),
    "home": (None, ""),
}


def _resolve_known(name: str) -> Path:
    reg, sub = _KNOWN_SPEC[name]
    if reg:
        path = _win_shell_folder(reg)
        if path:
            return path
    return _HOME / sub if sub else _HOME


def desktop_dir() -> Path:
    """The user's real, visible Desktop (the default place to create things)."""
    return _resolve_known("desktop")

_PATH_RE = re.compile(r"[a-zA-Z]:\\[^\n\"']*")        # C:\Users\Me\...
_DRIVE_RE = re.compile(r"\b([a-zA-Z])\s*drive\b", re.IGNORECASE)  # "D drive"

# Connector words stripped from a folder/file name.
_FS_CONNECTORS = {
    "in", "on", "at", "the", "a", "an", "to", "into", "inside", "drive",
    "me", "mein", "par", "pe", "ka", "ki", "ke", "ko", "named", "name",
    "called", "naam", "wala", "wale",
    # Hindi helper verbs / articles that wrap a create command.
    "do", "de", "dena", "diya", "kar", "karo", "kardo", "kr", "kro", "ek",
}


def resolve_location(text: str) -> tuple[str | None, str]:
    """Return ``(path, matched_text)`` for a location named in ``text``.

    Recognises an explicit Windows path, "<letter> drive", or a known folder
    name (Desktop, Downloads, ...). Returns ``(None, "")`` if none is found.
    """
    m = _PATH_RE.search(text)
    if m:
        cand = m.group(0).rstrip("\\/ ")
        # Windows paths may contain spaces, so the regex can over-match into the
        # following words (e.g. "... named foo"). Trim trailing tokens until the
        # path actually exists; the trimmed words fall back to being the name.
        parts = cand.split(" ")
        while len(parts) > 1:
            trial = " ".join(parts)
            if Path(trial).exists():
                return trial, trial
            parts.pop()
        return parts[0], parts[0]
    m = _DRIVE_RE.search(text)
    if m:
        return m.group(1).upper() + ":\\", m.group(0)
    low = text.lower()
    for name in _KNOWN_SPEC:
        if re.search(r"\b" + re.escape(name) + r"\b", low):
            return str(_resolve_known(name)), name
    return None, ""


def parse_create(text: str, keywords) -> tuple[str | None, str]:
    """Pull ``(location, name)`` out of a create-folder/file command."""
    rest = text
    for k in keywords:
        # Word boundaries for ASCII keywords so "bana" doesn't chew into "banao".
        pat = r"\b" + re.escape(k) + r"\b" if k.isascii() else re.escape(k)
        rest = re.sub(pat, " ", rest, flags=re.IGNORECASE)
    location, matched = resolve_location(rest)
    if matched:
        rest = re.sub(re.escape(matched), " ", rest, flags=re.IGNORECASE)
    tokens = [t for t in rest.split() if t.lower().strip(".,:;") not in _FS_CONNECTORS]
    name = " ".join(tokens).strip(" .:-")
    return location, name


def create_entry(name: str, location: str | None, *, is_file: bool = False,
                 lang: str = "en") -> tuple[bool, str]:
    """Create a folder (or file) called ``name`` inside ``location``."""
    if not name:
        return False, ("नाम समझ नहीं आया।" if lang == "hi" else "I didn't catch the name.")
    # Default to the visible Desktop (not the home root) so the user can see it.
    base = Path(location) if location else desktop_dir()
    target = base / name
    try:
        if is_file:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("Created by Sara\n", encoding="utf-8")
        else:
            target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, (
            f"नहीं बना पाई: {e}" if lang == "hi" else f"Couldn't create it: {e}"
        )
    if lang == "hi":
        kind = "फ़ाइल" if is_file else "फ़ोल्डर"
        return True, f"{kind} बना दी: {target}"
    kind = "file" if is_file else "folder"
    return True, f"Created {kind}: {target}"


def list_dir(text: str, *, lang: str = "en", limit: int = 40) -> tuple[bool, str]:
    """List the folders and files at a location named in ``text``."""
    location, _ = resolve_location(text)
    base = Path(location) if location else _HOME
    if not base.exists():
        return False, (f"पाथ नहीं मिला: {base}" if lang == "hi" else f"Path not found: {base}")
    if not base.is_dir():
        return False, (f"{base} फ़ोल्डर नहीं है।" if lang == "hi" else f"{base} is not a folder.")
    try:
        entries = sorted(os.scandir(base), key=lambda e: (not e.is_dir(), e.name.lower()))
    except PermissionError:
        return False, (
            "इस फ़ोल्डर को पढ़ने की अनुमति नहीं है।" if lang == "hi"
            else "Permission denied for that folder."
        )
    folders = [e.name for e in entries if e.is_dir()]
    files = [e.name for e in entries if e.is_file()]
    shown = (folders + files)[:limit]
    extra = len(folders) + len(files) - len(shown)

    if lang == "hi":
        head = f"{base} में {len(folders)} फ़ोल्डर, {len(files)} फ़ाइलें:"
    else:
        head = f"{base}: {len(folders)} folders, {len(files)} files:"
    body = ", ".join(shown) if shown else "(empty)"
    tail = f" …+{extra} more" if extra > 0 else ""
    return True, head + "\n" + body + tail
