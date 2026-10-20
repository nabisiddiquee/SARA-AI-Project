"""Phone mic -> speech-to-text -> AI -> response.

Each finished utterance from AudioProcessor (bytes of 16-bit PCM + its format)
goes through ``Pipeline.__call__``:

    utterance PCM -> STT engine -> text -> (optional) AI model -> reply

Engines (pick with --stt):
* ``vosk``    offline, free, fast on CPU. pip install vosk, download a model
              (e.g. vosk-model-small-en-in-0.4) and set VOSK_MODEL=<folder>.
* ``whisper`` offline, more accurate. pip install faster-whisper
              (WHISPER_MODEL=base / small / medium; first run downloads it).
* ``openai``  cloud. pip install openai, set OPENAI_API_KEY.

AI reply (pick with --ai): ``openai`` (OPENAI_MODEL, default gpt-4o-mini), or
``ollama`` for a local model (OLLAMA_MODEL, default llama3.2).
Everything is imported lazily, so the receiver runs without these packages.
"""

from __future__ import annotations

import io
import json
import os
import urllib.request
import wave

import numpy as np

from protocol import AudioFormat


def _to_mono16k(pcm: bytes, fmt: AudioFormat) -> np.ndarray:
    """float32 mono 16 kHz array (what most STT models want)."""
    a = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    if fmt.channels > 1:
        a = a.reshape(-1, fmt.channels).mean(axis=1)
    if fmt.sample_rate != 16000:
        n = int(len(a) * 16000 / fmt.sample_rate)
        a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a).astype(np.float32)
    return a


def _wav_bytes(pcm: bytes, fmt: AudioFormat) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(fmt.channels)
        w.setsampwidth(2)
        w.setframerate(fmt.sample_rate)
        w.writeframes(pcm)
    return out.getvalue()


# --- speech-to-text engines --------------------------------------------------------

class VoskSTT:
    def __init__(self) -> None:
        from vosk import KaldiRecognizer, Model  # type: ignore

        path = os.environ.get("VOSK_MODEL")
        if not path:
            raise RuntimeError("set VOSK_MODEL to an unpacked Vosk model folder")
        self._model, self._Rec = Model(path), KaldiRecognizer

    def __call__(self, pcm: bytes, fmt: AudioFormat) -> str:
        a = _to_mono16k(pcm, fmt)
        rec = self._Rec(self._model, 16000)
        rec.AcceptWaveform((a * 32767).astype("<i2").tobytes())
        return json.loads(rec.FinalResult()).get("text", "")


class WhisperSTT:
    def __init__(self) -> None:
        from faster_whisper import WhisperModel  # type: ignore

        self._model = WhisperModel(os.environ.get("WHISPER_MODEL", "base"), device="cpu", compute_type="int8")

    def __call__(self, pcm: bytes, fmt: AudioFormat) -> str:
        segments, _info = self._model.transcribe(_to_mono16k(pcm, fmt), vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()


class OpenAISTT:
    def __init__(self) -> None:
        from openai import OpenAI  # type: ignore

        self._client = OpenAI()

    def __call__(self, pcm: bytes, fmt: AudioFormat) -> str:
        r = self._client.audio.transcriptions.create(
            model=os.environ.get("OPENAI_STT_MODEL", "whisper-1"),
            file=("utterance.wav", _wav_bytes(pcm, fmt)))
        return (r.text or "").strip()


STT_ENGINES = {"vosk": VoskSTT, "whisper": WhisperSTT, "openai": OpenAISTT}


# --- AI reply ------------------------------------------------------------------------

def openai_reply(text: str) -> str:
    from openai import OpenAI  # type: ignore

    r = OpenAI().chat.completions.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        messages=[{"role": "system", "content": "You are a helpful voice assistant. Answer briefly."},
                  {"role": "user", "content": text}])
    return (r.choices[0].message.content or "").strip()


def ollama_reply(text: str) -> str:
    body = json.dumps({"model": os.environ.get("OLLAMA_MODEL", "llama3.2"), "stream": False,
                       "messages": [{"role": "user", "content": text}]}).encode()
    req = urllib.request.Request("http://127.0.0.1:11434/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["message"]["content"].strip()


AI_ENGINES = {"openai": openai_reply, "ollama": ollama_reply}


class Pipeline:
    """utterance -> text -> (AI reply). Plug into AudioProcessor(on_utterance=...)."""

    def __init__(self, stt: str, ai: str | None = None) -> None:
        self.stt = STT_ENGINES[stt]()
        self.ai = AI_ENGINES[ai] if ai else None

    def __call__(self, pcm: bytes, fmt: AudioFormat) -> None:
        secs = len(pcm) / fmt.bytes_per_second
        text = self.stt(pcm, fmt)
        if not text:
            return
        print(f"\n[YOU said · {secs:.1f} s] {text}", flush=True)
        if self.ai:
            print(f"[AI] {self.ai(text)}", flush=True)
