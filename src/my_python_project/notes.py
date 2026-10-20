"""Notebook storage: plain .txt files in the project's ``notes`` folder.

A new note is named after the date ("Note 01-Oct-2026"); if that name is
taken, " (2)", " (3)"... is added. Renaming a note renames its file.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime
from pathlib import Path

NOTES_DIR = Path(__file__).resolve().parents[2] / "notes"
_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _dir() -> Path:
    NOTES_DIR.mkdir(exist_ok=True)
    return NOTES_DIR


def clean_name(name: str) -> str:
    """Safe file name (no path tricks); empty -> today's default."""
    name = _BAD.sub(" ", name or "").strip().strip(".")
    name = re.sub(r"\s+", " ", name)[:80]
    return name or default_name()


def default_name() -> str:
    return "Note " + datetime.now().strftime("%d-%b-%Y")


def _path(name: str) -> Path:
    return _dir() / f"{clean_name(name)}.txt"


def unique_name(name: str, exclude: str | None = None) -> str:
    """``name``, or ``name (2)``, ``name (3)``... if a different note has it."""
    base = clean_name(name)
    candidate, n = base, 2
    while _path(candidate).exists() and candidate != exclude:
        candidate = f"{base} ({n})"
        n += 1
    return candidate


def list_notes() -> list[dict]:
    items = []
    for p in _dir().glob("*.txt"):
        st = p.stat()
        items.append({"name": p.stem, "modified": st.st_mtime, "size": st.st_size})
    items.sort(key=lambda d: d["modified"], reverse=True)
    return items


def read(name: str) -> dict:
    p = _path(name)
    if not p.exists():
        return {"ok": False, "error": "Note not found."}
    return {"ok": True, "name": p.stem, "text": p.read_text(encoding="utf-8")}


def save(name: str, text: str, old_name: str | None = None, is_new: bool = False) -> dict:
    """Save ``text``. With ``old_name`` the note is renamed to ``name`` first
    (made unique if needed); ``is_new`` never overwrites another note.
    Returns the final name."""
    name = clean_name(name)
    if is_new:
        name = unique_name(name)
    elif old_name:
        old = clean_name(old_name)
        if old != name:
            name = unique_name(name, exclude=None)
            if _path(old).exists():
                os.replace(_path(old), _path(name))
        else:
            name = old
    _path(name).write_text(text or "", encoding="utf-8")
    return {"ok": True, "name": name, "saved_at": time.time()}


def new_name() -> str:
    """Today's default name, made unique."""
    return unique_name(default_name())


def delete(name: str) -> dict:
    p = _path(name)
    if p.exists():
        p.unlink()
        return {"ok": True}
    return {"ok": False, "error": "Note not found."}
