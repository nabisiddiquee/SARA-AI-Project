import argparse
import os
import socket
import threading
import time
import webbrowser

from .actions import dispatch_command
from .voice import listen_once_vosk


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="my-python-project")
    sub = parser.add_subparsers(dest="command", required=True)

    hello = sub.add_parser("hello", help="Print a greeting")
    hello.add_argument("--name", default="world")

    listen = sub.add_parser("listen", help="Voice to text -> run a command (demo)")
    listen.add_argument(
        "--vosk-model",
        required=True,
        help="Path to an unpacked Vosk model folder (e.g. vosk-model-small-en-us-0.15)",
    )
    listen.add_argument("--seconds", type=float, default=4.0, help="How long to listen")

    serve = sub.add_parser("serve", help="Run a local web server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=1309)
    serve.add_argument(
        "--lan",
        action="store_true",
        help="Make the dashboard reachable from other devices (phone/laptop) on the same Wi-Fi",
    )

    train = sub.add_parser("train", help="Train Sara's intent model on this PC")
    train.add_argument("--no-system-apps", action="store_true",
                       help="Don't learn the names of apps installed on this PC")
    train.add_argument("--evaluate", action="store_true",
                       help="Also report cross-validated accuracy (slower)")

    teach = sub.add_parser("teach", help="Add your own example phrase, then retrain")
    teach.add_argument("text", help='The phrase, e.g. "tv band karo"')
    teach.add_argument("intent", help="Intent name, e.g. close_app or chat")

    predict = sub.add_parser("predict", help="Show what the intent model thinks a phrase means")
    predict.add_argument("text")

    return parser


def _lan_ip() -> str:
    """Best-effort local network IP of this machine."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packets sent; just picks the right interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _open_browser(host: str, port: int) -> None:
    """Open the UI page in the default web browser after a short delay."""
    time.sleep(1)
    webbrowser.open_new_tab(f"http://{host}:{port}/sara")


def _prewarm_tts() -> None:
    """Warm up neural TTS (import + one synthesis) at startup so the first
    spoken reply — usually the welcome — plays quickly instead of paying the
    cold-start cost. Silent no-op if TTS isn't installed or there's no network."""
    try:
        import asyncio

        from .neural_tts import synthesize

        asyncio.run(synthesize("Sara online", lang="en", gender="female"))
    except Exception:  # noqa: BLE001
        pass

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "hello":
        print(f"Hello, {args.name}!")
        return 0

    if args.command == "listen":
        r = listen_once_vosk(model_path=args.vosk_model, seconds=args.seconds)
        if not r.ok:
            print(r.message)
            return 1

        print(f"Heard: {r.text}")
        ar = dispatch_command(r.text)
        print(ar.message)
        return 0 if ar.ok else 2

    if args.command == "train":
        from . import intent

        include_system = not args.no_system_apps
        print("Training Sara's intent model...")
        model, count = intent.train_and_save(include_system=include_system)
        print(f"  Examples : {count}  (your phrases: {len(intent.load_user_examples())})")
        print(f"  Intents  : {', '.join(model.labels)}")
        print(f"  Saved to : {intent.MODEL_PATH}")
        if args.evaluate:
            acc = intent.evaluate(intent.training_examples(include_system=include_system))
            print(f"  Accuracy on unseen phrases (5-fold): {acc:.0%}")
        return 0

    if args.command == "teach":
        from . import intent

        known = sorted(intent.INTENT_EXAMPLES)
        if args.intent not in known:
            print(f"Unknown intent {args.intent!r}. Choose one of: {', '.join(known)}")
            return 2
        intent.add_user_example(args.text, args.intent)
        intent.train_and_save()
        pred = intent.predict(args.text)
        print(f"Learned: {args.text!r} -> {args.intent}  (model now says {pred.intent}, "
              f"{pred.confidence:.0%})")
        return 0

    if args.command == "predict":
        from . import intent

        pred = intent.predict(args.text)
        if pred is None:
            print("Intent model unavailable.")
            return 1
        print(f"{pred.intent}  ({pred.confidence:.0%} sure)")
        return 0

    if args.command == "serve":
        try:
            import uvicorn  # type: ignore
        except Exception as e:  # noqa: BLE001
            print(
                "Web server support not installed. Install extras:\n"
                "  python -m pip install -e \".[web]\"\n"
                f"Import error: {e}"
            )
            return 1

        host = "0.0.0.0" if args.lan else args.host
        # Lets the dashboard's 📱 Phone window know the phone can reach it.
        os.environ["SARA_LAN"] = "1" if host == "0.0.0.0" else "0"
        local_host = "127.0.0.1" if host in ("0.0.0.0", "") else host

        # Load .env so we can report whether the PIN lock is active.
        try:
            from .ai import _load_dotenv

            _load_dotenv()
        except Exception:  # noqa: BLE001
            pass
        pin_on = bool(os.environ.get("SARA_PIN", "").strip())

        print("SARA_OS is starting...")
        print(f"  This computer : http://{local_host}:{args.port}/sara")
        if host == "0.0.0.0":
            print(
                f"  Your network  : http://{_lan_ip()}:{args.port}/sara"
                "   (open this on your phone/laptop)"
            )
            print("  Tip: both devices must be on the same Wi-Fi; allow Python "
                  "through the firewall if Windows asks.")
        print(f"  PIN lock      : {'ON' if pin_on else 'OFF'}")
        if host == "0.0.0.0" and not pin_on:
            print("  WARNING: --lan exposes Sara to your whole network with no PIN.")
            print("           Set SARA_PIN in your .env to require a login (e.g. SARA_PIN=1234).")

        threading.Thread(target=_open_browser, args=(local_host, args.port), daemon=True).start()
        threading.Thread(target=_prewarm_tts, daemon=True).start()  # warm neural TTS
        uvicorn.run("my_python_project.server:app", host=host, port=args.port, reload=False)
        return 0

    parser.error("Unknown command")
    return 2
