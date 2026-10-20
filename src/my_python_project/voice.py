from __future__ import annotations

import json
import queue
from dataclasses import dataclass


@dataclass(frozen=True)
class VoiceToTextResult:
    ok: bool
    text: str
    message: str = ""


def listen_once_vosk(
    *,
    model_path: str,
    seconds: float = 4.0,
    samplerate: int = 16000,
) -> VoiceToTextResult:
    """
    Offline mic -> text using Vosk.

    Prereqs:
      - pip install -e ".[voice]"
      - download a Vosk model and pass its folder path, e.g.:
        https://alphacephei.com/vosk/models
    """
    try:
        import sounddevice as sd  # type: ignore
        from vosk import KaldiRecognizer, Model  # type: ignore
    except Exception as e:  # noqa: BLE001
        return VoiceToTextResult(
            ok=False,
            text="",
            message=(
                "Voice support not installed. Install extras:\n"
                "  python -m pip install -e \".[voice]\"\n"
                f"Import error: {e}"
            ),
        )

    try:
        model = Model(model_path)
    except Exception as e:  # noqa: BLE001
        return VoiceToTextResult(ok=False, text="", message=f"Failed to load model: {e}")

    audio_q: queue.Queue[bytes] = queue.Queue()

    def callback(indata, frames, time, status):  # noqa: ANN001,ARG001
        audio_q.put(bytes(indata))

    rec = KaldiRecognizer(model, samplerate)

    try:
        with sd.RawInputStream(
            samplerate=samplerate,
            blocksize=8000,
            dtype="int16",
            channels=1,
            callback=callback,
        ):
            total_bytes_target = int(seconds * samplerate * 2)  # 16-bit mono
            collected = 0
            while collected < total_bytes_target:
                chunk = audio_q.get()
                collected += len(chunk)
                rec.AcceptWaveform(chunk)
    except Exception as e:  # noqa: BLE001
        return VoiceToTextResult(ok=False, text="", message=f"Mic error: {e}")

    try:
        final = json.loads(rec.FinalResult())
        text = (final.get("text") or "").strip()
    except Exception as e:  # noqa: BLE001
        return VoiceToTextResult(ok=False, text="", message=f"Decode error: {e}")

    if not text:
        return VoiceToTextResult(ok=False, text="", message="No speech detected.")
    return VoiceToTextResult(ok=True, text=text)
