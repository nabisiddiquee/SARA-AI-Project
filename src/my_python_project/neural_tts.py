"""Neural text-to-speech via Microsoft Edge's online voices (``edge-tts``).

This gives Sara a far more natural voice than the local ``pyttsx3``/browser
voices, for free and without an API key. It needs an internet connection.

Voices are chosen by language (English/Hindi) and the gender Sara should speak
in. The web UI passes the *opposite* of the user's gender so a male user hears a
female voice and vice-versa (see ``server.py``). Install with::

    python -m pip install -e ".[neural]"
"""

from __future__ import annotations

# Edge neural voices for each (language, gender) pair. India-accented English
# and native Hindi voices keep Sara's bilingual replies natural.
_VOICES = {
    ("en", "female"): "en-IN-NeerjaNeural",
    ("en", "male"): "en-IN-PrabhatNeural",
    ("hi", "female"): "hi-IN-SwaraNeural",
    ("hi", "male"): "hi-IN-MadhurNeural",
    # Arabic (e.g. "Bismillah" before a Quran recitation).
    ("ar", "female"): "ar-SA-ZariyahNeural",
    ("ar", "male"): "ar-SA-HamedNeural",
}

_DEFAULT = _VOICES[("en", "female")]


def speech_settings() -> tuple[str, str]:
    """Speaking rate and volume for edge-tts, e.g. ("+25%", "+50%").

    Defaults: 1.25x speed and louder than Edge's quiet default. Override in
    .env with SARA_TTS_RATE (e.g. +10%, +50%) and SARA_TTS_VOLUME (-50%..+100%).
    """
    import os
    import re

    def pct(name: str, default: str) -> str:
        v = os.environ.get(name, default).strip().replace(" ", "")
        return v if re.fullmatch(r"[+-]\d{1,3}%", v) else default

    return pct("SARA_TTS_RATE", "+25%"), pct("SARA_TTS_VOLUME", "+50%")


def pick_voice(lang: str, gender: str) -> str:
    """Return the Edge voice name for a language ('en'/'hi'/'ar') and gender."""
    low = str(lang).lower()
    lang = "hi" if low.startswith("hi") else "ar" if low.startswith("ar") else "en"
    gender = "male" if str(gender).lower() == "male" else "female"
    return _VOICES.get((lang, gender), _DEFAULT)


async def synthesize(text: str, *, lang: str = "en", gender: str = "female") -> bytes:
    """Render ``text`` to MP3 audio bytes with an Edge neural voice.

    Raises ``RuntimeError`` if ``edge-tts`` is not installed; other exceptions
    (e.g. no network) propagate so the caller can fall back to browser TTS.
    """
    try:
        import edge_tts  # type: ignore
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            "Neural TTS not installed. Enable it with: "
            'python -m pip install -e ".[neural]"'
        ) from e

    voice = pick_voice(lang, gender)
    rate, volume = speech_settings()
    if str(lang).lower().startswith("ar"):
        rate = "-10%"          # Arabic (Bismillah / duas): calm and clear, not rushed
    communicate = edge_tts.Communicate(text, voice, rate=rate, volume=volume)
    chunks: list[bytes] = []
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            chunks.append(chunk["data"])
    return b"".join(chunks)
