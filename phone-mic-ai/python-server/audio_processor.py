"""Real-time processing of the phone's PCM stream.

* level meter (RMS in dBFS) and simple speech detection (energy + hangover)
* a live status line: connected / format / level / speaking
* optional output to a virtual audio cable, so every Windows app can use the
  phone as a microphone ("CABLE Input" -> apps record from "CABLE Output")
* optional WAV recording
* utterance segmentation: complete phrases are handed to ``on_utterance``
  (speech-to-text / AI hook, see stt.py)
"""

from __future__ import annotations

import sys
import threading
import time
import wave
from typing import Callable

import numpy as np

from protocol import AudioFormat

Utterance = Callable[[bytes, AudioFormat], None]


def _db(rms: float) -> float:
    return 20 * np.log10(max(rms, 1e-9))


class VirtualMic:
    """Plays the stream into an output device, e.g. VB-Audio "CABLE Input".
    Windows apps then select "CABLE Output" as their microphone."""

    def __init__(self, device_name: str) -> None:
        self.device_name = device_name
        self._stream = None
        self._fmt: AudioFormat | None = None

    def _find(self) -> int:
        import sounddevice as sd

        name = self.device_name.lower()
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] > 0 and name in d["name"].lower():
                return i
        raise RuntimeError(f"output device containing '{self.device_name}' not found "
                           "(install VB-Audio Virtual Cable, or run with --list-devices)")

    def open(self, fmt: AudioFormat) -> str:
        import sounddevice as sd

        self.close()
        dev = self._find()
        self._stream = sd.RawOutputStream(samplerate=fmt.sample_rate, channels=fmt.channels,
                                          dtype="int16", device=dev, latency="low")
        self._stream.start()
        self._fmt = fmt
        return sd.query_devices(dev)["name"]

    def write(self, pcm: bytes) -> None:
        if self._stream is not None:
            try:
                self._stream.write(pcm)
            except Exception:  # noqa: BLE001 - underrun / device removed: drop this block
                pass

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._stream = None


class AudioProcessor:
    """The receiver's sink: everything that happens to incoming audio."""

    def __init__(
        self,
        *,
        speech_threshold_db: float = -40.0,
        hangover_s: float = 0.7,
        min_utterance_s: float = 0.35,
        max_utterance_s: float = 15.0,
        virtual_mic: VirtualMic | None = None,
        record_path: str | None = None,
        on_utterance: Utterance | None = None,
        status_interval: float = 0.5,
    ) -> None:
        self.threshold = speech_threshold_db
        self.hangover = hangover_s
        self.min_utt, self.max_utt = min_utterance_s, max_utterance_s
        self.virtual_mic = virtual_mic
        self.record_path = record_path
        self.on_utterance = on_utterance
        self.status_interval = status_interval

        self.fmt: AudioFormat | None = None
        self.source = ""
        self.connected = False
        self.level_db = -120.0
        self.speaking = False
        self.bytes_total = 0
        self._last_voice = 0.0
        self._utt = bytearray()
        self._wav: wave.Wave_write | None = None
        self._last_status = 0.0
        self._connected_at = 0.0
        self._lock = threading.Lock()
        self._tty = sys.stdout.isatty()
        if not self._tty:                 # logs / pipes: one live line every 5 s
            self.status_interval = max(self.status_interval, 5.0)

    # -- sink interface ----------------------------------------------------------
    def on_status(self, text: str) -> None:
        self._line(f"[INFO] {text}", final=True)

    def on_connected(self, fmt: AudioFormat, source: str) -> None:
        with self._lock:
            self.fmt, self.source, self.connected = fmt, source, True
            self._connected_at = time.monotonic()
            self.bytes_total = 0
        self._line(f"[CONNECTED] Phone microphone via {source}: {fmt.describe()}", final=True)
        if self.virtual_mic:
            try:
                dev = self.virtual_mic.open(fmt)
                self._line(f"[VIRTUAL MIC] Playing into '{dev}' (Windows apps: choose its matching input)", final=True)
            except Exception as e:  # noqa: BLE001
                self._line(f"[VIRTUAL MIC] Disabled: {e}", final=True)
                self.virtual_mic = None
        if self.record_path and self._wav is None:
            self._wav = wave.open(self.record_path, "wb")
            self._wav.setnchannels(fmt.channels)
            self._wav.setsampwidth(2)
            self._wav.setframerate(fmt.sample_rate)
            self._line(f"[RECORD] Writing {self.record_path}", final=True)

    def on_audio(self, pcm: bytes) -> None:
        fmt = self.fmt
        if fmt is None:
            return
        self.bytes_total += len(pcm)
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
        self.level_db = _db(rms)
        now = time.monotonic()

        if self.virtual_mic:
            self.virtual_mic.write(pcm)
        if self._wav is not None:
            self._wav.writeframes(pcm)

        # Speech detection with hangover, and utterance collection.
        if self.level_db > self.threshold:
            self._last_voice = now
            if not self.speaking:
                self.speaking = True
                self._utt.clear()
        if self.speaking:
            self._utt += pcm
            too_long = len(self._utt) >= self.max_utt * fmt.bytes_per_second
            if now - self._last_voice > self.hangover or too_long:
                self.speaking = False
                self._emit_utterance()

        if now - self._last_status >= self.status_interval:
            self._last_status = now
            self._print_live()

    def on_disconnected(self, reason: str) -> None:
        with self._lock:
            was = self.connected
            self.connected = False
        if self.speaking:
            self.speaking = False
            self._emit_utterance()
        if self.virtual_mic:
            self.virtual_mic.close()
        if was:
            secs = time.monotonic() - self._connected_at
            self._line(f"[DISCONNECTED] {reason} (after {secs:.0f} s, "
                       f"{self.bytes_total / 1024:.0f} KB received). Will reconnect automatically.", final=True)

    # -- helpers -----------------------------------------------------------------
    def close(self) -> None:
        if self._wav is not None:
            self._wav.close()
            self._wav = None
        if self.virtual_mic:
            self.virtual_mic.close()

    def _emit_utterance(self) -> None:
        fmt, data = self.fmt, bytes(self._utt)
        self._utt.clear()
        if not fmt or not self.on_utterance or len(data) < self.min_utt * fmt.bytes_per_second:
            return
        # Run STT / AI off the audio thread so the stream never stalls.
        threading.Thread(target=self._safe_utterance, args=(data, fmt), daemon=True).start()

    def _safe_utterance(self, data: bytes, fmt: AudioFormat) -> None:
        try:
            self.on_utterance(data, fmt)  # type: ignore[misc]
        except Exception as e:  # noqa: BLE001
            self._line(f"[STT] Error: {e}", final=True)

    def _print_live(self) -> None:
        bars = int(max(0.0, min(1.0, (self.level_db + 60) / 60)) * 20)
        meter = "█" * bars + "·" * (20 - bars)
        state = "SPEAKING" if self.speaking else "silent  "
        kbps = self.fmt.bytes_per_second * 8 / 1000 if self.fmt else 0
        self._line(f"[LIVE] {self.source} · {self.fmt.describe() if self.fmt else ''} · "
                   f"{self.level_db:6.1f} dBFS {meter} {state} · {kbps:.0f} kbit/s")

    def _line(self, text: str, final: bool = False) -> None:
        if self._tty and not final:
            sys.stdout.write("\r" + text[:150].ljust(150))
            sys.stdout.flush()
        else:
            if self._tty:
                sys.stdout.write("\r" + " " * 150 + "\r")
            print(text, flush=True)
