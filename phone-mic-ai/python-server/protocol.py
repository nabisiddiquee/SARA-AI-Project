"""Wire format shared with the Android app.

Each connection starts with a 12-byte header, then raw PCM follows until the
connection closes:

    offset  size  field
    0       4     magic  b"PMIC"
    4       1     version (1)
    5       1     channels (1 = mono)
    6       1     bits per sample (16)
    7       1     reserved (0)
    8       4     sample rate, uint32 little-endian (16000 / 44100 / 48000)

PCM is signed 16-bit little-endian, interleaved if stereo. The Android app
sends 20 ms chunks, but the receiver doesn't rely on chunk boundaries.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = b"PMIC"
VERSION = 1
HEADER_SIZE = 12
_HEADER = struct.Struct("<4sBBBBI")

# RFCOMM service UUID; must match StreamProtocol.SERVICE_UUID in the app.
SERVICE_UUID = "8f4c2a10-6b3e-4f5d-9a7c-2e1b0d3c4a55"
SERVICE_NAME = "PhoneMic AI"
DEFAULT_USB_PORT = 5000


@dataclass(frozen=True)
class AudioFormat:
    sample_rate: int
    channels: int
    bits: int

    @property
    def bytes_per_frame(self) -> int:
        return self.channels * self.bits // 8

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.bytes_per_frame

    def describe(self) -> str:
        ch = "mono" if self.channels == 1 else f"{self.channels} ch"
        return f"{self.sample_rate} Hz · {self.bits}-bit · {ch}"


class ProtocolError(ValueError):
    pass


def build_header(fmt: AudioFormat) -> bytes:
    return _HEADER.pack(MAGIC, VERSION, fmt.channels, fmt.bits, 0, fmt.sample_rate)


def parse_header(data: bytes) -> AudioFormat:
    if len(data) < HEADER_SIZE:
        raise ProtocolError("header too short")
    magic, version, channels, bits, _reserved, rate = _HEADER.unpack(data[:HEADER_SIZE])
    if magic != MAGIC:
        raise ProtocolError(f"not a PhoneMic stream (magic {magic!r})")
    if version != VERSION:
        raise ProtocolError(f"unsupported protocol version {version}")
    if bits != 16 or channels not in (1, 2) or not 8000 <= rate <= 96000:
        raise ProtocolError(f"unsupported format: {rate} Hz, {bits}-bit, {channels} ch")
    return AudioFormat(sample_rate=rate, channels=channels, bits=bits)


class StreamParser:
    """Turns a byte stream into a format header followed by PCM blocks.
    Keeps PCM aligned to whole frames."""

    def __init__(self) -> None:
        self.format: AudioFormat | None = None
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[str, object]]:
        self._buf += data
        events: list[tuple[str, object]] = []
        if self.format is None:
            if len(self._buf) < HEADER_SIZE:
                return events
            self.format = parse_header(bytes(self._buf[:HEADER_SIZE]))
            del self._buf[:HEADER_SIZE]
            events.append(("format", self.format))
        usable = len(self._buf) - len(self._buf) % self.format.bytes_per_frame
        if usable:
            events.append(("audio", bytes(self._buf[:usable])))
            del self._buf[:usable]
        return events
