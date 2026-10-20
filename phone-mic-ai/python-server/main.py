"""Phone microphone -> Windows -> Python.

Examples
    python main.py                                 # Bluetooth (default)
    python main.py --transport usb                 # USB cable (adb forward)
    python main.py --virtual-mic "CABLE Input"     # also expose it as a Windows mic
    python main.py --record test.wav               # save what arrives
    python main.py --stt whisper --ai ollama       # speech-to-text + local AI reply
    python main.py --list-devices                  # audio outputs (for --virtual-mic)
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading

from audio_processor import AudioProcessor, VirtualMic
from audio_receiver import BluetoothReceiver, TcpReceiver
from protocol import DEFAULT_USB_PORT, SERVICE_UUID


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Use an Android phone as a live microphone (Bluetooth / USB).")
    p.add_argument("--transport", choices=["bluetooth", "usb", "tcp"], default="bluetooth",
                   help="bluetooth (default), usb (adb forward), or tcp (custom host:port, for testing)")
    p.add_argument("--port", type=int, default=DEFAULT_USB_PORT, help="USB/TCP port (default 5000)")
    p.add_argument("--host", default="127.0.0.1", help="TCP host (default 127.0.0.1)")
    p.add_argument("--uuid", default=SERVICE_UUID, help="Bluetooth RFCOMM service UUID (must match the app)")
    p.add_argument("--virtual-mic", metavar="DEVICE",
                   help='play into this output, e.g. "CABLE Input" (VB-Audio Virtual Cable)')
    p.add_argument("--record", metavar="FILE.wav", help="also save the audio to a WAV file")
    p.add_argument("--threshold", type=float, default=-40.0, help="speech threshold in dBFS (default -40)")
    p.add_argument("--stt", choices=["vosk", "whisper", "openai"], help="speech-to-text engine (optional)")
    p.add_argument("--ai", choices=["openai", "ollama"], help="send recognised text to an AI (needs --stt)")
    p.add_argument("--list-devices", action="store_true", help="list audio output devices and exit")
    p.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = run until Ctrl+C)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.list_devices:
        import sounddevice as sd

        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] > 0:
                print(f"{i:3d}  {d['name']}  ({d['hostapi']})")
        return 0

    on_utterance = None
    if args.stt:
        from stt import Pipeline

        print(f"[INFO] Loading speech-to-text: {args.stt}" + (f" + AI: {args.ai}" if args.ai else ""))
        on_utterance = Pipeline(args.stt, args.ai)

    processor = AudioProcessor(
        speech_threshold_db=args.threshold,
        virtual_mic=VirtualMic(args.virtual_mic) if args.virtual_mic else None,
        record_path=args.record,
        on_utterance=on_utterance,
    )
    if args.transport == "bluetooth":
        receiver = BluetoothReceiver(args.uuid)
    else:
        receiver = TcpReceiver(args.host, args.port, label="USB" if args.transport == "usb" else "TCP")
        if args.transport == "usb":
            print(f"[INFO] USB mode: run once per cable connection:  adb forward tcp:{args.port} tcp:{args.port}")

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if args.seconds > 0:
        threading.Timer(args.seconds, stop.set).start()
    worker = threading.Thread(target=receiver.run, args=(processor, stop), name="receiver", daemon=True)
    worker.start()
    try:
        while worker.is_alive() and not stop.is_set():
            worker.join(0.3)
    finally:
        stop.set()
        worker.join(3)
        processor.close()
        print("\n[INFO] Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
