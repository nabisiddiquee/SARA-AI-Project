"""Pretend to be the phone app in USB mode, for testing without a phone.

Listens on 127.0.0.1:<port> like the app does (behind ``adb forward``), sends
the header and then 20 ms PCM chunks in real time: a tone that switches on and
off (so speech detection triggers), with silence in between. With --drop N it
cuts the connection after N seconds to exercise the receiver's reconnect.

    python tools/fake_phone.py --port 5600 --drop 6
    python main.py --transport tcp --port 5600
"""

from __future__ import annotations

import argparse
import math
import socket
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from protocol import AudioFormat, build_header  # noqa: E402


def chunk(t0: float, n: int, rate: int) -> bytes:
    out = bytearray()
    for i in range(n):
        t = t0 + i / rate
        on = (t % 2.0) < 1.0                       # 1 s tone, 1 s silence
        v = 0.4 * math.sin(2 * math.pi * 440 * t) if on else 0.0005 * math.sin(2 * math.pi * 50 * t)
        out += struct.pack("<h", int(v * 32767))
    return bytes(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--rate", type=int, default=16000)
    ap.add_argument("--drop", type=float, default=0, help="close each connection after N seconds")
    ap.add_argument("--sessions", type=int, default=0, help="exit after N sessions (0 = forever)")
    args = ap.parse_args()

    fmt = AudioFormat(sample_rate=args.rate, channels=1, bits=16)
    per = args.rate // 50                                   # 20 ms
    srv = socket.create_server(("127.0.0.1", args.port))
    print(f"fake phone listening on 127.0.0.1:{args.port} ({fmt.describe()})", flush=True)
    sessions = 0
    while not args.sessions or sessions < args.sessions:
        conn, _ = srv.accept()
        sessions += 1
        print(f"session {sessions}: receiver connected", flush=True)
        start, sent = time.monotonic(), 0
        try:
            conn.sendall(build_header(fmt))
            while not args.drop or time.monotonic() - start < args.drop:
                conn.sendall(chunk(sent / args.rate, per, args.rate))
                sent += per
                time.sleep(max(0.0, start + sent / args.rate - time.monotonic()))
            print(f"session {sessions}: dropping the connection (simulated disconnect)", flush=True)
        except OSError as e:
            print(f"session {sessions}: {e}", flush=True)
        finally:
            conn.close()
        time.sleep(1)


if __name__ == "__main__":
    main()
