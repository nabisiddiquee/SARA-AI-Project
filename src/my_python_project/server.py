from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import urllib.request
import uuid
import time
from pathlib import Path
from time import monotonic, perf_counter, sleep

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .actions import dispatch_command
from .events import emit, list_events
from .tts import set_muted

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


_NETINFO_TTL = 600.0
_netinfo_cache: dict = {"at": -_NETINFO_TTL, "data": None}


def _netinfo() -> dict:
    """LAN IP plus public IP and its approximate location (ip-api.com, free,
    no key). The lookup is cached for 10 minutes; on failure only the LAN IP
    is returned."""
    now = monotonic()
    if _netinfo_cache["data"] and now - _netinfo_cache["at"] < _NETINFO_TTL:
        return {**_netinfo_cache["data"], "lan_ip": _lan_ip()}
    data: dict = {}
    try:
        url = ("http://ip-api.com/json/?fields=status,query,city,regionName,"
               "country,countryCode,lat,lon,isp,timezone")
        with urllib.request.urlopen(url, timeout=5) as r:
            j = json.loads(r.read().decode("utf-8"))
        if j.get("status") == "success":
            data = {
                "public_ip": j.get("query"), "city": j.get("city"),
                "region": j.get("regionName"), "country": j.get("country"),
                "country_code": j.get("countryCode"), "lat": j.get("lat"),
                "lon": j.get("lon"), "isp": j.get("isp"), "timezone": j.get("timezone"),
            }
            _netinfo_cache.update(at=now, data=data)
    except Exception:  # noqa: BLE001
        pass
    return {**data, "lan_ip": _lan_ip()}


def _token_for(pin: str) -> str:
    """Opaque auth-cookie token derived from the PIN."""
    return hashlib.sha256(f"sara-pin::{pin}".encode()).hexdigest()


def _login_html(*, error: bool = False) -> str:
    """Small themed PIN login page."""
    err = (
        '<div class="err">Wrong PIN — try again.</div>' if error else '<div class="err"></div>'
    )
    return f"""
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SARA_OS // LOCKED</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    height: 100vh; display: flex; align-items: center; justify-content: center;
    font-family: 'JetBrains Mono', ui-monospace, monospace;
    background: radial-gradient(circle at center, #0a1424, #050b14);
    color: #a7f3d0;
  }}
  .card {{
    width: min(360px, 90vw); padding: 32px;
    background: rgba(10,20,35,0.7); border: 1px solid rgba(16,185,129,0.3);
    border-radius: 14px; box-shadow: 0 0 40px rgba(16,185,129,0.25); text-align: center;
  }}
  .title {{ font-size: 20px; font-weight: 700; letter-spacing: 3px; color: #10b981;
    text-shadow: 0 0 12px rgba(16,185,129,0.6); margin-bottom: 6px; }}
  .sub {{ font-size: 11px; letter-spacing: 1px; color: #059669; margin-bottom: 20px; }}
  input {{ width: 100%; padding: 14px; text-align: center; letter-spacing: 6px; font-size: 18px;
    background: rgba(0,0,0,0.4); border: 1px solid rgba(255,255,255,0.1); border-radius: 8px;
    color: #a7f3d0; outline: none; font-family: inherit; }}
  input:focus {{ border-color: #10b981; box-shadow: 0 0 15px rgba(16,185,129,0.3); }}
  button {{ width: 100%; margin-top: 16px; padding: 14px; cursor: pointer; font-weight: 700;
    letter-spacing: 2px; text-transform: uppercase; background: rgba(16,185,129,0.15);
    border: 1px solid #10b981; border-radius: 8px; color: #10b981; font-family: inherit; }}
  button:hover {{ background: rgba(16,185,129,0.25); box-shadow: 0 0 15px rgba(16,185,129,0.4); }}
  .err {{ min-height: 18px; margin-top: 12px; font-size: 11px; color: #ef4444; letter-spacing: 1px; }}
</style></head>
<body>
  <form class="card" method="post" action="/login">
    <div class="title">SARA_OS</div>
    <div class="sub">ENTER PIN TO UNLOCK</div>
    <input type="password" name="pin" inputmode="numeric" autocomplete="off"
           placeholder="••••" autofocus>
    <button type="submit">Unlock</button>
    {err}
  </form>
</body></html>
"""


def _normalize_text(text: str) -> str:
    return " ".join(text.lower().strip().split())


def create_app() -> FastAPI:
    app = FastAPI(title="my_python_project", version="0.1.0")

    # Load .env so SARA_PIN (and other settings) are available at startup.
    try:
        from .ai import _load_dotenv

        _load_dotenv()
    except Exception:  # noqa: BLE001
        pass

    # New Gmail / WhatsApp message alerts (only while the server is running).
    @app.on_event("startup")
    def _start_inbox_watch() -> None:
        from . import captures_mail, inbox

        inbox.start()
        captures_mail.start()

    # A fresh id each time the server process starts. The UI re-asks the user's
    # name whenever this changes (i.e. on every server restart).
    run_id = uuid.uuid4().hex

    # Optional PIN lock. When SARA_PIN is set, every page/API requires a login
    # cookie — important when serving on the network (`serve --lan`).
    pin = os.environ.get("SARA_PIN", "").strip()
    auth_token = _token_for(pin) if pin else ""
    _cookie = "sara_auth"
    # Reachable without logging in.
    _open_paths = {"/login", "/health", "/favicon.ico"}

    # Paths that fire on a timer from the UI; logging them would flood the feed.
    _quiet_paths = {"/events", "/health", "/favicon.ico", "/logs/backend"}
    # Timer polls: in the BACKEND log only when they fail or are slow.
    _poll_paths = {"/bluetooth/state", "/ac/state", "/ac/history", "/ac/report", "/ac/schedule", "/remote/status", "/netinfo", "/session"}

    from . import backend_log
    backend_log.install()

    # Small cache of rendered TTS audio so repeated phrases (e.g. the greeting)
    # replay instantly instead of hitting the neural service again.
    _tts_cache: dict[tuple[str, str, str], bytes] = {}
    _TTS_CACHE_MAX = 48

    @app.middleware("http")
    async def gate_and_log(request: Request, call_next):
        path = request.url.path
        # Enforce the PIN lock (if configured) before anything else.
        if auth_token and path not in _open_paths:
            if request.cookies.get(_cookie) != auth_token:
                # Send browser page-loads to the login screen; fail API calls.
                if request.method == "GET" and path in {"/", "/sara", "/ui"}:
                    return RedirectResponse(url="/login")
                return Response("Unauthorized", status_code=401)
        # Every request goes to the BACKEND log with its status and time taken
        # (path only: query strings can carry text the user typed).
        t0 = perf_counter()
        try:
            response = await call_next(request)
        except Exception as e:  # noqa: BLE001
            backend_log.add("ERROR", "http", f"{request.method} {path} crashed · {type(e).__name__}: {e}")
            raise
        ms = (perf_counter() - t0) * 1000
        status = response.status_code
        if path not in _quiet_paths and (path not in _poll_paths or status >= 400 or ms > 1500):
            backend_log.request(request.method, path, status, ms)
        return response

    @app.get("/login", response_class=HTMLResponse)
    def login_page(error: int = 0):
        if not auth_token:
            return RedirectResponse(url="/sara")
        return _login_html(error=bool(error))

    @app.post("/login")
    async def login_submit(request: Request):
        # Parse the urlencoded form body directly (avoids a python-multipart dep).
        from urllib.parse import parse_qs

        body = (await request.body()).decode("utf-8", "ignore")
        entered = (parse_qs(body).get("pin", [""])[0]).strip()
        if auth_token and _token_for(entered) == auth_token:
            resp = RedirectResponse(url="/sara", status_code=303)
            resp.set_cookie(
                _cookie, auth_token, httponly=True, samesite="lax",
                max_age=60 * 60 * 24 * 30,
            )
            return resp
        return RedirectResponse(url="/login?error=1", status_code=303)

    @app.get("/logout")
    def logout():
        resp = RedirectResponse(url="/login" if auth_token else "/sara")
        resp.delete_cookie(_cookie)
        return resp

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/netinfo")
    def netinfo():
        return _netinfo()

    # --- Notebook: .txt files in the project's notes/ folder ---------------
    @app.get("/notes")
    def notes_list():
        from . import notes

        return {"ok": True, "notes": notes.list_notes(), "new_name": notes.new_name()}

    @app.get("/notes/get")
    def notes_get(name: str):
        from . import notes

        return notes.read(name)

    @app.post("/notes/save")
    async def notes_save(request: Request):
        from . import notes

        body = await request.json()
        try:
            return notes.save(str(body.get("name") or ""), str(body.get("text") or ""),
                              body.get("old_name") or None, bool(body.get("is_new")))
        except OSError as e:
            return {"ok": False, "error": str(e)}

    @app.post("/notes/delete")
    def notes_delete(name: str):
        from . import notes

        return notes.delete(name)

    # Real place name for the browser's precise (GPS / Wi-Fi) position; the
    # IP-based city in /netinfo is only the internet provider's hub.
    @app.get("/geo/reverse")
    def geo_reverse(lat: float, lon: float):
        from .geo import reverse

        return reverse(lat, lon)

    # Info card beside the camera circle, built from what the user types
    # (link / name / question) — never from the face itself.
    @app.get("/profile/lookup")
    def profile_lookup(q: str, lang: str = "en"):
        from .profile_lookup import lookup

        return lookup(q, lang=lang)

    # YouTube search for the panel's search screen ("open youtube").
    @app.get("/yt/search")
    def yt_search(q: str):
        from .web import youtube_search

        try:
            return {"ok": True, "results": youtube_search(q.strip())[:12]}
        except Exception:  # noqa: BLE001
            return {"ok": False, "results": []}

    # --- Bluetooth screen: list, search, pair, connect (Windows) ---
    @app.get("/bluetooth/state")
    def bt_state():
        from . import bluetooth

        return bluetooth.state()

    @app.post("/bluetooth/scan")
    def bt_scan():
        from . import bluetooth

        return {"ok": bluetooth.available() and bluetooth.start_scan()}

    @app.post("/bluetooth/radio")
    def bt_radio(on: bool):
        from . import bluetooth

        try:
            return {"ok": True, "radio": bluetooth.radio(on)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    @app.post("/bluetooth/pair")
    def bt_pair(id: str):  # noqa: A002 - matches the query parameter name
        from . import bluetooth

        return {"ok": bluetooth.pair(id)}

    @app.post("/bluetooth/pair/respond")
    def bt_pair_respond(answer: str = ""):
        from . import bluetooth

        bluetooth.respond(answer)
        return {"ok": True}

    @app.post("/bluetooth/unpair")
    def bt_unpair(id: str):  # noqa: A002
        from . import bluetooth

        try:
            ok, status = bluetooth.unpair(id)
            return {"ok": ok, "status": status}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    @app.post("/bluetooth/connect")
    def bt_connect(mac: str, on: bool = True):
        from . import bluetooth

        ok = bluetooth.connect(mac) if on else bluetooth.disconnect(mac)
        return {"ok": ok}

    # Star / unstar a paired device for auto-connect (reconnects when it comes
    # in range; the page announces it).
    @app.post("/bluetooth/auto")
    def bt_auto(mac: str, on: bool = True, name: str = ""):
        from . import bluetooth

        try:
            return {"ok": True, "auto": list(bluetooth.set_auto(mac, name, on))}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def _start_bt_auto() -> None:
        try:
            from . import bluetooth

            bluetooth.start_auto_watch(emit)
        except Exception:  # noqa: BLE001 - Bluetooth is optional
            pass

    threading.Thread(target=_start_bt_auto, name="bt-auto-start", daemon=True).start()

    from . import ac as _ac_mod

    _ac_mod.start(emit)

    # --- AC screen: Blue Star Smart AC through its cloud ---
    @app.get("/ac/state")
    def ac_state():
        from . import ac

        return ac.state()

    def _ac_do(fn, *args, id: str = ""):  # noqa: A002 - matches the query parameter name
        from . import ac

        try:
            return {"ok": True, "device": fn(*args, id or None)}
        except ac.ACError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @app.post("/ac/power")
    def ac_power(on: bool, id: str = ""):  # noqa: A002
        from . import ac

        return _ac_do(ac.power, on, id=id)

    @app.post("/ac/temp")
    def ac_temp(value: float, id: str = ""):  # noqa: A002
        from . import ac

        return _ac_do(ac.set_temp, value, id=id)

    @app.post("/ac/mode")
    def ac_mode(value: str, id: str = ""):  # noqa: A002
        from . import ac

        return _ac_do(ac.set_mode, value, id=id)

    @app.post("/ac/fan")
    def ac_fan(value: str, id: str = ""):  # noqa: A002
        from . import ac

        return _ac_do(ac.set_fan, value, id=id)

    @app.post("/ac/swing")
    def ac_swing(value: str, id: str = ""):  # noqa: A002
        from . import ac

        return _ac_do(ac.set_swing, value, id=id)

    @app.post("/ac/eco")
    def ac_eco(value: str, id: str = ""):  # noqa: A002
        from . import ac

        return _ac_do(ac.set_eco, value, id=id)

    @app.post("/ac/feature")
    def ac_feature(name: str, on: bool, id: str = ""):  # noqa: A002
        from . import ac

        return _ac_do(ac.set_feature, name, on, id=id)

    @app.post("/ac/login")
    async def ac_login(request: Request):
        """Link the AC from the screen: the login travels in the body, never the URL."""
        from . import ac

        try:
            data = await request.json()
            count = ac.save_credentials(str(data.get("user", "")), str(data.get("password", "")))
        except ac.ACError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "devices": count}

    @app.get("/ac/history")
    def ac_history(days: int = 1):
        from . import ac

        return ac.history(max(1, min(days, 30)))

    @app.get("/ac/report")
    def ac_report(days: int = 7):
        from . import ac

        return ac.daily_report(max(1, min(days, 90)))

    @app.get("/ac/report.csv")
    def ac_report_csv(days: int = 30):
        from . import ac

        return Response(ac.report_csv(max(1, min(days, 90))), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="ac-usage-report.csv"'})

    @app.get("/ac/schedule")
    def ac_schedule():
        from . import ac

        return {"ok": True, "rules": ac.schedule_list()}

    def _ac_rules(fn, *args, **kw):
        from . import ac

        try:
            return {"ok": True, "rules": fn(*args, **kw)}
        except ac.ACError as e:
            return {"ok": False, "error": str(e), "rules": ac.schedule_list()}

    @app.post("/ac/schedule/add")
    def ac_schedule_add(name: str, start: str, end: str, temp: float = 24, mode: str = "cool"):
        from . import ac

        return _ac_rules(ac.schedule_add, name, start, end, temp, mode)

    @app.post("/ac/schedule/toggle")
    def ac_schedule_toggle(id: str, on: bool):  # noqa: A002
        from . import ac

        return _ac_rules(ac.schedule_update, id, enabled=on)

    @app.post("/ac/schedule/delete")
    def ac_schedule_delete(id: str):  # noqa: A002
        from . import ac

        return _ac_rules(ac.schedule_update, id, delete=True)

    @app.post("/ac/timer")
    def ac_timer(minutes: int = 0):
        from . import ac

        return {"ok": True, "timer_secs": ac.set_timer(max(0, min(minutes, 720)))}

    @app.post("/ac/ask")
    def ac_ask(text: str):
        """The AI assistant box: AC voice commands typed in, plus usage questions."""
        import re

        from . import ac
        from .actions import _ac_command, is_hindi

        text = text.strip()[:200]
        if not text:
            return {"ok": False, "message": ""}
        query = text if ac.mentions_ac(text) else "ac " + text
        cmd = ac.parse_command(query)
        if cmd and cmd["action"] == "report":
            return {"ok": True, "message": ac.report_text(cmd["days"])}
        r = _ac_command(query, is_hindi(text), False)
        if r is None:
            return {"ok": False, "message": "Try: 'set temperature to 22', 'fan high', 'turn off the AC'."}
        return {"ok": r.ok, "message": r.message}

    # Daily jobs (e.g. "send today's note on WhatsApp at 11:59:58 PM").
    from . import schedule as _schedule

    _schedule.start(emit)

    @app.get("/schedule")
    def schedule_list():
        return {"ok": True, "jobs": _schedule.jobs()}

    @app.post("/schedule/clear")
    def schedule_clear(kind: str = "whatsapp_note"):
        return {"ok": True, "removed": _schedule.clear(kind)}

    # (Re)set the daily WhatsApp-note schedule (used by "Undo" in the Notebook).
    @app.post("/schedule/whatsapp")
    def schedule_whatsapp(number: str, at: str = "23:59:58"):
        return {"ok": True, "job": _schedule.set_whatsapp_note(number, at)}

    # "📲 Send" in the Notebook: send a note on WhatsApp right now.
    @app.post("/notes/send")
    def notes_send(name: str, number: str = ""):
        ok, msg = _schedule.send_note(name, number)
        emit("whatsapp", msg)
        return {"ok": ok, "message": msg, "number": number or _schedule.scheduled_number()}

    # Reader view of one headline, so stories open inside the dashboard.
    @app.get("/news/article")
    def news_article(link: str):
        from .article import read

        try:
            return {"ok": True, **read(link)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    # Headlines for the dashboard's news panel, cached per language for 10 min.
    _news_cache: dict = {}

    @app.get("/news/headlines")
    def news_headlines(lang: str = "en"):
        from .news import headlines

        lang = "hi" if lang == "hi" else "en"
        hit = _news_cache.get(lang)
        if hit and monotonic() - hit[0] < 600:
            return {"ok": True, "lang": lang, "items": hit[1]}
        try:
            items = headlines(lang=lang, count=12)
        except Exception:  # noqa: BLE001
            if hit:  # serve stale headlines rather than nothing
                return {"ok": True, "lang": lang, "items": hit[1], "stale": True}
            return {"ok": False, "lang": lang, "items": []}
        _news_cache[lang] = (monotonic(), items)
        return {"ok": True, "lang": lang, "items": items}

    @app.get("/session")
    def session():
        # The UI compares this against the saved profile; a new id means the
        # server restarted, so it asks for the name again.
        return {"run_id": run_id}

    @app.get("/sara", response_class=HTMLResponse)
    def sara():
        return """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>SARA_TERMINAL_OS</title>
    <link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><circle cx=%2250%22 cy=%2250%22 r=%2250%22 fill=%22%2310b981%22/><text x=%2250%22 y=%2250%22 font-family=%22monospace%22 font-size=%2250%22 font-weight=%22bold%22 text-anchor=%22middle%22 dy=%22.3em%22 fill=%22%23050b14%22>S</text></svg>">
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Orbitron:wght@700&family=Amiri:wght@400;700&display=swap" rel="stylesheet">
    <style>
      :root {
        --bg-color: #050b14;
        --panel-bg: rgba(10, 20, 35, 0.7);
        --panel-border: rgba(16, 185, 129, 0.3);
        --neon-green: #10b981;
        --neon-glow: rgba(16, 185, 129, 0.6);
        --text-primary: #a7f3d0;
        --text-secondary: #059669;
        --alert-red: #ef4444;
        --ai-blue: #3b82f6;
        --glass-blur: blur(12px);
        --pre-bg: rgba(0, 0, 0, 0.5);
        --input-bg: rgba(0, 0, 0, 0.4);
        --input-border: rgba(255, 255, 255, 0.1);
        --video-bg: #000;
        --btn-bg: rgba(255, 255, 255, 0.05);
        --header-text: #fff;
        --no-signal-color: rgba(255, 255, 255, 0.2);
      }

      :root[data-theme="light"] {
        --bg-color: #f8fafc;
        --panel-bg: rgba(255, 255, 255, 0.85);
        --panel-border: rgba(16, 185, 129, 0.4);
        --neon-green: #059669;
        --neon-glow: rgba(16, 185, 129, 0.3);
        --text-primary: #0f172a;
        --text-secondary: #047857;
        --alert-red: #dc2626;
        --ai-blue: #2563eb;
        --pre-bg: rgba(241, 245, 249, 0.8);
        --input-bg: rgba(255, 255, 255, 0.9);
        --input-border: rgba(0, 0, 0, 0.1);
        /* Keep the video feed dark in light mode too, so the green-on-black
           surveillance HUD stays readable (a bright feed washes it out). */
        --video-bg: #0a1420;
        --btn-bg: rgba(0, 0, 0, 0.05);
        --header-text: #065f46;
        --no-signal-color: rgba(255, 255, 255, 0.35);
      }

      * { box-sizing: border-box; margin: 0; padding: 0; }

      html, body { 
        height: 100%; 
        width: 100%;
        overflow: hidden; 
        font-family: 'JetBrains Mono', monospace; 
        background: var(--bg-color); 
        color: var(--text-primary); 
        transition: background-color 0.3s ease, color 0.3s ease;
      }
      
      body::before {
        content: "";
        position: absolute;
        inset: 0;
        background: radial-gradient(circle at center, transparent 0%, #000 100%);
        pointer-events: none;
        z-index: -1;
        transition: opacity 0.3s ease;
      }

      :root[data-theme="light"] body::before {
        opacity: 0;
      }

      .dashboard {
        display: grid;
        grid-template-columns: 350px 1fr;
        grid-template-rows: 60px 1fr;
        gap: 20px;
        height: 100vh;
        padding: 20px;
        position: relative;
        z-index: 1;
      }

      .glass-panel {
        background: var(--panel-bg);
        border: 1px solid var(--panel-border);
        border-radius: 12px;
        backdrop-filter: var(--glass-blur);
        -webkit-backdrop-filter: var(--glass-blur);
        box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.37);
        padding: 20px;
        display: flex;
        flex-direction: column;
        overflow: hidden;
        position: relative;
        transition: filter 0.3s ease;
      }

      .glass-panel::before {
        content: "";
        position: absolute;
        top: 0; left: 0; right: 0; height: 1px;
        background: linear-gradient(90deg, transparent, var(--neon-green), transparent);
        opacity: 0.5;
      }

      /* Header Area */
      .header {
        grid-column: 1 / -1;
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 0 20px;
        border-radius: 8px;
        flex-direction: row;
      }

      .header-title {
        font-weight: 700;
        font-size: 24px;
        text-transform: uppercase;
        letter-spacing: 4px;
        color: var(--header-text);
        text-shadow: 0 0 10px var(--neon-glow);
        display: flex;
        align-items: center;
        gap: 15px;
      }

      .status-indicator {
        font-size: 12px;
        padding: 4px 10px;
        border-radius: 20px;
        background: rgba(16, 185, 129, 0.1);
        border: 1px solid var(--neon-green);
        color: var(--neon-green);
        display: flex;
        align-items: center;
        gap: 6px;
      }

      /* Animations */
      @keyframes blink { 50% { opacity: 0.3; } }
      .dot {
        height: 8px; width: 8px;
        background-color: var(--neon-green);
        border-radius: 50%;
        display: inline-block;
        animation: blink 1.5s infinite;
        box-shadow: 0 0 8px var(--neon-green);
      }

      @keyframes scanline {
        0% { transform: translateY(-100%); }
        100% { transform: translateY(100vh); }
      }

      .scanline-overlay {
        position: fixed;
        inset: 0;
        background: linear-gradient(to bottom, transparent, rgba(16, 185, 129, 0.05), transparent);
        height: 50px;
        pointer-events: none;
        animation: scanline 8s linear infinite;
        z-index: 999;
      }

      @keyframes pulse {
        0% { box-shadow: 0 0 5px var(--neon-green); }
        50% { box-shadow: 0 0 20px var(--neon-green); }
        100% { box-shadow: 0 0 5px var(--neon-green); }
      }
      .pulse { animation: pulse 1.5s infinite; color: #fff !important; background: rgba(16, 185, 129, 0.2) !important; border-color: var(--neon-green) !important; }
      
      .blur { filter: blur(8px) brightness(0.6); pointer-events: none; }

      /* Sidebar Controls */
      .controls-area {
        grid-column: 1;
        grid-row: 2;
        gap: 20px;
      }

      .control-group {
        display: flex;
        flex-direction: column;
        gap: 12px;
        margin-bottom: 25px;
      }

      .control-group label {
        font-size: 11px;
        color: var(--text-secondary);
        text-transform: uppercase;
        letter-spacing: 1px;
      }

      input, select {
        width: 100%;
        padding: 14px;
        background: var(--input-bg);
        border: 1px solid var(--input-border);
        border-radius: 6px;
        color: var(--text-primary);
        font-family: inherit;
        font-size: 14px;
        transition: all 0.3s ease;
        outline: none;
      }

      input:focus, select:focus {
        border-color: var(--neon-green);
        box-shadow: 0 0 15px rgba(16, 185, 129, 0.2);
        background: rgba(16, 185, 129, 0.05);
      }

      .input-ai:focus {
        border-color: var(--ai-blue);
        box-shadow: 0 0 15px rgba(59, 130, 246, 0.3);
        background: rgba(59, 130, 246, 0.05);
        color: #93c5fd;
      }

      .btn {
        padding: 12px 20px;
        background: var(--btn-bg);
        border: 1px solid var(--input-border);
        border-radius: 6px;
        color: var(--text-primary);
        font-family: inherit;
        font-weight: bold;
        cursor: pointer;
        transition: all 0.2s ease;
        text-transform: uppercase;
        letter-spacing: 1px;
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 8px;
      }

      .btn:hover {
        background: rgba(16, 185, 129, 0.1);
        border-color: var(--neon-green);
        box-shadow: 0 0 15px var(--neon-glow);
        color: var(--neon-green);
      }
      
      .btn:active { transform: scale(0.96); }

      .btn-ai:hover {
        background: rgba(59, 130, 246, 0.1);
        border-color: var(--ai-blue);
        box-shadow: 0 0 15px rgba(59, 130, 246, 0.4);
        color: #60a5fa;
      }

      /* --- Today_News panel (fills the sidebar gap) --- */
      .news-group { flex: 1; min-height: 140px; gap: 8px; margin-bottom: 18px; }
      .news-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
      .news-head label { display: flex; align-items: center; gap: 6px; }
      .news-live {
        width: 6px; height: 6px; border-radius: 50%;
        background: var(--alert-red); box-shadow: 0 0 6px var(--alert-red);
        animation: blink 1.4s infinite;
      }
      .news-tabs { display: flex; border: 1px solid var(--input-border); border-radius: 5px; overflow: hidden; }
      .news-tabs button {
        font: inherit; font-size: 10px; letter-spacing: 1px;
        padding: 3px 9px; cursor: pointer;
        color: var(--text-secondary); background: transparent; border: 0;
      }
      .news-tabs button + button { border-left: 1px solid var(--input-border); }
      .news-tabs button.active { color: #04121a; background: var(--neon-green); font-weight: 700; }
      .news-list {
        list-style: none; margin: 0; padding: 0;
        flex: 1; min-height: 0; overflow-y: auto;
        display: flex; flex-direction: column; gap: 6px;
        scrollbar-width: thin;
      }
      .news-item a {
        display: block; padding: 6px 9px;
        border: 1px solid var(--input-border); border-left: 2px solid var(--neon-green);
        border-radius: 5px; background: var(--pre-bg);
        color: inherit; text-decoration: none;
        opacity: 0; animation: newsIn .4s ease-out forwards;
        transition: border-color .15s, transform .15s, background .15s;
      }
      .news-item a:hover, .news-item a:focus-visible {
        border-color: var(--neon-green); transform: translateX(3px); outline: none;
        background: rgba(16, 185, 129, 0.06);
      }
      @keyframes newsIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: none; } }
      .news-title {
        font-size: 12px; line-height: 1.45; color: var(--text-primary, #e2e8f0);
        display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
      }
      .news-meta { margin-top: 4px; font-size: 10px; color: var(--text-secondary); letter-spacing: .5px; }
      .news-meta b { color: var(--neon-green); font-weight: 600; }
      .news-empty { font-size: 11px; color: var(--text-secondary); padding: 8px 2px; }
      .news-foot { display: flex; align-items: center; justify-content: space-between; font-size: 10px; color: var(--text-secondary); }
      .news-refresh {
        font: inherit; font-size: 13px; line-height: 1; cursor: pointer;
        color: var(--text-secondary); background: transparent;
        border: 1px solid var(--input-border); border-radius: 4px; padding: 2px 7px;
      }
      .news-refresh:hover { color: var(--neon-green); border-color: var(--neon-green); }
      .news-refresh.spin { animation: spin .8s linear infinite; }

      .row-actions { display: flex; gap: 10px; margin-top: 10px; }
      .row-actions .btn { flex: 1; }

      /* Main View Area */
      .main-area {
        grid-column: 2;
        grid-row: 2;
        display: grid;
        grid-template-columns: 1fr 400px;
        gap: 20px;
        min-height: 0;
      }

      .top-view {
        display: flex;
        flex-direction: column;
        gap: 20px;
        min-height: 0;
      }
      
      .top-view .glass-panel {
        flex: 1;
      }

      .panel-title {
        font-size: 11px;
        color: var(--text-secondary);
        text-transform: uppercase;
        letter-spacing: 2px;
        margin-bottom: 12px;
        display: flex;
        align-items: center;
        gap: 8px;
      }
      
      .panel-title::before {
        content: "";
        display: block;
        width: 12px;
        height: 2px;
        background: var(--text-secondary);
      }

      pre {
        flex: 1;
        background: var(--pre-bg);
        padding: 15px;
        border-radius: 6px;
        border: 1px solid var(--input-border);
        overflow: auto;
        font-size: 13px;
        line-height: 1.6;
        min-height: 0;
      }

      pre::-webkit-scrollbar { width: 6px; }
      pre::-webkit-scrollbar-thumb { background: rgba(16, 185, 129, 0.3); border-radius: 3px; }
      pre::-webkit-scrollbar-track { background: transparent; }

      #aiBrain {
        color: #93c5fd;
        border-color: rgba(59, 130, 246, 0.2);
        box-shadow: inset 0 0 20px rgba(59, 130, 246, 0.05);
      }

      /* Song player + playlist menu inside the AI output panel. */
      .media-player {
        margin-bottom: 10px;
        border: 1px solid rgba(59, 130, 246, 0.35);
        border-radius: 6px;
        overflow: hidden;
        background: #000;
        box-shadow: 0 0 18px rgba(59, 130, 246, 0.15);
        animation: mediaIn .45s cubic-bezier(.2, .8, .2, 1);
      }
      .media-player[hidden] { display: none; }
      /* Quran list: calm gold accent */
      .media-player.quran { border-color: rgba(251, 191, 36, 0.45); box-shadow: 0 0 18px rgba(251, 191, 36, 0.15); }
      .media-player.quran .media-bar { background: rgba(251, 191, 36, 0.08); border-bottom-color: rgba(251, 191, 36, 0.3); }
      .media-player.quran .media-title small { color: #fbbf24; }
      @keyframes mediaIn { from { opacity: 0; transform: translateY(-6px) scale(.98); } }
      .media-bar {
        display: flex; align-items: center; gap: 10px;
        padding: 8px 10px;
        background: rgba(59, 130, 246, 0.08);
        border-bottom: 1px solid rgba(59, 130, 246, 0.25);
      }
      .media-title { flex: 1; min-width: 0; display: flex; flex-direction: column; line-height: 1.3; }
      .media-title small { font-size: 9px; letter-spacing: 2px; color: #60a5fa; }
      .media-title b {
        font-size: 12px; color: #dbeafe;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      }
      :root[data-theme="light"] .media-title b { color: #fff; }
      .media-btn {
        flex: none;
        font: inherit; font-size: 11px; font-weight: 700;
        color: #93c5fd; background: transparent;
        border: 1px solid rgba(59, 130, 246, 0.4); border-radius: 4px;
        padding: 3px 8px; cursor: pointer; text-decoration: none;
      }
      .media-btn:hover { background: rgba(59, 130, 246, 0.2); color: #fff; }
      .media-eq { display: inline-flex; align-items: flex-end; gap: 2px; height: 14px; }
      .media-eq i {
        width: 3px; height: 100%; background: #60a5fa; border-radius: 1px;
        transform-origin: bottom; animation: eqBounce .9s ease-in-out infinite;
      }
      .media-eq i:nth-child(2) { animation-delay: -.3s; }
      .media-eq i:nth-child(3) { animation-delay: -.6s; }
      .media-eq i:nth-child(4) { animation-delay: -.15s; }
      @keyframes eqBounce { 0%, 100% { transform: scaleY(.3); } 50% { transform: scaleY(1); } }
      .media-frame { aspect-ratio: 16 / 9; position: relative; }
      .media-frame iframe { position: absolute; inset: 0; width: 100%; height: 100%; border: 0; display: block; }
      .media-note { margin: 0; padding: 24px 14px; text-align: center; font-size: 12px; color: #93c5fd; }
      .media-list {
        list-style: none; margin: 0; padding: 8px;
        display: grid; gap: 6px;
        max-height: 250px; overflow: auto;
        background: var(--pre-bg);
      }
      .media-item {
        width: 100%; display: flex; align-items: center; gap: 10px;
        padding: 6px; text-align: left; cursor: pointer;
        font: inherit; color: inherit;
        background: rgba(59, 130, 246, 0.05);
        border: 1px solid rgba(59, 130, 246, 0.2); border-radius: 6px;
        opacity: 0; animation: itemIn .35s ease-out forwards;
        transition: background .15s, border-color .15s, transform .15s;
      }
      .media-item:hover, .media-item:focus-visible {
        background: rgba(59, 130, 246, 0.16); border-color: #60a5fa;
        transform: translateX(3px); outline: none;
      }
      @keyframes itemIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
      .media-thumb { position: relative; flex: none; width: 96px; aspect-ratio: 16 / 9; border-radius: 4px; overflow: hidden; background: #000; }
      .media-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
      .media-num {
        position: absolute; left: 4px; top: 4px;
        min-width: 18px; height: 18px; padding: 0 4px; border-radius: 3px;
        display: grid; place-items: center;
        font-size: 11px; font-weight: 700; color: #04121a; background: #60a5fa;
      }
      .media-info { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
      .media-info b { font-size: 12px; color: #dbeafe; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .media-info span { font-size: 10px; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      :root[data-theme="light"] .media-info b { color: #1d4ed8; }

      /* The pale-blue AI text is unreadable on the light panel — darken it. */
      :root[data-theme="light"] #aiBrain { color: #1d4ed8; }

      .video-wrap {
        flex: 1;
        background: var(--video-bg);
        border-radius: 6px;
        border: 1px solid var(--input-border);
        position: relative;
        overflow: hidden;
        display: flex;
        align-items: center;
        justify-content: center;
      }
      
      /* AI visual (animated core) filling the video panel when the camera is off. */
      .ai-visual {
        position: absolute;
        inset: 0;
        z-index: 0;
      }
      .ai-visual canvas { width: 100%; height: 100%; display: block; }
      .ai-visual-label {
        position: absolute;
        left: 0; right: 0; bottom: 18px;
        display: flex; flex-direction: column; align-items: center; gap: 4px;
        pointer-events: none;
        font-size: 11px; letter-spacing: 3px; text-transform: uppercase;
      }
      .ai-visual-name { color: var(--text-secondary); }
      .ai-visual-state {
        color: var(--neon-green);
        font-weight: 700;
        text-shadow: 0 0 10px var(--neon-glow);
      }
      .ai-visual[data-state="idle"] .ai-visual-state,
      .ai-visual[data-state="speaking"] .ai-visual-state { color: #7dd3fc; text-shadow: 0 0 10px rgba(59, 130, 246, 0.6); }
      .ai-visual[data-state="listening"] .ai-visual-state { color: #22d3ee; }
      .ai-visual[data-state="thinking"] .ai-visual-state { color: #a78bfa; }

      .video-overlay {
        position: absolute;
        inset: 0;
        background: 
          linear-gradient(rgba(16, 185, 129, 0.05) 1px, transparent 1px),
          linear-gradient(90deg, rgba(16, 185, 129, 0.05) 1px, transparent 1px);
        background-size: 40px 40px;
        pointer-events: none;
        z-index: 1;
        box-shadow: inset 0 0 50px rgba(0, 0, 0, 0.8);
      }

      .video-wrap::after {
        content: "REC";
        position: absolute;
        top: 15px; right: 15px;
        color: var(--alert-red);
        font-weight: bold;
        font-size: 12px;
        letter-spacing: 2px;
        display: flex;
        align-items: center;
        gap: 6px;
      }
      
      .video-wrap::before {
        content: "";
        position: absolute;
        top: 19px; right: 48px;
        width: 8px; height: 8px;
        background: var(--alert-red);
        border-radius: 50%;
        animation: blink 2s infinite;
        z-index: 2;
      }

      /* --- Stage: news board inside the feed panel --- */
      .stage {
        position: absolute; inset: 0; z-index: 5;
        display: flex; flex-direction: column;
        background: #05080d;
        animation: stageIn .5s cubic-bezier(.2, .8, .2, 1);
      }
      .stage[hidden] { display: none; }
      @keyframes stageIn {
        0% { clip-path: inset(48% 0 48% 0); filter: brightness(2); }
        60% { clip-path: inset(0 0 0 0); filter: brightness(1.2); }
        100% { clip-path: inset(0 0 0 0); filter: none; }
      }
      .stage-bar {
        flex: none; display: flex; align-items: center; gap: 10px;
        padding: 7px 10px;
        background: linear-gradient(90deg, rgba(16, 185, 129, 0.14), rgba(16, 185, 129, 0.03));
        border-bottom: 1px solid rgba(16, 185, 129, 0.35);
        font-size: 11px; letter-spacing: 1px;
      }
      .stage-kind {
        flex: none; padding: 2px 7px; border-radius: 3px;
        font-weight: 700; font-size: 10px; letter-spacing: 2px;
        color: #04121a; background: var(--neon-green);
      }
      .stage-title { flex: 1; min-width: 0; color: #d1fae5; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .stage-btn {
        flex: none; font: inherit; font-size: 12px; font-weight: 700; line-height: 1;
        color: #a7f3d0; background: transparent; text-decoration: none; cursor: pointer;
        border: 1px solid rgba(16, 185, 129, 0.45); border-radius: 4px; padding: 4px 9px;
      }
      .stage-btn:hover { background: rgba(16, 185, 129, 0.2); color: #fff; }
      .stage-body { flex: 1; min-height: 0; position: relative; }
      /* While staged: hide the HUD and shrink the live camera to a corner. */
      .video-wrap.staged .feed-hud { display: none; }
      .video-wrap.staged video {
        position: absolute !important; z-index: 7 !important;
        right: 14px; bottom: 14px; width: 24%; min-width: 160px; height: auto;
        aspect-ratio: 4 / 3; object-fit: cover;
        border: 1px solid #22d3ee; border-radius: 8px;
        box-shadow: 0 0 0 3px rgba(34, 211, 238, 0.15), 0 8px 24px rgba(0, 0, 0, 0.6);
        animation: pipIn .5s cubic-bezier(.2, .8, .2, 1);
      }
      @keyframes pipIn { from { opacity: 0; transform: scale(.85); } }
      /* News board ("news dikhao"): lead story, grid, bottom ticker. */
      .news-board {
        position: absolute; inset: 0 0 34px 0; overflow: auto;
        padding: 16px; display: grid; gap: 10px; align-content: start;
        grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
        background:
          radial-gradient(ellipse at top left, rgba(16, 185, 129, 0.08), transparent 60%),
          #05080d;
      }
      .nb-card {
        position: relative; display: flex; flex-direction: column; gap: 6px;
        padding: 12px 12px 10px 14px; text-decoration: none; color: inherit;
        background: rgba(16, 185, 129, 0.04);
        border: 1px solid var(--input-border); border-left: 3px solid var(--neon-green);
        border-radius: 8px;
        opacity: 0; animation: newsIn .4s ease-out forwards;
        transition: border-color .15s, transform .15s, background .15s;
      }
      .nb-card:hover, .nb-card:focus-visible {
        border-color: var(--neon-green); background: rgba(16, 185, 129, 0.09);
        transform: translateY(-2px); outline: none;
      }
      .nb-lead {
        grid-column: 1 / -1; padding: 16px 18px 14px;
        border-left-color: var(--alert-red);
        background: linear-gradient(90deg, rgba(239, 68, 68, 0.10), rgba(16, 185, 129, 0.03));
      }
      .nb-num { font-size: 10px; font-weight: 700; letter-spacing: 2px; color: var(--neon-green); }
      .nb-lead .nb-num { color: var(--alert-red); }
      .nb-title { font-size: 13px; line-height: 1.45; color: #e2e8f0; font-weight: 600; }
      .nb-lead .nb-title { margin: 0; font-size: 19px; line-height: 1.35; color: #fff; }
      .nb-meta { font-size: 10px; letter-spacing: .5px; color: var(--text-secondary); margin-top: auto; }
      .nb-meta b { color: var(--neon-green); font-weight: 600; }
      .nb-ticker {
        position: absolute; left: 0; right: 0; bottom: 0; height: 34px;
        display: flex; align-items: center; overflow: hidden;
        background: #0a1119; border-top: 1px solid rgba(239, 68, 68, 0.4);
        font-size: 12px; color: #fecaca;
      }
      .nb-live {
        flex: none; align-self: stretch; display: flex; align-items: center; padding: 0 12px;
        font-weight: 700; font-size: 11px; letter-spacing: 2px; color: #fff; background: var(--alert-red);
      }
      .nb-track { flex: 1; overflow: hidden; display: flex; white-space: nowrap; }
      .nb-run { flex: none; padding-left: 20px; animation: tickerRun linear infinite; }
      @keyframes tickerRun { to { transform: translateX(-100%); } }

      /* Reader view: a news story opened inside the panel. */
      .reader {
        position: absolute; inset: 0; overflow: auto;
        padding: 16px max(18px, calc((100% - 760px) / 2)) 28px;
        background: #05080d; color: #cbd5e1;
        font-size: 15px; line-height: 1.75;
        animation: newsIn .35s ease-out;
      }
      .reader-back {
        font: inherit; font-size: 11px; font-weight: 700; letter-spacing: 1px; cursor: pointer;
        color: var(--neon-green); background: transparent;
        border: 1px solid rgba(16, 185, 129, 0.45); border-radius: 4px; padding: 5px 10px;
        margin-bottom: 14px;
      }
      .reader-back:hover { background: rgba(16, 185, 129, 0.15); }
      .reader-hero {
        display: block; width: 100%; max-height: 360px; object-fit: cover;
        border-radius: 8px; border: 1px solid var(--input-border); margin-bottom: 14px;
      }
      .reader-title { margin: 0 0 8px; font-size: 24px; line-height: 1.35; color: #fff; }
      .reader-meta {
        font-size: 11px; letter-spacing: .5px; color: var(--text-secondary);
        margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid var(--input-border);
      }
      .reader-meta b { color: var(--neon-green); font-weight: 600; }
      .reader p { margin: 0 0 14px; }
      .reader .reader-lead { font-size: 17px; color: #e2e8f0; }
      .reader .reader-status { color: var(--text-secondary); font-size: 13px; }
      .reader-orig {
        display: inline-block; margin-top: 6px;
        font-size: 12px; font-weight: 700; letter-spacing: .5px; text-decoration: none;
        color: #04121a; background: var(--neon-green); border-radius: 5px; padding: 7px 12px;
      }

      /* --- Bluetooth screen --- */
      .bt-screen {
        position: fixed; inset: 0; z-index: 1000;
        display: grid; place-items: center; padding: 16px;
        background: rgba(2, 6, 12, 0.72); backdrop-filter: blur(4px);
        animation: newsIn .25s ease-out;
      }
      .bt-screen[hidden] { display: none; }
      .bt-card {
        width: min(560px, 100%); max-height: min(720px, calc(100vh - 32px));
        display: flex; flex-direction: column;
        background: var(--panel-bg); border: 1px solid rgba(96, 165, 250, 0.35); border-radius: 12px;
        box-shadow: 0 0 40px rgba(59, 130, 246, 0.18), 0 20px 50px rgba(0, 0, 0, 0.6);
        overflow: hidden;
      }
      .bt-head { display: flex; align-items: center; gap: 10px; padding: 14px 16px 10px; }
      .bt-logo {
        display: grid; place-items: center; width: 30px; height: 30px; border-radius: 50%;
        color: #fff; background: #2563eb; box-shadow: 0 0 14px rgba(37, 99, 235, 0.6);
      }
      .bt-head h3 { flex: 1; margin: 0; font-size: 15px; letter-spacing: 2px; text-transform: uppercase; color: var(--text-primary); }
      .bt-scan { padding: 6px 12px; font-size: 11px; }
      .bt-switch { position: relative; width: 40px; height: 22px; flex: none; cursor: pointer; }
      .bt-switch input { position: absolute; opacity: 0; inset: 0; margin: 0; cursor: pointer; }
      .bt-switch span {
        position: absolute; inset: 0; border-radius: 11px; background: #334155; transition: background .2s;
      }
      .bt-switch span::after {
        content: ""; position: absolute; top: 3px; left: 3px; width: 16px; height: 16px; border-radius: 50%;
        background: #fff; transition: transform .2s;
      }
      .bt-switch input:checked + span { background: #2563eb; }
      .bt-switch input:checked + span::after { transform: translateX(18px); }
      .bt-switch input:focus-visible + span { outline: 2px solid #60a5fa; outline-offset: 2px; }
      .bt-progress { height: 2px; background: rgba(96, 165, 250, 0.12); }
      .bt-progress i { display: block; height: 100%; width: 0; background: #60a5fa; box-shadow: 0 0 8px #60a5fa; transition: width .9s linear; }
      .bt-status { margin: 0; padding: 8px 16px 0; font-size: 11px; color: var(--text-secondary); min-height: 1.4em; }
      .bt-prompt {
        margin: 10px 16px 0; padding: 12px; border-radius: 8px;
        border: 1px solid rgba(251, 191, 36, 0.5); background: rgba(251, 191, 36, 0.08);
        font-size: 13px; color: var(--text-primary);
      }
      .bt-prompt[hidden] { display: none; }
      .bt-prompt b.bt-pin { display: block; margin: 6px 0 10px; font-size: 26px; letter-spacing: 6px; color: #fbbf24; }
      .bt-prompt .bt-row-actions { justify-content: flex-start; }
      .bt-prompt input { width: 140px; margin-right: 8px; }
      .bt-body { overflow: auto; padding: 4px 16px 16px; }
      .bt-sec { margin: 14px 0 6px; font-size: 10px; letter-spacing: 2px; text-transform: uppercase; color: #60a5fa; }
      .bt-sec span { color: var(--text-secondary); }
      .bt-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; }
      .bt-empty { font-size: 12px; color: var(--text-secondary); padding: 6px 2px; }
      .bt-item {
        display: flex; align-items: center; gap: 10px; padding: 9px 10px;
        border: 1px solid var(--input-border); border-radius: 8px; background: rgba(96, 165, 250, 0.04);
      }
      .bt-item.connected { border-color: rgba(16, 185, 129, 0.6); background: rgba(16, 185, 129, 0.07); }
      .bt-ico { flex: none; width: 32px; height: 32px; display: grid; place-items: center; font-size: 17px;
        border-radius: 8px; background: rgba(96, 165, 250, 0.1); }
      .bt-info { flex: 1; min-width: 0; }
      .bt-name { font-size: 13px; font-weight: 700; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .bt-sub { display: flex; align-items: center; gap: 6px; margin-top: 2px; font-size: 10px; color: var(--text-secondary); letter-spacing: .5px; }
      .bt-chip { padding: 1px 6px; border-radius: 3px; font-weight: 700; letter-spacing: 1px; font-size: 9px; }
      .bt-chip.on { color: #04121a; background: var(--neon-green); }
      .bt-chip.paired { color: #bfdbfe; background: rgba(59, 130, 246, 0.25); }
      .bt-sig { display: inline-flex; align-items: flex-end; gap: 1px; height: 9px; }
      .bt-sig i { width: 2px; background: rgba(148, 163, 184, 0.35); }
      .bt-sig i.lit { background: #60a5fa; }
      .bt-row-actions { display: flex; gap: 6px; flex: none; }
      .bt-act {
        font: inherit; font-size: 11px; font-weight: 700; cursor: pointer; padding: 5px 10px; border-radius: 5px;
        color: #04121a; background: #60a5fa; border: 1px solid #60a5fa;
      }
      .bt-act:hover { filter: brightness(1.15); }
      .bt-act.ghost { color: var(--text-secondary); background: transparent; border-color: var(--input-border); }
      .bt-act.ghost:hover { color: var(--alert-red); border-color: var(--alert-red); }
      .bt-act.auto { color: #fbbf24; background: transparent; border-color: rgba(251, 191, 36, 0.45); }
      .bt-act.auto.on { color: #04121a; background: #fbbf24; border-color: #fbbf24; }
      .bt-act:disabled { opacity: .5; cursor: progress; }

      /* --- AC screen (Blue Star Smart AC) --- */
      :root {
        --ac-bg: #040b1c;
        --ac-bg2: #071533;
        --ac-panel: rgba(10, 22, 48, 0.82);
        --ac-tile: rgba(255, 255, 255, 0.04);
        --ac-line: rgba(96, 165, 250, 0.16);
        --ac-text: #e8eefc;
        --ac-muted: #8a9bbd;
        --ac-accent: #3b9bff;
        --ac-accent-2: #22d3ee;
        --ac-good: #34d399;
        --ac-warm: #fbbf24;
        --ac-bad: #f87171;
        --ac-track: rgba(148, 163, 184, 0.16);
        --ac-glow: rgba(59, 155, 255, 0.45);
      }
      :root[data-theme="light"] {
        --ac-bg: #eef3fb;
        --ac-bg2: #dfe9f8;
        --ac-panel: rgba(255, 255, 255, 0.92);
        --ac-tile: rgba(15, 23, 42, 0.04);
        --ac-line: rgba(37, 99, 235, 0.16);
        --ac-text: #0f172a;
        --ac-muted: #55657d;
        --ac-accent: #1d6fe0;
        --ac-accent-2: #0891b2;
        --ac-good: #059669;
        --ac-warm: #b45309;
        --ac-bad: #dc2626;
        --ac-track: rgba(15, 23, 42, 0.11);
        --ac-glow: rgba(29, 111, 224, 0.28);
      }
      .ac-screen {
        position: fixed; inset: 0; z-index: 1000; overflow: auto;
        color: var(--ac-text);
        background: radial-gradient(900px 500px at 70% -10%, color-mix(in srgb, var(--ac-accent) 16%, transparent), transparent 70%), linear-gradient(160deg, var(--ac-bg2), var(--ac-bg));
        animation: newsIn .25s ease-out;
      }
      .ac-screen[hidden] { display: none; }
      .ac-app { min-height: 100%; display: grid; grid-template-columns: 224px minmax(0, 1fr); }
      .ac-side {
        position: sticky; top: 0; height: 100vh; display: flex; flex-direction: column; gap: 6px; padding: 18px 14px;
        border-right: 1px solid var(--ac-line); background: color-mix(in srgb, var(--ac-bg) 70%, transparent);
      }
      .ac-brand { display: flex; align-items: center; gap: 10px; padding: 2px 6px 16px; }
      .ac-brand .ac-star {
        width: 38px; height: 38px; flex: none; display: grid; place-items: center; border-radius: 9px; color: #fff;
        background: linear-gradient(135deg, #1d4ed8, #3b9bff); box-shadow: 0 0 18px var(--ac-glow);
      }
      .ac-brand b { display: block; font-family: 'Orbitron', 'JetBrains Mono', monospace; font-size: 16px; letter-spacing: 1.5px; }
      .ac-brand small { display: block; font-size: 9px; letter-spacing: 1.2px; color: var(--ac-muted); text-transform: uppercase; }
      .ac-nav { display: flex; flex-direction: column; gap: 3px; }
      .ac-nav button {
        display: flex; align-items: center; gap: 11px; width: 100%; padding: 11px 12px; cursor: pointer; text-align: left;
        font: inherit; font-size: 13px; color: var(--ac-muted); background: transparent; border: 1px solid transparent; border-radius: 10px;
        transition: color .15s, background .15s;
      }
      .ac-nav button:hover { color: var(--ac-text); background: var(--ac-tile); }
      .ac-nav button.on { color: #fff; background: linear-gradient(90deg, #1d4ed8, #2f7fe8); box-shadow: 0 6px 18px var(--ac-glow); }
      .ac-nav svg { flex: none; }
      .ac-side-ai {
        margin-top: auto; padding: 12px; border-radius: 14px; border: 1px solid var(--ac-line);
        background: linear-gradient(145deg, color-mix(in srgb, var(--ac-accent) 14%, transparent), var(--ac-tile));
      }
      .ac-side-ai b { display: block; font-size: 12px; }
      .ac-side-ai small { display: block; margin: 3px 0 9px; font-size: 10.5px; line-height: 1.4; color: var(--ac-muted); }
      .ac-side-ai button { width: 100%; padding: 8px; cursor: pointer; font: inherit; font-size: 11px; font-weight: 700; color: var(--ac-accent-2);
        background: transparent; border: 1px solid var(--ac-line); border-radius: 9px; }
      .ac-side-ai button:hover { border-color: var(--ac-accent-2); }
      .ac-main { padding: 20px 24px 28px; min-width: 0; display: flex; flex-direction: column; gap: 16px; }
      .ac-top { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
      .ac-top .ac-grow { flex: 1; min-width: 200px; }
      .ac-top h2 { margin: 0; font-size: 24px; font-weight: 700; letter-spacing: .3px; color: var(--ac-text); }
      .ac-top p { margin: 3px 0 0; font-size: 12.5px; color: var(--ac-muted); }
      .ac-top select {
        font: inherit; font-size: 12px; color: var(--ac-text); background: var(--ac-panel);
        border: 1px solid var(--ac-line); border-radius: 9px; padding: 8px 10px; max-width: 200px;
      }
      .ac-live { display: inline-flex; align-items: center; gap: 9px; padding: 7px 14px 7px 10px; border-radius: 99px;
        border: 1px solid var(--ac-line); background: var(--ac-panel); font-size: 11px; line-height: 1.25; }
      .ac-live i { width: 9px; height: 9px; border-radius: 50%; background: #64748b; flex: none; }
      .ac-live small { display: block; font-size: 9.5px; color: var(--ac-muted); }
      .ac-live b { font-weight: 700; letter-spacing: .5px; }
      .ac-live.on i { background: var(--ac-good); box-shadow: 0 0 9px var(--ac-good); animation: blink 1.6s infinite; }
      .ac-live.off i { background: var(--ac-bad); }
      .ac-x { width: 38px; height: 38px; cursor: pointer; font: inherit; font-size: 15px; color: var(--ac-text);
        background: var(--ac-panel); border: 1px solid var(--ac-line); border-radius: 50%; }
      .ac-x:hover { border-color: var(--ac-accent); }
      .ac-home { height: 38px; padding: 0 14px; cursor: pointer; font: inherit; font-size: 12px; font-weight: 700; white-space: nowrap;
        color: var(--ac-accent-2); background: var(--ac-panel); border: 1px solid var(--ac-line); border-radius: 999px; }
      .ac-home:hover { border-color: var(--ac-accent-2); }
      .ac-panel { border: 1px solid var(--ac-line); border-radius: 18px; background: var(--ac-panel); padding: 16px 18px; min-width: 0; backdrop-filter: blur(8px); }
      .ac-panel h3 { margin: 0; display: flex; align-items: center; gap: 9px; font-size: 14px; font-weight: 700; color: var(--ac-text); }
      .ac-panel h3 .ic { width: 28px; height: 28px; display: grid; place-items: center; border-radius: 8px; color: var(--ac-accent);
        background: color-mix(in srgb, var(--ac-accent) 14%, transparent); }
      .ac-panel .sub { margin: 4px 0 12px 37px; font-size: 11px; color: var(--ac-muted); }
      .ac-g1 { display: grid; grid-template-columns: minmax(0, 1.55fr) minmax(0, 1fr); gap: 16px; }
      .ac-col { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
      .ac-g2 { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; }
      /* hero */
      .ac-hero { position: relative; overflow: hidden; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 10px; padding-bottom: 14px; }
      .ac-hero-top { width: 100%; display: flex; justify-content: center; position: relative; }
      .ac-unit {
        position: relative; width: min(330px, 78%); height: 74px; border-radius: 16px 16px 24px 24px;
        background: linear-gradient(180deg, #f6f9ff 0%, #d5deee 70%, #b8c5dc 100%);
        box-shadow: 0 14px 30px rgba(0, 0, 0, 0.35), inset 0 -3px 6px rgba(0, 0, 0, 0.12);
      }
      .ac-unit::before { content: ""; position: absolute; left: 6%; right: 6%; bottom: 8px; height: 9px; border-radius: 6px; background: linear-gradient(#202a3c, #0c1220); }
      .ac-unit span { position: absolute; top: 14px; left: 50%; transform: translateX(-50%); font-size: 8px; font-weight: 800; letter-spacing: 2px; color: #3556a8; }
      .ac-unit i { position: absolute; top: 12px; right: 7%; width: 6px; height: 6px; border-radius: 50%; background: #94a3b8; }
      .ac-hero.on .ac-unit i { background: var(--ac-good); box-shadow: 0 0 8px var(--ac-good); }
      .ac-air { position: absolute; left: 50%; top: 66px; width: min(300px, 70%); height: 150px; transform: translateX(-50%); pointer-events: none; opacity: 0; transition: opacity .5s;
        background: linear-gradient(180deg, color-mix(in srgb, var(--ac-accent) 55%, transparent), transparent 85%);
        clip-path: polygon(8% 0, 92% 0, 100% 100%, 0 100%); filter: blur(7px); }
      .ac-hero.on .ac-air { opacity: .75; animation: acAir 3.2s ease-in-out infinite; }
      .ac-hero.on.cool-warm .ac-air { background: linear-gradient(180deg, color-mix(in srgb, var(--ac-warm) 50%, transparent), transparent 85%); }
      @keyframes acAir { 0%, 100% { opacity: .5; } 50% { opacity: .85; } }
      .ac-pill-state {
        position: absolute; right: 0; top: 4px; display: flex; flex-direction: column; gap: 5px; padding: 9px 13px; border-radius: 12px;
        background: var(--ac-tile); border: 1px solid var(--ac-line); font-size: 12px; font-weight: 700; text-align: left;
      }
      .ac-pill-state em { font-style: normal; font-size: 11px; font-weight: 600; color: var(--ac-muted); }
      .ac-pill-state .dot { display: inline-block; width: 8px; height: 8px; margin-right: 7px; border-radius: 50%; background: #64748b; }
      .ac-hero.on .ac-pill-state .dot { background: var(--ac-good); box-shadow: 0 0 7px var(--ac-good); }
      .ac-hero-mid { position: relative; width: 100%; display: grid; grid-template-columns: minmax(92px, 130px) minmax(0, 250px) minmax(92px, 130px); justify-content: center; align-items: center; gap: 12px; margin-top: 4px; }
      .ac-mini { padding: 12px; border-radius: 14px; background: var(--ac-tile); border: 1px solid var(--ac-line); min-width: 0; }
      .ac-mini small { display: block; font-size: 11px; color: var(--ac-muted); }
      .ac-mini b { display: block; margin: 3px 0; font-size: 24px; font-weight: 700; }
      .ac-mini span { font-size: 11px; color: var(--ac-good); }
      .ac-mini span.warn { color: var(--ac-warm); }
      .ac-dial { position: relative; width: 100%; aspect-ratio: 1; }
      .ac-dial svg { width: 100%; height: 100%; display: block; }
      .ac-dial .trk { stroke: var(--ac-track); }
      .ac-dial .val { stroke: url(#acGrad); transition: stroke-dasharray .45s ease; filter: drop-shadow(0 0 7px var(--ac-glow)); }
      .ac-dial .tick { stroke: var(--ac-muted); opacity: .4; }
      .ac-app.is-off .ac-dial .val { stroke: var(--ac-muted); filter: none; opacity: .45; }
      .ac-center { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; }
      .ac-center .snow { color: var(--ac-accent-2); margin-bottom: 2px; }
      .ac-center .tgt { font-family: 'Orbitron', 'JetBrains Mono', monospace; font-size: clamp(40px, 6vw, 56px); line-height: 1.05; font-variant-numeric: tabular-nums; }
      .ac-center .tgt sup { font-size: 18px; color: var(--ac-accent-2); }
      .ac-center .lbl { margin-top: 3px; font-size: 12px; color: var(--ac-muted); }
      .ac-center .rng { margin-top: 2px; font-size: 10px; color: var(--ac-accent-2); }
      .ac-step {
        position: absolute; bottom: -6px; width: 42px; height: 42px; cursor: pointer; display: grid; place-items: center;
        font: inherit; font-size: 22px; font-weight: 700; color: var(--ac-text); background: var(--ac-panel);
        border: 1px solid var(--ac-line); border-radius: 50%; box-shadow: 0 6px 16px rgba(0, 0, 0, .25);
      }
      .ac-step.l { left: 8%; } .ac-step.r { right: 8%; }
      .ac-step:hover:not(:disabled) { border-color: var(--ac-accent); color: var(--ac-accent); }
      .ac-step:disabled { opacity: .35; cursor: not-allowed; }
      .ac-quick { width: 100%; display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; margin-top: 18px; padding: 10px; border-radius: 16px; background: var(--ac-tile); border: 1px solid var(--ac-line); }
      .ac-qb { display: flex; flex-direction: column; align-items: center; gap: 5px; padding: 6px 2px; cursor: pointer; font: inherit; color: var(--ac-text); background: transparent; border: 0; border-radius: 12px; min-width: 0; }
      .ac-qb:hover:not(:disabled) { background: var(--ac-tile); }
      .ac-qb:disabled { opacity: .45; cursor: not-allowed; }
      .ac-qb .ring { width: 48px; height: 48px; display: grid; place-items: center; border-radius: 50%; font-size: 20px; color: var(--ac-accent); background: color-mix(in srgb, var(--ac-accent) 12%, transparent); }
      .ac-qb.pw .ring { color: #fff; background: linear-gradient(135deg, #1d4ed8, #3b9bff); box-shadow: 0 0 18px var(--ac-glow); }
      .ac-app.is-off .ac-qb.pw .ring { color: var(--ac-muted); background: var(--ac-tile); box-shadow: none; }
      .ac-qb b { font-size: 12px; font-weight: 600; }
      .ac-qb small { font-size: 10px; color: var(--ac-accent); text-transform: capitalize; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%; }
      /* control tabs */
      .ac-tabs { display: flex; gap: 3px; padding: 3px; margin: 2px 0 12px; border-radius: 12px; background: var(--ac-tile); border: 1px solid var(--ac-line); }
      .ac-tabs button { flex: 1; padding: 9px 4px; cursor: pointer; font: inherit; font-size: 12px; font-weight: 600; color: var(--ac-muted); background: transparent; border: 0; border-radius: 9px; }
      .ac-tabs button[aria-selected="true"] { color: #fff; background: linear-gradient(135deg, #1d4ed8, #2f7fe8); }
      .ac-tiles { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
      .ac-tiles.n3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .ac-tile {
        display: flex; flex-direction: column; align-items: center; gap: 6px; padding: 13px 4px 11px; cursor: pointer; text-align: center;
        font: inherit; color: var(--ac-text); background: var(--ac-tile); border: 1px solid var(--ac-line); border-radius: 14px; position: relative; min-width: 0;
        transition: border-color .15s, background .15s;
      }
      .ac-tile .ic { font-size: 22px; line-height: 1; color: var(--ac-accent); }
      .ac-tile b { font-size: 13px; }
      .ac-tile small { font-size: 9.5px; line-height: 1.25; color: var(--ac-muted); }
      .ac-tile:hover:not(:disabled) { border-color: color-mix(in srgb, var(--ac-accent) 60%, transparent); }
      .ac-tile[aria-pressed="true"] { border-color: var(--ac-accent); background: color-mix(in srgb, var(--ac-accent) 14%, transparent); box-shadow: 0 0 0 1px var(--ac-accent) inset; }
      .ac-tile[aria-pressed="true"]::after { content: "✓"; position: absolute; top: 5px; right: 6px; width: 15px; height: 15px; display: grid; place-items: center; font-size: 9px; font-weight: 800; color: #fff; background: var(--ac-accent); border-radius: 50%; }
      .ac-tile:disabled { opacity: .45; cursor: not-allowed; }
      /* device info */
      .ac-dev { display: grid; grid-template-columns: minmax(0, 1fr) 150px; gap: 14px; }
      .ac-dev dl { margin: 0; display: grid; gap: 10px; }
      .ac-dev dl div { display: grid; grid-template-columns: 96px minmax(0, 1fr); gap: 8px; font-size: 12px; align-items: center; }
      .ac-dev dt { color: var(--ac-muted); }
      .ac-dev dd { margin: 0; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .ac-health { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px; padding: 12px 8px; text-align: center; border-radius: 14px; background: var(--ac-tile); border: 1px solid var(--ac-line); }
      .ac-health .shield { width: 44px; height: 44px; display: grid; place-items: center; border-radius: 50%; color: var(--ac-good); background: color-mix(in srgb, var(--ac-good) 14%, transparent); }
      .ac-health.bad .shield { color: var(--ac-bad); background: color-mix(in srgb, var(--ac-bad) 14%, transparent); }
      .ac-health b { font-size: 12px; color: var(--ac-good); }
      .ac-health.bad b { color: var(--ac-bad); }
      .ac-health small { font-size: 10px; color: var(--ac-muted); }
      /* energy / history / schedule */
      .ac-range { display: flex; gap: 4px; margin: 0 0 12px; }
      .ac-range button { padding: 6px 12px; cursor: pointer; font: inherit; font-size: 11px; font-weight: 600; color: var(--ac-muted); background: var(--ac-tile); border: 1px solid var(--ac-line); border-radius: 8px; }
      .ac-range button[aria-pressed="true"] { color: #fff; background: #1d4ed8; border-color: #1d4ed8; }
      .ac-energy { display: grid; grid-template-columns: 112px minmax(0, 1fr); gap: 12px; align-items: center; }
      .ac-ring { position: relative; width: 112px; height: 112px; }
      .ac-ring svg { width: 100%; height: 100%; display: block; }
      .ac-ring .trk { stroke: var(--ac-track); }
      .ac-ring .val { stroke: var(--ac-accent-2); transition: stroke-dasharray .5s; filter: drop-shadow(0 0 5px var(--ac-glow)); }
      .ac-ring div { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; }
      .ac-ring b { font-size: 21px; } .ac-ring small { font-size: 10px; color: var(--ac-muted); }
      .ac-kv { display: grid; gap: 9px; }
      .ac-kv div { display: flex; flex-direction: column; font-size: 10.5px; color: var(--ac-muted); }
      .ac-kv b { font-size: 15px; color: var(--ac-text); }
      .ac-note { margin: 10px 0 0; font-size: 10px; line-height: 1.4; color: var(--ac-muted); }
      .ac-chart { width: 100%; height: 150px; display: block; }
      .ac-chart text { fill: var(--ac-muted); font-size: 9px; font-family: inherit; }
      .ac-chart .grid { stroke: var(--ac-track); stroke-width: 1; }
      .ac-chart .room { fill: none; stroke: var(--ac-accent); stroke-width: 2; stroke-linejoin: round; }
      .ac-chart .tgtl { fill: none; stroke: var(--ac-accent-2); stroke-width: 1.4; stroke-dasharray: 4 3; opacity: .8; }
      .ac-chart .area { fill: color-mix(in srgb, var(--ac-accent) 14%, transparent); stroke: none; }
      .ac-empty { padding: 26px 10px; text-align: center; font-size: 12px; color: var(--ac-muted); }
      .ac-sum { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; margin-top: 10px; }
      .ac-sum div { padding: 8px; border-radius: 10px; background: var(--ac-tile); border: 1px solid var(--ac-line); min-width: 0; }
      .ac-sum small { display: block; font-size: 9.5px; color: var(--ac-muted); }
      .ac-sum b { font-size: 13px; }
      .ac-rules { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }
      .ac-rule { display: flex; align-items: center; gap: 10px; padding: 9px 10px; border-radius: 12px; background: var(--ac-tile); border: 1px solid var(--ac-line); }
      .ac-rule .ri { flex: none; width: 32px; height: 32px; display: grid; place-items: center; font-size: 16px; border-radius: 50%; background: color-mix(in srgb, var(--ac-warm) 16%, transparent); }
      .ac-rule div { flex: 1; min-width: 0; }
      .ac-rule b { display: block; font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .ac-rule small { font-size: 10.5px; color: var(--ac-muted); }
      .ac-rule .del { flex: none; padding: 4px 6px; cursor: pointer; font: inherit; font-size: 12px; color: var(--ac-muted); background: transparent; border: 0; border-radius: 6px; }
      .ac-rule .del:hover { color: var(--ac-bad); }
      .ac-sw { position: relative; width: 38px; height: 22px; flex: none; cursor: pointer; }
      .ac-sw input { position: absolute; opacity: 0; inset: 0; margin: 0; cursor: pointer; }
      .ac-sw span { position: absolute; inset: 0; border-radius: 99px; background: var(--ac-track); transition: background .2s; }
      .ac-sw span::after { content: ""; position: absolute; top: 3px; left: 3px; width: 16px; height: 16px; border-radius: 50%; background: #fff; transition: transform .2s; }
      .ac-sw input:checked + span { background: #2563eb; }
      .ac-sw input:checked + span::after { transform: translateX(16px); }
      .ac-sw input:focus-visible + span { outline: 2px solid var(--ac-accent-2); outline-offset: 2px; }
      .ac-add { width: 100%; margin-top: 10px; padding: 10px; cursor: pointer; font: inherit; font-size: 12px; font-weight: 700; color: var(--ac-accent); background: color-mix(in srgb, var(--ac-accent) 10%, transparent); border: 1px solid var(--ac-line); border-radius: 12px; }
      .ac-add:hover { border-color: var(--ac-accent); }
      .ac-form { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 10px; padding: 10px; border-radius: 12px; background: var(--ac-tile); border: 1px solid var(--ac-line); }
      .ac-form[hidden] { display: none; }
      .ac-form label { display: flex; flex-direction: column; gap: 3px; font-size: 10px; color: var(--ac-muted); }
      .ac-form label.wide { grid-column: 1 / -1; }
      .ac-form input, .ac-form select { font: inherit; font-size: 12px; padding: 7px 8px; color: var(--ac-text); background: var(--ac-panel); border: 1px solid var(--ac-line); border-radius: 8px; min-width: 0; }
      .ac-form .row { grid-column: 1 / -1; display: flex; gap: 8px; justify-content: flex-end; }
      .ac-form .row button { padding: 7px 14px; cursor: pointer; font: inherit; font-size: 11px; font-weight: 700; color: var(--ac-text); background: var(--ac-tile); border: 1px solid var(--ac-line); border-radius: 8px; }
      .ac-form .row button.go { color: #fff; background: #1d4ed8; border-color: #1d4ed8; }
      /* assistant */
      .ac-chat { display: flex; flex-direction: column; gap: 8px; height: 100%; }
      .ac-log { flex: 1; min-height: 130px; max-height: 220px; overflow: auto; display: flex; flex-direction: column; gap: 7px; }
      .ac-b { max-width: 92%; padding: 8px 11px; font-size: 12px; line-height: 1.45; border-radius: 12px; background: var(--ac-tile); border: 1px solid var(--ac-line); }
      .ac-b.me { align-self: flex-end; color: #fff; background: #1d4ed8; border-color: #1d4ed8; }
      .ac-b.err { color: var(--ac-bad); }
      .ac-chips { display: flex; flex-direction: column; gap: 5px; }
      .ac-chips button { text-align: left; padding: 7px 10px; cursor: pointer; font: inherit; font-size: 11px; color: var(--ac-text); background: var(--ac-tile); border: 1px solid var(--ac-line); border-radius: 9px; }
      .ac-chips button:hover { border-color: var(--ac-accent); }
      .ac-ask { display: flex; gap: 6px; }
      .ac-ask input { flex: 1; min-width: 0; font: inherit; font-size: 12px; padding: 9px 11px; color: var(--ac-text); background: var(--ac-panel); border: 1px solid var(--ac-line); border-radius: 99px; }
      .ac-ask button { flex: none; width: 38px; cursor: pointer; font-size: 15px; color: #fff; background: #1d4ed8; border: 0; border-radius: 50%; }
      .ac-msg { margin: 0; min-height: 1.3em; font-size: 11.5px; color: var(--ac-muted); }
      .ac-msg.err { color: var(--ac-bad); }
      .ac-setup { max-width: 640px; padding: 8px 4px; font-size: 13px; line-height: 1.6; }
      .ac-setup h4 { margin: 0 0 6px; font-size: 18px; }
      .ac-setup ol { margin: 10px 0; padding-left: 20px; }
      .ac-setup pre { margin: 8px 0; padding: 12px; border-radius: 10px; background: var(--ac-tile); border: 1px solid var(--ac-line); font-size: 12px; white-space: pre-wrap; min-height: 0; }
      .ac-setup button { padding: 9px 16px; cursor: pointer; font: inherit; font-weight: 700; color: #fff; background: #1d4ed8; border: 0; border-radius: 10px; }
      .ac-setup .note { color: var(--ac-muted); font-size: 12px; }
      .ac-login { display: grid; gap: 12px; margin: 14px 0 10px; padding: 18px; border-radius: 16px; border: 1px solid var(--ac-line); background: var(--ac-panel); }
      .ac-login label { display: grid; gap: 6px; font-size: 11px; font-weight: 600; letter-spacing: .5px; color: var(--ac-muted); }
      .ac-login input { font: inherit; font-size: 14px; padding: 11px 12px; color: var(--ac-text); background: var(--ac-tile); border: 1px solid var(--ac-line); border-radius: 10px; min-width: 0; width: 100%; box-sizing: border-box; }
      .ac-login input:focus { outline: 2px solid var(--ac-accent); outline-offset: 1px; }
      .ac-pw { position: relative; display: flex; }
      .ac-pw input { padding-right: 62px; }
      .ac-pw button { position: absolute; right: 6px; top: 50%; transform: translateY(-50%); padding: 5px 9px; font-size: 11px; color: var(--ac-accent); background: transparent; border: 0; border-radius: 6px; cursor: pointer; }
      .ac-login .go:disabled { opacity: .6; cursor: progress; }
      .ac-login .err { margin: 0; min-height: 1.2em; font-size: 12px; color: var(--ac-bad); }
      .ac-screen { color-scheme: dark; scrollbar-width: thin; scrollbar-color: var(--ac-track) transparent; }
      :root[data-theme="light"] .ac-screen { color-scheme: light; }
      .ac-log { scrollbar-width: thin; scrollbar-color: var(--ac-track) transparent; max-height: 280px; }
      .ac-log::-webkit-scrollbar, .ac-screen::-webkit-scrollbar { width: 8px; }
      .ac-log::-webkit-scrollbar-thumb, .ac-screen::-webkit-scrollbar-thumb { background: var(--ac-track); border-radius: 8px; }
      /* sidebar pages: Dashboard shows everything, the others show just their section */
      #acMain[data-view="ctl"] #acSecDev, #acMain[data-view="ctl"] .ac-g2 { display: none; }
      #acMain:not([data-view="all"]):not([data-view="ctl"]) .ac-panel { display: none; }
      #acMain[data-view="sch"] #acSecSch, #acMain[data-view="en"] #acSecEn, #acMain[data-view="hist"] #acSecHist,
      #acMain[data-view="dev"] #acSecDev, #acMain[data-view="ai"] #acSecAi { display: block; }
      #acMain:not([data-view="all"]):not([data-view="ctl"]) .ac-g1, #acMain:not([data-view="all"]):not([data-view="ctl"]) .ac-g2 { grid-template-columns: minmax(0, 780px); margin-top: 0 !important; }
      #acMain[data-view="hist"] .ac-chart { height: 300px; }
      #acMain[data-view="ai"] .ac-log { max-height: 52vh; }
      #acMain[data-view="en"] .ac-energy { grid-template-columns: 150px minmax(0, 1fr); }
      #acMain[data-view="en"] .ac-ring { width: 150px; height: 150px; }
      .ac-ask .mic { flex: none; width: 38px; cursor: pointer; font-size: 16px; color: var(--ac-text); background: var(--ac-tile); border: 1px solid var(--ac-line); border-radius: 50%; }
      .ac-ask .mic.rec { color: #fff; background: var(--ac-bad); border-color: var(--ac-bad); animation: blink 1s infinite; }
      /* larger, easier-to-read text */
      .ac-main { font-size: 15px; }
      .ac-top h2 { font-size: 30px; } .ac-top p { font-size: 15px; }
      .ac-nav button { font-size: 15px; padding: 13px 14px; }
      .ac-brand b { font-size: 18px; } .ac-brand small { font-size: 10.5px; }
      .ac-side-ai b { font-size: 14px; } .ac-side-ai small { font-size: 12.5px; } .ac-side-ai button { font-size: 13px; padding: 10px; }
      .ac-live { font-size: 13.5px; } .ac-live small { font-size: 11.5px; }
      .ac-panel h3 { font-size: 18px; } .ac-panel .sub { font-size: 13.5px; }
      .ac-pill-state { font-size: 14.5px; } .ac-pill-state em { font-size: 13px; }
      .ac-mini small { font-size: 13.5px; } .ac-mini b { font-size: 28px; } .ac-mini span { font-size: 13px; }
      .ac-center .lbl { font-size: 14px; } .ac-center .rng { font-size: 12px; }
      .ac-qb b { font-size: 14px; } .ac-qb small { font-size: 12.5px; }
      .ac-tabs button { font-size: 14px; padding: 10px 4px; }
      .ac-tile b { font-size: 15.5px; } .ac-tile small { font-size: 12px; } .ac-tile .ic { font-size: 24px; }
      .ac-dev dl div { font-size: 14.5px; grid-template-columns: 118px minmax(0, 1fr); }
      .ac-health b { font-size: 14.5px; } .ac-health small { font-size: 12.5px; }
      .ac-range button { font-size: 13.5px; padding: 7px 13px; }
      .ac-ring b { font-size: 25px; } .ac-ring small { font-size: 12px; }
      .ac-kv div { font-size: 13px; } .ac-kv b { font-size: 18px; }
      .ac-note { font-size: 12.5px; }
      .ac-chart text { font-size: 12px; }
      .ac-sum small { font-size: 11.5px; } .ac-sum b { font-size: 15.5px; }
      .ac-rule b { font-size: 15px; } .ac-rule small { font-size: 13px; }
      .ac-add { font-size: 14.5px; padding: 12px; }
      .ac-form label { font-size: 12.5px; } .ac-form input, .ac-form select { font-size: 14.5px; }
      .ac-form .row button { font-size: 13px; }
      .ac-b { font-size: 14.5px; } .ac-chips button { font-size: 13.5px; padding: 9px 11px; }
      .ac-ask input { font-size: 14.5px; } .ac-ask .mic, .ac-ask button { width: 42px; height: 42px; }
      .ac-msg { font-size: 13.5px; } .ac-setup { font-size: 15px; } .ac-empty { font-size: 14px; }
      .ac-login label { font-size: 13px; } .ac-login input { font-size: 15px; } .ac-login .err { font-size: 13.5px; }
      .ac-brand b { font-size: 16px; white-space: nowrap; } .ac-brand small { font-size: 9.5px; letter-spacing: .6px; white-space: nowrap; }
      .ac-brand > div { min-width: 0; }
      .ac-dev { grid-template-columns: 1fr; }
      .ac-health { flex-direction: row; justify-content: flex-start; gap: 4px 14px; flex-wrap: wrap; text-align: left; padding: 12px 14px; }
      .ac-health > * { margin: 0; }
      .ac-health b, .ac-health small { flex: 1 1 140px; }
      .ac-dev dd { white-space: normal; overflow-wrap: anywhere; }
      .ac-tabs button { white-space: nowrap; }
      .ac-wide { margin-top: 16px; }
      .ac-rep-head { display: flex; gap: 10px; align-items: center; justify-content: space-between; flex-wrap: wrap; margin-bottom: 10px; }
      .ac-rep-head .ac-range { margin: 0; }
      .ac-dl { width: auto; margin: 0; padding: 9px 16px; }
      .ac-chart.rep { height: auto; max-height: 340px; }
      .ac-chart .bar { fill: var(--ac-accent); }
      .ac-chart .bar.today { fill: var(--ac-accent-2); }
      .ac-chart .bval { fill: var(--ac-text); font-size: 12px; text-anchor: middle; }
      #acRepSum { grid-template-columns: repeat(4, minmax(0, 1fr)); margin: 12px 0 0; }
      .ac-tablewrap { overflow-x: auto; margin-top: 14px; }
      .ac-table { width: 100%; border-collapse: collapse; font-size: 14px; }
      .ac-table th { text-align: left; font-weight: 600; font-size: 12px; color: var(--ac-muted); padding: 8px 12px; border-bottom: 1px solid var(--ac-line); white-space: nowrap; }
      .ac-table td { padding: 10px 12px; border-bottom: 1px solid var(--ac-line); white-space: nowrap; }
      .ac-table tr.today td { color: var(--ac-accent-2); font-weight: 700; }
      .ac-table td.zero { color: var(--ac-muted); }
      .ac-b { white-space: pre-line; }
      #acMain[data-view="ctl"] #acSecRep { display: none; }
      #acMain[data-view="rep"] #acSecRep { display: block; margin-top: 0; }
      @media (max-width: 640px) { #acRepSum { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
      .ac-head-row { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; }
      .ac-head-row > div { flex: 1; min-width: 0; }
      .ac-head-row .ac-sel { flex: none; width: auto; min-width: 118px; max-width: 160px; margin: 0; }
      .ac-sel {
        flex: none; font: inherit; font-size: 14px; font-weight: 600; color: var(--ac-text); cursor: pointer;
        padding: 8px 32px 8px 16px; border-radius: 99px; border: 1px solid var(--ac-line); appearance: none;
        background: var(--ac-tile) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1.5l5 5 5-5' fill='none' stroke='%238a9bbd' stroke-width='1.8' stroke-linecap='round'/%3E%3C/svg%3E") no-repeat right 13px center;
      }
      .ac-sel:focus-visible { outline: 2px solid var(--ac-accent); outline-offset: 2px; }
      #acMain .ac-chart.hist { height: auto; }
      .ac-chart .hline { fill: none; stroke: var(--ac-accent); stroke-width: 2.2; stroke-linecap: round; stroke-linejoin: round; filter: drop-shadow(0 0 3px var(--ac-glow)); }
      .ac-chart .hdot { fill: var(--ac-accent-2); filter: drop-shadow(0 0 4px var(--ac-accent-2)); }
      .ac-chart .hnote { text-anchor: middle; font-size: 13px; }
      .ac-sum.icons { margin-top: 12px; }
      .ac-sum.icons div { display: flex; align-items: center; gap: 10px; padding: 10px 11px; }
      .ac-sum.icons .ico { flex: none; width: 36px; height: 36px; display: grid; place-items: center; border-radius: 50%; color: var(--ac-accent); background: color-mix(in srgb, var(--ac-accent) 14%, transparent); border: 1px solid var(--ac-line); }
      .ac-sum.icons span:last-child { min-width: 0; }
      .ac-sum.icons b { display: block; white-space: nowrap; }
      @media (max-width: 1600px) { .ac-g2 { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
      @media (max-width: 980px) {
        .ac-app { grid-template-columns: 1fr; }
        .ac-side { position: static; height: auto; flex-direction: row; align-items: center; padding: 10px 12px; overflow-x: auto; }
        .ac-brand { padding: 0 8px 0 0; } .ac-brand small, .ac-side-ai { display: none; }
        .ac-nav { flex-direction: row; } .ac-nav button { white-space: nowrap; padding: 9px 11px; }
        .ac-g1 { grid-template-columns: 1fr; }
      }
      @media (max-width: 640px) {
        .ac-main { padding: 14px 12px 24px; }
        .ac-g2 { grid-template-columns: 1fr; }
        .ac-hero-mid { grid-template-columns: 1fr 1fr; }
        .ac-hero-mid .ac-dial-wrap { grid-column: 1 / -1; grid-row: 1; justify-self: center; width: min(250px, 100%); }
        .ac-pill-state { position: static; margin-top: 8px; flex-direction: row; gap: 10px; }
        .ac-hero-top { flex-direction: column; align-items: center; }
        .ac-dev { grid-template-columns: 1fr; }
        .ac-tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        .ac-tiles.n3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
        .ac-top h2 { font-size: 20px; }
      }
      @media (prefers-reduced-motion: reduce) {
        .ac-screen { animation: none; }
        .ac-dial .val, .ac-ring .val, .ac-tile, .ac-sw span, .ac-sw span::after { transition: none; }
        .ac-live.on i, .ac-hero.on .ac-air { animation: none; }
      }

      /* Full screen (voice "full screen" or the ⛶ button) shows the whole dashboard. */
      #maxBtn[aria-pressed="true"] { color: #04121a; background: var(--neon-green); }

      /* --- Feed HUD: animated frame layered over the camera / AI visual --- */
      .video-wrap { --hud: var(--neon-green); animation: feedGlow 4s ease-in-out infinite; }
      .video-wrap.is-live { --hud: #22d3ee; }
      @keyframes feedGlow {
        0%, 100% { box-shadow: 0 0 0 rgba(16, 185, 129, 0); }
        50% { box-shadow: 0 0 22px rgba(16, 185, 129, 0.18), inset 0 0 30px rgba(16, 185, 129, 0.06); }
      }
      .feed-hud {
        position: absolute; inset: 0; z-index: 3;
        pointer-events: none;
        color: var(--hud);
        font-size: 10px; letter-spacing: 2px; text-transform: uppercase;
      }
      /* Corner brackets that breathe inward. */
      .hud-corner {
        position: absolute; width: 34px; height: 34px;
        border: 2px solid var(--hud);
        filter: drop-shadow(0 0 4px var(--hud));
        opacity: 0.85;
        animation: cornerBreathe 3s ease-in-out infinite;
      }
      .hud-corner.tl { top: 12px; left: 12px; border-right: 0; border-bottom: 0; --dx: 1; --dy: 1; }
      .hud-corner.tr { top: 12px; right: 12px; border-left: 0; border-bottom: 0; --dx: -1; --dy: 1; }
      .hud-corner.bl { bottom: 12px; left: 12px; border-right: 0; border-top: 0; --dx: 1; --dy: -1; }
      .hud-corner.br { bottom: 12px; right: 12px; border-left: 0; border-top: 0; --dx: -1; --dy: -1; }
      @keyframes cornerBreathe {
        0%, 100% { transform: translate(0, 0); }
        50% { transform: translate(calc(var(--dx) * 5px), calc(var(--dy) * 5px)); }
      }
      /* Scan beam sweeping the panel. */
      .hud-scan {
        position: absolute; left: 0; right: 0; top: 0; height: 90px;
        background: linear-gradient(to bottom, transparent, rgba(16, 185, 129, 0.10) 85%, var(--hud) 100%);
        opacity: 0.55;
        mix-blend-mode: screen;
        animation: hudScan 5s cubic-bezier(.45, 0, .55, 1) infinite;
      }
      /* The sweeping beam is only for standby; it would cut across faces on camera. */
      .video-wrap.is-live .hud-scan { display: none; }
      .hud-scan::after {
        content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 1px;
        background: var(--hud); box-shadow: 0 0 12px 2px var(--hud);
      }
      @keyframes hudScan {
        0% { transform: translateY(-100%); }
        100% { transform: translateY(calc(var(--feed-h, 600px) + 10px)); }
      }
      /* Center reticle: shown while the camera is live. */
      .hud-reticle {
        position: absolute; left: 50%; top: 50%;
        width: min(46%, 320px); aspect-ratio: 1;
        transform: translate(-50%, -50%) scale(0.85);
        opacity: 0;
        transition: opacity .5s ease, transform .6s cubic-bezier(.2, .8, .2, 1);
      }
      .video-wrap.is-live .hud-reticle { opacity: 1; transform: translate(-50%, -50%) scale(1); }
      /* Once a face is locked, the tracking boxes take over from the reticle. */
      .video-wrap.is-live.tracking .hud-reticle { opacity: 0; transform: translate(-50%, -50%) scale(1.25); }
      .face-canvas { position: absolute; inset: 0; width: 100%; height: 100%; }
      /* Portrait-scan look while live: the feed sits in a glowing circle on a
         deep violet backdrop; the canvas draws the frame, mesh and gauges. */
      .video-wrap.is-live:not(.staged) {
        background:
          radial-gradient(circle at 50% 48%, rgba(99, 102, 241, 0.35), transparent 55%),
          radial-gradient(circle at 85% 90%, rgba(217, 70, 239, 0.18), transparent 45%),
          linear-gradient(160deg, #1e1b4b, #0b1026 60%, #160f33);
      }
      .video-wrap.is-live:not(.staged) video {
        object-fit: cover;
        background: transparent;
        clip-path: circle(var(--scan-r, 36%) at 50% 50%);
      }
      .video-wrap.is-live:not(.staged) .hud-reticle { display: none; }
      /* Full photo inside the HUD frame (JS sets --fx/--fy/--fw/--fh). */
      .video-wrap.is-live:not(.staged) video#video {
        position: absolute !important;
        left: var(--fx, 15%); top: var(--fy, 10%);
        width: var(--fw, 70%) !important; height: var(--fh, 80%) !important;
        object-fit: contain !important;
        clip-path: inset(0 round 10px) !important;
        border-radius: 10px;
        background: #000 !important;
        z-index: 1 !important;
      }
      #videoBlur { display: none; }
      .video-wrap.is-live:not(.staged) video#videoBlur {
        display: block !important;
        position: absolute !important; z-index: 0;
        left: calc(var(--cx, 50%) - var(--cr, 30%)) !important; top: calc(var(--cy, 50%) - var(--cr, 30%)) !important;
        width: calc(var(--cr, 30%) * 2) !important; height: calc(var(--cr, 30%) * 2) !important;
        object-fit: cover !important; border-radius: 50%;
        clip-path: circle(50% at 50% 50%) !important;
        filter: blur(16px) brightness(0.45) saturate(1.2);
        transform: translateX(var(--scan-shift, 0px)) !important;
        background: transparent !important;
        pointer-events: none;
      }
      .hud-reticle svg { width: 100%; height: 100%; overflow: visible; }
      .hud-reticle .ring-a { transform-origin: 50% 50%; animation: spin 14s linear infinite; }
      .hud-reticle .ring-b { transform-origin: 50% 50%; animation: spin 9s linear infinite reverse; }
      .hud-reticle .ticks { transform-origin: 50% 50%; animation: reticlePulse 2.4s ease-in-out infinite; }
      @keyframes spin { to { transform: rotate(360deg); } }
      @keyframes reticlePulse { 50% { transform: scale(0.94); opacity: 0.6; } }
      /* Telemetry text. */
      .hud-tag { position: absolute; display: flex; flex-direction: column; gap: 3px; text-shadow: 0 0 6px var(--hud); }
      .hud-tag.top-left { top: 18px; left: 56px; }
      .hud-tag.bottom-right { bottom: 18px; right: 56px; text-align: right; opacity: 0.8; }
      .hud-tag.top-right { top: 38px; right: 56px; text-align: right; align-items: flex-end; }
      /* IP + geotag stamp: only while the camera is live. */
      .hud-geo { display: none; }
      .video-wrap.is-live .hud-geo { display: flex; }
      /* Red stamp, with a dark outline so it stays readable on bright video. */
      .hud-tag.hud-geo, .hud-tag.hud-geo b {
        color: #ff3b3b;
        text-shadow: 0 0 2px #000, 0 0 4px #000, 0 0 8px rgba(255, 59, 59, 0.6);
      }
      .hud-tag b { font-weight: 700; color: #fff; text-shadow: 0 0 8px var(--hud); }
      .hud-live-dot {
        display: inline-block; width: 6px; height: 6px; border-radius: 50%;
        background: var(--hud); margin-right: 6px; vertical-align: middle;
        box-shadow: 0 0 6px var(--hud); animation: blink 1.2s infinite;
      }
      /* Signal meter bars. */
      .hud-bars { display: inline-flex; gap: 2px; align-items: flex-end; height: 10px; margin-left: 6px; vertical-align: middle; }
      .hud-bars i { width: 2px; background: var(--hud); animation: barJitter 1.1s ease-in-out infinite; }
      .hud-bars i:nth-child(1) { height: 30%; animation-delay: -.2s; }
      .hud-bars i:nth-child(2) { height: 55%; animation-delay: -.5s; }
      .hud-bars i:nth-child(3) { height: 80%; animation-delay: -.8s; }
      .hud-bars i:nth-child(4) { height: 100%; animation-delay: -.3s; }
      @keyframes barJitter { 50% { transform: scaleY(0.4); } }
      .hud-bars i { transform-origin: bottom; }
      /* Boot flash when the camera comes online. */
      .hud-boot {
        position: absolute; inset: 0; opacity: 0;
        background: radial-gradient(circle at center, rgba(34, 211, 238, 0.35), transparent 65%);
      }
      .video-wrap.booting .hud-boot { animation: hudBoot .9s ease-out; }
      .video-wrap.booting video { animation: feedReveal .9s cubic-bezier(.2, .8, .2, 1); }
      @keyframes hudBoot { 0% { opacity: 1; } 100% { opacity: 0; } }
      @keyframes feedReveal {
        0% { clip-path: inset(50% 0 50% 0); filter: brightness(2.2) saturate(0); }
        60% { clip-path: inset(0 0 0 0); filter: brightness(1.3) saturate(0.6); }
        100% { clip-path: inset(0 0 0 0); filter: none; }
      }
      /* Faster REC blink while actually live. */
      .video-wrap.is-live::before { animation-duration: 1s; }

      video { width: 100%; height: 100%; object-fit: contain; background: #000; }
      .no-signal { color: var(--no-signal-color); letter-spacing: 4px; font-size: 18px; }

      /* System Ready Tag */
      .sys-ready {
        position: absolute;
        bottom: 20px;
        left: 20px;
        font-size: 10px;
        color: rgba(255,255,255,0.3);
        letter-spacing: 1px;
      }

      /* Voice listening overlay + animated sound wave */
      .voice-overlay {
        position: fixed;
        inset: 0;
        z-index: 1000;
        display: none;
        align-items: center;
        justify-content: center;
        background: radial-gradient(circle at center, rgba(5,11,20,0.7), rgba(5,11,20,0.94));
        backdrop-filter: blur(8px);
        -webkit-backdrop-filter: blur(8px);
      }
      .voice-overlay.active { display: flex; }

      .voice-card {
        position: relative;
        width: min(400px, calc(100vw - 32px));
        padding: 36px 32px 24px;
        border-radius: 18px;
        background: linear-gradient(180deg, rgba(16, 185, 129, 0.06), transparent 45%), var(--panel-bg);
        border: 1px solid var(--panel-border);
        box-shadow: 0 24px 60px rgba(0, 0, 0, 0.55), 0 0 50px rgba(16, 185, 129, 0.18);
        text-align: center;
        overflow: hidden;
        animation: onboardIn 0.35s cubic-bezier(.2, .9, .3, 1.2);
      }
      .voice-card::before {
        content: "";
        position: absolute;
        top: 0; left: 0; right: 0;
        height: 2px;
        background: linear-gradient(90deg, transparent, var(--neon-green), transparent);
      }

      .voice-mic {
        position: relative;
        width: 76px; height: 76px;
        margin: 0 auto 22px;
        border-radius: 50%;
        display: grid; place-items: center;
        background: radial-gradient(circle at 35% 30%, #34d399, var(--neon-green) 55%, #047857);
        color: #04110c;
        box-shadow: 0 0 30px var(--neon-glow);
      }
      .voice-mic svg { width: 32px; height: 32px; }
      .voice-mic::before, .voice-mic::after {
        content: "";
        position: absolute;
        inset: 0;
        border-radius: 50%;
        border: 2px solid var(--neon-green);
        animation: voiceRipple 2s ease-out infinite;
      }
      .voice-mic::after { animation-delay: 1s; }
      @keyframes voiceRipple {
        from { transform: scale(1); opacity: 0.7; }
        to   { transform: scale(1.9); opacity: 0; }
      }

      .voice-wave {
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 5px;
        height: 48px;
        margin-bottom: 18px;
      }
      .voice-wave span {
        display: block;
        width: 4px;
        height: 8px;
        border-radius: 4px;
        background: linear-gradient(180deg, #34d399, var(--neon-green));
        box-shadow: 0 0 8px var(--neon-glow);
        animation: voiceBar 1.1s ease-in-out infinite;
      }
      .voice-wave span:nth-child(1)  { animation-delay: 0.00s; }
      .voice-wave span:nth-child(2)  { animation-delay: 0.10s; }
      .voice-wave span:nth-child(3)  { animation-delay: 0.20s; }
      .voice-wave span:nth-child(4)  { animation-delay: 0.30s; }
      .voice-wave span:nth-child(5)  { animation-delay: 0.40s; }
      .voice-wave span:nth-child(6)  { animation-delay: 0.50s; }
      .voice-wave span:nth-child(7)  { animation-delay: 0.40s; }
      .voice-wave span:nth-child(8)  { animation-delay: 0.30s; }
      .voice-wave span:nth-child(9)  { animation-delay: 0.20s; }
      .voice-wave span:nth-child(10) { animation-delay: 0.10s; }
      .voice-wave span:nth-child(11) { animation-delay: 0.00s; }

      @keyframes voiceBar {
        0%, 100% { height: 8px; opacity: 0.5; }
        50%      { height: 44px; opacity: 1; }
      }

      .voice-label {
        font-size: 20px;
        font-weight: 700;
        letter-spacing: 0.5px;
        color: var(--header-text);
        margin-bottom: 6px;
      }
      @keyframes voiceDots { 0%,20%{opacity:0} 50%{opacity:1} 100%{opacity:0} }
      .voice-dots { color: var(--neon-green); animation: voiceDots 1.4s steps(1) infinite; }
      .voice-hint {
        font-size: 12px;
        line-height: 1.6;
        color: var(--text-secondary);
        margin-bottom: 14px;
      }
      .voice-lang {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 4px 10px;
        border-radius: 999px;
        border: 1px solid var(--panel-border);
        background: rgba(16, 185, 129, 0.08);
        font-size: 10px;
        letter-spacing: 1.5px;
        text-transform: uppercase;
        color: var(--neon-green);
        margin-bottom: 22px;
      }
      .voice-lang::before {
        content: "";
        width: 6px; height: 6px;
        border-radius: 50%;
        background: var(--alert-red);
        box-shadow: 0 0 8px var(--alert-red);
        animation: voiceDots 1.4s ease-in-out infinite;
      }
      /* Countdown bar that matches the 8s mic timeout. */
      .voice-timer {
        height: 3px;
        border-radius: 3px;
        background: var(--input-border);
        overflow: hidden;
        margin-bottom: 18px;
      }
      .voice-timer i {
        display: block;
        height: 100%;
        background: linear-gradient(90deg, #34d399, var(--neon-green));
        transform-origin: left;
      }
      .voice-overlay.active .voice-timer i { animation: voiceTimer 8s linear forwards; }
      @keyframes voiceTimer { from { transform: scaleX(1); } to { transform: scaleX(0); } }

      #voiceStopBtn.btn {
        width: 100%;
        padding: 13px 20px;
        border-radius: 10px;
        font-size: 12px;
        letter-spacing: 2px;
      }
      #voiceStopBtn.btn:hover {
        background: rgba(239, 68, 68, 0.12);
        border-color: var(--alert-red);
        color: var(--alert-red);
        box-shadow: 0 0 18px rgba(239, 68, 68, 0.3);
      }
      #voiceStopBtn .stop-sq {
        width: 10px; height: 10px;
        border-radius: 2px;
        background: currentColor;
      }
      .voice-foot {
        margin-top: 14px;
        font-size: 10px;
        letter-spacing: 1px;
        color: var(--text-secondary);
        opacity: 0.7;
      }
      .voice-foot kbd {
        font-family: inherit;
        padding: 1px 6px;
        border: 1px solid var(--input-border);
        border-radius: 4px;
      }

      /* Onboarding (ask name + gender on first load) */
      .onboard-overlay {
        position: fixed;
        inset: 0;
        z-index: 1100;
        display: none;
        align-items: center;
        justify-content: center;
        background: radial-gradient(circle at center, rgba(5,11,20,0.8), rgba(5,11,20,0.96));
        backdrop-filter: blur(8px);
        -webkit-backdrop-filter: blur(8px);
      }
      .onboard-overlay.active { display: flex; }

      .onboard-card {
        position: relative;
        width: min(440px, calc(100vw - 32px));
        background: linear-gradient(180deg, rgba(16, 185, 129, 0.06), transparent 40%), var(--panel-bg);
        border: 1px solid var(--panel-border);
        border-radius: 18px;
        padding: 36px 32px 24px;
        box-shadow: 0 24px 60px rgba(0, 0, 0, 0.55), 0 0 0 1px rgba(16, 185, 129, 0.08), 0 0 50px rgba(16, 185, 129, 0.18);
        text-align: left;
        overflow: hidden;
        animation: onboardIn 0.45s cubic-bezier(.2, .9, .3, 1.2);
      }
      .onboard-card::before {
        content: "";
        position: absolute;
        top: 0; left: 0; right: 0;
        height: 2px;
        background: linear-gradient(90deg, transparent, var(--neon-green), transparent);
      }
      @keyframes onboardIn {
        from { opacity: 0; transform: translateY(14px) scale(0.97); }
        to   { opacity: 1; transform: none; }
      }
      .onboard-head { text-align: center; margin-bottom: 26px; }
      .onboard-logo {
        position: relative;
        width: 58px; height: 58px;
        margin: 0 auto 16px;
        border-radius: 50%;
        display: grid; place-items: center;
        background: radial-gradient(circle at 35% 30%, #34d399, var(--neon-green) 55%, #047857);
        color: #04110c;
        font-size: 24px;
        font-weight: 800;
        box-shadow: 0 0 24px var(--neon-glow);
      }
      .onboard-logo::after {
        content: "";
        position: absolute;
        inset: -7px;
        border-radius: 50%;
        border: 1px solid var(--neon-green);
        opacity: 0.5;
        animation: onboardPulse 2.4s ease-out infinite;
      }
      @keyframes onboardPulse {
        from { transform: scale(0.9); opacity: 0.6; }
        to   { transform: scale(1.35); opacity: 0; }
      }
      .onboard-kicker {
        font-size: 10px;
        letter-spacing: 3px;
        color: var(--text-secondary);
        text-transform: uppercase;
        margin-bottom: 6px;
      }
      .onboard-title {
        font-weight: 700;
        font-size: 22px;
        letter-spacing: 0.5px;
        color: var(--header-text);
        margin-bottom: 8px;
      }
      .onboard-title span { color: var(--neon-green); text-shadow: 0 0 14px var(--neon-glow); }
      .onboard-sub {
        font-size: 12px;
        line-height: 1.6;
        color: var(--text-secondary);
        margin: 0;
      }
      .onboard-field { margin-bottom: 20px; }
      .onboard-label {
        display: flex;
        justify-content: space-between;
        font-size: 10px;
        font-weight: 700;
        letter-spacing: 2px;
        text-transform: uppercase;
        color: var(--text-secondary);
        margin-bottom: 8px;
      }
      .onboard-label em { font-style: normal; font-weight: 400; letter-spacing: 1px; opacity: 0.7; text-transform: none; }
      .onboard-input-wrap { position: relative; }
      .onboard-input-wrap svg {
        position: absolute;
        left: 14px; top: 50%;
        width: 16px; height: 16px;
        transform: translateY(-50%);
        color: var(--text-secondary);
        pointer-events: none;
      }
      .onboard-input-wrap input {
        width: 100%;
        box-sizing: border-box;
        padding: 14px 14px 14px 42px;
        border-radius: 10px;
        font-size: 15px;
      }
      .onboard-gender {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 12px;
      }
      .onboard-gender .btn {
        flex-direction: column;
        align-items: flex-start;
        gap: 4px;
        padding: 14px 16px;
        border-radius: 10px;
        text-align: left;
        text-transform: none;
        letter-spacing: 0;
        position: relative;
      }
      .onboard-gender .btn:active { transform: scale(0.98); }
      .onboard-gender .g-name { font-size: 14px; letter-spacing: 1px; text-transform: uppercase; }
      .onboard-gender .g-name b { font-size: 16px; margin-right: 6px; }
      .onboard-gender .g-hint { font-size: 10px; font-weight: 400; opacity: 0.65; }
      .onboard-gender .btn::after {
        content: "";
        position: absolute;
        top: 12px; right: 12px;
        width: 14px; height: 14px;
        border-radius: 50%;
        border: 1px solid var(--input-border);
        transition: all 0.2s ease;
      }
      .onboard-gender .btn.selected {
        background: rgba(16, 185, 129, 0.14);
        border-color: var(--neon-green);
        color: var(--neon-green);
        box-shadow: 0 0 18px rgba(16, 185, 129, 0.25), inset 0 0 0 1px var(--neon-green);
      }
      .onboard-gender .btn.selected::after {
        border-color: var(--neon-green);
        background: var(--neon-green);
        box-shadow: inset 0 0 0 3px var(--bg-color);
      }
      .onboard-err {
        min-height: 18px;
        margin: 4px 0 8px;
        font-size: 11px;
        letter-spacing: 0.5px;
        color: var(--alert-red);
      }
      .onboard-err:not(:empty)::before { content: "⚠ "; }
      #onboardStart.btn {
        width: 100%;
        padding: 15px 20px;
        border-radius: 10px;
        border: none;
        background: linear-gradient(135deg, #34d399, var(--neon-green) 50%, #059669);
        color: #04110c;
        font-size: 13px;
        letter-spacing: 2px;
        box-shadow: 0 8px 24px rgba(16, 185, 129, 0.35);
      }
      #onboardStart.btn:hover {
        filter: brightness(1.08);
        box-shadow: 0 10px 30px rgba(16, 185, 129, 0.5);
        color: #04110c;
      }
      #onboardStart.btn.is-disabled { opacity: 0.45; box-shadow: none; filter: grayscale(0.4); }
      #onboardStart .arrow { transition: transform 0.2s ease; }
      #onboardStart:hover .arrow { transform: translateX(4px); }
      .onboard-foot {
        margin-top: 16px;
        text-align: center;
        font-size: 10px;
        letter-spacing: 1px;
        color: var(--text-secondary);
        opacity: 0.7;
      }
      .onboard-foot kbd {
        font-family: inherit;
        padding: 1px 6px;
        border: 1px solid var(--input-border);
        border-radius: 4px;
      }

      /* Welcome toast shown to every user on load */
      .welcome-toast {
        position: fixed;
        top: -140px;
        left: 50%;
        transform: translateX(-50%);
        z-index: 1050;
        background: var(--panel-bg);
        border: 1px solid var(--neon-green);
        border-radius: 12px;
        padding: 14px 28px;
        box-shadow: 0 0 30px var(--neon-glow);
        color: var(--text-primary);
        text-align: center;
        max-width: 90vw;
        backdrop-filter: var(--glass-blur);
        -webkit-backdrop-filter: var(--glass-blur);
        opacity: 0;
        transition: top 0.5s cubic-bezier(.2,.8,.2,1), opacity 0.5s ease;
        pointer-events: none;
      }
      .welcome-toast.show { top: 24px; opacity: 1; }
      .welcome-toast .wt-title {
        font-weight: 700;
        font-size: 17px;
        letter-spacing: 1px;
        color: var(--neon-green);
        text-shadow: 0 0 12px var(--neon-glow);
      }
      .welcome-toast .wt-sub {
        display: block;
        font-size: 10px;
        color: var(--text-secondary);
        margin-top: 5px;
        letter-spacing: 2px;
        text-transform: uppercase;
      }

      /* Live detections box (face / person) inside the AI_Brain_Output panel */
      .detect-box {
        flex-shrink: 0;
        margin-top: 12px;
        border: 1px solid var(--input-border);
        border-radius: 8px;
        background: var(--pre-bg);
        overflow: hidden;
      }
      .detect-box-head {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 9px 12px;
        border-bottom: 1px solid var(--input-border);
        font-size: 10px;
        font-weight: 700;
        letter-spacing: 2px;
        text-transform: uppercase;
        color: #60a5fa;
      }
      .detect-box-title { display: inline-flex; align-items: center; gap: 8px; flex: 1; }
      .detect-dot {
        width: 7px; height: 7px; border-radius: 50%;
        background: var(--text-secondary);
      }
      .detect-box.live .detect-dot {
        background: #ef4444;
        box-shadow: 0 0 8px #ef4444;
        animation: blink 1.2s infinite;
      }
      .detect-count {
        min-width: 20px;
        padding: 1px 7px;
        border-radius: 999px;
        text-align: center;
        letter-spacing: 0;
        color: var(--header-text);
        background: rgba(96, 165, 250, 0.15);
        border: 1px solid rgba(96, 165, 250, 0.35);
      }
      .detect-clear {
        background: none;
        border: 1px solid var(--input-border);
        border-radius: 5px;
        padding: 2px 8px;
        font: inherit;
        font-size: 9px;
        letter-spacing: 1px;
        color: var(--text-secondary);
        cursor: pointer;
      }
      .detect-clear:hover { color: var(--header-text); border-color: #60a5fa; }
      .detect-list {
        list-style: none;
        margin: 0;
        padding: 6px;
        display: flex;
        flex-direction: column;
        gap: 6px;
        max-height: 196px;
        overflow-y: auto;
      }
      .detect-empty {
        padding: 14px 8px;
        text-align: center;
        font-size: 11px;
        color: var(--text-secondary);
        opacity: 0.8;
      }
      .detect-toast {
        --dt-color: #22d3ee;
        position: relative;
        display: grid;
        grid-template-columns: auto 1fr auto;
        align-items: center;
        gap: 10px;
        padding: 8px 10px;
        border-radius: 7px;
        background: rgba(255, 255, 255, 0.02);
        border: 1px solid var(--input-border);
        border-left: 3px solid var(--dt-color);
        animation: dtIn 0.35s cubic-bezier(.2, .9, .3, 1.2);
      }
      .detect-toast.person { --dt-color: #4ade80; }
      .detect-toast.object { --dt-color: #fbbf24; }
      .detect-toast.fresh { background: color-mix(in srgb, var(--dt-color) 9%, transparent); }
      .detect-toast.leaving { animation: dtOut 0.25s ease forwards; }
      @keyframes dtIn  { from { opacity: 0; transform: translateY(-8px); } to { opacity: 1; transform: none; } }
      @keyframes dtOut { to { opacity: 0; transform: translateX(20px); } }
      .detect-toast .dt-icon {
        width: 30px; height: 30px;
        border-radius: 8px;
        display: grid; place-items: center;
        color: var(--dt-color);
        background: color-mix(in srgb, var(--dt-color) 14%, transparent);
        border: 1px solid color-mix(in srgb, var(--dt-color) 40%, transparent);
      }
      .detect-toast .dt-icon svg { width: 16px; height: 16px; }
      /* Face snapshot in place of the icon. */
      .detect-toast .dt-icon.has-thumb {
        width: 42px; height: 42px;
        padding: 0;
        overflow: hidden;
        border: 1px solid var(--dt-color);
        box-shadow: 0 0 10px color-mix(in srgb, var(--dt-color) 35%, transparent);
      }
      .detect-toast .dt-icon.has-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
      .detect-toast .dt-main { min-width: 0; }
      .detect-toast .dt-title {
        display: flex;
        align-items: baseline;
        justify-content: space-between;
        gap: 8px;
        font-size: 12px;
        font-weight: 700;
        color: var(--header-text);
        margin-bottom: 2px;
      }
      .detect-toast .dt-time { font-size: 10px; font-weight: 400; color: var(--text-secondary); white-space: nowrap; }
      .detect-toast .dt-meta {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 10px;
        letter-spacing: 0.5px;
        color: var(--text-secondary);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .detect-toast .dt-conf {
        flex: 0 0 48px;
        height: 4px;
        border-radius: 2px;
        background: var(--input-border);
        overflow: hidden;
      }
      .detect-toast .dt-conf i { display: block; height: 100%; background: var(--dt-color); }
      .detect-toast .dt-close {
        background: none;
        border: none;
        color: var(--text-secondary);
        font-size: 14px;
        line-height: 1;
        cursor: pointer;
        padding: 2px 4px;
        opacity: 0.6;
      }
      .detect-toast .dt-close:hover { opacity: 1; color: var(--header-text); }

      /* ---------- Processing / thinking animation ---------- */
      .thinking { display: inline-flex; align-items: center; gap: 10px; }
      .dots-bounce { display: inline-flex; gap: 5px; }
      .dots-bounce i {
        width: 7px; height: 7px; border-radius: 50%;
        background: currentColor; box-shadow: 0 0 8px currentColor;
        animation: dotBounce 1.2s infinite ease-in-out both;
      }
      .dots-bounce i:nth-child(1) { animation-delay: -0.32s; }
      .dots-bounce i:nth-child(2) { animation-delay: -0.16s; }
      @keyframes dotBounce {
        0%, 80%, 100% { transform: scale(0.5); opacity: 0.4; }
        40% { transform: scale(1); opacity: 1; }
      }
      .thinking .label { letter-spacing: 1px; opacity: 0.85; }

      /* Blinking caret shown while text is typing out */
      .type-caret::after {
        content: "▋";
        margin-left: 1px;
        color: var(--ai-blue);
        animation: caretBlink 1s steps(1) infinite;
      }
      @keyframes caretBlink { 50% { opacity: 0; } }

      /* Indeterminate progress bar */
      .proc-bar {
        position: relative; height: 2px; margin-bottom: 10px;
        background: rgba(59, 130, 246, 0.15); border-radius: 2px;
        overflow: hidden; display: none;
      }
      .proc-bar.active { display: block; }
      .proc-bar::before {
        content: ""; position: absolute; left: -40%; width: 40%; height: 100%;
        background: var(--ai-blue); box-shadow: 0 0 10px var(--ai-blue);
        animation: procSlide 1.1s infinite ease-in-out;
      }
      @keyframes procSlide { 0% { left: -40%; } 100% { left: 100%; } }

      /* ---------- Responsive: tablets & phones ---------- */
      /* Stack the video feed above the log/AI panels on narrower screens. */
      @media (max-width: 1100px) {
        .main-area { grid-template-columns: 1fr; }
        .video-wrap { min-height: 260px; }
      }

      /* Single-column dashboard; allow the page to scroll vertically. */
      @media (max-width: 860px) {
        html, body { overflow-y: auto; height: auto; }
        .dashboard {
          grid-template-columns: 1fr;
          grid-template-rows: auto;
          height: auto;
          min-height: 100vh;
          gap: 14px;
          padding: 14px;
        }
        .header { grid-column: 1; }
        .controls-area { grid-column: 1; grid-row: auto; }
        .main-area { grid-column: 1; grid-row: auto; }
        pre { min-height: 150px; }
        .scanline-overlay { display: none; } /* avoids jank while scrolling */
      }

      /* Phones: let the header wrap and tighten spacing. */
      @media (max-width: 480px) {
        .header {
          flex-wrap: wrap;
          gap: 10px;
          padding: 14px;
        }
        .header-title { font-size: 17px; letter-spacing: 2px; }
        .control-group { margin-bottom: 18px; }
        .welcome-toast { padding: 12px 18px; max-width: 94vw; }
        .welcome-toast .wt-title { font-size: 15px; }
        input, select, .btn { padding: 12px; }
      }

      /* Respect users who prefer reduced motion. */
      @media (prefers-reduced-motion: reduce) {
        .scanline-overlay, .dot, .video-wrap, .video-wrap::before, .feed-hud *,
        .video-wrap.booting video, .video-wrap.booting .hud-boot,
        .dots-bounce i, .proc-bar::before, .type-caret::after,
        .media-player, .media-eq i { animation: none; }
        .media-item, .news-item a, .nb-card { animation: none; opacity: 1; }
        .stage, .video-wrap.staged video, .nb-run, .reader, .bt-screen { animation: none; }
        .bt-progress i, .bt-switch span, .bt-switch span::after { transition: none; }
        .news-live, .news-refresh.spin { animation: none; }
      }

    
      /* ================================================================
         Professional polish layer: one consistent look for every panel.
         Kept last so it refines the base styles above without rewriting them.
         ================================================================ */
      :root {
        --ui-radius: 14px;
        --ui-radius-sm: 9px;
        --ui-line: rgba(148, 163, 184, 0.12);
        --ui-surface: rgba(12, 20, 34, 0.78);
        --ui-surface-2: rgba(255, 255, 255, 0.025);
        --ui-muted: #64748b;
        --ui-strong: #e2e8f0;
        --ui-accent-2: #60a5fa;
      }
      :root[data-theme="light"] {
        --ui-line: rgba(15, 23, 42, 0.10);
        --ui-surface: rgba(255, 255, 255, 0.92);
        --ui-surface-2: rgba(15, 23, 42, 0.03);
        --ui-muted: #64748b;
        --ui-strong: #0f172a;
      }

      /* Page backdrop: deep navy with a faint technical grid. */
      body {
        background:
          radial-gradient(1200px 600px at 15% -10%, rgba(16, 185, 129, 0.08), transparent 60%),
          radial-gradient(1000px 700px at 110% 110%, rgba(59, 130, 246, 0.10), transparent 60%),
          linear-gradient(rgba(148, 163, 184, 0.035) 1px, transparent 1px) 0 0 / 32px 32px,
          linear-gradient(90deg, rgba(148, 163, 184, 0.035) 1px, transparent 1px) 0 0 / 32px 32px,
          var(--bg-color);
      }
      :root[data-theme="light"] body { background: var(--bg-color); }
      .scanline-overlay { opacity: 0.35; }
      /* No sweeping green beam across the feed / AI globe. */
      .hud-scan { display: none !important; }

      /* --- Auto Mic switch in the header ------------------------------------ */
      #autoMicToggle { display: inline-flex !important; align-items: center; gap: 7px; color: #94a3b8 !important;
        letter-spacing: .5px !important; text-transform: none; font-size: 11px !important; }
      #autoMicToggle .am-label { font-weight: 700; }
      #autoMicToggle .am-switch { position: relative; width: 30px; height: 16px; border-radius: 999px;
        background: rgba(148, 163, 184, 0.3); transition: background .2s; flex: none; }
      #autoMicToggle .am-switch i { position: absolute; top: 2px; left: 2px; width: 12px; height: 12px; border-radius: 50%;
        background: #e2e8f0; transition: transform .2s; }
      #autoMicToggle .am-switch.on { background: #10b981; box-shadow: 0 0 8px rgba(16, 185, 129, 0.6); }
      #autoMicToggle .am-switch.on i { transform: translateX(14px); }
      #autoMicToggle .am-state { min-width: 22px; font-weight: 800; font-size: 10px; }
      #autoMicToggle.am-on { color: #a7f3d0 !important; }
      #autoMicToggle.am-on .am-state { color: #34d399; }

      /* Hover explanations for the header icons (floating, not clipped). */
      .hdr-tip {
        position: fixed; z-index: 2000; max-width: 280px;
        padding: 9px 12px; border-radius: 9px;
        background: #0f172a; border: 1px solid rgba(125, 211, 252, 0.35);
        box-shadow: 0 12px 30px rgba(0, 0, 0, 0.5);
        color: #e2e8f0; font-size: 11.5px; line-height: 1.5; letter-spacing: .2px; text-transform: none;
        pointer-events: none; opacity: 0; transform: translateY(-4px);
        transition: opacity .15s, transform .15s;
      }
      .hdr-tip.show { opacity: 1; transform: none; }
      .hdr-tip::before { content: ""; position: absolute; top: -5px; left: var(--arrow, 50%); width: 8px; height: 8px;
        background: #0f172a; border-left: 1px solid rgba(125, 211, 252, 0.35); border-top: 1px solid rgba(125, 211, 252, 0.35);
        transform: translateX(-50%) rotate(45deg); }

      /* --- Floating tool windows (Notebook, Calculator) --------------------- */
      .tool-win {
        position: fixed; z-index: 1060;
        display: flex; flex-direction: column;
        border-radius: 14px;
        border: 1px solid rgba(125, 211, 252, 0.28);
        background: linear-gradient(180deg, rgba(34, 211, 238, 0.05), transparent 30%), rgba(10, 16, 32, 0.94);
        backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);
        box-shadow: 0 24px 60px rgba(0, 0, 0, 0.55);
        color: #cbd5e1;
        animation: pcardIn .3s cubic-bezier(.2, .9, .3, 1.1);
      }
      .tool-win[hidden] { display: none; }
      .tw-head {
        display: flex; align-items: center; gap: 10px;
        padding: 10px 12px 10px 14px;
        border-bottom: 1px solid rgba(148, 163, 184, 0.14);
        cursor: move; user-select: none;
      }
      .tw-title { font-size: 12px; font-weight: 700; letter-spacing: 1.5px; color: #f1f5f9; }
      .tw-status { flex: 1; text-align: right; font-size: 10px; letter-spacing: .5px; color: #64748b; }
      .tw-status.ok { color: #34d399; }
      .tw-status.err { color: #f87171; }
      .tw-x { margin-left: auto; background: none; border: 0; color: #94a3b8; font-size: 18px; line-height: 1; cursor: pointer; padding: 0 4px; }
      .tw-status + .tw-x { margin-left: 0; }
      .tw-x:hover { color: #fff; }

      .note-win { width: min(720px, calc(100vw - 32px)); height: min(480px, calc(100vh - 120px)); top: 90px; left: calc(50% - min(360px, 50vw - 16px)); }
      .note-body { flex: 1; min-height: 0; display: grid; grid-template-columns: 200px 1fr; }
      .note-side { border-right: 1px solid rgba(148, 163, 184, 0.12); display: flex; flex-direction: column; min-height: 0; padding: 10px; gap: 8px; }
      .note-new {
        height: 34px; border-radius: 8px; border: 0; cursor: pointer;
        background: linear-gradient(135deg, #22d3ee, #0ea5e9); color: #04121a;
        font: inherit; font-size: 11px; font-weight: 700; letter-spacing: 1px;
      }
      .note-list { list-style: none; margin: 0; padding: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 4px; scrollbar-width: thin; }
      .note-list li button {
        width: 100%; text-align: left; padding: 8px 10px; border-radius: 7px; cursor: pointer;
        border: 1px solid transparent; background: transparent; color: #cbd5e1; font: inherit;
      }
      .note-list li button:hover { background: rgba(255, 255, 255, 0.04); }
      .note-list li button.active { border-color: rgba(34, 211, 238, 0.45); background: rgba(34, 211, 238, 0.08); }
      .note-list .nl-name { display: block; font-size: 12px; font-weight: 700; color: #f1f5f9; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .note-list .nl-date { display: block; margin-top: 2px; font-size: 9.5px; color: #64748b; }
      .note-list .nl-empty { font-size: 11px; color: #64748b; padding: 6px 2px; }
      .note-main { display: flex; flex-direction: column; min-width: 0; min-height: 0; padding: 10px 12px 8px; gap: 8px; }
      .note-top { display: flex; gap: 8px; }
      .note-name {
        flex: 1; height: 38px !important; padding: 0 12px !important;
        font-size: 14px !important; font-weight: 700; color: #f8fafc !important;
      }
      .note-del {
        width: 38px; height: 38px; border-radius: 9px; cursor: pointer; font-size: 14px;
        display: grid; place-items: center;
        border: 1px solid rgba(148, 163, 184, 0.22); background: rgba(255, 255, 255, 0.03);
      }
      .note-del:hover { border-color: #f87171; background: rgba(248, 113, 113, 0.1); }
      .note-main .note-name { flex: none; }
      .note-label { font-size: 9.5px; font-weight: 700; letter-spacing: 1.5px; text-transform: uppercase; color: #64748b; margin-bottom: -4px; }
      .note-tools { display: flex; gap: 8px; }
      .note-tools button { display: inline-flex; align-items: center; justify-content: center; gap: 7px; width: auto !important; padding: 0 14px; font: inherit; font-size: 12px; font-weight: 700; }
      .note-tools .note-mic { flex: 1; color: #e2e8f0; }
      .note-tools .note-del { color: #fca5a5; }
      .note-guide {
        padding: 12px 14px; border-radius: 10px;
        border: 1px dashed rgba(34, 211, 238, 0.45); background: rgba(34, 211, 238, 0.05);
        font-size: 11.5px; line-height: 1.6; color: #cbd5e1;
      }
      .note-guide[hidden] { display: none; }
      .ng-title { font-size: 12.5px; font-weight: 700; color: #f1f5f9; margin-bottom: 4px; }
      .ng-steps { margin: 0 0 6px 18px; padding: 0; }
      .ng-steps b { color: #7dd3fc; }
      .ng-steps em { font-style: normal; color: #fff; background: rgba(255, 255, 255, 0.08); padding: 0 5px; border-radius: 4px; }
      .ng-say { font-size: 10.5px; color: #94a3b8; }
      .ng-say q { color: #a7f3d0; }
      .ng-start {
        margin-top: 10px; height: 36px; padding: 0 16px; border-radius: 9px; border: 0; cursor: pointer;
        display: inline-flex; align-items: center; gap: 8px;
        background: linear-gradient(135deg, #22d3ee, #0ea5e9); color: #04121a; font: inherit; font-size: 12px; font-weight: 700;
      }
      .ng-start:hover { filter: brightness(1.08); }
      .tw-undo {
        margin-left: 6px; padding: 1px 8px; border-radius: 5px; cursor: pointer;
        border: 1px solid #22d3ee; background: rgba(34, 211, 238, 0.12); color: #22d3ee;
        font: inherit; font-size: 10px; font-weight: 700;
      }
      .tw-undo:hover { background: #22d3ee; color: #04121a; }
      .note-send { display: inline-flex; align-items: center; gap: 6px; }
      .note-mic {
        width: 38px; height: 38px; border-radius: 9px; cursor: pointer; font-size: 16px;
        display: grid; place-items: center; color: #cbd5e1;
        border: 1px solid rgba(148, 163, 184, 0.22); background: rgba(255, 255, 255, 0.03);
      }
      .note-mic:hover { border-color: #22d3ee; }
      .note-mic.on {
        color: #fca5a5; border-color: #ef4444; background: rgba(239, 68, 68, 0.15);
        box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.5); animation: noteMicPulse 1.4s infinite;
      }
      @keyframes noteMicPulse { 70% { box-shadow: 0 0 0 9px rgba(239, 68, 68, 0); } 100% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0); } }
      .note-interim { color: #7dd3fc; font-style: italic; }
      .note-send {
        height: 38px; padding: 0 14px; border-radius: 9px; cursor: pointer; border: 0;
        background: linear-gradient(135deg, #34d399, #10b981); color: #04121a;
        font: inherit; font-size: 12px; font-weight: 700; letter-spacing: .5px; white-space: nowrap;
      }
      .note-send:hover { filter: brightness(1.08); }
      .note-send:disabled { opacity: .5; cursor: progress; }
      .note-text {
        flex: 1; min-height: 0; resize: none;
        padding: 12px 14px; border-radius: 9px;
        border: 1px solid rgba(148, 163, 184, 0.18); background: rgba(0, 0, 0, 0.28);
        color: #e2e8f0; font-family: inherit; font-size: 13px; line-height: 1.7; outline: none;
      }
      .note-text:focus { border-color: #22d3ee; box-shadow: 0 0 0 3px rgba(34, 211, 238, 0.12); }
      .note-foot { font-size: 9.5px; letter-spacing: .5px; color: #64748b; display: flex; justify-content: space-between; }
      .note-sched {
        display: flex; align-items: center; gap: 8px;
        padding: 7px 10px; border-radius: 8px;
        border: 1px solid rgba(52, 211, 153, 0.35); background: rgba(52, 211, 153, 0.07);
        font-size: 10.5px; color: #a7f3d0;
      }
      .note-sched[hidden] { display: none; }
      .note-sched span { flex: 1; }
      .note-sched button {
        border: 1px solid rgba(148, 163, 184, 0.3); background: transparent; color: #94a3b8;
        border-radius: 6px; padding: 3px 8px; font: inherit; font-size: 10px; cursor: pointer;
      }
      .note-sched button:hover { color: #f87171; border-color: #f87171; }

      .phone-win { width: min(560px, calc(100vw - 32px)); top: 90px; left: calc(50% - min(280px, 50vw - 16px)); max-height: calc(100vh - 110px); }
      .ph-body { padding: 12px 16px 16px; overflow-y: auto; font-size: 12px; line-height: 1.6; }
      .ph-conn { display: flex; align-items: center; gap: 8px; padding: 9px 12px; border-radius: 9px;
        border: 1px solid rgba(148, 163, 184, 0.2); background: rgba(255, 255, 255, 0.03); font-weight: 700; color: #94a3b8; }
      .ph-conn i { width: 9px; height: 9px; border-radius: 50%; background: #64748b; flex: none; }
      .ph-conn.on { color: #a7f3d0; border-color: rgba(52, 211, 153, 0.45); background: rgba(52, 211, 153, 0.07); }
      .ph-conn.on i { background: #34d399; box-shadow: 0 0 8px #34d399; animation: blink 1.6s infinite; }
      .ph-intro { margin: 10px 0; color: #cbd5e1; }
      .ph-tabs { display: flex; gap: 6px; margin-bottom: 10px; }
      .ph-tabs button { flex: 1; height: 34px; border-radius: 8px; cursor: pointer; font: inherit; font-size: 11.5px; font-weight: 700;
        border: 1px solid rgba(148, 163, 184, 0.22); background: transparent; color: #94a3b8; }
      .ph-tabs button.on { background: rgba(34, 211, 238, 0.12); border-color: #22d3ee; color: #e0f2fe; }
      .ph-pane[hidden] { display: none; }
      .ph-steps { margin: 0 0 10px 18px; padding: 0; color: #cbd5e1; }
      .ph-steps li { margin-bottom: 6px; }
      .ph-steps b { color: #f1f5f9; }
      .ph-steps code, .ph-link code { padding: 1px 6px; border-radius: 4px; background: rgba(0, 0, 0, 0.35); color: #7dd3fc; }
      .ph-steps pre { margin: 6px 0; padding: 8px 10px; border-radius: 7px; background: rgba(0, 0, 0, 0.4); color: #a7f3d0; font-size: 12px; }
      .ph-steps a { color: #7dd3fc; }
      .ph-steps li.warn { color: #fde68a; }
      .ph-qr-row { display: flex; gap: 14px; align-items: center; }
      .ph-qr { width: 156px; height: 156px; flex: none; border-radius: 10px; background: #fff; padding: 8px; display: grid; place-items: center; color: #0f172a; font-size: 10px; text-align: center; }
      .ph-qr img, .ph-qr canvas { width: 100%; height: 100%; image-rendering: pixelated; }
      .ph-link { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
      .ph-link code { font-size: 13px; word-break: break-all; }
      .ph-link button { align-self: flex-start; height: 30px; padding: 0 12px; border-radius: 7px; cursor: pointer; border: 0;
        background: linear-gradient(135deg, #22d3ee, #0ea5e9); color: #04121a; font: inherit; font-size: 11px; font-weight: 700; }
      .ph-link small { color: #94a3b8; font-size: 10.5px; }
      .ph-last { margin-top: 10px; padding: 8px 10px; border-radius: 8px; background: rgba(34, 211, 238, 0.06);
        border: 1px solid rgba(34, 211, 238, 0.25); color: #e0f2fe; }
      /* --- FM Radio window ---------------------------------------------------- */
      .radio-win {
        width: min(460px, calc(100vw - 32px)); top: 84px; right: 40px; max-height: calc(100vh - 100px);
        border-color: rgba(244, 114, 182, 0.4);
        background: linear-gradient(180deg, rgba(244, 114, 182, 0.08), transparent 35%), rgba(10, 16, 32, 0.96);
      }
      .radio-win .tw-title { color: #fbcfe8; }
      .rd-now { display: flex; align-items: center; gap: 12px; margin: 12px 14px 8px; padding: 12px;
        border-radius: 12px; border: 1px solid rgba(244, 114, 182, 0.3); background: rgba(244, 114, 182, 0.06); }
      .rd-logo { width: 54px; height: 54px; flex: none; border-radius: 12px; overflow: hidden; display: grid; place-items: center;
        font-size: 26px; background: linear-gradient(135deg, #f472b6, #8b5cf6); }
      .rd-logo img { width: 100%; height: 100%; object-fit: cover; background: #fff; }
      .rd-info { flex: 1; min-width: 0; display: flex; flex-direction: column; line-height: 1.35; }
      .rd-info small { font-size: 9px; font-weight: 700; letter-spacing: 2px; color: #f472b6; }
      .rd-info b { font-size: 15px; color: #f8fafc; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .rd-info span { font-size: 11px; color: #94a3b8; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .rd-eq { display: flex; align-items: flex-end; gap: 3px; height: 26px; opacity: .25; }
      .rd-eq i { width: 4px; height: 30%; border-radius: 2px; background: #f472b6; }
      .radio-win.live .rd-eq { opacity: 1; }
      .radio-win.live .rd-eq i { animation: rdEq 0.9s ease-in-out infinite; }
      .rd-eq i:nth-child(2) { animation-delay: -.3s !important; } .rd-eq i:nth-child(3) { animation-delay: -.6s !important; }
      .rd-eq i:nth-child(4) { animation-delay: -.15s !important; } .rd-eq i:nth-child(5) { animation-delay: -.45s !important; }
      @keyframes rdEq { 0%, 100% { height: 25%; } 50% { height: 100%; } }
      .rd-play { width: 46px; height: 46px; flex: none; border-radius: 50%; border: 0; cursor: pointer; font-size: 17px;
        background: linear-gradient(135deg, #f472b6, #db2777); color: #fff; box-shadow: 0 6px 18px rgba(219, 39, 119, 0.4); }
      .rd-play:hover { filter: brightness(1.1); }
      .rd-vol { display: flex; align-items: center; gap: 10px; margin: 0 16px 10px; font-size: 11px; color: #94a3b8; }
      .rd-vol input { flex: 1; height: auto !important; padding: 0 !important; accent-color: #f472b6; border: 0 !important; background: none !important; }
      .rd-search { display: flex; gap: 8px; margin: 0 14px 8px; }
      .rd-search input { flex: 1; height: 38px !important; font-size: 12px !important; }
      .rd-search button { height: 38px; padding: 0 14px; border-radius: 9px; border: 0; cursor: pointer;
        background: linear-gradient(135deg, #f472b6, #db2777); color: #fff; font: inherit; font-size: 11px; font-weight: 700; }
      .rd-quick { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 14px 10px; }
      .rd-quick button { padding: 4px 10px; border-radius: 999px; cursor: pointer; font: inherit; font-size: 10.5px;
        border: 1px solid rgba(244, 114, 182, 0.35); background: transparent; color: #fbcfe8; }
      .rd-quick button:hover, .rd-quick button.on { background: rgba(244, 114, 182, 0.18); }
      .rd-list { list-style: none; margin: 0; padding: 0 14px 14px; overflow-y: auto; display: flex; flex-direction: column; gap: 6px;
        max-height: 330px; scrollbar-width: thin; }
      .rd-empty { font-size: 11.5px; color: #94a3b8; padding: 8px 2px; }
      .rd-item { width: 100%; display: flex; align-items: center; gap: 10px; padding: 7px 9px; border-radius: 9px; cursor: pointer;
        text-align: left; font: inherit; color: #e2e8f0; border: 1px solid rgba(148, 163, 184, 0.14); background: rgba(255, 255, 255, 0.02); }
      .rd-item:hover { border-color: rgba(244, 114, 182, 0.5); background: rgba(244, 114, 182, 0.05); }
      .rd-item.on { border-color: #f472b6; background: rgba(244, 114, 182, 0.12); }
      .rd-fav { width: 34px; height: 34px; flex: none; border-radius: 8px; overflow: hidden; display: grid; place-items: center;
        font-size: 16px; background: rgba(244, 114, 182, 0.15); }
      .rd-fav img { width: 100%; height: 100%; object-fit: cover; background: #fff; }
      .rd-item b { display: block; font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .rd-item span.rd-sub { display: block; font-size: 10px; color: #94a3b8; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .rd-item .rd-txt { flex: 1; min-width: 0; }

      /* --- Skills window ------------------------------------------------------- */
      .skills-btn { display: inline-flex; align-items: center; gap: 5px; padding: 5px 11px !important; border-radius: 999px !important;
        font-size: 11px !important; font-weight: 700; letter-spacing: 1px; cursor: pointer;
        color: #67e8f9 !important; background: rgba(34, 211, 238, 0.08) !important; border: 1px solid rgba(34, 211, 238, 0.35) !important; }
      .skills-btn:hover { background: rgba(34, 211, 238, 0.18) !important; }
      .skills-win { width: min(1040px, calc(100vw - 32px)); top: 70px; left: calc(50% - min(520px, 50vw - 16px));
        max-height: calc(100vh - 86px); display: flex; flex-direction: column;
        border-color: rgba(34, 211, 238, 0.35);
        background: radial-gradient(120% 60% at 100% 0%, rgba(34, 211, 238, 0.08), transparent 55%), rgba(8, 13, 26, 0.97); }
      .skills-win[hidden] { display: none; }
      .skills-win .tw-title { display: inline-flex; align-items: center; gap: 10px; }
      .sk-bolt { width: 30px; height: 30px; display: grid; place-items: center; border-radius: 9px; font-size: 16px;
        background: linear-gradient(135deg, rgba(34, 211, 238, 0.25), rgba(99, 102, 241, 0.25)); border: 1px solid rgba(34, 211, 238, 0.4); }
      .sk-title-txt { display: flex; flex-direction: column; line-height: 1.2; }
      .sk-title-txt b { font-size: 13px; letter-spacing: 1.5px; color: #e0f2fe; }
      .sk-title-txt small { font-size: 10.5px; color: #7dd3fc; letter-spacing: .3px; }
      .sk-body { display: flex; flex-direction: column; gap: 12px; padding: 14px 16px 12px; min-height: 0; flex: 1; }
      .sk-tools { display: flex; flex-direction: column; gap: 10px; }
      .sk-search { width: 100%; height: 40px !important; padding: 0 14px !important; border-radius: 10px !important; font-size: 13px !important;
        color: #f1f5f9 !important; background: rgba(255, 255, 255, 0.03) !important; border: 1px solid rgba(34, 211, 238, 0.25) !important; }
      .sk-search:focus { outline: none; border-color: #22d3ee !important; box-shadow: 0 0 0 3px rgba(34, 211, 238, 0.15); }
      .sk-cats { display: flex; gap: 6px; flex-wrap: wrap; }
      .sk-cat { height: 30px; padding: 0 11px; border-radius: 999px; cursor: pointer; font: inherit; font-size: 11.5px; white-space: nowrap;
        color: #94a3b8; background: rgba(255, 255, 255, 0.03); border: 1px solid rgba(148, 163, 184, 0.18); }
      .sk-cat:hover { color: #e2e8f0; border-color: rgba(34, 211, 238, 0.4); }
      .sk-cat.on { color: #082f49; background: #67e8f9; border-color: #67e8f9; font-weight: 700; }
      .sk-cat i { font-style: normal; opacity: .7; margin-left: 4px; font-size: 10px; }
      .sk-grid { overflow-y: auto; min-height: 0; flex: 1; display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
        gap: 10px; align-content: start; padding-right: 4px; scrollbar-width: thin; scrollbar-color: rgba(34, 211, 238, 0.3) transparent; }
      .sk-grid::-webkit-scrollbar { width: 6px; } .sk-grid::-webkit-scrollbar-button { display: none; height: 0; }
      .sk-grid::-webkit-scrollbar-thumb { background: rgba(34, 211, 238, 0.3); border-radius: 99px; }
      .sk-sec { grid-column: 1 / -1; display: flex; align-items: center; gap: 8px; margin: 6px 0 -2px;
        font-size: 11px; font-weight: 700; letter-spacing: 1.6px; color: #7dd3fc; text-transform: uppercase; }
      .sk-sec::after { content: ""; flex: 1; height: 1px; background: linear-gradient(90deg, rgba(34, 211, 238, 0.35), transparent); }
      .sk-sec span { font-size: 15px; letter-spacing: 0; }
      .sk-card { padding: 12px 13px; border-radius: 12px; background: rgba(255, 255, 255, 0.025);
        border: 1px solid rgba(148, 163, 184, 0.14); display: flex; flex-direction: column; gap: 8px; transition: border-color .15s, background .15s; }
      .sk-card:hover { border-color: rgba(34, 211, 238, 0.35); background: rgba(34, 211, 238, 0.04); }
      .sk-card h5 { margin: 0; font-size: 13.5px; color: #f1f5f9; letter-spacing: .2px; }
      .sk-card p { margin: 0; font-size: 11.5px; line-height: 1.5; color: #94a3b8; }
      .sk-ex { display: flex; flex-wrap: wrap; gap: 6px; }
      .sk-ex button { display: inline-flex; align-items: center; gap: 6px; max-width: 100%; padding: 5px 10px; border-radius: 8px; cursor: pointer;
        font: 11.5px/1.35 "JetBrains Mono", ui-monospace, monospace; text-align: left; color: #cffafe;
        background: rgba(34, 211, 238, 0.07); border: 1px solid rgba(34, 211, 238, 0.22); }
      .sk-ex button:hover { background: rgba(34, 211, 238, 0.18); border-color: #22d3ee; }
      .sk-ex button.chk { color: #fde68a; background: rgba(251, 191, 36, 0.06); border-color: rgba(251, 191, 36, 0.25); }
      .sk-ex button.chk:hover { background: rgba(251, 191, 36, 0.16); border-color: #fbbf24; }
      .sk-ex button.edt { color: #ddd6fe; background: rgba(167, 139, 250, 0.07); border-color: rgba(167, 139, 250, 0.28); }
      .sk-ex button.edt:hover { background: rgba(167, 139, 250, 0.18); border-color: #a78bfa; }
      .sk-ex button i { font-style: normal; font-size: 10px; opacity: .8; flex: none; }
      .sk-ex button span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .sk-ex em { font-size: 11px; color: #64748b; font-style: normal; }
      .sk-empty { grid-column: 1 / -1; text-align: center; padding: 30px 10px; color: #64748b; font-size: 12.5px; }
      .sk-foot { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 10.5px; color: #64748b; padding-top: 8px; border-top: 1px solid rgba(148, 163, 184, 0.12); }
      .sk-k { display: inline-grid; place-items: center; width: 16px; height: 16px; border-radius: 4px; font-style: normal; font-size: 9px; margin-right: 3px; }
      .sk-k.run { color: #67e8f9; border: 1px solid rgba(34, 211, 238, 0.5); }
      .sk-k.chk { color: #fde68a; border: 1px solid rgba(251, 191, 36, 0.5); }
      .sk-k.edt { color: #ddd6fe; border: 1px solid rgba(167, 139, 250, 0.5); }
      .sk-tip { margin-left: auto; color: #7dd3fc; }
      .sk-flash { animation: skFlash 1.2s ease-out; }
      @keyframes skFlash { 0% { box-shadow: 0 0 0 3px rgba(34, 211, 238, 0.7); } 100% { box-shadow: 0 0 0 0 rgba(34, 211, 238, 0); } }
      @media (max-width: 700px) { .sk-grid { grid-template-columns: 1fr; } .sk-tip { margin-left: 0; } }

      /* --- Quran window: audio player (mp3quran.net) + 114 surahs --------------- */
      .quran-win {
        width: min(960px, calc(100vw - 32px)); top: 76px; left: calc(50% - min(480px, 50vw - 16px));
        max-height: calc(100vh - 92px);
        border-color: rgba(212, 175, 55, 0.45);
        background:
          radial-gradient(120% 60% at 0% 0%, rgba(212, 175, 55, 0.10), transparent 60%),
          linear-gradient(180deg, rgba(9, 14, 28, 0.98), rgba(6, 10, 22, 0.98));
        box-shadow: 0 30px 80px rgba(0, 0, 0, 0.6), 0 0 0 1px rgba(212, 175, 55, 0.08) inset;
      }
      .quran-win .tw-head { border-bottom: 1px solid rgba(212, 175, 55, 0.18); }
      .quran-win .tw-status { font-size: 10.5px; letter-spacing: .6px; }
      .quran-win .tw-status.ok { flex: 0 0 auto; margin-left: auto; margin-right: 6px; display: inline-flex; align-items: center; gap: 7px; padding: 4px 10px; border-radius: 999px;
        color: #f3e3b3; background: rgba(212, 175, 55, 0.1); border: 1px solid rgba(212, 175, 55, 0.35); }
      .quran-win .tw-status.ok::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: #34d399;
        box-shadow: 0 0 0 0 rgba(52, 211, 153, 0.6); animation: qrDot 1.6s infinite; }
      @keyframes qrDot { 70% { box-shadow: 0 0 0 7px rgba(52, 211, 153, 0); } 100% { box-shadow: 0 0 0 0 rgba(52, 211, 153, 0); } }
      .quran-win .tw-title { color: #f3e3b3; display: inline-flex; align-items: center; gap: 12px; }
      .quran-win .tw-title .qr-bsm { font-size: 22px; }
      .qr-bsm { font-family: "Amiri", "Traditional Arabic", "Arabic Typesetting", "Segoe UI Historic", serif;
        font-size: 34px; line-height: 1; color: #d4af37; text-shadow: 0 0 14px rgba(212, 175, 55, 0.4); letter-spacing: 0; }
      .qr-title-txt { display: flex; flex-direction: column; line-height: 1.15; }
      .qr-title-txt b { font-size: 13px; letter-spacing: 1.5px; color: #f8ecc9; font-weight: 700; }
      .qr-title-txt small { font-family: "Amiri", "Traditional Arabic", serif; font-size: 13px; color: #b9a26a; letter-spacing: 0; }
      .tw-btn { width: 28px; height: 26px; border-radius: 7px; cursor: pointer; font-size: 13px; line-height: 1;
        border: 1px solid rgba(212, 175, 55, 0.3); background: rgba(212, 175, 55, 0.06); color: #f3e3b3; }
      .tw-btn:hover { background: #d4af37; color: #1c1405; }
      .tw-status + .tw-btn { margin-left: 0; }

      .qr-body { display: grid; grid-template-columns: minmax(0, 1fr) 330px; gap: 16px; padding: 16px; min-height: 0; overflow: hidden; }
      .qr-stage { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
      .qr-stage:fullscreen { justify-content: center; padding: 6vh 8vw; background: #060a16; }
      .qr-stage:fullscreen .qr-hero { flex: 1; }
      .qr-stage:fullscreen .qr-arabic { font-size: clamp(64px, 11vw, 150px); }

      .qr-hero {
        position: relative; overflow: hidden; border-radius: 16px; min-height: 290px;
        display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 6px; padding: 26px 20px 22px;
        text-align: center; border: 1px solid rgba(212, 175, 55, 0.28);
        flex: 1;
        background:
          radial-gradient(55% 65% at 50% 42%, rgba(212, 175, 55, 0.15), transparent 70%),
          linear-gradient(180deg, #0e1528, #080d1b);
      }
      .qr-hero::before, .qr-hero::after { content: ""; position: absolute; left: 18px; right: 18px; height: 1px;
        background: linear-gradient(90deg, transparent, rgba(212, 175, 55, 0.55), transparent); }
      .qr-hero::before { top: 12px; } .qr-hero::after { bottom: 12px; }
      .qr-medal { position: relative; width: 64px; height: 64px; display: grid; place-items: center; margin-bottom: 6px; }
      .qr-medal svg { position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; }
      .qr-medal polygon { fill: rgba(212, 175, 55, 0.14); stroke: #d4af37; stroke-width: 2.2; stroke-linejoin: round; }
      .qr-medal circle { fill: #0b1222; stroke: rgba(212, 175, 55, 0.7); stroke-width: 1.4; }
      .qr-medal span { position: relative; font: 700 16px "JetBrains Mono", monospace; color: #f3e3b3; }
      .qr-hero.playing .qr-medal svg { filter: drop-shadow(0 0 10px rgba(212, 175, 55, 0.55)); animation: qrGlow 3s ease-in-out infinite; }
      @keyframes qrGlow { 50% { filter: drop-shadow(0 0 3px rgba(212, 175, 55, 0.25)); } }
      .qr-watermark { position: absolute; width: 360px; height: 360px; left: 50%; top: 50%; transform: translate(-50%, -50%);
        pointer-events: none; opacity: .07; }
      .qr-watermark polygon, .qr-watermark circle { fill: none; stroke: #d4af37; stroke-width: .6; }
      .qr-hero > :not(.qr-watermark) { position: relative; }
      .qr-emblem { position: relative; width: 30px; height: 30px; flex: none; display: grid; place-items: center; }
      .qr-emblem svg { position: absolute; inset: 0; width: 100%; height: 100%; }
      .qr-emblem polygon { fill: rgba(212, 175, 55, 0.16); stroke: #d4af37; stroke-width: 3.5; stroke-linejoin: round; }
      .qr-emblem circle { fill: #0b1222; stroke: #d4af37; stroke-width: 2.5; }
      .qr-emblem i { position: relative; font-style: normal; font-size: 12px; line-height: 1; color: #f3e3b3; }
      .qr-arabic { font-family: "Amiri", "Traditional Arabic", "Arabic Typesetting", serif; direction: rtl;
        font-size: clamp(44px, 6.4vw, 76px); line-height: 1.35; color: #f3e3b3;
        text-shadow: 0 0 26px rgba(212, 175, 55, 0.35), 0 2px 0 rgba(0, 0, 0, 0.4); }
      .qr-arabic.long { font-size: clamp(26px, 3.6vw, 40px); }
      .qr-name { font-size: 18px; font-weight: 700; color: #f8fafc; letter-spacing: .3px; }
      .qr-meta { font-size: 11.5px; color: #9aa6bd; letter-spacing: .4px; }
      .qr-meta a { color: #d4af37; text-decoration: none; margin-left: 6px; }
      .qr-meta a:hover { text-decoration: underline; }
      .qr-eq { display: flex; align-items: flex-end; gap: 3px; height: 18px; margin-top: 8px; opacity: 0; transition: opacity .3s; }
      .qr-eq i { width: 3px; height: 4px; border-radius: 2px; background: #d4af37; }
      .qr-hero.playing .qr-eq { opacity: 1; }
      .qr-hero.playing .qr-eq i { animation: qrEq 1.1s ease-in-out infinite; }
      .qr-eq i:nth-child(2) { animation-delay: -.9s !important; } .qr-eq i:nth-child(3) { animation-delay: -.3s !important; }
      .qr-eq i:nth-child(4) { animation-delay: -.6s !important; } .qr-eq i:nth-child(5) { animation-delay: -.15s !important; }
      .qr-eq i:nth-child(6) { animation-delay: -.75s !important; } .qr-eq i:nth-child(7) { animation-delay: -.45s !important; }
      @keyframes qrEq { 0%, 100% { height: 4px; } 50% { height: 18px; } }
      .qr-msg { margin-top: 8px; padding: 7px 12px; border-radius: 8px; font-size: 12px; color: #fecaca;
        background: rgba(239, 68, 68, 0.12); border: 1px solid rgba(239, 68, 68, 0.35); }
      .qr-msg button { margin-left: 8px; border: 0; background: none; color: #fde68a; font-weight: 700; cursor: pointer; text-decoration: underline; }
      .qr-resume { margin-top: 10px; padding: 7px 14px; border-radius: 999px; cursor: pointer; font: inherit; font-size: 12px;
        border: 1px solid rgba(212, 175, 55, 0.45); background: rgba(212, 175, 55, 0.08); color: #f3e3b3; }
      .qr-resume:hover { background: #d4af37; color: #1c1405; }
      .qr-resume[hidden], .qr-msg[hidden] { display: none; }

      .qr-seek { display: grid; grid-template-columns: 44px 1fr 44px; align-items: center; gap: 10px;
        font: 600 11px "JetBrains Mono", monospace; color: #9aa6bd; }
      .qr-seek span:last-child { text-align: right; }
      .qr-range { -webkit-appearance: none; appearance: none; width: 100% !important; height: 5px !important; padding: 0 !important;
        border-radius: 99px; border: 0 !important; cursor: pointer; margin: 0;
        background: linear-gradient(90deg, #d4af37 var(--p, 0%), rgba(148, 163, 184, 0.22) var(--p, 0%)) !important; }
      .qr-range::-webkit-slider-thumb { -webkit-appearance: none; width: 14px; height: 14px; border-radius: 50%;
        background: #f3e3b3; border: 2px solid #d4af37; box-shadow: 0 0 10px rgba(212, 175, 55, 0.6); }
      .qr-range::-moz-range-thumb { width: 12px; height: 12px; border-radius: 50%; background: #f3e3b3; border: 2px solid #d4af37; }

      .qr-bar { display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 12px; }
      .qr-reciter { justify-self: start; max-width: 100%; min-width: 0; height: 36px !important; padding: 0 30px 0 10px !important;
        border-radius: 9px !important; font-size: 12px !important; color: #f3e3b3 !important; cursor: pointer;
        border: 1px solid rgba(212, 175, 55, 0.3) !important; background-color: rgba(212, 175, 55, 0.06) !important; }
      .qr-reciter option { background: #0d1426; color: #e2e8f0; }
      .qr-ctrls { display: flex; align-items: center; gap: 10px; }
      .qr-ctrls button, .qr-tog { display: grid; place-items: center; cursor: pointer; border-radius: 50%; padding: 0;
        border: 1px solid rgba(212, 175, 55, 0.35); background: rgba(212, 175, 55, 0.06); color: #f3e3b3; transition: transform .12s, background .15s; }
      .qr-ctrls button { width: 40px; height: 40px; }
      .qr-ctrls button svg, .qr-tog svg { width: 17px; height: 17px; fill: currentColor; }
      .qr-ctrls .qr-play { width: 56px; height: 56px; border: 0; color: #1c1405;
        background: linear-gradient(180deg, #f1d47a, #c9a02c); box-shadow: 0 8px 24px rgba(212, 175, 55, 0.35); }
      .qr-ctrls .qr-play svg { width: 22px; height: 22px; }
      .qr-ctrls button:hover, .qr-tog:hover { transform: scale(1.06); }
      .qr-ctrls button:not(.qr-play):hover, .qr-tog:hover { background: rgba(212, 175, 55, 0.2); }
      .qr-ctrls button:disabled { opacity: .4; cursor: default; transform: none; }
      .qr-right { justify-self: end; display: flex; align-items: center; gap: 8px; }
      .qr-tog { width: 32px; height: 32px; opacity: .55; }
      .qr-vol-wrap { display: inline-flex; align-items: center; gap: 6px; color: #b9a26a; }
      .qr-vol-wrap svg { width: 16px; height: 16px; fill: currentColor; flex: none; }
      .qr-tog svg { width: 15px; height: 15px; }
      .qr-tog[aria-pressed="true"] { opacity: 1; background: rgba(212, 175, 55, 0.22); border-color: #d4af37; }
      .qr-vol { width: 78px !important; }

      .qr-side { display: flex; flex-direction: column; gap: 10px; min-height: 0; }
      .qr-search { width: 100%; height: 38px !important; padding: 0 12px !important; border-radius: 10px !important; font-size: 13px !important;
        border: 1px solid rgba(212, 175, 55, 0.25) !important; background: rgba(255, 255, 255, 0.03) !important; color: #f1f5f9 !important; }
      .qr-search:focus { outline: none; border-color: #d4af37 !important; box-shadow: 0 0 0 3px rgba(212, 175, 55, 0.15); }
      .qr-tabs { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; padding: 3px; border-radius: 10px; background: rgba(255, 255, 255, 0.04); }
      .qr-tabs button { height: 30px; border: 0; border-radius: 8px; cursor: pointer; font: inherit; font-size: 12px; font-weight: 700;
        letter-spacing: .5px; background: transparent; color: #9aa6bd; }
      .qr-tabs button.on { background: rgba(212, 175, 55, 0.18); color: #f3e3b3; }
      .qr-list { list-style: none; margin: 0; padding: 0 4px 0 0; overflow-y: auto; display: flex; flex-direction: column; gap: 4px;
        max-height: 430px; scrollbar-width: thin; scrollbar-color: rgba(212, 175, 55, 0.35) transparent; }
      .qr-list::-webkit-scrollbar { width: 6px; }
      .qr-list::-webkit-scrollbar-button { display: none; height: 0; }
      .qr-list::-webkit-scrollbar-track { background: transparent; }
      .qr-list::-webkit-scrollbar-thumb { background: rgba(212, 175, 55, 0.3); border-radius: 99px; }
      .qr-list::-webkit-scrollbar-thumb:hover { background: rgba(212, 175, 55, 0.55); }
      .qr-item { width: 100%; display: grid; grid-template-columns: 34px 1fr auto 26px; gap: 10px; align-items: center; text-align: left;
        padding: 8px 6px 8px 8px; border-radius: 10px; cursor: pointer; font: inherit; color: #e2e8f0;
        border: 1px solid transparent; background: transparent; transition: background .15s, border-color .15s; }
      .qr-item:hover { background: rgba(212, 175, 55, 0.06); border-color: rgba(212, 175, 55, 0.2); }
      .qr-item.on { background: rgba(212, 175, 55, 0.12); border-color: rgba(212, 175, 55, 0.55); }
      .qr-n { position: relative; width: 32px; height: 32px; display: grid; place-items: center;
        font: 700 11px "JetBrains Mono", monospace; color: #f3e3b3; }
      .qr-n svg { position: absolute; inset: 0; width: 100%; height: 100%; }
      .qr-n polygon { fill: rgba(212, 175, 55, 0.07); stroke: rgba(212, 175, 55, 0.65); stroke-width: 3; stroke-linejoin: round; }
      .qr-n circle { fill: none; stroke: none; }
      .qr-item.on .qr-n { color: #1c1405; }
      .qr-item.on .qr-n polygon { fill: #d4af37; stroke: #d4af37; }
      .qr-n b { position: relative; z-index: 1; font-size: 10.5px; }
      .qr-it { min-width: 0; }
      .qr-it b { display: block; font-size: 13px; color: #f1f5f9; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .qr-it span { display: block; font-size: 10.5px; color: #8b97ad; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .qr-ar { font-family: "Amiri", "Traditional Arabic", serif; font-size: 19px; color: #d4af37; direction: rtl; }
      .qr-star { width: 26px; height: 26px; border: 0; background: none; cursor: pointer; padding: 0; font-size: 15px; line-height: 1;
        color: rgba(148, 163, 184, 0.45); border-radius: 6px; }
      .qr-star:hover { color: #f3e3b3; background: rgba(212, 175, 55, 0.12); }
      .qr-star.on { color: #d4af37; }
      .qr-empty-list { padding: 18px 8px; text-align: center; font-size: 12px; color: #8b97ad; }
      .qr-foot { font-size: 10px; color: #64708a; letter-spacing: .4px; text-align: right; }
      @media (max-width: 820px) {
        .qr-body { grid-template-columns: 1fr; overflow-y: auto; }
        .qr-list { max-height: 320px; }
        .qr-bar { grid-template-columns: 1fr; justify-items: center; }
        .qr-reciter, .qr-right { justify-self: center; }
      }

      .qr-mini {
        position: fixed; z-index: 1065; left: 20px; bottom: 20px; overflow: hidden;
        display: flex; align-items: center; gap: 10px; padding: 8px 10px 10px 14px; border-radius: 16px;
        border: 1px solid rgba(212, 175, 55, 0.5);
        background: linear-gradient(135deg, rgba(212, 175, 55, 0.14), rgba(9, 14, 28, 0.96));
        box-shadow: 0 12px 30px rgba(0, 0, 0, 0.5), 0 0 18px rgba(212, 175, 55, 0.15);
        backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
        animation: pcardIn .3s cubic-bezier(.2, .9, .3, 1.1);
      }
      .qr-mini[hidden] { display: none; }
      .qr-mini .qr-emblem { width: 34px; height: 34px; }
      .qr-mini-text { display: flex; flex-direction: column; line-height: 1.25; width: 190px; }
      .qr-mini-text small { font-size: 8.5px; font-weight: 700; letter-spacing: 1.8px; color: #d4af37; }
      .qr-mini-text b { font-size: 12.5px; color: #f8fafc; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .qr-mini button { width: 32px; height: 32px; border-radius: 50%; cursor: pointer; display: grid; place-items: center; padding: 0;
        border: 1px solid rgba(212, 175, 55, 0.45); background: rgba(212, 175, 55, 0.1); color: #f3e3b3; }
      .qr-mini button svg { width: 14px; height: 14px; fill: currentColor; }
      .qr-mini button:hover { background: #d4af37; color: #1c1405; }
      .qr-mini-bar { position: absolute; left: 0; bottom: 0; height: 2px; width: var(--p, 0%); background: #d4af37; transition: width .4s linear; }
      .calc-win { width: 300px; top: 100px; right: 40px; }
      .calc-screen { padding: 14px 16px 10px; text-align: right; border-bottom: 1px solid rgba(148, 163, 184, 0.12); }
      .calc-expr { min-height: 18px; font-size: 12px; color: #64748b; word-break: break-all; }
      .calc-out { margin-top: 4px; font-size: 30px; font-weight: 700; color: #f8fafc; word-break: break-all; font-variant-numeric: tabular-nums; }
      .calc-out.err { font-size: 16px; color: #f87171; }
      .calc-keys { display: grid; grid-template-columns: repeat(4, 1fr); gap: 7px; padding: 12px; }
      .calc-keys button {
        height: 46px; border-radius: 10px; cursor: pointer;
        border: 1px solid rgba(148, 163, 184, 0.16); background: rgba(255, 255, 255, 0.04);
        color: #f1f5f9; font: inherit; font-size: 16px; font-weight: 600;
        transition: background .15s, transform .08s;
      }
      .calc-keys button:hover { background: rgba(255, 255, 255, 0.09); }
      .calc-keys button:active { transform: scale(0.95); }
      .calc-keys .fn { color: #7dd3fc; }
      .calc-keys .op { color: #22d3ee; background: rgba(34, 211, 238, 0.08); }
      .calc-keys .eq { background: linear-gradient(135deg, #22d3ee, #0ea5e9); color: #04121a; border-color: transparent; }
      .calc-keys .wide { grid-column: 1 / -1; height: 36px; font-size: 12px; }
      .calc-hist { max-height: 84px; overflow-y: auto; padding: 0 16px 12px; font-size: 11px; color: #64748b; text-align: right; scrollbar-width: thin; }
      .calc-hist div { padding: 2px 0; cursor: pointer; }
      .calc-hist div:hover { color: #cbd5e1; }
      @media (max-width: 640px) { .note-body { grid-template-columns: 1fr; } .note-side { display: none; } }

      /* --- Globe screen: date / time / location + map box ------------------ */
      .globe-info {
        position: absolute;
        z-index: 2;
        top: 64px; left: 56px;
        display: flex; flex-direction: column; gap: 6px;
        padding: 10px 12px;
        border-radius: 12px;
        border: 1px solid rgba(125, 211, 252, 0.22);
        background: rgba(8, 16, 34, 0.55);
        backdrop-filter: blur(8px);
        -webkit-backdrop-filter: blur(8px);
        color: #cbd5e1;
        font-size: 11px;
        letter-spacing: 1px;
        pointer-events: auto;
      }
      .gi-row { display: flex; align-items: center; gap: 8px; white-space: nowrap; }
      .gi-when { color: #7dd3fc; font-weight: 600; }
      .gi-when b { color: #f8fafc; font-size: 13px; letter-spacing: 1.5px; font-variant-numeric: tabular-nums; }
      .gi-sep { width: 4px; height: 4px; border-radius: 50%; background: #7dd3fc; opacity: .6; }
      .gi-where svg { width: 13px; height: 13px; color: #f472b6; flex: none; }
      .gi-where span { color: #e2e8f0; text-transform: uppercase; letter-spacing: 1.5px; max-width: 200px; overflow: hidden; text-overflow: ellipsis; }
      .gi-btn {
        width: 26px; height: 26px;
        display: grid; place-items: center;
        padding: 0;
        border-radius: 7px;
        border: 1px solid rgba(125, 211, 252, 0.25);
        background: rgba(255, 255, 255, 0.04);
        font-size: 13px; line-height: 1;
        cursor: pointer;
        transition: border-color .2s, background .2s;
      }
      .gi-btn:hover, .gi-btn[aria-expanded="true"] { border-color: #7dd3fc; background: rgba(125, 211, 252, 0.12); }
      .gi-btn:focus-visible { outline: 2px solid #7dd3fc; outline-offset: 2px; }
      .gi-btn.off { opacity: .55; }
      .gi-map {
        width: min(320px, 60vw);
        margin-top: 4px;
        border-radius: 10px;
        overflow: hidden;
        border: 1px solid rgba(125, 211, 252, 0.3);
        background: #0b1220;
        box-shadow: 0 14px 34px rgba(0, 0, 0, 0.5);
        animation: pcardIn .3s cubic-bezier(.2, .9, .3, 1.1);
      }
      .gi-map-head {
        display: flex; align-items: center; gap: 8px;
        padding: 7px 10px;
        font-size: 9px; font-weight: 700; letter-spacing: 1.8px; color: #94a3b8;
        border-bottom: 1px solid rgba(125, 211, 252, 0.15);
      }
      .gi-map-head span { flex: 1; }
      .gi-map-head a, .gi-map-head button {
        background: none; border: 0; color: #94a3b8; font-size: 14px; line-height: 1;
        cursor: pointer; text-decoration: none; padding: 0 2px;
      }
      .gi-map-head a:hover, .gi-map-head button:hover { color: #fff; }
      .gi-bt svg { width: 13px; height: 13px; color: #60a5fa; flex: none; }
      .gi-bt span { color: #94a3b8; text-transform: uppercase; letter-spacing: 1.2px; max-width: 260px; overflow: hidden; text-overflow: ellipsis; }
      .gi-bt-dot { width: 6px; height: 6px; border-radius: 50%; background: #475569; flex: none; }
      .gi-bt.on span { color: #e2e8f0; }
      .gi-bt.on .gi-bt-dot { background: #34d399; box-shadow: 0 0 8px #34d399; animation: blink 1.6s infinite; }
      .gi-bt.flash { animation: btFlash 1.2s ease-out; border-radius: 6px; }
      @keyframes btFlash { 0% { background: rgba(52, 211, 153, 0.35); } 100% { background: transparent; } }

      /* On-screen Bluetooth alert, bottom-centre of the video panel. */
      .bt-toast {
        position: absolute; z-index: 5;
        left: 50%; bottom: 64px;
        display: flex; align-items: center; gap: 12px;
        min-width: 280px; max-width: min(440px, 80%);
        padding: 10px 16px 10px 12px;
        border-radius: 12px;
        border: 1px solid rgba(52, 211, 153, 0.45);
        background: rgba(8, 16, 34, 0.82);
        backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
        box-shadow: 0 14px 34px rgba(0, 0, 0, 0.45), 0 0 24px rgba(52, 211, 153, 0.15);
        color: #e2e8f0;
        opacity: 0; pointer-events: none;
        transform: translate(-50%, 16px);
        transition: opacity .35s ease, transform .45s cubic-bezier(.2, .9, .3, 1.15);
      }
      .bt-toast.show { opacity: 1; transform: translate(-50%, 0); }
      .bt-toast.off { border-color: rgba(148, 163, 184, 0.4); box-shadow: 0 14px 34px rgba(0, 0, 0, 0.45); }
      .bt-toast .bt-t-ico {
        width: 36px; height: 36px; flex: none;
        display: grid; place-items: center; font-size: 18px;
        border-radius: 10px;
        background: rgba(52, 211, 153, 0.14); border: 1px solid rgba(52, 211, 153, 0.4);
      }
      .bt-toast.off .bt-t-ico { background: rgba(148, 163, 184, 0.1); border-color: rgba(148, 163, 184, 0.35); }
      .bt-toast .bt-t-name { font-size: 13px; font-weight: 700; color: #f8fafc; }
      .bt-toast .bt-t-sub { margin-top: 2px; font-size: 9.5px; letter-spacing: 1.5px; text-transform: uppercase; color: #34d399; }
      .bt-toast.off .bt-t-sub { color: #94a3b8; }
      .bt-toast .bt-t-time { margin-left: auto; font-size: 10px; color: #64748b; }

      .gi-map-view { height: 190px; overflow: hidden; }
      .gi-map iframe {
        display: block; width: 100%; height: 236px; border: 0;
        /* Dark map to match the HUD. */
        filter: invert(0.92) hue-rotate(180deg) saturate(0.8) brightness(0.95);
      }
      .gi-map-foot { padding: 6px 10px; font-size: 9.5px; letter-spacing: 1px; color: #7dd3fc; }
      .gi-map-foot small { display: block; margin-top: 3px; font-size: 8px; letter-spacing: .5px; color: #64748b; text-transform: none; }

      /* --- Info card beside the face circle ------------------------------ */
      .video-wrap.is-live:not(.staged) video { transform: translateX(var(--scan-shift, 0px)); transition: transform .45s cubic-bezier(.2, .8, .2, 1); }
      .video-wrap.staged .pcard-wrap { display: none; }
      .pcard-wrap {
        position: absolute;
        z-index: 4;
        top: 118px; right: 18px; bottom: 64px;
        width: min(310px, 42%);
        display: flex;
        flex-direction: column;
        gap: 10px;
        pointer-events: none;
      }
      .pcard-wrap > * { pointer-events: auto; }
      .pcard-search {
        display: flex;
        align-items: center;
        gap: 8px;
        height: 40px;
        padding: 0 4px 0 12px;
        border-radius: 10px;
        border: 1px solid rgba(148, 163, 184, 0.22);
        background: rgba(10, 16, 32, 0.78);
        backdrop-filter: blur(10px);
        -webkit-backdrop-filter: blur(10px);
        box-shadow: 0 10px 28px rgba(0, 0, 0, 0.35);
      }
      .pcard-search:focus-within { border-color: #22d3ee; box-shadow: 0 0 0 3px rgba(34, 211, 238, 0.15); }
      .pcard-search {
        align-self: flex-end;
        width: 100%;
        overflow: hidden;
        transition: width .35s cubic-bezier(.2, .8, .2, 1), padding .35s ease, border-radius .35s ease;
      }
      .pcard-search .pcard-toggle {
        flex: none;
        width: 26px; height: 26px;
        display: grid; place-items: center;
        padding: 0; border: 0; border-radius: 50%; background: none;
        font-size: 15px; line-height: 1; cursor: pointer;
      }
      /* Collapsed: just the round 🔍 button. */
      .pcard-search.collapsed {
        width: 44px; height: 44px;
        padding: 0;
        justify-content: center;
        border-radius: 50%;
        border-color: rgba(34, 211, 238, 0.45);
        animation: pcPing 2.6s ease-out infinite;
      }
      .pcard-search.collapsed input, .pcard-search.collapsed button[type="submit"] { display: none; }
      .pcard-search.collapsed .pcard-toggle { width: 44px; height: 44px; font-size: 18px; }
      .pcard-search .pcard-toggle:focus-visible { outline: 2px solid #22d3ee; outline-offset: 2px; }
      .pcard-search.collapsed:hover { border-color: #22d3ee; }
      @keyframes pcPing {
        0% { box-shadow: 0 0 0 0 rgba(34, 211, 238, 0.35); }
        70%, 100% { box-shadow: 0 0 0 10px rgba(34, 211, 238, 0); }
      }
      .pcard-search input {
        flex: 1; min-width: 0;
        height: 100%; padding: 0;
        border: 0 !important; background: transparent !important; box-shadow: none !important;
        color: #e2e8f0; font-size: 12px;
      }
      .pcard-search button {
        height: 30px; padding: 0 12px;
        border: 0; border-radius: 7px;
        background: linear-gradient(135deg, #22d3ee, #0ea5e9);
        color: #04121a; font: inherit; font-size: 11px; font-weight: 700; letter-spacing: 1px;
        cursor: pointer;
      }
      .pcard {
        flex: 0 1 auto;
        min-height: 120px;
        overflow-x: hidden;
        overflow-y: auto;
        scrollbar-width: thin;
        border-radius: 14px;
        border: 1px solid rgba(148, 163, 184, 0.2);
        background: linear-gradient(180deg, rgba(34, 211, 238, 0.06), transparent 30%), rgba(10, 16, 32, 0.86);
        backdrop-filter: blur(12px);
        -webkit-backdrop-filter: blur(12px);
        box-shadow: 0 18px 44px rgba(0, 0, 0, 0.45);
        color: #cbd5e1;
        animation: pcardIn .4s cubic-bezier(.2, .9, .3, 1.1);
      }
      @keyframes pcardIn { from { opacity: 0; transform: translateX(16px); } to { opacity: 1; transform: none; } }
      .pc-head {
        display: flex; align-items: center; gap: 8px;
        padding: 10px 12px;
        border-bottom: 1px solid rgba(148, 163, 184, 0.14);
        font-size: 9px; font-weight: 700; letter-spacing: 1.8px; text-transform: uppercase; color: #94a3b8;
      }
      .pc-chip { flex: none; white-space: nowrap; padding: 2px 8px; border-radius: 999px; color: #04121a; background: #22d3ee; letter-spacing: 1px; }
      .pc-chip.LinkedIn { background: #0a66c2; color: #fff; }
      .pc-chip.GitHub { background: #e2e8f0; }
      .pc-chip.Instagram { background: linear-gradient(135deg, #f58529, #dd2a7b, #8134af); color: #fff; }
      .pc-chip.answer { background: #a78bfa; }
      .pc-head .pc-src { flex: 1; min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis;
        text-align: right; font-weight: 500; letter-spacing: 1px; text-transform: none; }
      .pc-x { background: none; border: 0; color: #94a3b8; font-size: 15px; line-height: 1; cursor: pointer; padding: 0 2px; }
      .pc-x:hover { color: #fff; }
      .pc-id { display: flex; gap: 12px; align-items: center; padding: 14px 14px 10px; }
      .pc-avatar {
        width: 54px; height: 54px; flex: none;
        border-radius: 50%;
        object-fit: cover;
        display: grid; place-items: center;
        background: linear-gradient(135deg, #22d3ee, #6366f1);
        color: #fff; font-size: 18px; font-weight: 700;
        box-shadow: 0 0 0 2px rgba(10, 16, 32, 1), 0 0 0 3px rgba(34, 211, 238, 0.6);
      }
      .pc-name { font-size: 15px; font-weight: 700; color: #f8fafc; line-height: 1.3; word-break: break-word; }
      .pc-headline { margin-top: 3px; font-size: 11px; line-height: 1.5; color: #94a3b8; }
      .pc-handle { margin-top: 3px; font-size: 10px; color: #22d3ee; }
      .pc-summary { padding: 0 14px 10px; font-size: 11px; line-height: 1.65; color: #cbd5e1; white-space: pre-wrap; }
      .pc-facts { display: grid; grid-template-columns: auto 1fr; gap: 6px 12px; margin: 0 14px 12px; padding: 10px 12px;
        border-radius: 9px; background: rgba(255, 255, 255, 0.03); border: 1px solid rgba(148, 163, 184, 0.12); font-size: 11px; }
      .pc-facts dt { color: #64748b; font-size: 9px; letter-spacing: 1.2px; text-transform: uppercase; align-self: center; }
      .pc-facts dd { color: #e2e8f0; margin: 0; word-break: break-word; }
      .pc-notice { margin: 0 14px 12px; padding: 9px 11px; border-radius: 8px; font-size: 10.5px; line-height: 1.55;
        color: #fde68a; background: rgba(251, 191, 36, 0.08); border: 1px solid rgba(251, 191, 36, 0.25); }
      .pc-actions { display: flex; flex-wrap: wrap; gap: 6px; padding: 0 14px 12px; }
      .pc-actions a, .pc-actions button {
        flex: 1 1 auto;
        display: inline-flex; align-items: center; justify-content: center; gap: 6px;
        height: 32px; padding: 0 10px;
        border-radius: 8px; border: 1px solid rgba(148, 163, 184, 0.22);
        background: rgba(255, 255, 255, 0.03); color: #e2e8f0;
        font: inherit; font-size: 10.5px; font-weight: 700; letter-spacing: .5px; text-decoration: none; cursor: pointer;
      }
      .pc-actions a:hover, .pc-actions button:hover { border-color: #22d3ee; color: #22d3ee; }
      .pc-actions .primary { background: linear-gradient(135deg, #22d3ee, #0ea5e9); color: #04121a; border-color: transparent; }
      .pc-actions .primary:hover { color: #04121a; filter: brightness(1.08); }
      .pc-form { display: grid; gap: 7px; padding: 0 14px 12px; }
      .pc-form input, .pc-form textarea {
        height: 34px; padding: 0 10px; border-radius: 7px; font-size: 11px;
        border: 1px solid rgba(148, 163, 184, 0.22); background: rgba(0, 0, 0, 0.3); color: #e2e8f0; font-family: inherit;
      }
      .pc-form textarea { height: 60px; padding: 8px 10px; resize: vertical; }
      .pc-foot { padding: 9px 14px; border-top: 1px solid rgba(148, 163, 184, 0.12); font-size: 9px; letter-spacing: .5px; color: #64748b; }
      .pc-skel { height: 10px; margin: 8px 14px; border-radius: 4px;
        background: linear-gradient(90deg, rgba(148,163,184,.08), rgba(148,163,184,.22), rgba(148,163,184,.08)) 0 0 / 200% 100%;
        animation: pcShimmer 1.2s linear infinite; }
      @keyframes pcShimmer { to { background-position: -200% 0; } }

      .dashboard { gap: 16px; padding: 16px; grid-template-rows: 64px 1fr; }
      .main-area { gap: 16px; grid-template-columns: 1fr 380px; }
      .top-view { gap: 16px; }

      /* Panels */
      .glass-panel {
        background: var(--ui-surface);
        border: 1px solid var(--ui-line);
        border-radius: var(--ui-radius);
        box-shadow: 0 1px 0 rgba(255, 255, 255, 0.03) inset, 0 18px 40px rgba(0, 0, 0, 0.35);
      }
      .glass-panel::before { opacity: 0.35; }

      .panel-title {
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 1.8px;
        color: var(--ui-strong);
        padding-bottom: 12px;
        margin-bottom: 14px;
        border-bottom: 1px solid var(--ui-line);
      }
      .panel-title::before {
        width: 6px; height: 6px;
        border-radius: 2px;
        background: var(--neon-green);
        box-shadow: 0 0 8px var(--neon-glow);
      }
      .feed-panel .panel-title { border-bottom: 0; padding-bottom: 0; }

      /* Header */
      .header { padding: 0 18px; border-radius: var(--ui-radius); }
      .header-title { font-size: 20px; letter-spacing: 3px; gap: 12px; text-shadow: none; }
      .header-title svg {
        padding: 7px;
        width: 36px; height: 36px;
        border-radius: 10px;
        background: rgba(16, 185, 129, 0.12);
        border: 1px solid rgba(16, 185, 129, 0.35);
      }
      .brand-sub {
        display: block;
        margin-top: 2px;
        font-size: 9px;
        font-weight: 500;
        letter-spacing: 2px;
        color: var(--ui-muted);
      }
      .header-actions {
        display: flex;
        align-items: center;
        gap: 4px;
        padding: 4px;
        border: 1px solid var(--ui-line);
        border-radius: 12px;
        background: var(--ui-surface-2);
      }
      .header-actions .btn {
        height: 34px;
        min-width: 36px;
        padding: 0 10px !important;
        border: 1px solid transparent !important;
        border-radius: 8px;
        background: transparent !important;
        box-shadow: none;
      }
      .header-actions .btn:hover {
        background: rgba(148, 163, 184, 0.10) !important;
        border-color: var(--ui-line) !important;
        box-shadow: none;
      }
      .header-actions .sep { width: 1px; height: 20px; background: var(--ui-line); margin: 0 4px; }
      .status-indicator {
        height: 26px;
        padding: 0 12px;
        font-size: 10px;
        font-weight: 700;
        letter-spacing: 1.5px;
        border-color: rgba(16, 185, 129, 0.45);
      }

      /* Form controls */
      .control-group { gap: 8px; margin-bottom: 20px; }
      .control-group label {
        font-size: 10px;
        font-weight: 700;
        letter-spacing: 1.5px;
        color: var(--ui-muted);
      }
      input, select {
        height: 44px;
        padding: 0 14px;
        border-radius: var(--ui-radius-sm);
        border-color: var(--ui-line);
        font-size: 13px;
      }
      input[type="checkbox"] { height: auto; }
      select {
        appearance: none;
        -webkit-appearance: none;
        padding-right: 36px;
        background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%2364748b' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E");
        background-repeat: no-repeat;
        background-position: right 14px center;
        cursor: pointer;
      }
      input:focus, select:focus {
        border-color: var(--neon-green);
        box-shadow: 0 0 0 3px rgba(16, 185, 129, 0.15);
      }
      .input-ai:focus { box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.18); }
      input::placeholder { color: var(--ui-muted); opacity: 0.8; }

      /* Buttons */
      .btn { border-radius: var(--ui-radius-sm); font-size: 12px; letter-spacing: 1.2px; }
      .btn:hover { box-shadow: 0 0 0 3px rgba(16, 185, 129, 0.12); }
      .btn:focus-visible, .detect-clear:focus-visible, .news-tabs button:focus-visible {
        outline: 2px solid var(--neon-green);
        outline-offset: 2px;
      }
      .row-actions .btn { height: 44px; }
      #runBtn {
        background: linear-gradient(135deg, #34d399, var(--neon-green) 55%, #059669);
        border-color: transparent;
        color: #04110c;
      }
      #runBtn:hover { filter: brightness(1.08); color: #04110c; box-shadow: 0 8px 22px rgba(16, 185, 129, 0.35); }
      #aiBtn {
        background: linear-gradient(135deg, #60a5fa, #3b82f6 55%, #2563eb);
        border-color: transparent;
        color: #fff;
      }
      #aiBtn:hover { filter: brightness(1.08); color: #fff; box-shadow: 0 8px 22px rgba(59, 130, 246, 0.35); }

      /* Status line under the terminal input */
      #status {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 8px 10px;
        margin-top: 8px !important;
        border: 1px solid var(--ui-line);
        border-radius: 8px;
        background: var(--ui-surface-2);
        font-size: 10px !important;
        letter-spacing: 0.5px;
        line-height: 1.4;
      }
      #status::before {
        content: "";
        flex: 0 0 6px; height: 6px;
        border-radius: 50%;
        background: var(--neon-green);
        box-shadow: 0 0 6px var(--neon-green);
      }

      /* Output panes (logs / AI output) */
      pre {
        border-color: var(--ui-line);
        border-radius: 10px;
        font-size: 12px;
        line-height: 1.75;
        scrollbar-width: thin;
      }
      #logs { color: #94a3b8; }
      /* System Logs: ACTIVITY (top) + BACKEND (bottom) */
      .log-panel { display: flex; flex-direction: column; min-height: 0; }
      .log-split { flex: 1; min-height: 0; display: grid; grid-template-rows: 1fr 1fr; gap: 10px; }
      .log-part { min-height: 0; display: flex; flex-direction: column; gap: 6px; }
      .log-part pre, .be-logs { flex: 1; min-height: 90px; margin: 0; }
      .log-head { display: flex; align-items: center; gap: 8px; font-size: 10px; letter-spacing: 1.4px; color: var(--text-secondary); }
      .log-head b { font-weight: 700; color: #cbd5e1; }
      .log-sub { letter-spacing: .3px; color: #64748b; font-size: 10px; }
      .log-dot { width: 7px; height: 7px; border-radius: 50%; flex: none; }
      .log-dot.act { background: #34d399; box-shadow: 0 0 8px rgba(52, 211, 153, .6); }
      .log-dot.be { background: #60a5fa; box-shadow: 0 0 8px rgba(96, 165, 250, .6); }
      .log-btn { margin-left: auto; height: 22px; padding: 0 9px; border-radius: 6px; cursor: pointer; font: inherit; font-size: 10px;
        letter-spacing: .6px; color: #94a3b8; background: transparent; border: 1px solid var(--ui-line, rgba(148,163,184,.2)); }
      .log-btn:hover { color: #e2e8f0; border-color: #64748b; }
      .log-only + .log-btn { margin-left: 0; }
      .log-only { margin-left: auto; display: inline-flex; align-items: center; gap: 5px; cursor: pointer; letter-spacing: .3px; font-size: 10px; }
      .log-only input { width: 12px !important; height: 12px !important; margin: 0; accent-color: #60a5fa; }
      .log-badge { padding: 1px 7px; border-radius: 99px; font-size: 9.5px; font-weight: 700; letter-spacing: .5px; }
      .log-badge.warn { color: #fbbf24; background: rgba(251, 191, 36, .12); border: 1px solid rgba(251, 191, 36, .35); }
      .log-badge.err { color: #f87171; background: rgba(248, 113, 113, .12); border: 1px solid rgba(248, 113, 113, .35); }
      .log-badge[hidden] { display: none; }
      .be-logs { overflow: auto; padding: 10px 12px; border-radius: 10px; background: var(--pre-bg); border: 1px solid var(--ui-line, var(--input-border));
        font: 11.5px/1.7 "JetBrains Mono", ui-monospace, monospace; color: #94a3b8; scrollbar-width: thin; }
      .be-row { display: grid; grid-template-columns: 52px 32px 1fr; gap: 6px; align-items: baseline; white-space: nowrap; }
      .be-row .t { color: #475569; }
      .be-row .lv { font-size: 9.5px; font-weight: 700; letter-spacing: .5px; color: #60a5fa; }
      .be-row .m { overflow: hidden; text-overflow: ellipsis; color: #94a3b8; }
      .be-row .m .ok { color: #34d399; } .be-row .m .w { color: #fbbf24; } .be-row .m .e { color: #f87171; }
      .be-row.warning .lv { color: #fbbf24; } .be-row.warning .m { color: #fde68a; }
      .be-row.error .lv { color: #f87171; } .be-row.error .m { color: #fecaca; white-space: normal; }
      .be-row .src { color: #64748b; }
      .be-empty { color: #475569; }
      :root[data-theme="light"] .be-row .m { color: #334155; }
      :root[data-theme="light"] .log-head b { color: #1e293b; }
      :root[data-theme="light"] #logs { color: #334155; }

      /* News list */
      .news-item a { border-radius: 8px; border-color: var(--ui-line); padding: 8px 10px; }
      .news-title { color: var(--ui-strong); }
      .news-tabs { border-color: var(--ui-line); border-radius: 7px; }

      /* Camera header controls */
      #camOffBtn, #maxBtn { height: 30px; }

      @media (max-width: 1280px) {
        .main-area { grid-template-columns: 1fr 320px; }
      }
    </style>
  </head>
  <body>
    <div class="scanline-overlay"></div>

    <!-- Welcome message shown to every user on load -->
    <div class="welcome-toast" id="welcomeToast"></div>

    <!-- Notebook window: notes auto-save as .txt files in the notes/ folder. -->
    <section class="tool-win note-win" id="noteWin" hidden aria-label="Notebook">
      <header class="tw-head">
        <span class="tw-title">📒 Notebook</span>
        <span class="tw-status" id="noteStatus"></span>
        <button type="button" class="tw-x" data-close title="Close (Esc)" aria-label="Close notebook">×</button>
      </header>
      <div class="note-body">
        <aside class="note-side">
          <button type="button" class="note-new" id="noteNew">+ New note</button>
          <ul class="note-list" id="noteList"></ul>
        </aside>
        <div class="note-main">
          <label class="note-label" for="noteName">Note ka naam</label>
          <input type="text" id="noteName" class="note-name" maxlength="80" aria-label="Note name" spellcheck="false">
          <div class="note-tools">
            <button type="button" class="note-mic" id="noteMic" title="Click karke boliye: jo bolenge woh note mein likh jayega" aria-pressed="false"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0M12 17v4M8 21h8"/></svg><span id="noteMicLabel">Bolkar likho</span></button>
            <button type="button" class="note-send" id="noteSend" title="Ye note abhi WhatsApp pe bhejo"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 2 11 13M22 2l-7 20-4-9-9-4z"/></svg><span>Send</span></button>
            <button type="button" class="note-del" id="noteDel" title="Ye note delete karo (Undo milega)"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6M10 11v6M14 11v6"/></svg><span>Delete</span></button>
          </div>
          <label class="note-label" for="noteText">Aaj ka kaam</label>
          <div class="note-guide" id="noteGuide">
            <div class="ng-title">Note kaise likhein?</div>
            <ol class="ng-steps">
              <li><b>Type karein:</b> neeche wale box mein likhiye. Apne aap save ho jata hai.</li>
              <li><b>Bolkar likhein:</b> <em>Bolkar likho</em> dabaiye aur boliye. Jo bolenge, woh likh jayega. Rokne ke liye dobara dabaiye.</li>
              <li><b>Bhejein:</b> <em>Send</em> dabaiye, note WhatsApp pe chala jayega. Roz raat 11:59:58 PM ko bhi apne aap jata hai.</li>
            </ol>
            <div class="ng-say">Sara se bhi bol sakte hain: <q>notebook me likho: client call ho gaya</q> · <q>notes whatsapp pe bhejo</q></div>
            <button type="button" class="ng-start" id="noteGuideMic"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0M12 17v4M8 21h8"/></svg> Bolna shuru karein</button>
          </div>
          <textarea id="noteText" class="note-text" placeholder="Yahan apna aaj ka kaam likhiye... (ya upar 'Bolkar likho' dabakar boliye)" aria-label="Note text"></textarea>
          <div class="note-foot" id="noteFoot"></div>
          <div class="note-sched" id="noteSched" hidden>
            <span id="noteSchedText"></span>
            <button type="button" id="noteSchedOff" title="Stop the daily WhatsApp send">Turn off</button>
          </div>
        </div>
      </div>
    </section>

    <!-- Phone input window: how to connect the phone, QR code, live status. -->
    <section class="tool-win phone-win" id="phoneWin" hidden aria-label="Phone input">
      <header class="tw-head">
        <span class="tw-title">📱 Phone se input</span>
        <span class="tw-status" id="phoneStatus"></span>
        <button type="button" class="tw-x" data-close title="Close (Esc)" aria-label="Close">×</button>
      </header>
      <div class="ph-body">
        <div class="ph-conn" id="phConn"><i></i><span id="phConnText">Phone connected nahi hai</span></div>
        <p class="ph-intro">Phone se bolkar ya likhkar Sara ko command dijiye. Sara laptop par kaam karegi aur jawab bolegi. Koi app install nahi karni, phone ka browser kaafi hai.</p>
        <div class="ph-tabs" role="tablist">
          <button type="button" class="on" data-tab="wifi">Wi-Fi / Hotspot</button>
          <button type="button" data-tab="usb">USB cable (bina Wi-Fi)</button>
        </div>

        <div class="ph-pane" data-pane="wifi">
          <ol class="ph-steps">
            <li>Phone aur laptop <b>ek hi Wi-Fi</b> par hon. Wi-Fi nahi hai? Phone ka <b>Hotspot</b> on karke laptop ko us se jodiye.</li>
            <li id="phLanStep">Sara ko LAN mode mein chalaiye: <code>my-python-project serve --lan</code></li>
            <li>Phone ke camera se ye <b>QR scan</b> kijiye, ya link kholiye:</li>
          </ol>
          <div class="ph-qr-row">
            <div class="ph-qr" id="phQr"></div>
            <div class="ph-link"><code id="phUrl">--</code><button type="button" id="phCopy">Copy link</button>
              <small>Is tarike mein bada mic button nahi chalta (browser security). Phone keyboard ka 🎤 voice typing use kijiye.</small></div>
          </div>
        </div>

        <div class="ph-pane" data-pane="usb" hidden>
          <ol class="ph-steps">
            <li>Phone: <b>Settings → About phone</b> → <b>Build number</b> par 7 baar tap → <b>Developer options → USB debugging ON</b>.</li>
            <li>Laptop: <a href="https://developer.android.com/tools/releases/platform-tools" target="_blank" rel="noopener">Platform-Tools</a> download karke unzip kijiye.</li>
            <li>USB cable lagaiye, phone par "Allow USB debugging" OK kijiye. Phir laptop par chalaiye:
              <pre id="phAdb">adb reverse tcp:1309 tcp:1309</pre></li>
            <li>Phone ke Chrome mein kholiye: <code>http://localhost:1309/remote</code>. Is tarike mein <b>bada mic button bhi chalta hai</b>.</li>
          </ol>
        </div>
        <div class="ph-last" id="phLast" hidden></div>
      </div>
    </section>

    <!-- FM Radio window: now playing + search + station list. -->
    <section class="tool-win radio-win" id="radioWin" hidden aria-label="FM Radio">
      <header class="tw-head">
        <span class="tw-title">📻 FM Radio</span>
        <span class="tw-status" id="radioStatus"></span>
        <button type="button" class="tw-x" data-close title="Close (radio keeps playing)" aria-label="Close radio window">×</button>
      </header>
      <div class="rd-now" id="radioNow">
        <div class="rd-logo" id="radioLogo">📻</div>
        <div class="rd-info">
          <small id="radioState">CHUNIYE</small>
          <b id="radioName">Koi station chuniye</b>
          <span id="radioMeta">Live Indian FM aur online radio</span>
        </div>
        <div class="rd-eq" id="radioEq" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></div>
        <button type="button" class="rd-play" id="radioPlay" aria-label="Play / stop">▶</button>
      </div>
      <div class="rd-vol">
        <span>🔈</span><input type="range" id="radioVol" min="0" max="100" value="80" aria-label="Volume"><span id="radioVolVal">80%</span>
      </div>
      <form class="rd-search" id="radioSearch" autocomplete="off">
        <input type="text" id="radioQuery" placeholder="Station dhoondhiye: Mirchi, Red FM, Ghazal..." aria-label="Search stations">
        <button type="submit">Search</button>
      </form>
      <div class="rd-quick" id="radioQuick"></div>
      <ol class="rd-list" id="radioList"><li class="rd-empty">Stations load ho rahe hain...</li></ol>
    </section>

    <!-- Skills window: everything Sara can do, with clickable example commands. -->
    <section class="tool-win skills-win" id="skillsWin" hidden aria-label="Sara skills">
      <header class="tw-head">
        <span class="tw-title"><span class="sk-bolt" aria-hidden="true">⚡</span>
          <span class="sk-title-txt"><b>SARA SKILLS</b><small id="skillsCount">Sara kya kya kar sakti hai</small></span></span>
        <span class="tw-status" id="skillsStatus"></span>
        <button type="button" class="tw-x" data-close title="Band karein" aria-label="Close skills">×</button>
      </header>
      <div class="sk-body">
        <div class="sk-tools">
          <input type="search" class="sk-search" id="skillsSearch" placeholder="Dhoondhein: mausam, sleep, quran, whatsapp…" aria-label="Search skills">
          <div class="sk-cats" id="skillsCats" role="tablist" aria-label="Categories"></div>
        </div>
        <div class="sk-grid" id="skillsGrid"></div>
        <div class="sk-foot">
          <span><i class="sk-k run">▶</i> click karte hi chalega</span>
          <span><i class="sk-k chk">↵</i> command box mein aayega, dekh kar Enter dabaiye</span>
          <span><i class="sk-k edt">✎</i> number/naam badal kar chalaiye</span>
          <span class="sk-tip">Bolkar bhi: "tum kya kya kar sakti ho"</span>
        </div>
      </div>
    </section>

    <!-- Quran window: audio player (mp3quran.net) + all 114 surahs. -->
    <section class="tool-win quran-win" id="quranWin" hidden aria-label="Quran">
      <header class="tw-head">
        <span class="tw-title"><span class="qr-emblem"><svg viewBox="0 0 100 100" aria-hidden="true"><polygon points="50,4 63.5,17.5 82.5,17.5 82.5,36.5 96,50 82.5,63.5 82.5,82.5 63.5,82.5 50,96 36.5,82.5 17.5,82.5 17.5,63.5 4,50 17.5,36.5 17.5,17.5 36.5,17.5"/><circle cx="50" cy="50" r="27"/></svg><i>۞</i></span>
          <span class="qr-title-txt"><b>AL-QUR'AN AL-KAREEM</b><small>القرآن الكريم</small></span></span>
        <span class="tw-status" id="quranStatus"></span>
        <button type="button" class="tw-btn" id="quranMin" title="Chhota karein: tilawat background mein chalti rahegi" aria-label="Minimize">▁</button>
        <button type="button" class="tw-btn" id="quranFull" title="Full screen" aria-label="Full screen">⛶</button>
        <button type="button" class="tw-x" data-close title="Band karein (tilawat ruk jayegi)" aria-label="Close Quran">×</button>
      </header>
      <div class="qr-body">
        <div class="qr-stage" id="quranStage">
          <div class="qr-hero" id="quranHero">
            <svg class="qr-watermark" viewBox="0 0 100 100" aria-hidden="true"><polygon points="50,4 63.5,17.5 82.5,17.5 82.5,36.5 96,50 82.5,63.5 82.5,82.5 63.5,82.5 50,96 36.5,82.5 17.5,82.5 17.5,63.5 4,50 17.5,36.5 17.5,17.5 36.5,17.5"/><circle cx="50" cy="50" r="27"/><circle cx="50" cy="50" r="20"/></svg>
            <div class="qr-medal" aria-hidden="true">
              <svg viewBox="0 0 100 100" aria-hidden="true"><polygon points="50,4 63.5,17.5 82.5,17.5 82.5,36.5 96,50 82.5,63.5 82.5,82.5 63.5,82.5 50,96 36.5,82.5 17.5,82.5 17.5,63.5 4,50 17.5,36.5 17.5,17.5 36.5,17.5"/><circle cx="50" cy="50" r="27"/></svg>
              <span id="quranNum">۞</span>
            </div>
            <div class="qr-arabic long" id="quranArabic">بِسْمِ ٱللَّٰهِ ٱلرَّحْمَٰنِ ٱلرَّحِيمِ</div>
            <div class="qr-name" id="quranName">Tilawat ke liye koi surah chuniye</div>
            <div class="qr-meta" id="quranMeta">114 surahs · Sheikh Yasser Al-Dosari</div>
            <div class="qr-eq" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div>
            <button type="button" class="qr-resume" id="quranResume" hidden></button>
            <div class="qr-msg" id="quranMsg" role="alert" hidden></div>
          </div>
          <div class="qr-seek">
            <span id="quranCur">0:00</span>
            <input type="range" class="qr-range" id="quranSeek" min="0" max="1000" value="0" step="1" aria-label="Recitation position" disabled>
            <span id="quranDur">0:00</span>
          </div>
          <div class="qr-bar">
            <select class="qr-reciter" id="quranReciter" aria-label="Qari (reciter)" title="Qari chuniye"></select>
            <div class="qr-ctrls">
              <button type="button" id="quranPrev" title="Pichli surah" aria-label="Previous surah"><svg viewBox="0 0 24 24"><path d="M6 5h2v14H6zM20 5v14L9 12z"/></svg></button>
              <button type="button" class="qr-play" id="quranPlay" title="Chalayein / rokein (Space)" aria-label="Play"></button>
              <button type="button" id="quranNext" title="Agli surah" aria-label="Next surah"><svg viewBox="0 0 24 24"><path d="M16 5h2v14h-2zM4 5v14l11-7z"/></svg></button>
            </div>
            <div class="qr-right">
              <button type="button" class="qr-tog" id="quranRepeat" aria-pressed="false" title="Yahi surah dobara (repeat)" aria-label="Repeat surah"><svg viewBox="0 0 24 24"><path d="M7 7h10v3l4-4-4-4v3H5v6h2zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2z"/></svg></button>
              <button type="button" class="qr-tog" id="quranAuto" aria-pressed="true" title="Surah khatam hone par agli apne aap" aria-label="Auto next"><svg viewBox="0 0 24 24"><path d="M4 18l8.5-6L4 6zm9-12v12l8.5-6z"/></svg></button>
              <span class="qr-vol-wrap"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9v6h4l5 4V5L8 9zm12.5 3a4.5 4.5 0 0 0-2.5-4v8a4.5 4.5 0 0 0 2.5-4z"/></svg>
              <input type="range" class="qr-range qr-vol" id="quranVol" min="0" max="100" value="100" aria-label="Volume" title="Awaaz"></span>
            </div>
          </div>
        </div>
        <aside class="qr-side">
          <input type="search" class="qr-search" id="quranSearch" placeholder="Surah dhoondhein (naam ya number)" aria-label="Search surah">
          <div class="qr-tabs" role="tablist">
            <button type="button" role="tab" data-tab="fav" class="on" aria-selected="true">★ PASANDIDA</button>
            <button type="button" role="tab" data-tab="all" aria-selected="false">SAB 114 SURAH</button>
          </div>
          <ol class="qr-list" id="quranList"></ol>
          <div class="qr-foot">Audio: mp3quran.net · Hafs 'an 'Asim</div>
        </aside>
      </div>
    </section>

    <!-- Quran mini player: shown while the Quran window is minimised. -->
    <div class="qr-mini" id="quranMini" hidden role="region" aria-label="Quran mini player">
      <span class="qr-emblem"><svg viewBox="0 0 100 100" aria-hidden="true"><polygon points="50,4 63.5,17.5 82.5,17.5 82.5,36.5 96,50 82.5,63.5 82.5,82.5 63.5,82.5 50,96 36.5,82.5 17.5,82.5 17.5,63.5 4,50 17.5,36.5 17.5,17.5 36.5,17.5"/><circle cx="50" cy="50" r="27"/></svg><i>۞</i></span>
      <span class="qr-mini-text"><small id="quranMiniState">TILAWAT</small><b id="quranMiniTitle">Quran</b></span>
      <button type="button" id="quranMiniPlay" title="Chalayein / rokein" aria-label="Play or pause"></button>
      <button type="button" id="quranMiniStop" title="Band karein" aria-label="Stop"><svg viewBox="0 0 24 24"><path d="M6 6h12v12H6z"/></svg></button>
      <button type="button" id="quranMiniOpen" title="Wapas kholein" aria-label="Open Quran window"><svg viewBox="0 0 24 24"><path d="M14 3h7v7h-2V6.4l-6.3 6.3-1.4-1.4L17.6 5H14zM5 5h6v2H7v10h10v-4h2v6H5z"/></svg></button>
      <span class="qr-mini-bar" id="quranMiniBar"></span>
    </div>

    <!-- Calculator window. -->
    <section class="tool-win calc-win" id="calcWin" hidden aria-label="Calculator">
      <header class="tw-head">
        <span class="tw-title">🧮 Calculator</span>
        <button type="button" class="tw-x" data-close title="Close (Esc)" aria-label="Close calculator">×</button>
      </header>
      <div class="calc-screen">
        <div class="calc-expr" id="calcExpr"></div>
        <div class="calc-out" id="calcOut">0</div>
      </div>
      <div class="calc-keys" id="calcKeys">
        <button data-k="C" class="fn">C</button><button data-k="(" class="fn">(</button><button data-k=")" class="fn">)</button><button data-k="/" class="op">÷</button>
        <button data-k="7">7</button><button data-k="8">8</button><button data-k="9">9</button><button data-k="*" class="op">×</button>
        <button data-k="4">4</button><button data-k="5">5</button><button data-k="6">6</button><button data-k="-" class="op">−</button>
        <button data-k="1">1</button><button data-k="2">2</button><button data-k="3">3</button><button data-k="+" class="op">+</button>
        <button data-k="%" class="fn">%</button><button data-k="0">0</button><button data-k=".">.</button><button data-k="=" class="eq">=</button>
        <button data-k="Backspace" class="fn wide">⌫ Back</button>
      </div>
      <div class="calc-hist" id="calcHist"></div>
    </section>

    <!-- Voice listening overlay with animated sound wave -->
    <div class="voice-overlay" id="voiceOverlay">
      <div class="voice-card" role="dialog" aria-modal="true" aria-labelledby="voiceLabel">
        <div class="voice-mic">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0"/><path d="M12 17v4"/><path d="M8 21h8"/></svg>
        </div>
        <div class="voice-wave" aria-hidden="true">
          <span></span><span></span><span></span><span></span><span></span>
          <span></span><span></span><span></span><span></span><span></span><span></span>
        </div>
        <div class="voice-label" id="voiceLabel">Listening<span class="voice-dots">...</span></div>
        <div class="voice-hint">Go ahead, I'm listening. Speak your command clearly.</div>
        <div class="voice-lang" id="voiceLang">Mic live</div>
        <div class="voice-timer"><i></i></div>
        <button class="btn" id="voiceStopBtn" type="button">
          <span class="stop-sq"></span> Stop listening
        </button>
        <div class="voice-foot">Press <kbd>Esc</kbd> to cancel</div>
      </div>
    </div>

    <!-- Onboarding overlay: ask the user's name + gender on first visit -->
    <div class="onboard-overlay" id="onboardOverlay">
      <div class="onboard-card" role="dialog" aria-modal="true" aria-labelledby="onboardTitle">
        <div class="onboard-head">
          <div class="onboard-logo">S</div>
          <div class="onboard-kicker">SARA_OS · User setup</div>
          <div class="onboard-title" id="onboardTitle">Welcome to <span>SARA</span></div>
          <p class="onboard-sub">Please tell me your name so I can address you personally.<br>
            <span style="opacity:0.75;">Kripya apna naam batayein — main aapko isi naam se sambodhit karungi.</span></p>
        </div>

        <div class="onboard-field">
          <label class="onboard-label" for="onboardName">Your name</label>
          <div class="onboard-input-wrap">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/></svg>
            <input type="text" id="onboardName" placeholder="Enter your name" autocomplete="off" maxlength="40">
          </div>
        </div>

        <div class="onboard-field">
          <div class="onboard-label">You are <em>sets Sara's voice</em></div>
          <div class="onboard-gender" role="radiogroup">
            <button class="btn" id="genderMale" type="button" role="radio">
              <span class="g-name"><b>♂</b>Male</span>
              <span class="g-hint">Sara replies in a female voice</span>
            </button>
            <button class="btn" id="genderFemale" type="button" role="radio">
              <span class="g-name"><b>♀</b>Female</span>
              <span class="g-hint">Sara replies in a male voice</span>
            </button>
          </div>
        </div>

        <div class="onboard-err" id="onboardErr" role="alert"></div>
        <button class="btn is-disabled" id="onboardStart" type="button">
          Initialize session <span class="arrow">→</span>
        </button>
        <div class="onboard-foot">Press <kbd>Enter</kbd> to continue · Saved on this device only</div>
      </div>
    </div>

    <div class="dashboard">
      <!-- Header -->
      <div class="glass-panel header" id="headerPanel">
        <div class="header-title" id="brandTitle" style="cursor: pointer;" title="Click to change user / voice">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="stroke: var(--neon-green);"><polygon points="12 2 2 7 12 12 22 7 12 2"></polygon><polyline points="2 17 12 22 22 17"></polyline><polyline points="2 12 12 17 22 12"></polyline></svg>
          <span>SARA_OS<small class="brand-sub">AI VOICE ASSISTANT</small></span>
        </div>
        <div style="display: flex; gap: 12px; align-items: center;">
          <div class="header-actions" role="toolbar" aria-label="Quick settings">
          <button id="autoMicToggle" class="btn" style="padding: 6px 10px; font-size: 12px; font-weight: 700; letter-spacing: 1px; background: transparent; cursor: pointer;" title="Auto mic: listen again after every answer">
            🎙️ AUTO OFF
          </button>
          <button id="voiceToggle" class="btn" style="padding: 6px 12px; font-size: 16px; background: transparent; border: none; cursor: pointer;" title="Voice output on/off">
            🔊
          </button>
          <button id="btBtn" class="btn" style="padding: 6px 10px; background: transparent; border: none; cursor: pointer; color: #60a5fa;" title="Bluetooth devices" aria-label="Bluetooth devices">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="6.5 6.5 17.5 17.5 12 23 12 1 17.5 6.5 6.5 17.5"></polyline></svg>
          </button>
          <button id="acBtn" class="btn" type="button" style="padding: 6px 10px; background: transparent; border: none; cursor: pointer; color: #22d3ee;" aria-label="AC control"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2v20M4.9 6l14.2 12M19.1 6 4.9 18M9 3.5l3 2.5 3-2.5M9 20.5l3-2.5 3 2.5"/></svg></button>
          <button id="radioBtn" class="btn" type="button" style="padding: 6px 10px; background: transparent; border: none; cursor: pointer; color: #f472b6;" aria-label="FM Radio"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="8" width="20" height="13" rx="2.5"/><path d="M7 8 17 3"/><circle cx="8" cy="14.5" r="3"/><path d="M15 12h4M15 15h4M15 18h2"/></svg></button>
          <button id="skillsBtn" class="btn skills-btn" type="button" title="Sara kya kya kar sakti hai" aria-label="Skills">⚡ <span>Skills</span></button>
          <button id="quranBtn" class="btn" type="button" style="padding: 6px 10px; background: transparent; border: none; cursor: pointer; color: #fbbf24;" title="Quran · Tilawat" aria-label="Quran"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 5c3-1.5 6.5-1.5 10 1 3.5-2.5 7-2.5 10-1v14c-3-1.5-6.5-1.5-10 1-3.5-2.5-7-2.5-10-1z"/><path d="M12 6v14"/></svg></button>
          <button id="phoneBtn" class="btn" type="button" style="padding: 6px 10px; background: transparent; border: none; cursor: pointer; color: #34d399;" title="Phone se input do" aria-label="Phone input"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="6" y="2" width="12" height="20" rx="2.5"/><path d="M11 18h2"/></svg></button>
          <button id="noteBtn" class="btn" type="button" style="padding: 6px 10px; font-size: 15px; background: transparent; border: none; cursor: pointer;" title="Notebook" aria-label="Notebook">📒</button>
          <button id="calcBtn" class="btn" type="button" style="padding: 6px 10px; font-size: 15px; background: transparent; border: none; cursor: pointer;" title="Calculator" aria-label="Calculator">🧮</button>
          <button id="themeToggle" class="btn" style="padding: 6px 12px; font-size: 16px; background: transparent; border: none; cursor: pointer;" title="Toggle Theme">
            🌓
          </button>
          </div>
          <div class="status-indicator">
            <span class="dot" id="connDot"></span>
            <span id="connStatus">ONLINE</span>
          </div>
        </div>
      </div>

      <!-- Left Sidebar: Controls -->
      <div class="glass-panel controls-area" id="controlsPanel">
        <h4 class="panel-title">Command Interface</h4>
        
        <div class="control-group">
          <label>Voice Recognition Model</label>
          <select id="langSelect">
            <option value="en-IN">English (India)</option>
            <option value="hi-IN">Hindi (हिंदी)</option>
            <option value="ur-IN">Urdu (اردو)</option>
          </select>
        </div>

        <div class="control-group">
          <label for="replyLangSelect">Sara ki bhasha (jawab)</label>
          <select id="replyLangSelect">
            <option value="hi">हिंदी (Hindi)</option>
            <option value="en">English</option>
            <option value="auto">Auto (jaisa aap bolein)</option>
          </select>
        </div>

        <div class="control-group">
          <label>Video Input Source</label>
          <select id="cameraSelect">
            <option value="">Default Camera</option>
          </select>
        </div>

        <div class="control-group">
          <label>Terminal Input</label>
          <input type="text" id="cmdInput" placeholder="Type a command, e.g. weather today" autocomplete="off">
          <div class="row-actions">
            <button class="btn" id="voiceBtn" title="Voice Search">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"></path><path d="M19 10v2a7 7 0 0 1-14 0v-2"></path><line x1="12" y1="19" x2="12" y2="23"></line><line x1="8" y1="23" x2="16" y2="23"></line></svg>
            </button>
            <button class="btn" id="runBtn" style="flex: 3;">Execute</button>
          </div>
          <div id="status" style="font-size: 11px; color: var(--text-secondary); margin-top: 5px;">SYS_READY: Awaiting input...</div>
        </div>

        <!-- Today's headlines (Google News, India) in English or Hindi. -->
        <div class="control-group news-group">
          <div class="news-head">
            <label><i class="news-live"></i>Today's News</label>
            <div class="news-tabs" role="tablist" aria-label="News language">
              <button type="button" role="tab" data-lang="en">EN</button>
              <button type="button" role="tab" data-lang="hi">हिंदी</button>
            </div>
          </div>
          <ul id="newsList" class="news-list" aria-live="polite"></ul>
          <div class="news-foot">
            <span id="newsUpdated">Loading...</span>
            <button type="button" id="newsRefresh" class="news-refresh" title="Refresh">↻</button>
          </div>
        </div>

        <div class="control-group" style="margin-top: auto; margin-bottom: 0;">
          <label style="color: #60a5fa;">Ask AI</label>
          <input type="text" id="aiInput" class="input-ai" placeholder="Ask anything..." autocomplete="off">
          <div class="row-actions">
            <button class="btn btn-ai" id="aiBtn" style="width: 100%;">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2a10 10 0 1 0 10 10H12V2z"></path><path d="M12 12 2.1 7.1"></path><path d="M12 12l9.9 4.9"></path></svg>
              Process AI Task
            </button>
          </div>
        </div>
      </div>

      <!-- Main Area: Monitors -->
      <div class="main-area" id="mainPanel">
        <div class="glass-panel feed-panel" id="feedPanel" style="padding: 15px;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
            <h4 class="panel-title" style="margin-bottom: 0;">Live Video Feed</h4>
            <div style="display: flex; align-items: center; gap: 12px;">
              <label style="display: flex; align-items: center; gap: 8px; font-size: 11px; color: var(--text-secondary); cursor: pointer; text-transform: uppercase;">
                <input type="checkbox" id="camToggle" style="width: 16px; height: 16px; margin: 0; padding: 0; accent-color: var(--neon-green); cursor: pointer;">
                Camera Power
              </label>
              <button id="camOffBtn" class="btn" type="button" style="padding: 5px 12px; font-size: 11px;">⏻ Camera Off</button>
              <button id="maxBtn" class="btn" type="button" style="padding: 5px 10px; font-size: 13px;" title="Full screen (Esc to exit)" aria-pressed="false">⛶</button>
            </div>
          </div>
          <div class="video-wrap">
            <div class="video-overlay"></div>
            <!-- AI visual shown while the camera is off (camera code hides it). -->
            <div id="noSignal" class="ai-visual" data-state="idle">
              <canvas id="aiCanvas" aria-hidden="true"></canvas>
              <div class="ai-visual-label">
                <span class="ai-visual-name">SARA_CORE</span>
                <span id="aiVisualState" class="ai-visual-state">IDLE</span>
              </div>
              <!-- Date, time, location + map and sound buttons (globe screen). -->
              <div class="globe-info" id="globeInfo">
                <div class="gi-row gi-when">
                  <span id="giDate">--</span><i class="gi-sep"></i><b id="giTime">--:--:--</b>
                </div>
                <div class="gi-row gi-where">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 22s7-6.2 7-12a7 7 0 0 0-14 0c0 5.8 7 12 7 12z"/><circle cx="12" cy="10" r="2.5"/></svg>
                  <span id="giPlace">Locating...</span>
                  <button type="button" class="gi-btn" id="giMapBtn" title="Show map" aria-label="Show map" aria-expanded="false">🗺️</button>
                  <button type="button" class="gi-btn" id="giSoundBtn" title="Sound effects on/off" aria-label="Sound effects">🔊</button>
                  <button type="button" class="gi-btn" id="giMusicBtn" title="Soft background music on/off" aria-label="Background music">🎵</button>
                </div>
                <div class="gi-row gi-bt" id="giBt" title="Bluetooth">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="6.5 6.5 17.5 17.5 12 23 12 1 17.5 6.5 6.5 17.5"/></svg>
                  <i class="gi-bt-dot"></i><span id="giBtText">Checking...</span>
                </div>
                <div class="gi-map" id="giMap" hidden>
                  <div class="gi-map-head">
                    <span>LOCATION MAP</span>
                    <a id="giMapOpen" target="_blank" rel="noopener" title="Open full map">↗</a>
                    <button type="button" id="giMapClose" title="Close map" aria-label="Close map">×</button>
                  </div>
                  <!-- The frame is taller than its window so OSM's own link bar
                       is cropped off; the required credit is in our footer. -->
                  <div class="gi-map-view"><iframe id="giMapFrame" title="Location map" referrerpolicy="no-referrer"></iframe></div>
                  <div class="gi-map-foot" id="giMapFoot">--</div>
                </div>
              </div>
            </div>
            <video id="videoBlur" autoplay playsinline muted aria-hidden="true" tabindex="-1"></video>
            <video id="video" autoplay playsinline muted style="display: none; position: relative; z-index: 0;"></video>
            <!-- Animated HUD frame over the feed (purely decorative). -->
            <div class="feed-hud" aria-hidden="true">
              <div class="hud-boot"></div>
              <div class="hud-scan"></div>
              <span class="hud-corner tl"></span><span class="hud-corner tr"></span>
              <span class="hud-corner bl"></span><span class="hud-corner br"></span>
              <div class="hud-reticle">
                <svg viewBox="0 0 200 200" fill="none" stroke="currentColor">
                  <circle class="ring-a" cx="100" cy="100" r="92" stroke-width="1.5" stroke-dasharray="40 18 6 18" opacity="0.8"/>
                  <circle class="ring-b" cx="100" cy="100" r="78" stroke-width="1" stroke-dasharray="3 7" opacity="0.6"/>
                  <g class="ticks" stroke-width="2" opacity="0.9">
                    <path d="M100 2v16M100 182v16M2 100h16M182 100h16"/>
                    <path d="M92 100h16M100 92v16" stroke-width="1"/>
                  </g>
                </svg>
              </div>
              <canvas id="faceCanvas" class="face-canvas"></canvas>
              <div class="hud-tag top-left">
                <span><i class="hud-live-dot"></i><b id="hudMode">STANDBY</b></span>
                <span id="hudClock">--:--:--</span>
                <span id="hudTrack"></span>
              </div>
              <div class="hud-tag top-right hud-geo">
                <span id="hudStamp"></span>
                <span>IP&nbsp; <b id="hudIp">--</b></span>
                <span>GEO <b id="hudGeo">LOCATING...</b></span>
                <span id="hudPlace"></span>
              </div>
              <div class="hud-tag bottom-right">
                <span id="hudFaces"></span>
                <span id="hudRes">NO_SIGNAL</span>
                <span>SIG<span class="hud-bars"><i></i><i></i><i></i><i></i></span></span>
              </div>
            </div>
            <!-- On-screen Bluetooth alert (connect / disconnect), camera + globe. -->
            <div class="bt-toast" id="btToast" role="status" aria-live="polite"></div>
            <!-- Info card beside the face circle, built from what the user types
                 (link / name / question). The face is never used for lookup. -->
            <div class="pcard-wrap" id="pcardWrap">
              <form class="pcard-search collapsed" id="pcardForm" autocomplete="off">
                <button type="button" class="pcard-toggle" id="pcardToggle" title="Look up a link, name or question" aria-label="Open search" aria-expanded="false">🔍</button>
                <input id="pcardInput" type="text" placeholder="Paste a profile link, a name or a question" aria-label="Look up a link, name or question">
                <button type="submit">Show</button>
              </form>
              <div class="pcard" id="pcard" hidden></div>
            </div>
            <!-- Stage: the news board shown over the feed ("news dikhao");
                 the camera shrinks to a corner. -->
            <div id="stage" class="stage" hidden>
              <div class="stage-bar">
                <span class="stage-kind" id="stageKind">WEB</span>
                <span class="stage-title" id="stageTitle"></span>
                <a id="stageOpen" class="stage-btn" target="_blank" rel="noopener" title="Open in a new tab">↗</a>
                <button id="stageClose" class="stage-btn" type="button" title="Close (back to camera)">✕</button>
              </div>
              <div class="stage-body" id="stageBody"></div>
            </div>
          </div>
        </div>

        <div class="top-view">
          <div class="glass-panel log-panel" style="padding: 15px;">
            <h4 class="panel-title">System Logs</h4>
            <div class="log-split">
              <section class="log-part" aria-label="Activity log">
                <div class="log-head">
                  <span class="log-dot act"></span><b>ACTIVITY</b>
                  <span class="log-sub">Sara ke kaam</span>
                  <button type="button" class="log-btn" id="actClear" title="Activity log saaf karein">Clear</button>
                </div>
                <pre id="logs"></pre>
              </section>
              <section class="log-part" aria-label="Backend log">
                <div class="log-head">
                  <span class="log-dot be"></span><b>BACKEND</b>
                  <span class="log-badge warn" id="beWarn" hidden></span>
                  <span class="log-badge err" id="beErr" hidden></span>
                  <label class="log-only" title="Sirf warnings aur errors dikhayein"><input type="checkbox" id="beOnlyIssues"> Sirf errors</label>
                  <button type="button" class="log-btn" id="beClear" title="Backend log saaf karein">Clear</button>
                </div>
                <div class="be-logs" id="beLogs" role="log" aria-live="off"></div>
              </section>
            </div>
          </div>
          <div class="glass-panel" style="padding: 15px;">
            <h4 class="panel-title" style="color: #60a5fa;">AI Output</h4>
            <div class="proc-bar" id="procBar"></div>
            <!-- Song/naat player ("love song chalao", "youtube par ... chalao"). -->
            <div id="mediaPlayer" class="media-player" hidden>
              <div class="media-bar">
                <span class="media-eq" aria-hidden="true"><i></i><i></i><i></i><i></i></span>
                <span class="media-title"><small>NOW_PLAYING</small><b id="mediaTitle"></b></span>
                <a id="mediaOpen" class="media-btn" target="_blank" rel="noopener" title="Open on YouTube">YT ↗</a>
                <button id="mediaClose" class="media-btn" type="button" title="Stop">✕</button>
              </div>
              <div class="media-frame" id="mediaFrame"></div>
            </div>
            <!-- Playlist picker, shown for general requests ("gaana chalao"). -->
            <div id="mediaMenu" class="media-player" hidden>
              <div class="media-bar">
                <span class="media-eq" aria-hidden="true"><i></i><i></i><i></i><i></i></span>
                <span class="media-title"><small id="mediaMenuLabel">SELECT_PLAYLIST</small><b>Tap one, or say its number</b></span>
                <button id="mediaMenuClose" class="media-btn" type="button" title="Close">✕</button>
              </div>
              <ol id="mediaList" class="media-list"></ol>
            </div>
            <pre id="aiBrain">Awaiting task sequence...</pre>
            <!-- Live face / person detections from the camera feed. -->
            <div class="detect-box" id="detectBox">
              <div class="detect-box-head">
                <span class="detect-box-title"><i class="detect-dot"></i>Live detections</span>
                <span class="detect-count" id="detectCount" title="Detections this session">0</span>
                <button class="detect-clear" id="detectClear" type="button">Clear</button>
              </div>
              <ol class="detect-list" id="detectToasts" aria-live="polite">
                <li class="detect-empty">No detections yet. Turn on the camera to start.</li>
              </ol>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Bluetooth screen: search, list, pair, connect ("bluetooth kholo"). -->
    <div id="btScreen" class="bt-screen" hidden>
      <div class="bt-card" role="dialog" aria-modal="true" aria-labelledby="btTitle">
        <div class="bt-head">
          <span class="bt-logo"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="6.5 6.5 17.5 17.5 12 23 12 1 17.5 6.5 6.5 17.5"></polyline></svg></span>
          <h3 id="btTitle">Bluetooth</h3>
          <label class="bt-switch" title="Bluetooth on/off">
            <input type="checkbox" id="btRadio" aria-label="Bluetooth on or off"><span></span>
          </label>
          <button id="btScan" class="btn bt-scan" type="button">⟳ Search</button>
          <button id="btClose" class="stage-btn" type="button" title="Close (Esc)">✕</button>
        </div>
        <div class="bt-progress" aria-hidden="true"><i id="btBar"></i></div>
        <p id="btStatus" class="bt-status" aria-live="polite"></p>
        <div id="btPrompt" class="bt-prompt" hidden></div>
        <div class="bt-body">
          <h4 class="bt-sec">Paired devices</h4>
          <ul id="btPaired" class="bt-list"></ul>
          <h4 class="bt-sec">Nearby devices <span id="btNearbyCount"></span></h4>
          <ul id="btNearby" class="bt-list"></ul>
        </div>
      </div>
    </div>


    <!-- AC screen: Blue Star Smart AC control ("ac kholo"). -->
    <div id="acScreen" class="ac-screen" hidden>
      <div class="ac-app" id="acCard" role="dialog" aria-modal="true" aria-labelledby="acTitle">
        <aside class="ac-side">
          <div class="ac-brand">
            <span class="ac-star"><svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2.5l2.8 6.3 6.7.6-5.1 4.5 1.5 6.6L12 16.9l-5.9 3.6 1.5-6.6L2.5 9.4l6.7-.6z"/></svg></span>
            <div><b>BLUE STAR</b><small>Smart AC · via Sara</small></div>
          </div>
          <nav class="ac-nav" id="acNav" aria-label="AC sections">
            <button type="button" data-view="all" class="on"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 11l9-8 9 8M5 10v10h14V10"/></svg>Dashboard</button>
            <button type="button" data-view="ctl"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/></svg>AC Control</button>
            <button type="button" data-view="sch"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><rect x="3.5" y="5" width="17" height="15" rx="2.5"/><path d="M3.5 10h17M8 3v4M16 3v4"/></svg>Schedule</button>
            <button type="button" data-view="en"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13 2L4 14h7l-1 8 9-12h-7z"/></svg>Energy Monitor</button>
            <button type="button" data-view="hist"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>History</button>
            <button type="button" data-view="rep"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M5 20V10M12 20V4M19 20v-7"/></svg>Report</button>
            <button type="button" data-view="dev"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/></svg>Device Info</button>
          </nav>
          <div class="ac-side-ai">
            <b>AI Assistant</b>
            <small>Ask anything about your AC, or give it a command.</small>
            <button type="button" id="acAskGo">🎙 Ask Sara</button>
          </div>
        </aside>
        <main class="ac-main">
          <header class="ac-top">
            <div class="ac-grow">
              <h2 id="acTitle">Good Morning</h2>
              <p id="acSub">Your Blue Star AC is ready to keep you comfortable.</p>
            </div>
            <select id="acPick" hidden aria-label="Choose AC"></select>
            <span id="acLive" class="ac-live"><i></i><span><b>...</b><small>Blue Star cloud</small></span></span>
            <button id="acHome" class="ac-home" type="button" title="Back to Sara AI (say 'sara ai' or 'back')" aria-label="Back to Sara AI">← Sara AI</button>
            <button id="acClose" class="ac-x" type="button" title="Close (Esc)" aria-label="Close">✕</button>
          </header>
          <div id="acSetup" class="ac-setup" hidden></div>
          <div id="acMain" data-view="all" hidden>
            <div class="ac-g1">
              <section class="ac-panel ac-hero" id="acSecHero" aria-label="AC status">
                <div class="ac-hero-top">
                  <div class="ac-unit"><span>BLUE STAR</span><i></i></div>
                  <div class="ac-air"></div>
                  <div class="ac-pill-state">
                    <div><span class="dot"></span><span id="acOnText">AC is OFF</span></div>
                    <em id="acModeText">Standby</em>
                  </div>
                </div>
                <div class="ac-hero-mid">
                  <div class="ac-mini"><small>Room Temp.</small><b id="acRoom">--</b><span id="acRoomNote">--</span></div>
                  <div class="ac-dial-wrap" style="position: relative; z-index: 1;">
                    <div class="ac-dial">
                      <svg viewBox="0 0 240 240" aria-hidden="true">
                        <defs>
                          <linearGradient id="acGrad" x1="0" y1="1" x2="1" y2="0">
                            <stop offset="0" style="stop-color: var(--ac-accent)"/>
                            <stop offset="1" style="stop-color: var(--ac-accent-2)"/>
                          </linearGradient>
                        </defs>
                        <g id="acTicks"></g>
                        <circle class="trk" cx="120" cy="120" r="100" fill="none" stroke-width="12" stroke-linecap="round" pathLength="100" stroke-dasharray="75 100" transform="rotate(135 120 120)"/>
                        <circle id="acArc" class="val" cx="120" cy="120" r="100" fill="none" stroke-width="12" stroke-linecap="round" pathLength="100" stroke-dasharray="0 100" transform="rotate(135 120 120)"/>
                      </svg>
                      <div class="ac-center">
                        <span class="snow"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M12 2v20M4.9 6l14.2 12M19.1 6 4.9 18"/></svg></span>
                        <span class="tgt"><span id="acTarget">--</span><sup>°C</sup></span>
                        <span class="lbl">Set Temperature</span>
                        <span class="rng">16°C - 30°C</span>
                      </div>
                    </div>
                    <button id="acMinus" class="ac-step l" type="button" aria-label="Lower temperature">−</button>
                    <button id="acPlus" class="ac-step r" type="button" aria-label="Raise temperature">+</button>
                  </div>
                  <div class="ac-mini"><small>Comfort</small><b id="acGap">--</b><span id="acGapNote">--</span></div>
                </div>
                <div class="ac-quick" id="acQuick">
                  <button type="button" class="ac-qb pw" id="qPower" aria-label="Power"><span class="ring"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><path d="M12 3v8"/><path d="M6.4 6.6a8 8 0 1 0 11.2 0"/></svg></span><b>Power</b><small id="qPowerV">Off</small></button>
                  <button type="button" class="ac-qb" id="qMode" aria-label="Change mode"><span class="ring">❄</span><b>Mode</b><small id="qModeV">--</small></button>
                  <button type="button" class="ac-qb" id="qFan" aria-label="Change fan speed"><span class="ring">🌀</span><b>Fan Speed</b><small id="qFanV">--</small></button>
                  <button type="button" class="ac-qb" id="qSwing" aria-label="Change swing"><span class="ring">≋</span><b>Swing</b><small id="qSwingV">--</small></button>
                  <button type="button" class="ac-qb" id="qTimer" aria-label="Change off timer"><span class="ring">⏱</span><b>Timer</b><small id="qTimerV">Off</small></button>
                </div>
              </section>
              <div class="ac-col">
                <section class="ac-panel" id="acSecCtl" aria-label="AC controls">
                  <h3><span class="ic"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/></svg></span>AC Control</h3>
                  <p class="sub">Customize your comfort</p>
                  <div class="ac-tabs" id="acTabs" role="tablist"></div>
                  <div id="acTiles" class="ac-tiles"></div>
                </section>
                <section class="ac-panel" id="acSecDev" aria-label="Device information">
                  <h3><span class="ic"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.5"/></svg></span>Device Information</h3>
                  <p class="sub">Your AC details</p>
                  <div class="ac-dev">
                    <dl id="acDevList"></dl>
                    <div class="ac-health" id="acHealth">
                      <span class="shield"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M8.5 12l2.5 2.5 4.5-5"/></svg></span>
                      <b id="acHealthT">Working Perfectly</b>
                      <small id="acHealthS">No issues detected</small>
                      <small id="acUpd">--</small>
                    </div>
                  </div>
                </section>
              </div>
            </div>
            <div class="ac-g2" style="margin-top: 16px;">
              <section class="ac-panel" id="acSecEn" aria-label="Energy monitor">
                <h3><span class="ic"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13 2L4 14h7l-1 8 9-12h-7z"/></svg></span>Energy Monitor</h3>
                <p class="sub">Estimated usage</p>
                <div class="ac-range" id="acEnRange"></div>
                <div class="ac-energy">
                  <div class="ac-ring">
                    <svg viewBox="0 0 120 120" aria-hidden="true"><circle class="trk" cx="60" cy="60" r="50" fill="none" stroke-width="9"/><circle id="acEnArc" class="val" cx="60" cy="60" r="50" fill="none" stroke-width="9" stroke-linecap="round" pathLength="100" stroke-dasharray="0 100" transform="rotate(-90 60 60)"/></svg>
                    <div><b id="acEnKwh">--</b><small>kWh (est.)</small></div>
                  </div>
                  <div class="ac-kv">
                    <div>Estimated Cost<b id="acEnCost">--</b></div>
                    <div>Avg. Daily Usage<b id="acEnAvg">--</b></div>
                    <div>Efficiency<b id="acEnEff">--</b></div>
                  </div>
                </div>
                <p class="ac-note" id="acEnNote"></p>
              </section>
              <section class="ac-panel" id="acSecHist" aria-label="Usage history">
                <div class="ac-head-row">
                  <div>
                    <h3><span class="ic"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg></span>Usage History</h3>
                    <p class="sub">Temperature &amp; operation log</p>
                  </div>
                  <select class="ac-sel" id="acHiSel" aria-label="History period">
                    <option value="1">Today</option><option value="7">7 Days</option><option value="30">30 Days</option>
                  </select>
                </div>
                <div id="acChartBox"></div>
                <div class="ac-sum icons">
                  <div><span class="ico"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg></span><span><small>Total ON Time</small><b id="acSumOn">--</b></span></div>
                  <div><span class="ico"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M14 14.8V5a2 2 0 0 0-4 0v9.8a4 4 0 1 0 4 0z"/></svg></span><span><small>Avg. Temp.</small><b id="acSumTemp">--</b></span></div>
                  <div><span class="ico"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13 2L4 14h7l-1 8 9-12h-7z"/></svg></span><span><small>Total Usage</small><b id="acSumKwh">--</b></span></div>
                </div>
              </section>
              <section class="ac-panel" id="acSecSch" aria-label="Smart schedule">
                <h3><span class="ic"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><rect x="3.5" y="5" width="17" height="15" rx="2.5"/><path d="M3.5 10h17M8 3v4M16 3v4"/></svg></span>Smart Schedule</h3>
                <p class="sub">Runs while Sara is running</p>
                <ul class="ac-rules" id="acRules"></ul>
                <button type="button" class="ac-add" id="acAddBtn">+ Add Schedule</button>
                <form class="ac-form" id="acForm" hidden>
                  <label class="wide">Name<input id="fName" maxlength="30" placeholder="Night Mode" required></label>
                  <label>Start<input id="fStart" type="time" value="22:00" required></label>
                  <label>Stop<input id="fEnd" type="time" value="06:00" required></label>
                  <label>Temperature °C<input id="fTemp" type="number" min="16" max="30" value="26" required></label>
                  <label>Mode<select id="fMode"><option value="cool">Cool</option><option value="dry">Dry</option><option value="fan">Fan</option><option value="auto">Auto</option></select></label>
                  <div class="row"><button type="button" id="fCancel">Cancel</button><button type="submit" class="go">Save</button></div>
                </form>
              </section>
              <section class="ac-panel" id="acSecAi" aria-label="AI assistant">
                <div class="ac-chat">
                  <h3><span class="ic">✦</span>Blue Star AI</h3>
                  <div class="ac-log" id="acLog" aria-live="polite"></div>
                  <div class="ac-chips" id="acChips"></div>
                  <form class="ac-ask" id="acAsk"><input id="acAskIn" placeholder="Type or speak..." autocomplete="off" maxlength="200" aria-label="Ask the AC assistant"><button type="button" class="mic" id="acMic" aria-label="Speak" title="Speak a command">🎙</button><button type="submit" aria-label="Send">➤</button></form>
                </div>
              </section>
            </div>
            <section class="ac-panel ac-wide" id="acSecRep" aria-label="Daily usage report">
              <h3><span class="ic"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M5 20V10M12 20V4M19 20v-7"/></svg></span>Daily Usage Report</h3>
              <p class="sub">How long the AC ran each day</p>
              <div class="ac-rep-head">
                <div class="ac-range" id="acRepRange"></div>
                <button type="button" class="ac-add ac-dl" id="acRepCsv">⬇ Download CSV</button>
              </div>
              <div id="acRepChart"></div>
              <div class="ac-sum" id="acRepSum">
                <div><small>Total ON Time</small><b id="acRepOn">--</b></div>
                <div><small>Avg. per Day</small><b id="acRepAvg">--</b></div>
                <div><small>Energy (est.)</small><b id="acRepKwh">--</b></div>
                <div><small>Cost (est.)</small><b id="acRepCost">--</b></div>
              </div>
              <div class="ac-tablewrap">
                <table class="ac-table" id="acRepTable">
                  <thead><tr><th>Date</th><th>AC ON</th><th>Energy</th><th>Cost</th><th>Set temp</th><th>Room temp</th></tr></thead>
                  <tbody id="acRepBody"></tbody>
                </table>
              </div>
              <p class="ac-note" id="acRepNote"></p>
            </section>
          </div>
          <p id="acMsg" class="ac-msg" aria-live="polite"></p>
        </main>
      </div>
    </div>
    <script>
      const htmlTag = document.documentElement;
      const themeToggleBtn = document.getElementById("themeToggle");
      if (localStorage.getItem("theme") === "light") {
        htmlTag.setAttribute("data-theme", "light");
      }
      themeToggleBtn.addEventListener("click", () => {
        if (htmlTag.getAttribute("data-theme") === "light") {
          htmlTag.removeAttribute("data-theme");
          localStorage.setItem("theme", "dark");
        } else {
          htmlTag.setAttribute("data-theme", "light");
          localStorage.setItem("theme", "light");
        }
      });

      const statusEl = document.getElementById("status");
      const logsEl = document.getElementById("logs");
      const videoEl = document.getElementById("video");
      const noSignal = document.getElementById("noSignal");

      // --- AI visual: animated core in the video panel -----------------------
      // States: idle (slow breathing), listening (cyan pulse), thinking (fast
      // violet spin), speaking (waveform driven by Sara's real voice when the
      // neural audio can be analysed, simulated otherwise).
      const aiVisual = (() => {
        const canvas = document.getElementById("aiCanvas");
        const stateEl = document.getElementById("aiVisualState");
        const ctx = canvas.getContext("2d");
        const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        const LABELS = { idle: "IDLE", listening: "LISTENING", thinking: "THINKING", speaking: "SPEAKING" };
        let state = "idle";
        let level = 0;          // smoothed loudness 0..1
        let analyser = null;    // set while neural audio is playing
        let freq = null;
        let audioCtx = null;
        let rafId = null;
        let spin = 0;
        let lastW = 0, lastH = 0;
        // Real-time extras: neuron flashes between nodes, data rising from the
        // pedestal, and live readouts (uptime + measured frame rate).
        let flashes = [], lastFlash = 0, globeShift = 0, radarRev = null;
        // Compass heading in degrees (0 = north). Phones / tablets with a
        // magnetometer report it; laptops usually don't, so it stays north-up.
        let heading = 0, targetHeading = 0, hasCompass = false;
        function onOrientation(e) {
          let h = null;
          if (typeof e.webkitCompassHeading === "number") h = e.webkitCompassHeading;
          else if (e.absolute && typeof e.alpha === "number") h = 360 - e.alpha;
          if (h != null && !Number.isNaN(h)) { targetHeading = h; hasCompass = true; }
        }
        window.addEventListener("deviceorientationabsolute", onOrientation);
        window.addEventListener("deviceorientation", onOrientation);
        const risers = Array.from({ length: 28 }, () => ({ p: Math.random(), x: Math.random() * 2 - 1, v: 0.002 + Math.random() * 0.004 }));
        const bornAt = Date.now();
        let fps = 0, fpsFrames = 0, fpsSince = 0;
        const RING_TEXT = " SARA · NEURAL CORE · ONLINE · REAL-TIME ·";
        if (document.fonts && document.fonts.load) document.fonts.load("700 40px Orbitron").catch(() => {});
        const particles = Array.from({ length: 46 }, () => ({
          a: Math.random() * Math.PI * 2,
          r: 1.5 + Math.random() * 1.6,
          s: (0.002 + Math.random() * 0.006) * (Math.random() < 0.5 ? -1 : 1),
          z: 0.6 + Math.random() * 1.4,
        }));

        // Circuit traces wrapped around the globe's surface, in longitude /
        // latitude (radians): short paths of 45-degree steps ending in nodes,
        // each with a travelling pulse. The globe spins, so they circle 360°.
        const circuits = Array.from({ length: 70 }, () => {
          let lon = Math.random() * Math.PI * 2, lat = (Math.random() * 2 - 1) * 1.15;
          let dir = Math.floor(Math.random() * 8);
          const pts = [{ lon, lat }];
          const steps = 2 + Math.floor(Math.random() * 3);
          for (let i = 0; i < steps; i++) {
            if (i && Math.random() < 0.6) dir = (dir + (Math.random() < 0.5 ? 1 : 7)) % 8;
            const len = 0.12 + Math.random() * 0.25;
            const dLat = Math.sin(dir * Math.PI / 4) * len;
            const dLon = Math.cos(dir * Math.PI / 4) * len / Math.max(0.35, Math.cos(lat));
            if (Math.abs(lat + dLat) > 1.25) break;
            // Subdivide so each step bends with the sphere's curve.
            for (let k = 1; k <= 4; k++) pts.push({ lon: lon + dLon * k / 4, lat: lat + dLat * k / 4 });
            lon += dLon; lat += dLat;
          }
          return { pts, phase: Math.random(), speed: 0.15 + Math.random() * 0.35 };
        }).filter(c => c.pts.length > 1);

        // Project a point on the unit sphere (spun by ``rot``, tilted toward
        // the viewer) to screen offsets; z > 0 is the front face.
        const TILT = 0.35;
        function project(lon, lat, rot) {
          const cl = Math.cos(lat);
          const x = cl * Math.sin(lon + rot), y = Math.sin(lat), z = cl * Math.cos(lon + rot);
          return { x, y: -(y * Math.cos(TILT) - z * Math.sin(TILT)), z: y * Math.sin(TILT) + z * Math.cos(TILT) };
        }
        // Front-facing lines bright, ones wrapping round the back faint.
        function depthAlpha(z, front, back) { return z > 0 ? back + (front - back) * z : back * (1 + z); }

        function color() {
          if (state === "listening") return "#22d3ee";
          if (state === "thinking") return "#a78bfa";
          return "#3b82f6";
        }
        // Point at fraction f (0..1) along a lon/lat polyline.
        function along(pts, f) {
          const i = Math.min(pts.length - 2, Math.floor(f * (pts.length - 1)));
          const k = f * (pts.length - 1) - i;
          return { lon: pts[i].lon + (pts[i + 1].lon - pts[i].lon) * k,
                   lat: pts[i].lat + (pts[i + 1].lat - pts[i].lat) * k };
        }
        function rgba(hex, a) {
          const h = hex.replace("#", "");
          const n = parseInt(h.length === 3 ? h.split("").map(c => c + c).join("") : h, 16);
          return "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + a + ")";
        }

        function resize() {
          const dpr = Math.min(window.devicePixelRatio || 1, 2);
          const w = canvas.clientWidth, h = canvas.clientHeight;
          if (!w || !h) return;
          canvas.width = Math.round(w * dpr);
          canvas.height = Math.round(h * dpr);
          ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        }

        function targetLevel(t) {
          if (state === "speaking") {
            if (analyser) {
              analyser.getByteFrequencyData(freq);
              let sum = 0;
              for (let i = 0; i < freq.length; i++) sum += freq[i];
              return Math.min(1, (sum / freq.length) / 90);
            }
            return 0.35 + 0.3 * Math.abs(Math.sin(t / 110) * Math.sin(t / 270));
          }
          if (state === "listening") return 0.25 + 0.12 * Math.sin(t / 180);
          if (state === "thinking") return 0.18 + 0.06 * Math.sin(t / 90);
          return 0.06 + 0.04 * Math.sin(t / 900);
        }

        function draw(t) {
          rafId = null;
          const w = canvas.clientWidth, h = canvas.clientHeight;
          if (!w || !h || noSignal.style.display === "none" || document.hidden) return;
          if (w !== lastW || h !== lastH) { resize(); lastW = w; lastH = h; }
          const c = color();
          const textC = state === "thinking" ? "#ddd6fe" : "#a5f3fc";
          // An open info card takes the right side: centre the globe in the rest.
          const room = noSignal.parentElement.classList.contains("has-card") ? Math.max(w * 0.55, w - 340) : w;
          globeShift += ((room - w) / 2 - globeShift) * (reduceMotion ? 1 : 0.12);
          const cx = w / 2 + globeShift, cy = h / 2 - 16;
          const R = Math.min(room, h) * 0.3 * (1 + level * 0.04);
          level += (targetLevel(t) - level) * 0.18;
          spin += reduceMotion ? 0 : (state === "thinking" ? 0.02 : state === "listening" ? 0.01 : 0.004);
          const pulseSpeed = state === "thinking" ? 3 : state === "speaking" ? 1.5 + level * 2 : 1;

          ctx.clearRect(0, 0, w, h);

          // Deep blue ambience behind the globe.
          const glow = ctx.createRadialGradient(cx, cy, R * 0.6, cx, cy, R * 2.6);
          glow.addColorStop(0, rgba(c, 0.28 + level * 0.25));
          glow.addColorStop(1, rgba(c, 0));
          ctx.fillStyle = glow;
          ctx.fillRect(0, 0, w, h);

          // Searchlights: soft beams rising from the pedestal, each sweeping
          // side to side at its own pace (faster while busy, brighter while
          // speaking), plus a radar-style sweep circling behind the globe.
          const busy = state === "thinking" ? 2.2 : state === "listening" ? 1.5 : 1;
          const sweepT = reduceMotion ? 0 : t * busy;
          ctx.save();
          ctx.globalCompositeOperation = "lighter";
          const baseY = cy + R * 1.12, beamLen = Math.hypot(w, h) * 0.75;
          [[-1, 0.0, 0.00042, 0.62], [0, 2.1, 0.00031, 0.42], [1, 4.2, 0.00055, 0.62]].forEach(([k, ph, sp, amp]) => {
            const ox = cx + k * R * 0.32, oy = baseY;
            const ang = -Math.PI / 2 + Math.sin(sweepT * sp + ph) * amp + k * 0.18;
            const spread = 0.075 + 0.015 * Math.sin(sweepT / 1100 + ph);
            const bg = ctx.createRadialGradient(ox, oy, 0, ox, oy, beamLen);
            bg.addColorStop(0, rgba(textC, 0.2 + level * 0.18));
            bg.addColorStop(0.35, rgba(c, 0.07 + level * 0.05));
            bg.addColorStop(1, rgba(c, 0));
            ctx.fillStyle = bg;
            ctx.beginPath(); ctx.moveTo(ox, oy);
            ctx.lineTo(ox + Math.cos(ang - spread) * beamLen, oy + Math.sin(ang - spread) * beamLen);
            ctx.lineTo(ox + Math.cos(ang + spread) * beamLen, oy + Math.sin(ang + spread) * beamLen);
            ctx.closePath(); ctx.fill();
            // Brighter core down the middle of each beam.
            const cg = ctx.createLinearGradient(ox, oy, ox + Math.cos(ang) * beamLen * 0.6, oy + Math.sin(ang) * beamLen * 0.6);
            cg.addColorStop(0, rgba("#ffffff", 0.22)); cg.addColorStop(1, rgba("#ffffff", 0));
            ctx.strokeStyle = cg; ctx.lineWidth = 1.2;
            ctx.beginPath(); ctx.moveTo(ox, oy);
            ctx.lineTo(ox + Math.cos(ang) * beamLen * 0.6, oy + Math.sin(ang) * beamLen * 0.6); ctx.stroke();
          });
          if (ctx.createConicGradient) {
            const ra = sweepT / 2600;
            // One soft radar ping per full turn of the sweep.
            const rev = Math.floor(ra / (Math.PI * 2));
            if (rev !== radarRev) { if (radarRev !== null && window.saraGlobeSound) window.saraGlobeSound.ping(); radarRev = rev; }
            const cg = ctx.createConicGradient(ra, cx, cy);
            cg.addColorStop(0, rgba(textC, 0));
            cg.addColorStop(0.86, rgba(textC, 0));
            cg.addColorStop(0.995, rgba(textC, 0.16 + level * 0.12));
            cg.addColorStop(1, rgba(textC, 0));
            ctx.fillStyle = cg;
            ctx.beginPath(); ctx.arc(cx, cy, R * 1.45, 0, Math.PI * 2); ctx.arc(cx, cy, R * 1.02, 0, Math.PI * 2, true); ctx.fill();
            // Leading edge of the sweep.
            ctx.strokeStyle = rgba(textC, 0.35);
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(cx + Math.cos(ra) * R * 1.02, cy + Math.sin(ra) * R * 1.02);
            ctx.lineTo(cx + Math.cos(ra) * R * 1.45, cy + Math.sin(ra) * R * 1.45);
            ctx.stroke();
          }
          ctx.restore();

          // Light pedestal under the globe.
          const by = cy + R * 1.12;
          const beam = ctx.createRadialGradient(cx, by, 0, cx, by, R * 0.9);
          beam.addColorStop(0, "rgba(255,255,255,0.9)");
          beam.addColorStop(0.15, rgba(textC, 0.7));
          beam.addColorStop(1, rgba(c, 0));
          ctx.save();
          ctx.translate(cx, by); ctx.scale(1, 0.12); ctx.translate(-cx, -by);
          ctx.fillStyle = beam;
          ctx.beginPath(); ctx.arc(cx, by, R * 0.9, 0, Math.PI * 2); ctx.fill();
          ctx.restore();
          // Soft upward light from the pedestal (a squashed radial glow, no hard edges).
          const up = ctx.createRadialGradient(cx, by, 0, cx, by, R * 0.45);
          up.addColorStop(0, rgba(textC, 0.3)); up.addColorStop(1, rgba(textC, 0));
          ctx.save();
          ctx.translate(cx, by); ctx.scale(1, 1.8); ctx.translate(-cx, -by);
          ctx.fillStyle = up;
          ctx.beginPath(); ctx.arc(cx, by, R * 0.45, Math.PI, Math.PI * 2); ctx.fill();
          ctx.restore();

          // Tilted orbit ring with a satellite: back half behind the globe,
          // front half drawn over it after the rim.
          const rot = spin * 1.6;
          const orbit = (front) => {
            const rx = R * 1.32, ry = R * 0.28, tilt = -0.35, sa = spin * 2.5;
            ctx.save(); ctx.translate(cx, cy); ctx.rotate(tilt);
            ctx.strokeStyle = rgba(textC, front ? 0.55 : 0.18); ctx.lineWidth = 1.5;
            ctx.beginPath(); ctx.ellipse(0, 0, rx, ry, 0, front ? 0 : Math.PI, front ? Math.PI : Math.PI * 2); ctx.stroke();
            const th = ((sa % (Math.PI * 2)) + Math.PI * 2) % (Math.PI * 2);
            if ((th < Math.PI) === front) {
              ctx.fillStyle = "#ffffff"; ctx.shadowColor = textC; ctx.shadowBlur = 14;
              ctx.beginPath(); ctx.arc(Math.cos(th) * rx, Math.sin(th) * ry, front ? 4 : 2.5, 0, Math.PI * 2); ctx.fill();
              ctx.shadowBlur = 0;
            }
            ctx.restore();
          };
          orbit(false);

          // Data particles streaming up from the pedestal into the globe.
          for (const r of risers) {
            if (!reduceMotion) r.p += r.v * (1 + level * 3 + (state === "thinking" ? 1.5 : 0));
            if (r.p > 1) { r.p = 0; r.x = Math.random() * 2 - 1; }
            const px = cx + r.x * R * 0.38 * (1 - r.p * 0.5), py = by - r.p * R * 1.3;
            ctx.fillStyle = rgba(textC, Math.sin(r.p * Math.PI) * 0.8);
            ctx.fillRect(px - 0.75, py, 1.5, 6 + r.v * 900);
          }

          // Sphere body: dark centre, bright rim.
          const body = ctx.createRadialGradient(cx, cy, R * 0.1, cx, cy, R);
          body.addColorStop(0, rgba(c, 0.10));
          body.addColorStop(0.7, rgba(c, 0.20));
          body.addColorStop(0.92, rgba(c, 0.45));
          body.addColorStop(1, rgba(textC, 0.75));
          ctx.fillStyle = body;
          ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.fill();

          // Inside the globe: a 3D grid and circuit traces spinning 360°.
          ctx.save();
          ctx.beginPath(); ctx.arc(cx, cy, R * 0.985, 0, Math.PI * 2); ctx.clip();
          const R2 = R * 0.97;
          // Draw a lon/lat polyline segment by segment, faded by depth.
          const curve = (pts, front, back, width) => {
            ctx.lineWidth = width;
            let prev = null;
            for (const p of pts) {
              const q = project(p.lon, p.lat, rot);
              if (prev) {
                ctx.strokeStyle = rgba(textC, depthAlpha((q.z + prev.z) / 2, front, back));
                ctx.beginPath();
                ctx.moveTo(cx + prev.x * R2, cy + prev.y * R2);
                ctx.lineTo(cx + q.x * R2, cy + q.y * R2);
                ctx.stroke();
              }
              prev = q;
            }
          };
          for (let m = 0; m < 12; m++) {
            const lon = m * Math.PI / 6;
            curve(Array.from({ length: 25 }, (_, i) => ({ lon, lat: -Math.PI / 2 + i * Math.PI / 24 })), 0.12, 0.03, 1);
          }
          for (const lat of [-1.05, -0.52, 0, 0.52, 1.05]) {
            curve(Array.from({ length: 49 }, (_, i) => ({ lon: i * Math.PI / 24, lat })), 0.12, 0.03, 1);
          }

          const front = 0.55 + level * 0.3;
          const frontNodes = [];
          for (const cir of circuits) {
            curve(cir.pts, front, 0.07, 1.2);
            for (const p of [cir.pts[0], cir.pts[cir.pts.length - 1]]) {
              const q = project(p.lon, p.lat, rot);
              if (q.z > 0.35) frontNodes.push({ x: cx + q.x * R2, y: cy + q.y * R2 });
              ctx.fillStyle = rgba(textC, depthAlpha(q.z, 0.9, 0.08));
              ctx.beginPath(); ctx.arc(cx + q.x * R2, cy + q.y * R2, q.z > 0 ? 2.2 : 1.4, 0, Math.PI * 2); ctx.fill();
            }
          }
          // Data pulses running along the traces (front face only).
          ctx.shadowColor = textC; ctx.shadowBlur = 8;
          for (const cir of circuits) {
            if (!reduceMotion) cir.phase = (cir.phase + cir.speed * 0.006 * pulseSpeed) % 1;
            const p = along(cir.pts, cir.phase), q = project(p.lon, p.lat, rot);
            if (q.z < 0) continue;
            ctx.fillStyle = "rgba(255,255,255," + (0.3 + 0.65 * q.z) + ")";
            ctx.beginPath(); ctx.arc(cx + q.x * R2, cy + q.y * R2, 1.7, 0, Math.PI * 2); ctx.fill();
          }
          ctx.shadowBlur = 0;

          // Neuron flashes: a bright link fires between two nearby front nodes.
          // They fire faster while thinking / speaking.
          const flashEvery = state === "thinking" ? 60 : state === "speaking" ? 90 - level * 50 : state === "listening" ? 120 : 220;
          if (!reduceMotion && frontNodes.length > 3 && t - lastFlash > flashEvery) {
            lastFlash = t;
            const a = frontNodes[Math.floor(Math.random() * frontNodes.length)];
            const near = frontNodes.filter(b => b !== a && Math.hypot(b.x - a.x, b.y - a.y) < R * 0.55);
            if (near.length) flashes.push({ a, b: near[Math.floor(Math.random() * near.length)], born: t });
          }
          flashes = flashes.filter(f => t - f.born < 650);
          for (const f of flashes) {
            const k = 1 - (t - f.born) / 650;
            const mx = (f.a.x + f.b.x) / 2 + (f.b.y - f.a.y) * 0.15, my = (f.a.y + f.b.y) / 2 - (f.b.x - f.a.x) * 0.15;
            ctx.strokeStyle = "rgba(255,255,255," + (0.85 * k) + ")";
            ctx.lineWidth = 1.4;
            ctx.shadowColor = textC; ctx.shadowBlur = 12;
            ctx.beginPath(); ctx.moveTo(f.a.x, f.a.y); ctx.quadraticCurveTo(mx, my, f.b.x, f.b.y); ctx.stroke();
            ctx.fillStyle = "rgba(255,255,255," + k + ")";
            for (const n of [f.a, f.b]) { ctx.beginPath(); ctx.arc(n.x, n.y, 2.5 + 2 * k, 0, Math.PI * 2); ctx.fill(); }
          }
          ctx.shadowBlur = 0;
          // Glassy highlight on the upper-left.
          const hi = ctx.createRadialGradient(cx - R * 0.45, cy - R * 0.5, 0, cx - R * 0.45, cy - R * 0.5, R * 0.7);
          hi.addColorStop(0, "rgba(255,255,255,0.12)"); hi.addColorStop(1, "rgba(255,255,255,0)");
          ctx.fillStyle = hi; ctx.fillRect(cx - R, cy - R, R * 2, R * 2);
          ctx.restore();

          // Glowing rim (thicker while speaking).
          ctx.strokeStyle = rgba(textC, 0.85);
          ctx.lineWidth = 2 + level * 3;
          ctx.shadowColor = c; ctx.shadowBlur = 24 + level * 30;
          ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.stroke();
          ctx.shadowBlur = 0;
          orbit(true);

          // Voice-reactive halo just outside the rim.
          const pts = 120;
          ctx.beginPath();
          for (let i = 0; i <= pts; i++) {
            const a = (i / pts) * Math.PI * 2;
            let amp;
            if (analyser && state === "speaking") {
              amp = freq[Math.floor(Math.abs(Math.sin(a * 1.5)) * (freq.length - 1))] / 255 * 0.5;
            } else {
              amp = (Math.sin(a * 6 + t / 300) + Math.sin(a * 11 - t / 200)) * 0.12 * level;
            }
            const r = R * (1.08 + Math.abs(amp) * 0.25);
            const x = cx + Math.cos(a) * r, y = cy + Math.sin(a) * r;
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
          }
          ctx.strokeStyle = rgba(c, 0.35 + level * 0.4);
          ctx.lineWidth = 1.5;
          ctx.stroke();

          // Compass ring: ticks every 5°, degrees every 30°, N/E/S/W. North-up,
          // or turned to the real heading when the device has a compass.
          heading += (((targetHeading - heading + 540) % 360) - 180) * 0.1;
          const r0 = R * 1.13;
          ctx.save(); ctx.translate(cx, cy);
          ctx.rotate(-heading * Math.PI / 180);
          ctx.textAlign = "center"; ctx.textBaseline = "middle";
          for (let deg = 0; deg < 360; deg += 5) {
            const a = (deg - 90) * Math.PI / 180;           // 0° = north (up)
            const major = deg % 30 === 0, cardinal = deg % 90 === 0;
            const r1 = r0 + (cardinal ? 11 : major ? 8 : 4);
            ctx.strokeStyle = rgba(cardinal && deg === 0 ? "#f472b6" : textC, cardinal ? 0.85 : major ? 0.55 : 0.22);
            ctx.lineWidth = major ? 1.5 : 1;
            ctx.beginPath(); ctx.moveTo(Math.cos(a) * r0, Math.sin(a) * r0); ctx.lineTo(Math.cos(a) * r1, Math.sin(a) * r1); ctx.stroke();
            if (!major) continue;
            const lr = r0 + 22;
            ctx.save();
            ctx.translate(Math.cos(a) * lr, Math.sin(a) * lr);
            ctx.rotate(heading * Math.PI / 180);            // labels stay upright
            if (cardinal) {
              const letter = { 0: "N", 90: "E", 180: "S", 270: "W" }[deg];
              ctx.font = "800 15px Orbitron, 'JetBrains Mono', monospace";
              ctx.fillStyle = deg === 0 ? "#f472b6" : "#e0f2fe";
              ctx.shadowColor = deg === 0 ? "#f472b6" : textC; ctx.shadowBlur = 10;
              ctx.fillText(letter, 0, 0);
              ctx.shadowBlur = 0;
            } else {
              ctx.font = "600 9px 'JetBrains Mono', monospace";
              ctx.fillStyle = rgba(textC, 0.7);
              ctx.fillText(String(deg), 0, 0);
            }
            ctx.restore();
          }
          ctx.restore();
          // Fixed heading pointer just inside the ring, pointing at the top.
          const py = cy - r0 - 1;
          ctx.fillStyle = "#f472b6";
          ctx.beginPath(); ctx.moveTo(cx, py); ctx.lineTo(cx - 5, py + 9); ctx.lineTo(cx + 5, py + 9); ctx.closePath(); ctx.fill();
          const hdg = Math.round((heading % 360 + 360) % 360) % 360;
          const POINTS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
          const hdgText = String(hdg).padStart(3, "0") + "° " + POINTS[Math.round(hdg / 45) % 8];

          // Circling caption, outside the compass.
          ctx.save(); ctx.translate(cx, cy);
          ctx.rotate(reduceMotion ? 0 : t / 4500);
          ctx.font = "600 10px 'JetBrains Mono', monospace";
          ctx.textAlign = "center"; ctx.textBaseline = "middle";
          ctx.fillStyle = rgba(textC, 0.45);
          // Space letters like normal text (~8px each) and repeat to fill the ring.
          const ringR = r0 + 42;
          const fit = Math.floor(Math.PI * 2 * ringR / 8.5);
          const chars = RING_TEXT.repeat(Math.max(1, Math.floor(fit / RING_TEXT.length)));
          const step = Math.PI * 2 / chars.length;
          for (let i = 0; i < chars.length; i++) {
            ctx.save(); ctx.rotate(i * step); ctx.translate(0, -ringR); ctx.fillText(chars[i], 0, 0); ctx.restore();
          }
          ctx.restore();

          // Energy core: a heartbeat glow behind the lettering.
          const beat = reduceMotion ? 0.5 : 0.5 + 0.5 * Math.pow(Math.max(0, Math.sin(t / (state === "thinking" ? 180 : 420))), 6);
          const core = ctx.createRadialGradient(cx, cy, 0, cx, cy, R * 0.6);
          core.addColorStop(0, rgba(textC, 0.28 + beat * 0.18 + level * 0.25));
          core.addColorStop(1, rgba(c, 0));
          ctx.save(); ctx.globalCompositeOperation = "lighter";
          ctx.fillStyle = core; ctx.beginPath(); ctx.arc(cx, cy, R * 0.6, 0, Math.PI * 2); ctx.fill();
          ctx.restore();

          // "SARA_OS" wrapped around the inside of the globe, spinning with it:
          // letters travel across the front and fade round the back (seen
          // mirrored through the glass). Two copies, half a turn apart, so one
          // is always passing the front.
          const WORD = "SARA_OS";
          const fs = R * 0.25 * (1 + level * 0.05), stepA = 0.24, TR = R * 0.9;
          ctx.font = "700 " + fs + "px Orbitron, 'Segoe UI', system-ui, sans-serif";
          ctx.textAlign = "center"; ctx.textBaseline = "middle";
          const glyphs = [];
          for (const base of [0, Math.PI]) {
            for (let i = 0; i < WORD.length; i++) {
              const lon = base + (i - (WORD.length - 1) / 2) * stepA;
              // Latitude = tilt, so the front-facing word sits at the globe's centre.
              const q = project(lon, TILT, rot);
              glyphs.push({ ch: WORD[i], x: cx + q.x * TR, y: cy + q.y * TR, z: q.z, face: Math.cos(lon + rot) });
            }
          }
          glyphs.sort((a, b) => a.z - b.z);
          const tg = ctx.createLinearGradient(0, -fs / 2, 0, fs / 2);
          tg.addColorStop(0, "#ffffff"); tg.addColorStop(0.55, "#e0f7ff"); tg.addColorStop(1, textC);
          for (const g of glyphs) {
            const k = Math.abs(g.face);
            ctx.save();
            ctx.translate(g.x, g.y);
            ctx.scale(Math.sign(g.face || 1) * Math.max(0.06, k), 1);   // foreshortening
            if (g.face > 0) {
              ctx.globalAlpha = 0.25 + 0.75 * k;
              ctx.fillStyle = tg;
              ctx.shadowColor = textC; ctx.shadowBlur = (14 + level * 20 + beat * 8) * k;
              ctx.fillText(g.ch, 0, 0);
              ctx.shadowBlur = 0;
              if (k > 0.5) { ctx.lineWidth = 0.8; ctx.strokeStyle = "rgba(255,255,255,0.5)"; ctx.strokeText(g.ch, 0, 0); }
            } else {
              ctx.fillStyle = rgba(textC, 0.14 * k);
              ctx.fillText(g.ch, 0, 0);
            }
            ctx.restore();
          }

          if ("letterSpacing" in ctx) ctx.letterSpacing = Math.round(fs * 0.08) + "px";
          ctx.font = "600 " + Math.max(9, Math.round(R * 0.042)) + "px 'JetBrains Mono', monospace";
          ctx.fillStyle = rgba(textC, 0.8);
          ctx.fillText("NEURAL CORE", cx, cy + R * 0.3);
          if ("letterSpacing" in ctx) ctx.letterSpacing = "0px";

          // Live readouts beside the globe (real values: state, uptime, FPS).
          fpsFrames++;
          if (t - fpsSince >= 1000) { fps = Math.round(fpsFrames * 1000 / (t - fpsSince)); fpsFrames = 0; fpsSince = t; }
          if (room > R * 4.2) {
            const up = Math.floor((Date.now() - bornAt) / 1000), pad = n => String(n).padStart(2, "0");
            const rows = [
              ["CORE", LABELS[state]],
              ["UPTIME", pad(Math.floor(up / 3600)) + ":" + pad(Math.floor(up / 60) % 60) + ":" + pad(up % 60)],
              ["HEADING", hdgText + (hasCompass ? "" : " · N-UP")],
              ["RENDER", fps + " FPS"],
              ["SIGNAL", Math.round(level * 100) + "%"],
            ];
            const lx = cx - R * 1.95, rx = cx + R * 1.95;
            rows.forEach(([k, v], i) => {
              // Left: CORE / UPTIME / HEADING; right: RENDER / SIGNAL.
              const left = i < 3, side = left ? lx : rx;
              const yy = left ? cy - 36 + i * 36 : cy - 18 + (i - 3) * 36;
              ctx.textAlign = left ? "left" : "right";
              ctx.font = "600 9px 'JetBrains Mono', monospace"; ctx.fillStyle = rgba(textC, 0.5);
              ctx.fillText(k, side, yy);
              ctx.font = "700 13px 'JetBrains Mono', monospace"; ctx.fillStyle = "rgba(255,255,255,0.88)";
              ctx.fillText(v, side, yy + 15);
            });
            // Signal meter under the right-hand readouts.
            for (let i = 0; i < 10; i++) {
              ctx.fillStyle = i / 10 < level ? textC : rgba(textC, 0.15);
              ctx.fillRect(rx - 100 + i * 10, cy + 50, 7, 3);
            }
          }
          ctx.textAlign = "start"; ctx.textBaseline = "alphabetic";

          // Floating particles around the globe.
          for (const p of particles) {
            if (!reduceMotion) p.a += p.s * (state === "thinking" ? 3 : 1 + level * 2);
            const r = R * (0.75 + p.r * 0.45) * (1 + level * 0.1);
            const x = cx + Math.cos(p.a) * r, y = cy + Math.sin(p.a) * r * 0.92;
            ctx.fillStyle = rgba(textC, 0.2 + 0.4 * (p.z / 2));
            ctx.beginPath();
            ctx.arc(x, y, p.z * 0.8, 0, Math.PI * 2);
            ctx.fill();
          }

          rafId = requestAnimationFrame(draw);
        }

        function kick() {
          if (rafId === null) rafId = requestAnimationFrame(draw);
        }

        function setState(next) {
          state = LABELS[next] ? next : "idle";
          noSignal.dataset.state = state;
          stateEl.textContent = LABELS[state];
          kick();
        }

        // Route a playing <audio> through an analyser so the wave follows the
        // real voice. Only when the audio context is running — otherwise the
        // element would be silenced — and never twice for the same element.
        async function attachAudio(audio) {
          try {
            const AC = window.AudioContext || window.webkitAudioContext;
            if (!AC) return;
            audioCtx = audioCtx || new AC();
            if (audioCtx.state !== "running") {
              await Promise.race([audioCtx.resume(), new Promise(r => setTimeout(r, 150))]);
            }
            if (audioCtx.state !== "running") return;
            const src = audioCtx.createMediaElementSource(audio);
            const an = audioCtx.createAnalyser();
            an.fftSize = 128;
            // Louder voice: boost, then a compressor so peaks don't distort.
            const boost = audioCtx.createGain();
            boost.gain.value = 1.8;
            const comp = audioCtx.createDynamicsCompressor();
            comp.threshold.value = -12; comp.knee.value = 6; comp.ratio.value = 4;
            comp.attack.value = 0.003; comp.release.value = 0.15;
            src.connect(boost); boost.connect(comp); comp.connect(an);
            an.connect(audioCtx.destination);
            analyser = an;
            freq = new Uint8Array(an.frequencyBinCount);
          } catch (e) { analyser = null; }
        }
        function detachAudio() { analyser = null; }

        window.addEventListener("resize", () => { resize(); kick(); });
        document.addEventListener("visibilitychange", kick);
        new MutationObserver(kick).observe(noSignal, { attributes: true, attributeFilter: ["style"] });
        resize();
        kick();
        return { setState, attachAudio, detachAudio, get state() { return state; } };
      })();

      // "5M AGO" / "5 मिनट पहले" for a headline's ISO timestamp.
      function timeAgo(iso, hindi) {
        const mins = Math.max(0, Math.round((Date.now() - new Date(iso)) / 60000));
        if (!iso || !Number.isFinite(mins)) return "";
        if (mins < 60) return hindi ? mins + " मिनट पहले" : mins + "M AGO";
        const h = Math.round(mins / 60);
        if (h < 24) return hindi ? h + " घंटे पहले" : h + "H AGO";
        return hindi ? Math.round(h / 24) + " दिन पहले" : Math.round(h / 24) + "D AGO";
      }

      // --- YouTube player helper ---------------------------------------------
      // Uses the IFrame Player API so we notice when a video refuses to play
      // embedded (an owner/label restriction) and can move on to another
      // upload: first the given alternates, then a search for the same title.
      const ytPlayer = (() => {
        let api = null;
        function loadApi() {
          if (!api) api = new Promise((resolve) => {
            if (window.YT && window.YT.Player) return resolve(window.YT);
            const prev = window.onYouTubeIframeAPIReady;
            window.onYouTubeIframeAPIReady = () => { if (prev) prev(); resolve(window.YT); };
            const s = document.createElement("script");
            s.src = "https://www.youtube.com/iframe_api";
            s.onerror = () => resolve(null);
            document.head.append(s);
            setTimeout(() => resolve(window.YT && window.YT.Player ? window.YT : null), 8000);
          });
          return api;
        }

        // Play ``v`` inside ``host``. ``onChange(item)`` fires when a fallback
        // upload takes over; ``onGiveUp()`` when nothing could be played.
        function mount(host, v, onChange, onGiveUp) {
          let player = null, dead = false, searched = false;
          const tried = new Set();
          let queue = (v.alternates || []).slice();

          function destroyPlayer() {
            if (player) { try { player.destroy(); } catch (e) {} player = null; }
          }
          async function next() {
            if (dead) return;
            let cand = queue.shift();
            while (cand && tried.has(cand.video_id)) cand = queue.shift();
            if (!cand && !searched && v.title) {
              searched = true;
              try {
                const d = await (await fetch("/yt/search?q=" + encodeURIComponent(v.title))).json();
                queue = (d.results || []).filter(r => !tried.has(r.video_id));
              } catch (e) { queue = []; }
              if (dead) return;
              cand = queue.shift();
            }
            if (!cand) { destroyPlayer(); onGiveUp(); return; }
            start(cand, false);
          }
          async function start(item, first) {
            tried.add(item.video_id);
            if (!first) onChange(item);
            const YT = await loadApi();
            if (dead) return;
            destroyPlayer();
            const holder = document.createElement("div");
            host.replaceChildren(holder);
            const vars = { autoplay: 1, rel: 0, modestbranding: 1, playsinline: 1 };
            if (item.list_id) { vars.list = item.list_id; vars.listType = "playlist"; }
            if (!YT) {  // API blocked/offline: plain embed, no fallback
              const f = document.createElement("iframe");
              f.src = "https://www.youtube-nocookie.com/embed/" + encodeURIComponent(item.video_id) + "?" + new URLSearchParams(vars);
              f.allow = "autoplay; encrypted-media; picture-in-picture; fullscreen";
              f.referrerPolicy = "strict-origin-when-cross-origin";
              f.allowFullscreen = true;
              f.title = item.title || "YouTube player";
              host.replaceChildren(f);
              return;
            }
            player = new YT.Player(holder, {
              host: "https://www.youtube-nocookie.com",
              videoId: item.video_id,
              playerVars: vars,
              events: { onError: () => next() },
            });
          }
          start(v, true);
          return { destroy() { dead = true; destroyPlayer(); host.replaceChildren(); } };
        }
        return { mount };
      })();

      // --- Stage: the news board in the big feed panel -------------------------
      // "news dikhao" / "aaj ki khabar" shows headlines here. The live camera
      // keeps running and shrinks to a corner; closing brings the feed back.
      const stage = (() => {
        const wrap = document.querySelector(".video-wrap");
        const box = document.getElementById("stage");
        const body = document.getElementById("stageBody");
        const kindEl = document.getElementById("stageKind");
        const titleEl = document.getElementById("stageTitle");
        const openEl = document.getElementById("stageOpen");
        const panel = document.getElementById("feedPanel");
        const maxBtn = document.getElementById("maxBtn");

        function show(kind, title, href) {
          kindEl.textContent = kind;
          titleEl.textContent = title || "";
          openEl.href = href || "#";
          openEl.hidden = !href;
          box.hidden = false;
          box.style.animation = "none";  // replay the reveal each time
          void box.offsetWidth;
          box.style.animation = "";
          wrap.classList.add("staged");
        }

        let board = null;  // last headlines shown, for the reader's back button
        let seq = 0;       // bumps on every view change; stale loads bail out

        function news(d) {
          board = d;
          seq++;
          const hindi = d.lang === "hi";
          const items = d.items || [];
          const grid = document.createElement("div");
          grid.className = "news-board";
          items.forEach((n, i) => {
            const a = document.createElement("a");
            a.className = i === 0 ? "nb-card nb-lead" : "nb-card";
            a.href = n.link;
            a.target = "_blank";
            a.rel = "noopener";
            a.style.animationDelay = (i * 50) + "ms";
            const num = document.createElement("span");
            num.className = "nb-num";
            num.textContent = String(i + 1).padStart(2, "0");
            const t = document.createElement(i === 0 ? "h3" : "b");
            t.className = "nb-title";
            t.textContent = n.title;
            const meta = document.createElement("div");
            meta.className = "nb-meta";
            const src = document.createElement("b");
            src.textContent = n.source || "";
            const when = timeAgo(n.published, hindi);
            meta.append(src, when ? " · " + when : "");
            a.append(num, t, meta);
            openInReader(a, n);
            grid.append(a);
          });

          // Scrolling headline ticker along the bottom (text doubled so the
          // loop is seamless).
          const ticker = document.createElement("div");
          ticker.className = "nb-ticker";
          const live = document.createElement("span");
          live.className = "nb-live";
          live.textContent = hindi ? "लाइव" : "LIVE";
          const track = document.createElement("div");
          track.className = "nb-track";
          const run = document.createElement("span");
          run.className = "nb-run";
          run.textContent = items.map(n => n.title).join("   ◆   ") + "   ◆   ";
          run.style.animationDuration = Math.max(30, items.length * 7) + "s";
          track.append(run, run.cloneNode(true));
          ticker.append(live, track);

          body.replaceChildren(grid, ticker);
          const title = d.topic
            ? (hindi ? d.topic + " — ताज़ा खबरें" : d.topic + " — latest news")
            : (hindi ? "आज की मुख्य खबरें" : "Today's top headlines");
          const href = d.topic
            ? "https://news.google.com/search?q=" + encodeURIComponent(d.topic)
            : "https://news.google.com/";
          show(hindi ? "समाचार" : "NEWS", title, href);
        }

        // Plain click opens the story here; Ctrl/Shift/middle-click keep the
        // normal new-tab behaviour.
        function openInReader(a, n, ctx) {
          a.addEventListener("click", (e) => {
            if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey) return;
            e.preventDefault();
            article(n, ctx);
          });
        }

        // Reader view: the story's text fetched by the server (news sites
        // refuse to be framed), styled like the dashboard.
        async function article(n, ctx) {
          if (ctx) board = ctx;
          const my = ++seq;
          const hindi = board ? board.lang === "hi" : isHindiUI();
          const view = document.createElement("article");
          view.className = "reader";
          const back = document.createElement("button");
          back.type = "button";
          back.className = "reader-back";
          back.textContent = hindi ? "← खबरें" : "← HEADLINES";
          back.addEventListener("click", () => (board ? news(board) : close()));
          const status = document.createElement("p");
          status.className = "reader-status";
          status.textContent = hindi ? "खबर लोड हो रही है..." : "Loading story...";
          view.append(back, status);
          body.replaceChildren(view);
          show(hindi ? "समाचार" : "NEWS", n.title, n.link);

          let d;
          try {
            d = await (await fetch("/news/article?link=" + encodeURIComponent(n.link))).json();
          } catch (e) { d = { ok: false }; }
          if (my !== seq) return;  // the user moved on meanwhile
          status.remove();
          if (d.ok && d.url) openEl.href = d.url;

          if (d.ok && d.image) {
            const img = document.createElement("img");
            img.className = "reader-hero";
            img.src = d.image;
            img.alt = "";
            img.referrerPolicy = "no-referrer";
            img.onerror = () => img.remove();
            view.append(img);
          }
          const h = document.createElement("h1");
          h.className = "reader-title";
          h.textContent = (d.ok && d.title) || n.title;
          const meta = document.createElement("div");
          meta.className = "reader-meta";
          const src = document.createElement("b");
          src.textContent = (d.ok && d.site) || n.source || "";
          const when = timeAgo((d.ok && d.published) || n.published, hindi);
          meta.append(src, when ? " · " + when : "");
          view.append(h, meta);

          const paras = (d.ok && d.paragraphs) || [];
          if (paras.length) {
            paras.forEach((t) => {
              const p = document.createElement("p");
              p.textContent = t;
              view.append(p);
            });
          } else {
            if (d.ok && d.description) {
              const lead = document.createElement("p");
              lead.className = "reader-lead";
              lead.textContent = d.description;
              view.append(lead);
            }
            const note = document.createElement("p");
            note.className = "reader-status";
            note.textContent = hindi
              ? "इस साइट का पूरा लेख यहाँ नहीं दिख सकता।"
              : "The full text of this story can't be shown here.";
            view.append(note);
          }
          const site = (d.ok && d.site) || n.source || "";
          const orig = document.createElement("a");
          orig.className = "reader-orig";
          orig.href = (d.ok && d.url) || n.link;
          orig.target = "_blank";
          orig.rel = "noopener";
          orig.textContent = hindi ? "मूल लेख पढ़ें ↗" : "Read on " + site + " ↗";
          view.append(orig);
        }

        function close() {
          seq++;
          body.replaceChildren();
          box.hidden = true;
          wrap.classList.remove("staged");
        }
        document.getElementById("stageClose").addEventListener("click", () => {
          close();
          resumeAutoMic();
        });

        // Full screen: the whole dashboard goes browser-fullscreen, so every
        // panel (commands, logs, AI output, detections) stays visible.
        // Resolves false when the browser refuses (a voice command has no
        // click behind it, and browsers only allow fullscreen after one).
        const isMax = () => document.fullscreenElement === document.documentElement;
        async function setMax(on) {
          try {
            if (on && !isMax()) await document.documentElement.requestFullscreen();
            else if (!on && document.fullscreenElement) await document.exitFullscreen();
            return true;
          } catch (e) { return false; }
        }
        document.addEventListener("fullscreenchange", () => {
          maxBtn.setAttribute("aria-pressed", isMax() ? "true" : "false");
          window.dispatchEvent(new Event("resize"));  // canvases re-measure
        });
        maxBtn.addEventListener("click", () => setMax(!isMax()));

        return { news, article, openInReader, close, setMax };
      })();

      // --- Songs & naats: menu + player in the AI output panel ----------------
      const mediaPlayer = (() => {
        const box = document.getElementById("mediaPlayer");
        const frame = document.getElementById("mediaFrame");
        const titleEl = document.getElementById("mediaTitle");
        const openEl = document.getElementById("mediaOpen");
        let handle = null, playing = false;

        function setInfo(v) {
          titleEl.textContent = v.title || "";
          openEl.href = v.url || ("https://www.youtube.com/watch?v=" + v.video_id);
        }
        function play(song) {
          if (!song || !song.video_id) return;
          if (handle) handle.destroy();
          setInfo(song);
          box.hidden = false;
          playing = true;
          handle = ytPlayer.mount(frame, song, setInfo, () => {
            playing = false;
            const p = document.createElement("p");
            p.className = "media-note";
            p.textContent = "This video can't be played here. Use YT ↗ to watch it on YouTube.";
            frame.replaceChildren(p);
          });
        }
        function stop() {
          if (handle) handle.destroy();
          handle = null;
          box.hidden = true;
          playing = false;
        }
        document.getElementById("mediaClose").addEventListener("click", () => {
          stop();
          resumeAutoMic();
        });

        // Playlist picker: numbered cards; tap one or say its number.
        const menu = document.getElementById("mediaMenu");
        const list = document.getElementById("mediaList");
        let choices = [];
        function showMenu(songs, category) {
          choices = songs || [];
          document.getElementById("mediaMenuLabel").textContent =
            category === "quran" ? "QURAN · TILAWAT" : category === "naat" ? "SELECT_NAAT" : "SELECT_PLAYLIST";
          menu.classList.toggle("quran", category === "quran");
          list.replaceChildren(...choices.map((song, i) => {
            const li = document.createElement("li");
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "media-item";
            btn.style.animationDelay = (i * 70) + "ms";
            const thumb = document.createElement("span");
            thumb.className = "media-thumb";
            const img = document.createElement("img");
            img.src = "https://i.ytimg.com/vi/" + encodeURIComponent(song.video_id) + "/mqdefault.jpg";
            img.alt = "";
            img.loading = "lazy";
            const num = document.createElement("span");
            num.className = "media-num";
            num.textContent = i + 1;
            thumb.append(img, num);
            const info = document.createElement("span");
            info.className = "media-info";
            const t = document.createElement("b");
            t.textContent = song.title;
            const ch = document.createElement("span");
            ch.textContent = song.channel || "";
            info.append(t, ch);
            btn.append(thumb, info);
            btn.addEventListener("click", () => choose(i));
            li.append(btn);
            return li;
          }));
          menu.hidden = false;
        }
        function hideMenu() { menu.hidden = true; choices = []; }
        // (The 📖 header button opens its own Quran window: see "Quran window".)
        document.getElementById("mediaMenuClose").addEventListener("click", hideMenu);

        // Announce, then play (so Sara's voice doesn't talk over the music).
        async function choose(i) {
          const song = choices[i];
          if (!song) return false;
          hideMenu();
          const hindi = isHindiUI();
          setAiResponse(hindi ? "चला रही हूँ: " + song.title : "Playing " + song.title + " for you.");
          await speakOut(taskDoneLine(true, hindi) + " " +
            (hindi ? "आपके लिए चला रही हूँ: " + song.title : "Playing " + song.title + " for you."));
          play(song);
          return true;
        }

        // "2", "number 2", "second", "do", "दूसरा", "teesra chalao"... -> 1-based index, else 0.
        const WORDS = [
          ["1", "one", "first", "ek", "pehla", "pahla", "एक", "पहला"],
          ["2", "two", "second", "do", "doosra", "dusra", "दो", "दूसरा"],
          ["3", "three", "third", "teen", "teesra", "tisra", "तीन", "तीसरा"],
          ["4", "four", "fourth", "char", "chaar", "chautha", "चार", "चौथा"],
          ["5", "five", "fifth", "panch", "paanch", "pachva", "पांच", "पाँच", "पांचवा"],
          ["6", "six", "sixth", "chhe", "chhah", "chhatha", "छह", "छठा"],
        ];
        function pickNumber(text) {
          const tokens = text.toLowerCase().replace(/[^\\p{L}\\p{N}\\s]/gu, " ").split(/\\s+/).filter(Boolean);
          if (!tokens.length || tokens.length > 4) return 0;  // only short replies
          for (let n = 0; n < WORDS.length; n++) {
            if (tokens.some(tok => WORDS[n].includes(tok))) return n + 1;
          }
          return 0;
        }
        async function chooseFromText(text) {
          if (menu.hidden) return false;
          const n = pickNumber(text);
          return n ? choose(n - 1) : false;
        }

        return { play, stop, showMenu, hideMenu, chooseFromText, get playing() { return playing; } };
      })();
      const camToggle = document.getElementById("camToggle");
      const cameraSelect = document.getElementById("cameraSelect");
      const runBtn = document.getElementById("runBtn");
      const voiceBtn = document.getElementById("voiceBtn");
      const cmdInput = document.getElementById("cmdInput");
      const aiInput = document.getElementById("aiInput");
      const aiBtn = document.getElementById("aiBtn");
      const aiBrain = document.getElementById("aiBrain");

      const headerPanel = document.getElementById("headerPanel");
      const mainPanel = document.getElementById("mainPanel");

      let afterId = 0;
      let pollTimer = null;
      let stream = null;

      // --- Typewriter for AI replies (progress shown via the slim proc-bar) ---
      const procBar = document.getElementById("procBar");
      let typeTimer = null;
      let lastAiText = "";

      function thinkingHTML(label) {
        return '<span class="thinking"><span class="dots-bounce"><i></i><i></i><i></i></span>'
          + '<span class="label">' + label + '</span></span>';
      }

      function showThinking(label) {
        if (typeTimer) { clearInterval(typeTimer); typeTimer = null; }
        lastAiText = "";
        aiBrain.classList.remove("type-caret");
        aiBrain.innerHTML = thinkingHTML(label);
        if (procBar) procBar.classList.add("active");
        aiVisual.setState("thinking");
      }

      function typeOut(text) {
        if (typeTimer) { clearInterval(typeTimer); typeTimer = null; }
        if (procBar) procBar.classList.remove("active");
        if (aiVisual.state === "thinking") aiVisual.setState("idle");
        aiBrain.textContent = "";
        aiBrain.classList.add("type-caret");
        const step = Math.max(1, Math.round(text.length / 120)); // ~constant duration
        let i = 0;
        typeTimer = setInterval(() => {
          i += step;
          aiBrain.textContent = text.slice(0, i);
          aiBrain.scrollTop = aiBrain.scrollHeight;
          if (i >= text.length) {
            clearInterval(typeTimer); typeTimer = null;
            aiBrain.textContent = text;
            aiBrain.classList.remove("type-caret");
          }
        }, 18);
      }

      // Render an AI reply with the typewriter effect, skipping duplicates so a
      // polled event doesn't re-type a message we just showed.
      function setAiResponse(text) {
        if (!text || text === lastAiText) return;
        lastAiText = text;
        typeOut(text);
      }

      // Auto-connect events from the server: banner + spoken message.
      // Only events newer than page load are announced (no replay on refresh).
      const pageLoadedAt = Date.now() / 1000;
      function announceBluetooth(e) {
        if (e.ts && e.ts < pageLoadedAt) return;
        const name = e.message || "Bluetooth device";
        const on = e.kind === "bt_connected";
        const hindi = isHindiUI();
        showNotice((on ? "🎧 " : "🔌 ") + name + (on ? " connected" : " disconnected"),
                   on ? "Bluetooth · auto-connect" : "Bluetooth · out of range or switched off");
        showBtToast(name, on);
        refreshBtStatus(on ? name : null);
        if (on) {
          speakOut(hindi ? "आपका " + name + " कनेक्ट हो गया है।" : "Your " + name + " is connected.");
        }
      }
      // Commands from the phone remote (/remote): act on them here like a
      // command typed on the laptop, and speak the reply on the laptop.
      async function handlePhoneEvent(e) {
        let d;
        try { d = JSON.parse(e.message); } catch (err) { return; }
        if (d.event === "connected") {
          logsEl.textContent += "[PHONE] Connected: " + (d.device || "phone") + "\\n";
          if (!(e.ts && e.ts < pageLoadedAt)) showNotice("📱 Phone connected", (d.device || "Phone") + " · remote input ready");
          if (window.phonePanel) window.phonePanel.refresh();
          return;
        }
        logsEl.textContent += "[PHONE] > " + d.text + "\\n[PHONE] " + d.message + "\\n";
        logsEl.scrollTop = logsEl.scrollHeight;
        if (e.ts && e.ts < pageLoadedAt) return;          // don't replay old commands
        if (window.phonePanel) window.phonePanel.last(d.text, d.message);
        showNotice("📱 " + d.text, "From your phone");
        setAiResponse(d.message);
        const hindi = DEVANAGARI.test(d.message || "") || DEVANAGARI.test(d.text || "");
        const line = await replyFor({ ok: d.ok, message: d.message, kind: d.kind, data: d.data }, hindi);
        speakOut(line).then(() => afterSpeech({ kind: d.kind, data: d.data }));
      }

      // On-screen alert inside the video panel (camera and globe screens).
      function showBtToast(name, on) {
        const t = document.getElementById("btToast");
        if (!t) return;
        t.className = "bt-toast" + (on ? "" : " off");
        t.replaceChildren();
        const ico = document.createElement("div"); ico.className = "bt-t-ico"; ico.textContent = on ? "🎧" : "🔌";
        const body = document.createElement("div");
        const nm = document.createElement("div"); nm.className = "bt-t-name"; nm.textContent = name;
        const sub = document.createElement("div"); sub.className = "bt-t-sub";
        sub.textContent = on ? "Bluetooth · connected" : "Bluetooth · disconnected";
        body.append(nm, sub);
        const time = document.createElement("div"); time.className = "bt-t-time";
        time.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        t.append(ico, body, time);
        void t.offsetWidth;            // restart the slide-in
        t.classList.add("show");
        clearTimeout(t._t);
        t._t = setTimeout(() => t.classList.remove("show"), 6000);
      }

      // Bluetooth status row in the globe info panel. Refreshed every 15 s and
      // right after a connect / disconnect event.
      let btStatusBusy = false;
      async function refreshBtStatus(justConnected) {
        const row = document.getElementById("giBt"), text = document.getElementById("giBtText");
        if (!row || btStatusBusy) return;
        btStatusBusy = true;
        try {
          const st = await (await fetch("/bluetooth/state")).json();
          const conn = (st.paired || []).filter(d => d.connected);
          const auto = st.auto || [];
          row.classList.toggle("on", conn.length > 0);
          if (!st.available) text.textContent = "Not available";
          else if (st.radio === false) text.textContent = "Bluetooth off";
          else if (conn.length) {
            // The green dot already says "connected"; keep the names readable.
            text.textContent = conn.map(d => (auto.includes(d.mac) ? "★ " : "") + d.name).join(", ");
          } else {
            const starred = (st.paired || []).filter(d => auto.includes(d.mac)).map(d => d.name);
            text.textContent = starred.length ? "Waiting for " + starred.join(", ") : "No device connected";
          }
          row.title = conn.length ? "Connected: " + conn.map(d => d.name).join(", ") : text.textContent;
          if (justConnected) {
            row.classList.remove("flash"); void row.offsetWidth; row.classList.add("flash");
          }
        } catch (e) {
          text.textContent = "Offline";
        } finally {
          btStatusBusy = false;
        }
      }
      refreshBtStatus();
      setInterval(() => { if (!document.hidden) refreshBtStatus(); }, 15000);

      // Banner at the top of the screen (same style as the welcome toast).
      function showNotice(title, sub) {
        const toast = document.getElementById("welcomeToast");
        if (!toast) return;
        toast.innerHTML = '<span class="wt-title"></span><span class="wt-sub"></span>';
        toast.querySelector(".wt-title").textContent = title;
        toast.querySelector(".wt-sub").textContent = sub;
        toast.classList.add("show");
        clearTimeout(toast._t);
        toast._t = setTimeout(() => toast.classList.remove("show"), 5000);
      }

      // New Gmail / WhatsApp message: Sara says who it's from (and the
      // subject / text); returns the line for the activity log.
      function announceInbox(e) {
        let m;
        try { m = JSON.parse(e.message); } catch (err) { return e.message; }
        const hi = isHindiUI();
        let line;
        if (m.source === "error") {
          line = hi ? `${m.sender} अलर्ट चालू नहीं हो पाए: ${m.text}` : `${m.sender} alerts could not start: ${m.text}`;
        } else if (m.source === "email") {
          // Only a nudge to check the inbox; who sent it stays in the log.
          const who = ((profile && profile.name) ? profile.name + " " : "") + honorific(hi);
          const say = hi ? `${who}, नया ईमेल आया है। कृपया अपना ईमेल चेक कीजिए।`
                         : `Please check your email, I got a new mail. Please check, ${who}.`;
          if (typeof speakOut === "function") { try { speakOut(say); } catch (err) {} }
          return say + ` (${m.sender}: ${m.text})`;
        } else {
          line = hi ? `${m.sender} का व्हाट्सऐप मैसेज आया है: ${m.text}` : `New WhatsApp message from ${m.sender}: ${m.text}`;
        }
        if (m.source !== "error" && typeof speakOut === "function") { try { speakOut(line); } catch (err) {} }
        return line;
      }

      async function pollLogs() {
        try {
          const r = await fetch(`/events?after_id=${afterId}`);
          const data = await r.json();
          for (let e of data.events) {
            afterId = Math.max(afterId, e.id);
            if (e.kind === 'bt_connected' || e.kind === 'bt_disconnected') announceBluetooth(e);
            // Timed sleep / wake notices ("1 minute mein system sleep...", "Good morning").
            if (e.kind === 'power' && typeof speakOut === 'function') { try { speakOut(e.message); } catch (err) {} }
            if (e.kind === 'phone') { handlePhoneEvent(e); continue; }
            if (e.kind === 'inbox') { e = { ...e, message: announceInbox(e) }; }
            if (e.kind === 'ai_task') {
               setAiResponse(e.message);
               aiBrain.style.boxShadow = "inset 0 0 30px rgba(59, 130, 246, 0.2)";
               setTimeout(() => aiBrain.style.boxShadow = "inset 0 0 20px rgba(59, 130, 246, 0.05)", 1000);
            }
            logsEl.textContent += `[${e.kind.toUpperCase()}] ${e.message}\\n`;
            logsEl.scrollTop = logsEl.scrollHeight;
          }
        } catch {}
      }

      // --- System Logs: BACKEND pane (server requests + Python warnings/errors) ---
      (() => {
        const box = document.getElementById("beLogs");
        if (!box) return;
        const onlyIssues = document.getElementById("beOnlyIssues");
        const warnEl = document.getElementById("beWarn"), errEl = document.getElementById("beErr");
        let after = 0, warns = 0, errs = 0;
        const MAX_ROWS = 300;
        const two = (n) => String(n).padStart(2, "0");
        function badges() {
          warnEl.hidden = !warns; warnEl.textContent = warns + " WARN";
          errEl.hidden = !errs; errEl.textContent = errs + " ERR";
        }
        function row(e) {
          const d = new Date(e.ts * 1000);
          const r = document.createElement("div");
          r.className = "be-row " + e.level.toLowerCase();
          const t = document.createElement("span"); t.className = "t";
          t.textContent = two(d.getHours()) + ":" + two(d.getMinutes()) + ":" + two(d.getSeconds());
          const lv = document.createElement("span"); lv.className = "lv";
          lv.textContent = e.level === "WARNING" ? "WARN" : e.level === "ERROR" ? "ERR" : e.source === "http" ? "HTTP" : "INFO";
          const m = document.createElement("span"); m.className = "m"; m.title = e.message;
          const hit = e.source === "http" ? e.message.match(/^(.* )([0-9]{3})( [0-9]+ms)$/) : null;
          if (hit) {
            const code = Number(hit[2]);
            const c = document.createElement("span"); c.className = code >= 500 ? "e" : code >= 400 ? "w" : "ok"; c.textContent = hit[2];
            const ms = document.createElement("span"); ms.textContent = hit[3];
            if (parseInt(hit[3], 10) > 1000) ms.className = "w";      // slow request
            m.append(hit[1], c, ms);
          } else {
            if (e.source !== "http") { const s = document.createElement("span"); s.className = "src"; s.textContent = e.source + ": "; m.append(s); }
            m.append(e.message);
          }
          r.append(t, lv, m);
          r.hidden = onlyIssues.checked && e.level === "INFO";
          return r;
        }
        async function poll() {
          try {
            const d = await (await fetch("/logs/backend?after=" + after)).json();
            if (!d.logs || !d.logs.length) return;
            const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
            box.querySelector(".be-empty")?.remove();
            for (const e of d.logs) {
              after = Math.max(after, e.id);
              if (e.level === "WARNING") warns++; else if (e.level === "ERROR") errs++;
              box.append(row(e));
            }
            while (box.childElementCount > MAX_ROWS) box.firstElementChild.remove();
            badges();
            if (stick) box.scrollTop = box.scrollHeight;
          } catch (e) {}
        }
        onlyIssues.addEventListener("change", () => {
          box.querySelectorAll(".be-row.info").forEach(r => { r.hidden = onlyIssues.checked; });
          box.scrollTop = box.scrollHeight;
        });
        document.getElementById("beClear").addEventListener("click", () => {
          box.replaceChildren(); warns = 0; errs = 0; badges();
        });
        const empty = document.createElement("div"); empty.className = "be-empty"; empty.textContent = "Backend se jud rahe hain…";
        box.append(empty);
        poll();
        setInterval(poll, 2000);
      })();
      document.getElementById("actClear")?.addEventListener("click", () => { logsEl.textContent = ""; });

      async function loadCameras() {
        try {
          const devices = await navigator.mediaDevices.enumerateDevices();
          const videoDevices = devices.filter(device => device.kind === 'videoinput');
          const currentVal = cameraSelect.value;
          
          cameraSelect.innerHTML = '<option value="">Default Camera</option>';
          videoDevices.forEach((device, index) => {
            const option = document.createElement('option');
            option.value = device.deviceId;
            option.text = device.label || `Camera ${index + 1}`;
            cameraSelect.appendChild(option);
          });
          cameraSelect.value = currentVal;
        } catch (e) {}
      }

      // --- Target tracking: MediaPipe face + object detectors (native
      // FaceDetector as a fallback) drive animated AF lock-on boxes drawn over
      // the live feed. Faces run every frame; objects a few times a second. ---
      const faceTrack = (() => {
        const MP = "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14";
        const FACE_MODEL = "https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite";
        const OBJECT_MODEL = "https://storage.googleapis.com/mediapipe-models/object_detector/efficientdet_lite0/float16/1/efficientdet_lite0.tflite";
        const MESH_MODEL = "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task";
        const OBJECT_EVERY_MS = 300, MESH_EVERY_MS = 50;
        // Sparse subset of the 478 face landmarks for the "scan mesh" look:
        // face outline, brows, eyes, nose, cheeks, lips, chin.
        const MESH_IDX = [10, 338, 332, 389, 454, 361, 397, 378, 152, 149, 172, 132, 234, 162, 103, 109,
          151, 9, 70, 105, 107, 336, 334, 300, 33, 133, 159, 145, 362, 263, 386, 374,
          168, 6, 1, 98, 327, 50, 280, 205, 425, 61, 291, 0, 17, 199, 58, 288];
        let meshes = [];        // latest landmark sets, in video pixels
        let meshEdges = null;   // [i, j] pairs, built once from the first face
        const wrap = document.querySelector(".video-wrap");
        const canvas = document.getElementById("faceCanvas");
        const ctx = canvas.getContext("2d");
        const trackEl = document.getElementById("hudTrack");
        const facesEl = document.getElementById("hudFaces");
        const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        let detect = null;      // (video, now) => [{label, x, y, w, h, score, points}] in video pixels
        let loading = null;
        let running = false, rafId = null, busy = false, lastVideoTime = -1;
        let tracks = [], nextId = 1;
        let detectErrors = 0;   // face-detector failures in a row (shown in the HUD)

        // --- Detection notifications --------------------------------------
        // Each kind of object (face, person, cell phone...) is announced ONE
        // time per camera session: a row in "Live detections" plus a spoken
        // line. Turning the camera off and on again starts a fresh session.
        const NOTIFY = {
          face:   { title: "Face detected",   icon: '<circle cx="12" cy="10" r="4"/><path d="M3 7V5a2 2 0 0 1 2-2h2M17 3h2a2 2 0 0 1 2 2v2M21 17v2a2 2 0 0 1-2 2h-2M7 21H5a2 2 0 0 1-2-2v-2"/><path d="M8 18c1-1.5 2.5-2 4-2s3 .5 4 2"/>' },
          person: { title: "Person detected", icon: '<circle cx="12" cy="5" r="3"/><path d="M12 8v7M8 11h8M9 22l3-7 3 7"/>' },
        };
        const OBJECT_ICON = '<path d="M21 8 12 3 3 8v8l9 5 9-5z"/><path d="m3 8 9 5 9-5M12 13v8"/>';
        const HINDI_NAMES = {
          face: "चेहरा", person: "व्यक्ति", "cell phone": "मोबाइल फ़ोन", laptop: "लैपटॉप", cup: "कप",
          bottle: "बोतल", chair: "कुर्सी", book: "किताब", keyboard: "कीबोर्ड", mouse: "माउस",
          tv: "टीवी", remote: "रिमोट", dog: "कुत्ता", cat: "बिल्ली", backpack: "बैग", clock: "घड़ी",
        };
        const MIN_OBJECT_SCORE = 0.6;   // ignore shaky object guesses
        const announced = new Set();    // kinds already announced this session
        let voiceQueue = [], lastQueued = 0;
        function notifyCfg(kind) {
          return NOTIFY[kind] || { title: kind.charAt(0).toUpperCase() + kind.slice(1) + " detected", icon: OBJECT_ICON };
        }
        const toastBox = document.getElementById("detectToasts");
        const detectBox = document.getElementById("detectBox");
        const detectCount = document.getElementById("detectCount");
        const EMPTY_HTML = '<li class="detect-empty">No detections yet. Turn on the camera to start.</li>';
        const MAX_ROWS = 40;    // the list scrolls; snapshots are also saved on disk
        let detectTotal = 0;

        function showEmptyIfNone() {
          if (toastBox && !toastBox.querySelector(".detect-toast")) toastBox.innerHTML = EMPTY_HTML;
        }
        const clearBtn = document.getElementById("detectClear");
        if (clearBtn) clearBtn.addEventListener("click", () => {
          detectTotal = 0;
          if (detectCount) detectCount.textContent = "0";
          if (toastBox) toastBox.innerHTML = EMPTY_HTML;
        });

        // Add a row to the "Live detections" box (newest on top).
        function showDetectToast(kind, track, count, thumb) {
          if (!toastBox) return;
          const cfg = notifyCfg(kind);
          const empty = toastBox.querySelector(".detect-empty");
          if (empty) empty.remove();
          toastBox.querySelectorAll(".detect-toast.fresh").forEach(r => r.classList.remove("fresh"));

          const el = document.createElement("li");
          el.className = "detect-toast fresh " + (NOTIFY[kind] ? kind : "object");
          const pct = track.score != null ? Math.round(track.score * 100) : null;
          const time = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
          el.innerHTML =
            '<div class="dt-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + cfg.icon + '</svg></div>' +
            '<div class="dt-main"><div class="dt-title"><span class="dt-name"></span><span class="dt-time"></span></div>' +
            '<div class="dt-meta"><span class="dt-id"></span>' +
            (pct != null ? '<span class="dt-conf" title="Confidence"><i style="width:' + pct + '%"></i></span><span>' + pct + '%</span>' : '') +
            '</div></div>' +
            '<button class="dt-close" type="button" aria-label="Remove">×</button>';
          if (thumb) {
            const icon = el.querySelector(".dt-icon");
            icon.classList.add("has-thumb");
            icon.innerHTML = "";
            const img = document.createElement("img");
            img.src = thumb; img.alt = "Face capture";
            icon.appendChild(img);
          }
          el.querySelector(".dt-name").textContent = cfg.title + (count > 1 ? " (" + count + ")" : "");
          el.querySelector(".dt-time").textContent = time;
          el.querySelector(".dt-id").textContent = kind.toUpperCase().replace(/ +/g, "_") + "_" + String(track.id).padStart(2, "0");
          el.querySelector(".dt-close").addEventListener("click", () => {
            el.classList.add("leaving");
            setTimeout(() => { el.remove(); showEmptyIfNone(); }, 250);
          });
          toastBox.prepend(el);
          while (toastBox.querySelectorAll(".detect-toast").length > MAX_ROWS) toastBox.lastElementChild.remove();
          toastBox.scrollTop = 0;
          detectTotal += 1;
          if (detectCount) detectCount.textContent = String(detectTotal);
          return el;
        }

        function checkNotify(now) {
          // Only count targets that have finished locking on (~550 ms); other
          // objects also need a confident score.
          const ready = tracks.filter(t => now - t.born >= 550 &&
            (NOTIFY[t.label] || (t.score || 0) >= MIN_OBJECT_SCORE));

          // Faces: EVERY appearance is logged with a snapshot (new face, or the
          // face back after >= 3 s away; at most one row per 10 s while it
          // flickers). Spoken only once per camera session (below).
          const faces = ready.filter(t => t.label === "face");
          for (const f of faces) {
            if (loggedFaces.has(f.id)) continue;
            loggedFaces.add(f.id);
            if (now - faceLastSeen > FACE_AWAY_MS || now - lastFaceLog > FACE_MIN_GAP_MS) {
              lastFaceLog = now;
              logFace(f, now, faces.length);
            }
          }
          if (faces.length) faceLastSeen = now;

          // Persons / objects: one row + voice per kind per camera session.
          for (const kind of new Set(ready.map(t => t.label))) {
            if (announced.has(kind)) continue;
            announced.add(kind);
            if (kind !== "face") {
              const same = ready.filter(t => t.label === kind);
              const best = same.reduce((a, b) => ((b.score || 0) > (a.score || 0) ? b : a));
              showDetectToast(kind, best, same.length, null);
            }
            voiceQueue.push(kind);
            lastQueued = now;
          }
          flushVoice(now);
        }

        const FACE_AWAY_MS = 3000, FACE_MIN_GAP_MS = 10000;
        const loggedFaces = new Set();
        let lastFaceLog = -Infinity, faceLastSeen = -Infinity, faceLogCount = 0;

        // Snapshot (>= 20% confidence, 20% margin) -> detections row, System
        // Logs line, and a JPEG saved on the laptop in captures/<date>/.
        function logFace(f, now, count) {
          let thumb = null;
          if ((f.score || 0) >= CAPTURE_MIN_SCORE) {
            const cap = makeCapture(f, now);
            if (cap) { thumb = cap.img.toDataURL("image/jpeg", 0.85); capture = cap; }
          }
          const row = showDetectToast("face", f, count, thumb);
          faceLogCount++;
          const id = "FACE_" + String(f.id).padStart(2, "0");
          const pct = f.score != null ? Math.round(f.score * 100) + "%" : "";
          const stamp = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
          if (!thumb) {
            logsEl.textContent += "[FACE] " + id + " " + pct + " · " + stamp + " (no snapshot: low confidence)\\n";
            return;
          }
          fetch("/captures/save", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ image: thumb, label: id, score: f.score }),
          }).then(r => r.json()).then(d => {
            logsEl.textContent += "[FACE] " + id + " " + pct + " · " + stamp + (d.ok ? " · saved " + d.path : " · not saved") + "\\n";
            logsEl.scrollTop = logsEl.scrollHeight;
            // Click the thumbnail to open the full-size snapshot.
            const icon = row && row.querySelector(".dt-icon.has-thumb");
            if (d.ok && icon) {
              icon.style.cursor = "zoom-in";
              icon.title = "Open snapshot · " + d.path;
              icon.addEventListener("click", () => window.open(d.url, "_blank", "noopener"));
            }
          }).catch(() => {});
        }

        // Speak queued detections as one short line, only when Sara is idle
        // (not listening, thinking or already talking) and no song is playing.
        // Things seen within ~1s of each other are grouped together.
        function flushVoice(now) {
          if (!voiceQueue.length || now - lastQueued < 1000) return;
          let busy = aiVisual.state !== "idle";
          try { busy = busy || (mediaPlayer && mediaPlayer.playing) || !!window.quranPlaying || !!window.radioPlaying; } catch (e) {}
          if (busy) return;
          const kinds = voiceQueue.splice(0);
          const hindi = isHindiUI();
          const names = kinds.map(k => hindi ? (HINDI_NAMES[k] || k) : k);
          const list = names.length > 1
            ? names.slice(0, -1).join(", ") + (hindi ? " और " : " and ") + names[names.length - 1]
            : names[0];
          speakOut(hindi ? list + " डिटेक्ट हुआ।" : "Detected " + list + ".");
        }

        function rgba(hex, a) {
          const n = parseInt(hex.slice(1), 16);
          return "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + a + ")";
        }

        async function load() {
          try {
            const { FilesetResolver, FaceDetector, ObjectDetector, FaceLandmarker } = await import(MP + "/vision_bundle.mjs");
            const fileset = await FilesetResolver.forVisionTasks(MP + "/wasm");
            // CPU delegate: the GPU path returns garbage boxes on some
            // graphics drivers, and these small models are fast enough on CPU.
            const fd = await FaceDetector.createFromOptions(fileset, {
              baseOptions: { modelAssetPath: FACE_MODEL, delegate: "CPU" },
              runningMode: "VIDEO",
              minDetectionConfidence: 0.6,
            });
            // Objects are optional: if that model fails, keep tracking faces.
            const od = await ObjectDetector.createFromOptions(fileset, {
              baseOptions: { modelAssetPath: OBJECT_MODEL, delegate: "CPU" },
              runningMode: "VIDEO",
              scoreThreshold: 0.5,
              maxResults: 8,
            }).catch(() => null);
            // Face mesh is optional too: without it faces fall back to boxes.
            const fl = await FaceLandmarker.createFromOptions(fileset, {
              baseOptions: { modelAssetPath: MESH_MODEL, delegate: "CPU" },
              runningMode: "VIDEO",
              numFaces: 3,
            }).catch(() => null);
            let objects = [], lastObjects = -Infinity, lastMesh = -Infinity;
            // Each model runs in its own try: a failing mesh / object frame
            // must never stop face detection (that made faces "disappear").
            let meshFails = 0, lastTs = 0;
            return (video, now) => {
              const ts = Math.max(now, lastTs + 1);   // MediaPipe needs increasing timestamps
              lastTs = ts;
              let faces = [];
              try {
                faces = fd.detectForVideo(video, ts).detections.map(d => ({
                  label: "face",
                  x: d.boundingBox.originX, y: d.boundingBox.originY,
                  w: d.boundingBox.width, h: d.boundingBox.height,
                  score: d.categories[0] ? d.categories[0].score : null,
                  points: (d.keypoints || []).map(k => ({ x: k.x * video.videoWidth, y: k.y * video.videoHeight })),
                }));
                detectErrors = 0;
              } catch (e) {
                detectErrors++;
              }
              if (fl && meshFails < 20 && now - lastMesh >= MESH_EVERY_MS) {
                lastMesh = now;
                try {
                  const vw = video.videoWidth, vh = video.videoHeight;
                  meshes = (fl.detectForVideo(video, ts).faceLandmarks || []).map(lm =>
                    MESH_IDX.map(i => ({ x: lm[i].x * vw, y: lm[i].y * vh })));
                  meshFails = 0;
                } catch (e) {
                  meshes = [];
                  meshFails++;                         // after 20 failures in a row: boxes only
                }
              }
              if (od && now - lastObjects >= OBJECT_EVERY_MS) {
                lastObjects = now;
                try {
                  objects = od.detectForVideo(video, ts).detections.map(d => ({
                    label: (d.categories[0] && d.categories[0].categoryName) || "object",
                    x: d.boundingBox.originX, y: d.boundingBox.originY,
                    w: d.boundingBox.width, h: d.boundingBox.height,
                    score: d.categories[0] ? d.categories[0].score : null,
                    points: [],
                  }));
                } catch (e) { objects = []; }
              }
              return faces.concat(objects);
            };
          } catch (e) {
            // Offline / CDN blocked: fall back to the browser's own detector if present.
            if (!("FaceDetector" in window)) return null;
            const fd = new window.FaceDetector({ fastMode: true, maxDetectedFaces: 5 });
            return async video => (await fd.detect(video)).map(f => ({
              label: "face",
              x: f.boundingBox.x, y: f.boundingBox.y, w: f.boundingBox.width, h: f.boundingBox.height,
              score: null, points: (f.landmarks || []).flatMap(l => l.locations || []),
            }));
          }
        }

        // Drop degenerate detections: tiny, off-frame or non-numeric boxes
        // (and, for faces, implausible shapes).
        function valid(f) {
          const vw = videoEl.videoWidth, vh = videoEl.videoHeight;
          const isFace = f.label === "face";
          const minSide = Math.min(vw, vh) * (isFace ? 0.06 : 0.04);
          return [f.x, f.y, f.w, f.h].every(Number.isFinite) &&
            f.w >= minSide && f.h >= minSide &&
            f.x + f.w > 0 && f.y + f.h > 0 && f.x < vw && f.y < vh &&
            (!isFace || (f.w / f.h > 0.5 && f.w / f.h < 2));
        }

        // Match new detections to existing tracks of the same kind (nearest
        // centre) so each target keeps its id and its box glides instead of jumping.
        function update(found, now) {
          found = found.filter(valid).slice(0, 12);
          const used = new Set();
          for (const f of found) {
            const cx = f.x + f.w / 2, cy = f.y + f.h / 2;
            let best = null, bestD = Infinity;
            for (const t of tracks) {
              if (used.has(t) || t.label !== f.label) continue;
              const d = Math.hypot(t.tx + t.tw / 2 - cx, t.ty + t.th / 2 - cy);
              if (d < bestD && d < Math.max(f.w, t.tw) * 0.8) { best = t; bestD = d; }
            }
            if (!best) {
              best = { id: nextId++, label: f.label, born: now, x: f.x, y: f.y, w: f.w, h: f.h };
              tracks.push(best);
            }
            used.add(best);
            Object.assign(best, { tx: f.x, ty: f.y, tw: f.w, th: f.h, score: f.score, points: f.points, seen: now });
          }
        }

        // --- Portrait-scan mode (live camera, not staged) ----------------------
        let scanMode = false, scanR = 0, gaugeVal = 0;
        const CY = "#22d3ee", MG = "#e879f9";

        // Mesh whose centre falls inside this face track's box.
        function matchMesh(t) {
          for (const m of meshes) {
            let mx = 0, my = 0;
            for (const p of m) { mx += p.x; my += p.y; }
            mx /= m.length; my /= m.length;
            if (mx > t.tx - t.tw * 0.2 && mx < t.tx + t.tw * 1.2 &&
                my > t.ty - t.th * 0.3 && my < t.ty + t.th * 1.3) return m;
          }
          return null;
        }

        // Join each landmark to its 3 nearest neighbours (built once, so the
        // wireframe keeps the same shape while the face moves).
        function buildEdges(m) {
          const seen = new Set(), edges = [];
          m.forEach((p, i) => {
            m.map((q, j) => [j, Math.hypot(p.x - q.x, p.y - q.y)])
              .filter(([j]) => j !== i)
              .sort((a, b) => a[1] - b[1])
              .slice(0, 3)
              .forEach(([j]) => {
                const key = Math.min(i, j) + "-" + Math.max(i, j);
                if (!seen.has(key)) { seen.add(key); edges.push([i, j]); }
              });
          });
          return edges;
        }

        function drawMesh(t, m, now, s, ox, oy, fade, e, locked) {
          if (!meshEdges) meshEdges = buildEdges(m);
          const k = reduceMotion ? 1 : 0.5;
          if (!t.mesh || t.mesh.length !== m.length) t.mesh = m.map(p => ({ x: p.x, y: p.y }));
          else t.mesh.forEach((p, i) => { p.x += (m[i].x - p.x) * k; p.y += (m[i].y - p.y) * k; });
          const P = t.mesh.map(p => ({ x: ox + p.x * s, y: oy + p.y * s }));
          ctx.globalAlpha = fade * (0.35 + 0.65 * e);

          // Wireframe draws itself in while locking on.
          const shown = Math.ceil(meshEdges.length * e);
          ctx.strokeStyle = "rgba(255, 255, 255, 0.55)";
          ctx.lineWidth = 1;
          ctx.beginPath();
          for (let i = 0; i < shown; i++) {
            const [a, b] = meshEdges[i];
            ctx.moveTo(P[a].x, P[a].y); ctx.lineTo(P[b].x, P[b].y);
          }
          ctx.stroke();

          ctx.shadowColor = CY; ctx.shadowBlur = 8; ctx.fillStyle = "#ffffff";
          P.forEach((p, i) => {
            const r = reduceMotion ? 2.5 : 2 + 0.9 * (1 + Math.sin(now / 280 + i * 1.7)) / 2;
            ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2); ctx.fill();
          });
          ctx.shadowBlur = 0;

          // Scan line sweeping over the face + status tag above it.
          let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
          for (const p of P) { x0 = Math.min(x0, p.x); x1 = Math.max(x1, p.x); y0 = Math.min(y0, p.y); y1 = Math.max(y1, p.y); }
          if (!reduceMotion) {
            const sy = y0 + ((Math.sin(now / 500) + 1) / 2) * (y1 - y0);
            const g = ctx.createLinearGradient(x0, 0, x1, 0);
            g.addColorStop(0, rgba(CY, 0)); g.addColorStop(0.5, rgba(CY, 0.8)); g.addColorStop(1, rgba(CY, 0));
            ctx.fillStyle = g; ctx.fillRect(x0, sy, x1 - x0, 1.5);
          }
          const tag = "FACE_" + String(t.id).padStart(2, "0") + "  " + (locked ? "MESH LOCKED" : "MAPPING...");
          ctx.font = "700 10px 'JetBrains Mono', monospace"; ctx.textBaseline = "middle";
          const tw = ctx.measureText(tag).width + 14, tx = (x0 + x1) / 2 - tw / 2, ty = Math.max(4, y0 - 30);
          ctx.fillStyle = "rgba(15, 23, 42, 0.7)"; ctx.fillRect(tx, ty, tw, 17);
          ctx.fillStyle = locked ? CY : "#fbbf24"; ctx.fillRect(tx, ty, 2, 17);
          ctx.fillText(tag, tx + 8, ty + 9);
          ctx.globalAlpha = 1;
        }

        function ring(x, y, r) { ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); }

        // A track counts once it has been visible for a moment (avoids flicker).
        function now0(t) { return performance.now() - t.born > 300; }

        // --- Face capture ---------------------------------------------------
        // Snapshot of a detected face (confidence >= 20%) with a 20% margin,
        // cropped square from the live video. Kept in memory only.
        const CAPTURE_MIN_SCORE = 0.2, CAPTURE_MARGIN = 0.2, CAPTURE_EVERY_MS = 3000;
        let capture = null;   // { img: canvas, id, score, time, at }
        function grabFace(t) {
          const vw = videoEl.videoWidth, vh = videoEl.videoHeight;
          if (!vw || !vh) return null;
          const side = Math.min(Math.max(t.tw, t.th) * (1 + CAPTURE_MARGIN * 2), vw, vh);
          const sx = Math.max(0, Math.min(vw - side, t.tx + t.tw / 2 - side / 2));
          const sy = Math.max(0, Math.min(vh - side, t.ty + t.th / 2 - side / 2));
          const c = document.createElement("canvas");
          c.width = c.height = 160;
          try { c.getContext("2d").drawImage(videoEl, sx, sy, side, side, 0, 0, 160, 160); } catch (e) { return null; }
          return { c, sx, sy, side };
        }
        // Snapshot + the face-mesh points (0..1 inside the photo) for the scan panel.
        function makeCapture(t, now) {
          const crop = grabFace(t);
          if (!crop) return null;
          const m = matchMesh(t);
          if (m && !meshEdges) meshEdges = buildEdges(m);
          return {
            img: crop.c, id: t.id, score: t.score, at: now,
            pts: m ? m.map(q => ({ x: (q.x - crop.sx) / crop.side, y: (q.y - crop.sy) / crop.side })) : [],
            time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
          };
        }
        // --- Scan quality (0..1) for the best face, with a hint for the weakest
        // factor. 100% = detector sure + good distance + centred + good light +
        // sharp. Checked every 300 ms on a 64x64 copy of the face.
        let quality = { at: -Infinity, value: 0, tip: "", perfect: false };
        const qCanvas = document.createElement("canvas");
        qCanvas.width = qCanvas.height = 64;
        const qCtx = qCanvas.getContext("2d", { willReadFrequently: true });
        function scanQuality(now) {
          if (now - quality.at < 300) return quality;
          const faces = tracks.filter(t => t.label === "face" && t.score != null && t.tw);
          const vw = videoEl.videoWidth, vh = videoEl.videoHeight;
          if (!faces.length || !vw || !vh) { quality = { at: now, value: 0, tip: "", perfect: false }; return quality; }
          const f = faces.reduce((a, b) => ((b.score || 0) > (a.score || 0) ? b : a));
          const clamp = v => Math.max(0, Math.min(1, v));

          const conf = clamp((f.score - 0.6) / 0.35);                       // 95%+ detector score = full marks
          const rel = f.th / vh;                                             // face height share of the frame
          const size = rel < 0.2 ? clamp(rel / 0.2) : rel > 0.7 ? clamp(1 - (rel - 0.7) / 0.25) : 1;
          const dist = Math.hypot((f.tx + f.tw / 2) / vw - 0.5, (f.ty + f.th / 2) / vh - 0.45);
          const center = dist < 0.1 ? 1 : clamp(1 - (dist - 0.1) / 0.25);

          let mean = 128, lap = 100;
          try {
            qCtx.drawImage(videoEl, f.tx, f.ty, f.tw, f.th, 0, 0, 64, 64);
            const d = qCtx.getImageData(0, 0, 64, 64).data;
            const g = new Float32Array(64 * 64);
            let sum = 0;
            for (let i = 0, j = 0; i < d.length; i += 4, j++) { g[j] = 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2]; sum += g[j]; }
            mean = sum / g.length;
            let s1 = 0, s2 = 0, n = 0;                                       // Laplacian variance = sharpness
            for (let y = 1; y < 63; y++) for (let x = 1; x < 63; x++) {
              const k = y * 64 + x;
              const v = 4 * g[k] - g[k - 1] - g[k + 1] - g[k - 64] - g[k + 64];
              s1 += v; s2 += v * v; n++;
            }
            lap = s2 / n - (s1 / n) * (s1 / n);
          } catch (e) {}
          const light = mean < 85 ? clamp(mean / 85) : mean > 195 ? clamp(1 - (mean - 195) / 45) : 1;
          const sharp = clamp(lap / 60);

          let value = conf * 0.3 + size * 0.2 + center * 0.15 + light * 0.2 + sharp * 0.15;
          const parts = [
            [conf, "Seedha camera ki taraf dekhiye"],
            [size, rel < 0.2 ? "Thoda paas aaiye" : "Thoda peeche hatiye"],
            [center, "Chehra beech mein laaiye"],
            [light, mean < 85 ? "Roshni badhaiye" : "Roshni thodi kam kijiye"],
            [sharp, "Hiliye mat, camera lens saaf kijiye"],
          ].sort((a, b) => a[0] - b[0]);
          const perfect = value >= 0.97;
          if (perfect) value = 1;
          quality = { at: now, value, perfect, tip: perfect ? "✓ PERFECT SCAN" : parts[0][1] };
          return quality;
        }

        function bestFace() {
          const faces = tracks.filter(t => t.label === "face" && (t.score || 0) >= CAPTURE_MIN_SCORE && now0(t));
          return faces.length ? faces.reduce((a, b) => ((b.score || 0) > (a.score || 0) ? b : a)) : null;
        }
        function refreshCapture(now) {
          if (capture && now - capture.at < CAPTURE_EVERY_MS) return;
          const f = bestFace();
          if (!f) return;
          const cap = makeCapture(f, now);
          if (cap) capture = cap;
        }

        // Face-scan panel, centred in the left column: the latest capture is
        // swept by a scan beam; mesh points and links light up as it passes.
        // Each new capture (every 3 s) gets a fresh 2 s scan, then "complete".
        const SCAN_MS = 2000;
        function drawFaceScan(T, now, lx, cy, colW, colH) {
          const s = Math.min(colW - 40, colH * 0.62, 190);
          if (s < 70) return;
          const x0 = lx - s / 2, y0 = cy - colH / 2 - 14;
          const live = !!capture && now - capture.at < CAPTURE_EVERY_MS * 3;
          const col = live ? CY : "#94a3b8";
          const pad = 10, L = 12;
          const prog = capture ? Math.min(1, (now - capture.at) / SCAN_MS) : 0;
          const done = live && prog >= 1;

          ctx.strokeStyle = rgba(col, 0.7); ctx.lineWidth = 1.5;
          [[x0 - pad, y0 - pad, 1, 1], [x0 + s + pad, y0 - pad, -1, 1],
           [x0 - pad, y0 + s + pad, 1, -1], [x0 + s + pad, y0 + s + pad, -1, -1]].forEach(([x, y, dx, dy]) => {
            ctx.beginPath(); ctx.moveTo(x, y + dy * L); ctx.lineTo(x, y); ctx.lineTo(x + dx * L, y); ctx.stroke();
          });
          ctx.font = "700 9px 'JetBrains Mono', monospace"; ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
          ctx.fillStyle = rgba(col, 0.9);
          ctx.fillText("FACE_SCAN", x0 - pad, y0 - pad - 8);

          ctx.save();
          ctx.beginPath(); ctx.roundRect ? ctx.roundRect(x0, y0, s, s, 8) : ctx.rect(x0, y0, s, s); ctx.clip();
          if (capture) {
            // Holographic duotone of the photo.
            ctx.globalAlpha = live ? 1 : 0.4;
            ctx.drawImage(capture.img, x0, y0, s, s);
            ctx.globalAlpha = 1;
            ctx.globalCompositeOperation = "color";
            ctx.fillStyle = "rgba(34, 211, 238, 0.55)"; ctx.fillRect(x0, y0, s, s);
            ctx.globalCompositeOperation = "source-over";
            ctx.fillStyle = "rgba(4, 18, 32, 0.28)"; ctx.fillRect(x0, y0, s, s);
            // Measurement grid.
            ctx.strokeStyle = rgba(CY, 0.12); ctx.lineWidth = 1;
            ctx.beginPath();
            for (let i = 1; i < 8; i++) {
              ctx.moveTo(x0 + i * s / 8, y0); ctx.lineTo(x0 + i * s / 8, y0 + s);
              ctx.moveTo(x0, y0 + i * s / 8); ctx.lineTo(x0 + s, y0 + i * s / 8);
            }
            ctx.stroke();

            const beamY = y0 + prog * s;
            // Not yet scanned: darker.
            if (!done) { ctx.fillStyle = "rgba(2, 8, 20, 0.45)"; ctx.fillRect(x0, beamY, s, y0 + s - beamY); }

            // Mesh on the photo: links + points revealed above the beam.
            const P = capture.pts.map(q => ({ x: x0 + q.x * s, y: y0 + q.y * s }));
            if (P.length && meshEdges) {
              ctx.strokeStyle = "rgba(255, 255, 255, 0.5)"; ctx.lineWidth = 0.8;
              ctx.beginPath();
              for (const [i, j] of meshEdges) {
                if (!P[i] || !P[j] || Math.max(P[i].y, P[j].y) > beamY) continue;
                ctx.moveTo(P[i].x, P[i].y); ctx.lineTo(P[j].x, P[j].y);
              }
              ctx.stroke();
              for (const q of P) {
                if (q.y > beamY + 2) continue;
                const near = done ? 0 : Math.max(0, 1 - Math.abs(q.y - beamY) / 14);
                ctx.fillStyle = near > 0 ? CY : "#ffffff";
                ring(q.x, q.y, 1.5 + near * 1.6); ctx.fill();
              }
            }

            if (!done) {
              // Beam with a soft trail.
              const g = ctx.createLinearGradient(0, beamY - 28, 0, beamY);
              g.addColorStop(0, rgba(CY, 0)); g.addColorStop(1, rgba(CY, 0.35));
              ctx.fillStyle = g; ctx.fillRect(x0, beamY - 28, s, 28);
              ctx.strokeStyle = CY; ctx.lineWidth = 1.5; ctx.shadowColor = CY; ctx.shadowBlur = 10;
              ctx.beginPath(); ctx.moveTo(x0, beamY); ctx.lineTo(x0 + s, beamY); ctx.stroke();
              ctx.shadowBlur = 0;
            }
          } else {
            // Waiting: dark tile with a slow spinning reticle.
            ctx.fillStyle = "rgba(15, 23, 42, 0.55)"; ctx.fillRect(x0, y0, s, s);
            ctx.strokeStyle = rgba(col, 0.4); ctx.lineWidth = 1.2;
            ctx.beginPath(); ctx.arc(lx, y0 + s / 2, s * 0.22, T / 900, T / 900 + 4.2); ctx.stroke();
            ctx.beginPath(); ctx.arc(lx, y0 + s / 2, s * 0.14, -T / 600, -T / 600 + 2.4); ctx.stroke();
          }
          ctx.restore();
          ctx.strokeStyle = done ? rgba(CY, 0.55 + 0.35 * Math.sin(T / 300)) : rgba(col, 0.35);
          ctx.lineWidth = done ? 1.5 : 1;
          if (ctx.roundRect) { ctx.beginPath(); ctx.roundRect(x0, y0, s, s, 8); ctx.stroke(); } else ctx.strokeRect(x0, y0, s, s);

          const ry = y0 + s + pad + 18;
          ctx.font = "700 10px 'JetBrains Mono', monospace"; ctx.fillStyle = live ? "#fff" : rgba(col, 0.8);
          ctx.fillText(!capture ? "WAITING FOR FACE" : done ? "SCAN COMPLETE" : live ? "ANALYZING " + Math.round(prog * 100) + "%" : "LAST SCAN", x0 - pad, ry);
          ctx.fillStyle = rgba(col, 0.2); ctx.fillRect(x0 - pad, ry + 7, s + pad * 2, 3);
          ctx.fillStyle = col; ctx.fillRect(x0 - pad, ry + 7, (s + pad * 2) * prog, 3);
          ctx.font = "600 9px 'JetBrains Mono', monospace"; ctx.fillStyle = rgba(col, 0.75);
          ctx.fillText(capture ? "FACE_" + String(capture.id).padStart(2, "0") + " · " +
                                 (capture.pts.length ? capture.pts.length + " LANDMARKS" : "MESH N/A")
                               : "MIN CONF " + Math.round(CAPTURE_MIN_SCORE * 100) + "%", x0 - pad, ry + 22);
        }

        // Face-capture panel, centred in the right column (rx = column centre).
        function drawFaceCapture(T, now, rx, cy, colW, colH) {
          const s = Math.min(colW - 40, colH * 0.62, 190);
          if (s < 70) return;
          // Same top edge as the human scan, so both labels line up.
          const x0 = rx - s / 2, y0 = cy - colH / 2 - 14;
          const live = !!capture && now - capture.at < CAPTURE_EVERY_MS * 3;
          const col = live ? CY : "#94a3b8";
          const pad = 10, L = 12;

          ctx.strokeStyle = rgba(col, 0.7); ctx.lineWidth = 1.5;
          [[x0 - pad, y0 - pad, 1, 1], [x0 + s + pad, y0 - pad, -1, 1],
           [x0 - pad, y0 + s + pad, 1, -1], [x0 + s + pad, y0 + s + pad, -1, -1]].forEach(([x, y, dx, dy]) => {
            ctx.beginPath(); ctx.moveTo(x, y + dy * L); ctx.lineTo(x, y); ctx.lineTo(x + dx * L, y); ctx.stroke();
          });
          ctx.font = "700 9px 'JetBrains Mono', monospace"; ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
          ctx.fillStyle = rgba(col, 0.9);
          ctx.fillText("FACE_CAPTURE", x0 - pad, y0 - pad - 8);

          ctx.save();
          ctx.beginPath(); ctx.roundRect ? ctx.roundRect(x0, y0, s, s, 8) : ctx.rect(x0, y0, s, s); ctx.clip();
          if (capture) {
            ctx.globalAlpha = live ? 1 : 0.45;
            ctx.drawImage(capture.img, x0, y0, s, s);
            ctx.globalAlpha = 1;
            // Cyan tint + raster lines for the HUD look.
            ctx.fillStyle = rgba(CY, 0.08); ctx.fillRect(x0, y0, s, s);
            ctx.fillStyle = "rgba(0, 0, 0, 0.12)";
            for (let y = y0; y < y0 + s; y += 3) ctx.fillRect(x0, y, s, 1);
            // Shutter flash right after a new capture.
            const since = now - capture.at;
            if (since < 350) { ctx.fillStyle = "rgba(255,255,255," + (0.6 * (1 - since / 350)) + ")"; ctx.fillRect(x0, y0, s, s); }
          } else {
            ctx.fillStyle = "rgba(15, 23, 42, 0.55)"; ctx.fillRect(x0, y0, s, s);
            ctx.strokeStyle = rgba(col, 0.35); ctx.lineWidth = 1.2;
            ring(rx, y0 + s * 0.4, s * 0.16); ctx.stroke();
            ctx.beginPath(); ctx.arc(rx, y0 + s * 0.95, s * 0.32, Math.PI, Math.PI * 2); ctx.stroke();
          }
          ctx.restore();
          ctx.strokeStyle = rgba(col, 0.35); ctx.lineWidth = 1;
          if (ctx.roundRect) { ctx.beginPath(); ctx.roundRect(x0, y0, s, s, 8); ctx.stroke(); } else ctx.strokeRect(x0, y0, s, s);
          // Small centre crosshair.
          ctx.strokeStyle = rgba(col, 0.6);
          ctx.beginPath(); ctx.moveTo(rx - 6, y0 + s / 2); ctx.lineTo(rx + 6, y0 + s / 2);
          ctx.moveTo(rx, y0 + s / 2 - 6); ctx.lineTo(rx, y0 + s / 2 + 6); ctx.stroke();

          const ry = y0 + s + pad + 18;
          ctx.font = "700 10px 'JetBrains Mono', monospace"; ctx.fillStyle = live ? "#fff" : rgba(col, 0.8);
          ctx.fillText(capture ? (live ? "FACE CAPTURED" : "LAST CAPTURE") : "WAITING FOR FACE", x0 - pad, ry);
          const sc = capture ? capture.score || 0 : 0;
          ctx.fillStyle = rgba(col, 0.2); ctx.fillRect(x0 - pad, ry + 7, s + pad * 2, 3);
          ctx.fillStyle = col; ctx.fillRect(x0 - pad, ry + 7, (s + pad * 2) * sc, 3);
          ctx.font = "600 9px 'JetBrains Mono', monospace"; ctx.fillStyle = rgba(col, 0.75);
          ctx.fillText(capture ? "FACE_" + String(capture.id).padStart(2, "0") + " · " + Math.round(sc * 100) + "% · " + capture.time
                               : "MIN CONF " + Math.round(CAPTURE_MIN_SCORE * 100) + "%", x0 - pad, ry + 22);
        }

        // While an info card is open, the portrait moves left so the card
        // sits beside the face instead of over it (eased, in px).
        let scanShift = 0, scanFrame = null, scanKey = "";
        function drawScanDecor(now, W, H) {
          const card = wrap.classList.contains("has-card");
          const room = card ? Math.max(W * 0.55, W - 340) : W;
          const want = card ? room / 2 - W / 2 : 0;
          scanShift += (want - scanShift) * (reduceMotion ? 1 : 0.12);
          if (Math.abs(want - scanShift) < 0.5) scanShift = want;
          wrap.style.setProperty("--scan-shift", scanShift.toFixed(1) + "px");
          const cx = W / 2 + scanShift, cy = H / 2;
          const R = Math.min(room, H) * 0.36;
          // Full camera photo (nothing cropped) inside the circle: the photo's
          // corners touch the ring; a blurred copy of the feed fills the rest.
          const aspect = videoEl.videoWidth && videoEl.videoHeight ? videoEl.videoWidth / videoEl.videoHeight : 4 / 3;
          const diag = 2 * R * 0.985;
          const fw = diag * aspect / Math.sqrt(1 + aspect * aspect), fh = fw / aspect;
          const fx = cx - fw / 2, fy = cy - fh / 2;
          scanFrame = { x: fx, y: fy, w: fw, h: fh };
          const key = [fx, fy, fw, fh, R].map(v => v.toFixed(0)).join(",");
          if (key !== scanKey) {
            scanKey = key;
            wrap.style.setProperty("--fx", (fx - scanShift) + "px");   // video moves via --scan-shift
            wrap.style.setProperty("--fy", fy + "px");
            wrap.style.setProperty("--fw", fw + "px");
            wrap.style.setProperty("--fh", fh + "px");
            wrap.style.setProperty("--cx", (cx - scanShift) + "px");
            wrap.style.setProperty("--cy", cy + "px");
            wrap.style.setProperty("--cr", R + "px");
          }
          const T = reduceMotion ? 0 : now;

          // Layout: two side columns in the free space left / right of the
          // heptagon, both vertically centred on the portrait. Human scan on
          // the left, face capture on the right (hidden while a card is open).
          const clear = R * 1.32;
          const leftW = cx - clear - 16, rightW = W - (cx + clear) - 16;
          const lx = 16 + leftW / 2, rx = cx + clear + rightW / 2;
          const showLeft = leftW > 110, showRight = !card && rightW > 110;
          const colH = Math.min(H * 0.42, 320);
          const colTop = cy - colH / 2 - 14;
          const gridY = colTop + colH + 70;

          // Twinkling dot grids under each column.
          ctx.fillStyle = CY;
          for (let i = 0; i < 6; i++) for (let j = 0; j < 3; j++) {
            ctx.globalAlpha = 0.15 + 0.35 * Math.abs(Math.sin(T / 900 + i * 1.3 + j));
            if (showLeft && gridY + 28 < H - 20) { ring(lx - 35 + i * 14, gridY + j * 14, 1.4); ctx.fill(); }
            if (showRight && gridY + 28 < H - 50) { ring(rx - 35 + i * 14, gridY + j * 14, 1.6); ctx.fill(); }
          }
          ctx.globalAlpha = 1;

          // "+" marks above the capture column.
          // (Only when there's room under the 🔍 search button.)
          if (showRight && colTop - 64 > 200) {
            ctx.strokeStyle = rgba(MG, 0.55); ctx.lineWidth = 2;
            for (let i = 0; i < 3; i++) {
              const px = rx - 28 + i * 28, py = colTop - 64;
              ctx.beginPath(); ctx.moveTo(px - 7, py); ctx.lineTo(px + 7, py); ctx.moveTo(px, py - 7); ctx.lineTo(px, py + 7); ctx.stroke();
            }
          }

          // Circuit lines running in from the left edge above the human scan.
          if (showLeft && colTop - 70 > 70) {
            ctx.lineWidth = 1.5;
            [[-72, 0.7], [-58, 1], [-44, 0.45]].forEach(([dy, k], i) => {
              const yy = colTop + dy, x2 = Math.max(40, (lx + 20) * k);
              ctx.strokeStyle = rgba(CY, 0.7);
              ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(x2, yy); ctx.stroke();
              ring(x2 + 5, yy, 5); ctx.stroke();
              ctx.fillStyle = "#fff";
              ring(((T / 1400 + i * 0.33) % 1) * x2, yy, 2); ctx.fill();
            });
          }

          // Rotating neon heptagon around the portrait.
          const rot = T / 40000 - Math.PI / 2, HR = R * 1.24;
          const V = Array.from({ length: 7 }, (_, i) => {
            const a = rot + i * Math.PI * 2 / 7;
            return { x: cx + Math.cos(a) * HR, y: cy + Math.sin(a) * HR, a };
          });
          const grad = ctx.createLinearGradient(cx - HR, cy - HR, cx + HR, cy + HR);
          grad.addColorStop(0, CY); grad.addColorStop(0.5, "#818cf8"); grad.addColorStop(1, MG);
          ctx.strokeStyle = grad; ctx.lineWidth = 2; ctx.shadowColor = "#818cf8"; ctx.shadowBlur = 12;
          ctx.beginPath();
          V.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
          ctx.closePath(); ctx.stroke();
          ctx.lineWidth = 1; ctx.globalAlpha = 0.45;
          ctx.beginPath();
          V.forEach((p, i) => {
            if (i % 2) return;
            ctx.moveTo(p.x, p.y);
            ctx.lineTo(cx + Math.cos(p.a) * R * 1.04, cy + Math.sin(p.a) * R * 1.04);
          });
          ctx.stroke();
          ctx.globalAlpha = 1;
          V.forEach((p, i) => {
            const pr = 5 + 1.5 * Math.sin(T / 500 + i), col = i % 3 === 0 ? MG : CY;
            ctx.fillStyle = col; ctx.shadowColor = col; ctx.shadowBlur = 16;
            ring(p.x, p.y, pr); ctx.fill();
            ctx.shadowBlur = 0; ctx.fillStyle = "#fff";
            ring(p.x, p.y, pr * 0.4); ctx.fill();
          });

          // Thin glowing edge around the full photo, then the portrait ring.
          ctx.lineWidth = 1.2; ctx.strokeStyle = rgba(CY, 0.45);
          if (ctx.roundRect) { ctx.beginPath(); ctx.roundRect(fx, fy, fw, fh, 10); ctx.stroke(); } else ctx.strokeRect(fx, fy, fw, fh);
          ctx.lineWidth = 3; ctx.strokeStyle = rgba(CY, 0.9); ctx.shadowColor = CY; ctx.shadowBlur = 20;
          ring(cx, cy, R); ctx.stroke(); ctx.shadowBlur = 0;
          ctx.save(); ctx.translate(cx, cy); ctx.rotate(T / 6000);
          ctx.setLineDash([4, 10]); ctx.lineWidth = 1.5; ctx.strokeStyle = rgba(MG, 0.6);
          ring(0, 0, R * 1.07); ctx.stroke();
          ctx.setLineDash([]); ctx.restore();

          // Scan-quality gauge (spins while searching) + a hint to reach 100%.
          const faces = tracks.filter(t => t.label === "face" && t.score != null);
          const q = scanQuality(now);
          gaugeVal += (q.value - gaugeVal) * 0.1;
          if (q.perfect && gaugeVal > 0.995) gaugeVal = 1;
          // In the circle's free band above the photo (never over the face).
          const gr = R * 0.12, gx = cx - R * 0.36, gy = cy - R * 0.78;
          const good = faces.length && q.perfect;
          ctx.fillStyle = good ? "rgba(6, 78, 59, 0.6)" : "rgba(15, 23, 42, 0.5)"; ring(gx, gy, gr); ctx.fill();
          ctx.lineWidth = 2; ctx.strokeStyle = "rgba(255, 255, 255, 0.25)"; ring(gx, gy, gr); ctx.stroke();
          ctx.lineWidth = 3; ctx.strokeStyle = good ? "#34d399" : "#fff"; ctx.lineCap = "round";
          if (good) { ctx.shadowColor = "#34d399"; ctx.shadowBlur = 14; }
          ctx.beginPath();
          if (faces.length) ctx.arc(gx, gy, gr, -Math.PI / 2, -Math.PI / 2 + gaugeVal * Math.PI * 2);
          else ctx.arc(gx, gy, gr, T / 300, T / 300 + 1.2);
          ctx.stroke(); ctx.lineCap = "butt"; ctx.shadowBlur = 0;
          ctx.fillStyle = "#fff"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
          ctx.font = "700 " + Math.round(gr * 0.5) + "px 'JetBrains Mono', monospace";
          ctx.fillText(faces.length ? Math.round(gaugeVal * 100) + "%" : "--", gx, gy - gr * 0.08);
          ctx.font = "600 " + Math.max(7, Math.round(gr * 0.2)) + "px 'JetBrains Mono', monospace";
          ctx.fillStyle = good ? "#a7f3d0" : rgba(CY, 0.9);
          ctx.fillText("QUALITY", gx, gy + gr * 0.4);
          // Hint beside the gauge: what to change to reach 100%.
          if (faces.length && q.tip) {
            ctx.textAlign = "left";
            ctx.font = "700 " + Math.max(10, Math.round(gr * 0.24)) + "px 'JetBrains Mono', monospace";
            const tx = gx + gr + 12, ty = gy;
            const tw = ctx.measureText(q.tip).width;
            ctx.fillStyle = "rgba(15, 23, 42, 0.7)";
            if (ctx.roundRect) { ctx.beginPath(); ctx.roundRect(tx - 8, ty - 12, tw + 16, 24, 12); ctx.fill(); }
            ctx.fillStyle = q.perfect ? "#34d399" : "#fbbf24";
            ctx.fillText(q.tip, tx, ty + 1);
          }

          // Rotating dials (right side).
          // Dials in the free band below the photo.
          [[cx + R * 0.36, cy + R * 0.79, R * 0.11, 1], [cx - R * 0.36, cy + R * 0.8, R * 0.08, -1.6],
           [cx + R * 0.62, cy + R * 0.68, R * 0.05, 2.4]].forEach(([dx, dy, dr, sp]) => {
            ctx.strokeStyle = "rgba(255, 255, 255, 0.55)"; ctx.lineWidth = 1;
            ring(dx, dy, dr); ctx.stroke();
            ctx.strokeStyle = "rgba(255, 255, 255, 0.9)"; ctx.lineWidth = 2;
            const a = T / 1000 * sp;
            ctx.beginPath(); ctx.arc(dx, dy, dr * 0.72, a, a + 1.6); ctx.stroke();
            ctx.beginPath(); ctx.arc(dx, dy, dr * 0.72, a + Math.PI, a + Math.PI + 0.8); ctx.stroke();
            ctx.fillStyle = "#fff"; ring(dx, dy, 1.5); ctx.fill();
          });

          refreshCapture(now);
          if (showLeft) drawFaceScan(T, now, lx, cy, leftW, colH);
          if (showRight) drawFaceCapture(T, now, rx, cy, rightW, colH);

          // Status line under the portrait.
          ctx.font = "600 10px 'JetBrains Mono', monospace";
          ctx.fillStyle = rgba(CY, 0.85);
          const hasFace = tracks.some(t => t.label === "face");
          ctx.fillText("FACE_SCAN // " + (hasFace ? "TARGET ACQUIRED" : "SEARCHING..."), cx, Math.min(H - 14, cy + R * 1.3));
          ctx.textAlign = "start"; ctx.textBaseline = "alphabetic";
        }

        function drawTrack(t, now, s, ox, oy, W, H) {
          const isFace = t.label === "face";
          const k = reduceMotion ? 1 : (isFace ? 0.35 : 0.2);
          t.x += (t.tx - t.x) * k; t.y += (t.ty - t.y) * k;
          t.w += (t.tw - t.w) * k; t.h += (t.th - t.h) * k;
          const lock = reduceMotion ? 1 : Math.min(1, (now - t.born) / 550);
          const e = 1 - Math.pow(1 - lock, 3);
          // Objects refresh less often, so they get a longer grace period.
          const hold = isFace ? 150 : 450;
          const fade = Math.max(0, Math.min(1, 1 - (now - t.seen - hold) / 450));
          const locked = lock >= 1;
          const c = !locked ? "#fbbf24" : isFace ? "#22d3ee" : "#4ade80";

          if (scanMode) {
            // The portrait circle already frames the person; faces get the mesh.
            if (t.label === "person") return;
            if (isFace) {
              const m = matchMesh(t);
              if (m) { drawMesh(t, m, now, s, ox, oy, fade, e, locked); return; }
            }
          }

          // Pad face boxes so they frame the whole head; while locking on the
          // box starts oversized and snaps in.
          const pad = isFace ? 0.12 : 0.02, grow = (1 - e) * 0.5;
          let bw = t.w * s * (1 + pad * 2), bh = t.h * s * (1 + pad * 2);
          let bx = ox + (t.x - t.w * pad) * s, by = oy + (t.y - t.h * pad) * s;
          bx -= bw * grow / 2; by -= bh * grow / 2; bw *= 1 + grow; bh *= 1 + grow;
          ctx.globalAlpha = fade * (0.4 + 0.6 * e);

          // Guide lines out to the panel edges (faces only, to limit clutter).
          if (isFace) {
            ctx.strokeStyle = rgba(c, 0.22); ctx.lineWidth = 1; ctx.setLineDash([2, 6]);
            ctx.beginPath();
            ctx.moveTo(0, by + bh / 2); ctx.lineTo(bx, by + bh / 2);
            ctx.moveTo(bx + bw, by + bh / 2); ctx.lineTo(W, by + bh / 2);
            ctx.moveTo(bx + bw / 2, 0); ctx.lineTo(bx + bw / 2, by);
            ctx.moveTo(bx + bw / 2, by + bh); ctx.lineTo(bx + bw / 2, H);
            ctx.stroke(); ctx.setLineDash([]);
          }

          // Faint frame + glowing corner brackets.
          ctx.lineWidth = 1;
          ctx.strokeStyle = rgba(c, 0.3); ctx.strokeRect(bx, by, bw, bh);
          const L = Math.min(bw, bh) * 0.22;
          // Brief "focus confirmed" pulse right after locking, like a camera AF.
          const since = now - t.born - 550;
          const pulse = locked && since < 350 && !reduceMotion ? 1 - since / 350 : 0;
          ctx.strokeStyle = c; ctx.lineWidth = 2.5 + pulse * 2.5; ctx.shadowColor = c; ctx.shadowBlur = 10 + pulse * 20;
          ctx.beginPath();
          ctx.moveTo(bx, by + L); ctx.lineTo(bx, by); ctx.lineTo(bx + L, by);
          ctx.moveTo(bx + bw - L, by); ctx.lineTo(bx + bw, by); ctx.lineTo(bx + bw, by + L);
          ctx.moveTo(bx + bw, by + bh - L); ctx.lineTo(bx + bw, by + bh); ctx.lineTo(bx + bw - L, by + bh);
          ctx.moveTo(bx + L, by + bh); ctx.lineTo(bx, by + bh); ctx.lineTo(bx, by + bh - L);
          ctx.stroke(); ctx.shadowBlur = 0;

          // Centre focus point.
          const fx = bx + bw / 2, fy = by + bh / 2, fr = Math.max(6, Math.min(bw, bh) * 0.05);
          ctx.lineWidth = 1.5;
          ctx.beginPath();
          ctx.moveTo(fx - fr, fy); ctx.lineTo(fx + fr, fy);
          ctx.moveTo(fx, fy - fr); ctx.lineTo(fx, fy + fr);
          ctx.stroke();

          // Scan line sweeping inside face boxes.
          if (isFace && !reduceMotion) {
            const sy = by + ((Math.sin(now / 450) + 1) / 2) * bh;
            const g = ctx.createLinearGradient(bx, 0, bx + bw, 0);
            g.addColorStop(0, rgba(c, 0)); g.addColorStop(0.5, rgba(c, 0.7)); g.addColorStop(1, rgba(c, 0));
            ctx.fillStyle = g; ctx.fillRect(bx, sy, bw, 1.5);
          }

          // Landmarks (eyes, nose, mouth, ears), shifted with the smoothed box.
          ctx.fillStyle = rgba(c, 0.9);
          for (const p of t.points || []) {
            ctx.beginPath();
            ctx.arc(ox + (p.x + t.x - t.tx) * s, oy + (p.y + t.y - t.ty) * s, 2, 0, Math.PI * 2);
            ctx.fill();
          }

          // Name / status tag above the box, position readout below face boxes.
          const name = t.label.toUpperCase().replace(/ +/g, "_");
          const tag = name + "_" + String(t.id).padStart(2, "0") + "  " + (locked ? "AF LOCKED" : "FOCUSING") +
            (t.score != null ? "  " + Math.round(t.score * 100) + "%" : "");
          ctx.font = "700 10px 'JetBrains Mono', monospace"; ctx.textBaseline = "middle";
          const ly = by - 20 >= 0 ? by - 20 : by + 2;
          ctx.fillStyle = rgba(c, 0.85); ctx.fillRect(bx, ly, ctx.measureText(tag).width + 12, 16);
          ctx.fillStyle = "#04121a"; ctx.fillText(tag, bx + 6, ly + 8);
          if (isFace) {
            const vw = videoEl.videoWidth, vh = videoEl.videoHeight;
            ctx.font = "10px 'JetBrains Mono', monospace"; ctx.fillStyle = rgba(c, 0.8);
            ctx.fillText("X " + ((t.x + t.w / 2) / vw).toFixed(2) + "  Y " + ((t.y + t.h / 2) / vh).toFixed(2) +
              "  " + Math.round(t.w) + "PX", bx, Math.min(H - 8, by + bh + 12));
          }
          ctx.globalAlpha = 1;
        }

        function draw(now) {
          const W = canvas.clientWidth, H = canvas.clientHeight;
          const dpr = Math.min(window.devicePixelRatio || 1, 2);
          if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(H * dpr)) {
            canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
          }
          ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
          ctx.clearRect(0, 0, W, H);
          tracks = tracks.filter(t => now - t.seen < (t.label === "face" ? 600 : 900));
          wrap.classList.toggle("tracking", tracks.length > 0);
          try { checkNotify(now); } catch (e) { console.warn("detections log:", e); }
          if (detect) {
            trackEl.textContent = detectErrors > 30 ? "DETECTOR ERROR · RELOAD PAGE"
              : tracks.length ? "TRACKING" : "SEARCHING...";
            facesEl.textContent = "TARGETS: " + tracks.length;
          }
          scanMode = wrap.classList.contains("is-live") && !wrap.classList.contains("staged");
          if (scanMode) {
            try { drawScanDecor(now, W, H); } catch (e) { console.warn("scan decor:", e); }
          }
          const vw = videoEl.videoWidth, vh = videoEl.videoHeight;
          if (!vw || !vh) return;
          // Map video pixels to the panel: "cover" in scan mode (cropped to
          // fill the circle), otherwise "contain" (letterboxed).
          // Scan mode: the whole video is shown inside scanFrame (nothing cropped).
          const sf = scanMode && scanFrame;
          const s = sf ? Math.min(sf.w / vw, sf.h / vh) : Math.min(W / vw, H / vh);
          const ox = sf ? sf.x + (sf.w - vw * s) / 2 : (W - vw * s) / 2;
          const oy = sf ? sf.y + (sf.h - vh * s) / 2 : (H - vh * s) / 2;
          // Big objects first so faces and small items draw on top.
          tracks.sort((a, b) => b.w * b.h - a.w * a.h);
          for (const t of tracks) {
            try { drawTrack(t, now, s, ox, oy, W, H); } catch (e) { ctx.globalAlpha = 1; ctx.shadowBlur = 0; }
          }
        }

        function loop() {
          rafId = null;
          if (!running) return;
          if (!busy && detect && videoEl.readyState >= 2 && videoEl.currentTime !== lastVideoTime) {
            lastVideoTime = videoEl.currentTime;
            busy = true;
            Promise.resolve()
              .then(() => detect(videoEl, performance.now()))
              .then(faces => { if (running) update(faces, performance.now()); })
              .catch(() => {})
              .finally(() => { busy = false; });
          }
          // Keep the loop alive whatever happens while drawing.
          try { draw(performance.now()); } catch (e) { console.warn("face overlay:", e); }
          rafId = requestAnimationFrame(loop);
        }

        function start() {
          running = true;
          if (detectBox) detectBox.classList.add("live");
          announced.clear();   // new camera session: announce everything once again
          voiceQueue = [];
          loggedFaces.clear(); faceLastSeen = -Infinity; lastFaceLog = -Infinity;
          trackEl.textContent = detect ? "SEARCHING..." : "MODEL: LOADING...";
          if (!loading) {
            loading = load().then(d => {
              detect = d;
              if (!d && running) trackEl.textContent = "TRACKING: N/A";
            });
          }
          if (rafId === null) rafId = requestAnimationFrame(loop);
        }

        function stop() {
          running = false;
          if (detectBox) detectBox.classList.remove("live");
          if (rafId !== null) cancelAnimationFrame(rafId);
          rafId = null;
          tracks = [];
          meshes = [];
          capture = null;
          nextId = 1;
          lastVideoTime = -1;
          ctx.setTransform(1, 0, 0, 1, 0, 0);
          ctx.clearRect(0, 0, canvas.width, canvas.height);
          wrap.classList.remove("tracking");
          trackEl.textContent = "";
          facesEl.textContent = "";
        }

        return { start, stop };
      })();

      // --- Geotag: date/time stamp, IP addresses and location on the feed ---
      // Location comes from the public IP first (via /netinfo), then is
      // upgraded to the browser's own position if the user allows it.
      // Real place name for a precise position (server-side OpenStreetMap
      // lookup, cached ~10 m). Resolves to null if unavailable.
      const placeCache = new Map();
      function placeFor(lat, lon) {
        const key = lat.toFixed(4) + "," + lon.toFixed(4);
        if (!placeCache.has(key)) {
          placeCache.set(key, fetch("/geo/reverse?lat=" + lat + "&lon=" + lon)
            .then(r => r.json()).then(a => (a && a.ok ? a : null)).catch(() => null));
        }
        return placeCache.get(key);
      }

      const geoTag = (() => {
        const stampEl = document.getElementById("hudStamp");
        const ipEl = document.getElementById("hudIp");
        const geoEl = document.getElementById("hudGeo");
        const placeEl = document.getElementById("hudPlace");
        const pad = n => String(n).padStart(2, "0");
        const coord = (v, pos, neg) => Math.abs(v).toFixed(4) + "°" + (v >= 0 ? pos : neg);

        function stamp() {
          const d = new Date();
          stampEl.textContent = d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
            " " + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
        }
        function showCoords(lat, lon, source) {
          geoEl.textContent = coord(lat, "N", "S") + " " + coord(lon, "E", "W") + " · " + source;
        }

        async function locate() {
          try {
            const n = await (await fetch("/netinfo")).json();
            ipEl.textContent = [n.lan_ip, n.public_ip && "PUB " + n.public_ip].filter(Boolean).join(" · ") || "--";
            if (n.lat != null && n.lon != null) showCoords(n.lat, n.lon, "IP-GEO");
            else geoEl.textContent = "UNAVAILABLE";
            placeEl.textContent = [n.city, n.country_code].filter(Boolean).join(", ").toUpperCase();
          } catch (e) {
            geoEl.textContent = "OFFLINE";
          }
          if (navigator.geolocation) {
            navigator.geolocation.getCurrentPosition(
              async p => {
                showCoords(p.coords.latitude, p.coords.longitude, "GPS ±" + Math.round(p.coords.accuracy) + "M");
                // Replace the IP city with the real place name.
                const a = await placeFor(p.coords.latitude, p.coords.longitude);
                if (a) placeEl.textContent = (a.short + (a.country_code ? ", " + a.country_code : "")).toUpperCase();
              },
              () => {},  // denied / unavailable: keep the IP-based position
              { enableHighAccuracy: true, timeout: 10000, maximumAge: 300000 },
            );
          }
        }

        return {
          start() {
            stamp();
            locate();  // cheap: the server caches the IP lookup
          },
          tick: stamp,
        };
      })();

      // --- Feed HUD: live/standby state, uptime clock, resolution readout ---
      const feedHud = (() => {
        const wrap = document.querySelector(".video-wrap");
        const modeEl = document.getElementById("hudMode");
        const clockEl = document.getElementById("hudClock");
        const resEl = document.getElementById("hudRes");
        let started = 0, timer = null;
        const pad = n => String(n).padStart(2, "0");
        function tick() {
          const s = Math.floor((Date.now() - started) / 1000);
          clockEl.textContent = pad(Math.floor(s / 3600)) + ":" + pad(Math.floor(s / 60) % 60) + ":" + pad(s % 60);
          geoTag.tick();
        }
        // The scan beam travels the panel's real height.
        new ResizeObserver(() => wrap.style.setProperty("--feed-h", wrap.clientHeight + "px")).observe(wrap);
        return {
          live(track) {
            wrap.classList.add("is-live");
            wrap.classList.remove("booting");
            void wrap.offsetWidth; // restart the boot animation
            wrap.classList.add("booting");
            setTimeout(() => wrap.classList.remove("booting"), 950);
            modeEl.textContent = "LIVE · CAM_01";
            const st = track && track.getSettings ? track.getSettings() : {};
            resEl.textContent = st.width ? `${st.width}x${st.height} · ${Math.round(st.frameRate || 30)}FPS` : "SIGNAL_OK";
            started = Date.now();
            clearInterval(timer);
            tick();
            timer = setInterval(tick, 1000);
            faceTrack.start();
            geoTag.start();
          },
          standby() {
            faceTrack.stop();
            wrap.classList.remove("is-live", "booting");
            modeEl.textContent = "STANDBY";
            resEl.textContent = "NO_SIGNAL";
            clearInterval(timer);
            clockEl.textContent = "--:--:--";
          },
        };
      })();

      // The camera stays on until the user turns it off (Camera Power / Camera
      // Off / "camera band karo"). That choice is remembered, so a page reload
      // re-opens it, and a dropped webcam (sleep, driver hiccup) reconnects.
      const CAM_KEY = "saraCameraOn";
      function cameraWanted() {
        try { return localStorage.getItem(CAM_KEY) === "1"; } catch (e) { return false; }
      }
      function setCameraWanted(on) {
        try { localStorage.setItem(CAM_KEY, on ? "1" : "0"); } catch (e) {}
      }
      let camRetry = null;

      // Resolves true once the camera is live, false if permission was denied
      // or no camera is available.
      async function startWebcamInPage() {
        clearTimeout(camRetry);
        try {
          if (stream) stream.getTracks().forEach(track => track.stop());

          const constraints = { video: true, audio: false };
          if (cameraSelect.value) {
            constraints.video = { deviceId: { exact: cameraSelect.value } };
          }

          stream = await navigator.mediaDevices.getUserMedia(constraints);
          const track = stream.getVideoTracks()[0];
          videoEl.srcObject = stream;
          const vb = document.getElementById("videoBlur"); if (vb) { vb.srcObject = stream; vb.play().catch(() => {}); }
          videoEl.style.display = "block";
          if (noSignal) noSignal.style.display = "none";
          camToggle.checked = true;
          setCameraWanted(true);
          feedHud.live(track);
          statusEl.textContent = "SIGNAL_ACQUIRED: Camera active.";

          // Lost the device without the user asking? Reconnect.
          track.addEventListener("ended", () => {
            if (!cameraWanted() || !stream || stream.getVideoTracks()[0] !== track) return;
            statusEl.textContent = "SIGNAL_LOST: Reconnecting camera...";
            camRetry = setTimeout(startWebcamInPage, 2000);
          });

          loadCameras(); // Get actual labels once permission is granted
          return true;
        } catch (e) {
          const denied = e && (e.name === "NotAllowedError" || e.name === "SecurityError");
          statusEl.textContent = denied
            ? "ERROR: Camera permission denied. Allow it from the address bar."
            : "ERROR: Camera unavailable (" + ((e && e.name) || "unknown") + ").";
          camToggle.checked = false;
          if (denied) setCameraWanted(false);
          else if (cameraWanted()) camRetry = setTimeout(startWebcamInPage, 5000);  // busy? try again
          return false;
        }
      }

      // Only called when the user explicitly turns the camera off.
      function stopWebcamInPage() {
        clearTimeout(camRetry);
        setCameraWanted(false);
        if (stream) {
          stream.getTracks().forEach(track => track.stop());
          stream = null;
          videoEl.srcObject = null;
          const vb0 = document.getElementById("videoBlur"); if (vb0) vb0.srcObject = null;
          videoEl.style.display = "none";
          if (noSignal) noSignal.style.display = "block";
          camToggle.checked = false;
          feedHud.standby();
          statusEl.textContent = "SIGNAL_LOST: Camera offline.";
        }
      }

      camToggle.addEventListener("change", (e) => {
        if (e.target.checked) {
          startWebcamInPage();
        } else {
          stopWebcamInPage();
        }
      });

      // Dedicated "Camera Off" button — stops the in-page camera and the
      // server-side (OpenCV) camera window if one is running.
      const camOffBtn = document.getElementById("camOffBtn");
      if (camOffBtn) {
        camOffBtn.addEventListener("click", () => {
          stopWebcamInPage();
          fetch(`/run?text=${encodeURIComponent("close camera")}`, { method: "POST" }).catch(() => {});
          statusEl.textContent = "SIGNAL_LOST: Camera turned off.";
        });
      }

      cameraSelect.addEventListener("change", () => {
        camToggle.checked = true;
        startWebcamInPage();
      });
      loadCameras();

      // Re-open the camera after a reload if it was left on.
      if (cameraWanted()) startWebcamInPage();

      // --- Voice output (browser text-to-speech) ---
      const voiceToggle = document.getElementById("voiceToggle");
      let voiceOut = localStorage.getItem("voiceOut") !== "off";
      function paintVoiceToggle() {
        voiceToggle.textContent = voiceOut ? "🔊" : "🔇";
        voiceToggle.removeAttribute("title");
        voiceToggle.dataset.tip = voiceOut
          ? "Sara ki awaaz ON: jawab bolkar sunati hai. Click = awaaz band (sirf text dikhega)."
          : "Sara ki awaaz OFF: jawab sirf screen par likha aata hai. Click = awaaz ON.";
      }
      paintVoiceToggle();
      voiceToggle.addEventListener("click", () => {
        voiceOut = !voiceOut;
        localStorage.setItem("voiceOut", voiceOut ? "on" : "off");
        if (!voiceOut) stopSpeaking();
        paintVoiceToggle();
      });

      // --- Auto Mic: hands-free conversation ---------------------------------
      // When ON, the mic opens on its own and re-opens after every answer, once
      // Sara has finished speaking (so she never hears herself). It turns off
      // when you say "stop listening" / "bas karo", press Stop, or stay silent
      // three times in a row.
      const autoMicToggle = document.getElementById("autoMicToggle");
      // ON by default: after every result Sara listens again, like a live
      // voice conversation. Stays off only if the user turned it off.
      let autoMic = localStorage.getItem("autoMic") !== "off";
      let startListening = () => false;  // replaced by the voice-input block below
      // Auto Mic as a clear ON/OFF switch with an explanation on hover.
      function paintAutoMic() {
        autoMicToggle.innerHTML =
          '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0M12 17v4"/></svg>' +
          '<span class="am-label">Auto Mic</span>' +
          '<span class="am-switch' + (autoMic ? " on" : "") + '"><i></i></span>' +
          '<span class="am-state">' + (autoMic ? "ON" : "OFF") + '</span>';
        autoMicToggle.classList.toggle("am-on", autoMic);
        autoMicToggle.setAttribute("role", "switch");
        autoMicToggle.setAttribute("aria-checked", String(autoMic));
        autoMicToggle.dataset.tip = autoMic
          ? "Auto Mic ON: Sara har jawab ke baad khud sunna shuru karti hai. Mic dabane ki zaroorat nahi, bas boliye. 'Stop' bolne par band. Click = OFF."
          : "Auto Mic OFF: Sara tabhi sunti hai jab aap mic button dabate hain. Click karke ON kijiye taaki bina dabaye baat kar sakein.";
        autoMicToggle.removeAttribute("title");
      }
      function setAutoMic(on, msg) {
        autoMic = on;
        localStorage.setItem("autoMic", on ? "on" : "off");
        paintAutoMic();
        if (msg) statusEl.textContent = msg;
      }
      // Called after each answer: listen again if Auto Mic is on.
      function resumeAutoMic() {
        // Not while a song plays: the mic would hear the lyrics as commands.
        if (autoMic && !mediaPlayer.playing && !window.quranPlaying && !window.radioPlaying) setTimeout(() => startListening(), 400);
      }
      paintAutoMic();
      autoMicToggle.addEventListener("click", () => {
        setAutoMic(!autoMic, autoMic ? "AUTO MIC: off." : "AUTO MIC: on — speak any time.");
        if (autoMic) { stopSpeaking(); startListening(); }
      });

      // --- User profile (name + gender) -------------------------------------
      // Sara speaks in the OPPOSITE gender voice: male user -> female voice,
      // female user -> male voice.
      const PROFILE_KEY = "saraProfile";
      function loadProfile() {
        try { return JSON.parse(localStorage.getItem(PROFILE_KEY) || "null"); }
        catch (e) { return null; }
      }
      function saveProfile(p) { localStorage.setItem(PROFILE_KEY, JSON.stringify(p)); }
      let profile = loadProfile();

      function assistantGender() {
        // Opposite of the user. Default to female when unknown.
        return (profile && profile.gender === "female") ? "male" : "female";
      }

      // Name hints used to pick a clearly-gendered, good-quality system voice.
      const FEMALE_HINTS = ["female", "zira", "samantha", "susan", "linda", "heera",
        "kalpana", "swara", "neerja", "aria", "jenny", "google uk english female",
        "google us english", "woman", "girl", "eva", "hazel"];
      const MALE_HINTS = ["male", "david", "mark", "george", "ravi", "hemant",
        "prabhat", "madhur", "guy", "google uk english male", "man", "boy",
        "alex", "fred", "daniel"];

      function pickVoice(langPrefix, gender) {
        const all = (window.speechSynthesis && window.speechSynthesis.getVoices()) || [];
        const byLang = all.filter(v => (v.lang || "").toLowerCase().startsWith(langPrefix));
        const pool = byLang.length ? byLang : all;
        const hints = gender === "male" ? MALE_HINTS : FEMALE_HINTS;
        const anti = gender === "male" ? FEMALE_HINTS : MALE_HINTS;
        const named = v => (v.name || "").toLowerCase();
        // 1. A voice whose name matches the desired gender. Prefer "natural"/Google ones.
        const wanted = pool.filter(v => hints.some(h => named(v).includes(h)));
        if (wanted.length) {
          return wanted.find(v => /google|natural|online|premium/.test(named(v))) || wanted[0];
        }
        // 2. Otherwise avoid voices that clearly sound like the wrong gender.
        const neutral = pool.find(v => !anti.some(h => named(v).includes(h)));
        return neutral || pool[0] || null;
      }

      // Built-in browser voices (fallback when neural TTS is unavailable).
      // Resolves when the sentence has finished (so Auto Mic never hears Sara).
      function speakBrowser(text) {
        return new Promise((resolve) => {
          if (!window.speechSynthesis) return resolve();
          try {
            window.speechSynthesis.cancel();
            const isHindi = /[ऀ-ॿ]/.test(text);
            const isArabic = /[؀-ۿ]/.test(text) && !isHindi;
            const gender = assistantGender();
            const u = new SpeechSynthesisUtterance(text);
            u.lang = isArabic ? "ar-SA" : isHindi ? "hi-IN" : "en-IN";
            const v = pickVoice(isArabic ? "ar" : isHindi ? "hi" : "en", gender);
            if (v) u.voice = v;
            // Tuning for a clearer, more natural read.
            u.rate = isArabic ? 0.9 : 1.25;   // Arabic calm; otherwise the 1.25x pace
            u.pitch = gender === "female" ? 1.15 : 0.85;
            u.volume = 1.0;
            u.onend = resolve;
            u.onerror = resolve;
            // Some browsers never fire onend; don't wait forever.
            setTimeout(resolve, Math.min(60000, 2000 + text.length * 120));
            window.speechSynthesis.speak(u);
          } catch (e) { resolve(); }
        });
      }

      // Neural TTS: fetch a high-quality MP3 from the server and play it.
      let currentAudio = null;
      function stopSpeaking() {
        try { if (window.speechSynthesis) window.speechSynthesis.cancel(); } catch (e) {}
        if (currentAudio) {
          try { currentAudio.pause(); } catch (e) {}
          try { URL.revokeObjectURL(currentAudio.src); } catch (e) {}
          currentAudio = null;
        }
      }
      async function speakNeural(text, lang, gender) {
        const url = `/tts?text=${encodeURIComponent(text)}&lang=${lang}&gender=${gender}`;
        const r = await fetch(url);
        if (!r.ok) throw new Error("tts " + r.status);
        const blob = await r.blob();
        if (!blob || blob.size === 0) throw new Error("empty audio");
        stopSpeaking();
        const audio = new Audio(URL.createObjectURL(blob));
        currentAudio = audio;
        await aiVisual.attachAudio(audio);  // wave follows the real voice
        const finished = new Promise((resolve) => {
          audio.addEventListener("ended", resolve);
          audio.addEventListener("pause", resolve);  // stopSpeaking() pauses
          audio.addEventListener("error", resolve);
        });
        audio.onended = () => { if (currentAudio === audio) stopSpeaking(); };
        await audio.play();
        await finished;  // resolve only once Sara has finished talking
      }

      // Speak a reply. Returns a promise that resolves when speech is over.
      function speakOut(text) {
        if (!voiceOut || !text) return Promise.resolve();
        const isHindi = /[ऀ-ॿ]/.test(text);
        const isArabic = /[؀-ۿ]/.test(text) && !isHindi;   // e.g. Bismillah before a recitation
        const lang = isArabic ? "ar" : isHindi ? "hi" : "en";
        const gender = assistantGender();
        // Prefer neural TTS; fall back to the browser's built-in voices.
        aiVisual.setState("speaking");
        return speakNeural(text, lang, gender)
          .catch(() => speakBrowser(text))
          .finally(() => {
            aiVisual.detachAudio();
            if (aiVisual.state === "speaking") aiVisual.setState("idle");
          });
      }
      // Voice list loads async in some browsers — warm it up.
      if (window.speechSynthesis) {
        window.speechSynthesis.getVoices();
        window.speechSynthesis.onvoiceschanged = () => window.speechSynthesis.getVoices();
      }

      // --- Professional voice replies --------------------------------------
      // Tasks get a formal wrapper: a "please wait" line when the job is slow,
      // then a completion (or apology) line before the details. Chat and info
      // answers are spoken as-is.
      const DEVANAGARI = /[ऀ-ॿ]/;
      function honorific(hindi) {
        const female = profile && profile.gender === "female";
        return hindi ? (female ? "मैडम" : "सर") : (female ? "Ma'am" : "Sir");
      }
      function pleaseWaitLine(hindi) {
        const h = honorific(hindi);
        const will = assistantGender() === "female" ? "करूँगी" : "करूँगा";
        return hindi
          ? `जी ${h}, कृपया थोड़ा इंतज़ार करें। काम पूरा होते ही मैं आपको अपडेट ${will}।`
          : `Please wait a moment, ${h}. I will update you as soon as it is complete.`;
      }
      function taskDoneLine(ok, hindi) {
        const h = honorific(hindi);
        if (ok) return hindi ? `${h}, आपका काम पूरा हो गया है।` : `I have completed your task, ${h}.`;
        return hindi ? `माफ़ कीजिए ${h}, आपका काम पूरा नहीं हो पाया।` : `I'm sorry, ${h}. I could not complete your task.`;
      }
      function professionalReply(data) {
        if (data.kind !== "task") return data.message;
        const hindi = DEVANAGARI.test(data.message || "");
        const msg = (data.message || "").replace(/\\.\\.\\.$/, hindi ? "।" : ".");
        return taskDoneLine(data.ok, hindi) + " " + msg;
      }

      // Camera commands switch the dashboard's own camera (the browser asks
      // for permission the first time); everything else gets the professional
      // wrapper. Returns the line Sara should speak.
      async function replyFor(data, hindi) {
        if (data.kind === "camera_open") {
          const ok = stream ? true : await startWebcamInPage();
          return taskDoneLine(ok, hindi) + " " + (ok
            ? (hindi ? "कैमरा चालू है।" : "The camera is now on.")
            : (hindi ? "कृपया ब्राउज़र में कैमरे की अनुमति दें।" : "Please allow camera access in the browser."));
        }
        if (data.kind === "stop_all") {
          stopSpeaking();
          mediaPlayer.hideMenu();
          mediaPlayer.stop();
          if (window.quranBox) window.quranBox.stop();
          if (window.radioBox) window.radioBox.stop();
          return honorific(DEVANAGARI.test(data.message)) + ", " + data.message;
        }
        if (data.kind === "radio" && window.radioBox) {
          const r = data.data || {};
          if (r.action === "stop") window.radioBox.stop();
          else window.radioBox.open();                   // tune after Sara speaks (afterSpeech)
          return honorific(DEVANAGARI.test(data.message)) + ", " + data.message;
        }
        if (data.kind === "media_menu" && data.data && data.data.category === "quran" && window.quranBox) {
          window.quranBox.open();                        // Quran gets its own window
          const hindi = DEVANAGARI.test(data.message);
          return honorific(hindi) + ", " + (hindi ? "क़ुरआन की तिलावत खोल दी है। कोई सूरह चुनिए।" : "The Quran recitations are open. Please choose a surah.");
        }
        if (data.kind === "media_menu") {
          mediaPlayer.showMenu(data.data && data.data.songs, data.data && data.data.category);
          // Speak just the intro line; the numbered list is on screen.
          const hindi = DEVANAGARI.test(data.message);
          const first = data.message.split("\\n")[0].replace(/[:।]$/, hindi ? "।" : ".");
          return honorific(hindi) + ", " + first;
        }
        if (data.kind === "media_play" || data.kind === "media_stop") {
          mediaPlayer.hideMenu();
          if (data.kind === "media_stop") { mediaPlayer.stop(); if (window.quranBox) window.quranBox.stop(); }
          if (data.kind === "media_play" && window.quranBox && data.data && (data.data.surah || window.quranBox.has(data.data.video_id))) {
            window.quranBox.open();                      // show the window now; play after Sara speaks
          }
          // Match the reply's language (Hinglish commands get Hindi replies).
          return taskDoneLine(true, DEVANAGARI.test(data.message)) + " " + data.message;
        }
        if ((data.kind || "").startsWith("stage")) {
          const d = data.data || {};
          if (data.kind === "stage_close") stage.close();
          else if (data.kind === "stage_max" && !(await stage.setMax(true))) {
            return DEVANAGARI.test(data.message)
              ? "ब्राउज़र आवाज़ से फ़ुल स्क्रीन नहीं करने देता। कृपया ⛶ बटन दबाइए या F11 दबाइए।"
              : "The browser won't go full screen from a voice command. Please click the ⛶ button or press F11.";
          }
          else if (data.kind === "stage_restore") await stage.setMax(false);
          else if (d.type === "news") { stage.news(d); return data.message; }  // read the headlines
          return taskDoneLine(true, DEVANAGARI.test(data.message)) + " " + data.message;
        }
        if (data.kind === "skills_open") {
          if (window.skillsBox) window.skillsBox.open();
          return honorific(DEVANAGARI.test(data.message)) + ", " + data.message;
        }
        if (data.kind === "notebook_open") {
          if (notebook) notebook.show(data.data && data.data.name);
          return honorific(DEVANAGARI.test(data.message)) + ", " + data.message;
        }
        if (data.kind === "calc_open") {
          if (window.saraCalc) window.saraCalc.show(data.data && data.data.expr);
          return honorific(DEVANAGARI.test(data.message)) + ", " + data.message;
        }
        if (data.kind === "ac_open") {
          acScreen.open();
          return honorific(DEVANAGARI.test(data.message)) + ", " + data.message;
        }
        if (data.kind === "bt_open") {
          btScreen.open(false);  // the server already started the search
          return honorific(DEVANAGARI.test(data.message)) + ", " + data.message;
        }
        if (data.kind === "camera_close") {
          stopWebcamInPage();
          return taskDoneLine(true, hindi) + " " + (hindi ? "कैमरा बंद कर दिया है।" : "The camera is now off.");
        }
        return professionalReply(data);
      }

      // Things that should start only once Sara has finished speaking, so her
      // voice doesn't talk over the music/video.
      function afterSpeech(data) {
        if (data.kind === "radio" && window.radioBox && data.data && data.data.action === "play") {
          window.radioBox.tune(data.data.query);
        }
        if (data.kind === "media_play") {
          if (window.radioBox && window.radioBox.playing) window.radioBox.stop();
          // A surah plays in the Quran window, everything else in the media panel.
          if (window.quranBox && data.data && data.data.surah) window.quranBox.playSurah(data.data.surah);
          else if (window.quranBox && data.data && window.quranBox.has(data.data.video_id)) window.quranBox.playVideo(data.data.video_id);
          else mediaPlayer.play(data.data);
        }
      }

      async function runCustomCommand() {
        const text = cmdInput.value.trim();
        if (!text) return;
        const hindiCmd = DEVANAGARI.test(text) || isHindiUI();

        // Echo the typed command into the System Logs, then clear the input.
        logsEl.textContent += "> " + text + "\\n";
        logsEl.scrollTop = logsEl.scrollHeight;
        cmdInput.value = "";

        // Picking from the open playlist menu ("2", "teesra") is handled here.
        if (await mediaPlayer.chooseFromText(text)) { resumeAutoMic(); return; }

        // "hindi mein baat karo" / "english mein baat karo"
        const switched = languageSwitch(text);
        if (switched) {
          statusEl.textContent = "RESULT: " + switched;
          setAiResponse(switched);
          speakOut(switched).then(resumeAutoMic);
          return;
        }

        statusEl.innerHTML = thinkingHTML("Executing");
        showThinking("Processing");  // animate the output panel for every command

        // Slow job? Tell the user to hold on instead of going silent.
        let waitSpeech = null;
        const waitTimer = setTimeout(() => { waitSpeech = speakOut(pleaseWaitLine(hindiCmd)); }, 1500);

        try {
          const resp = await fetch(`/run?text=${encodeURIComponent(text)}&in_page=true&reply_lang=${replyLangParam()}`, { method: "POST" });
          const data = await resp.json();
          clearTimeout(waitTimer);
          statusEl.textContent = `RESULT: ${data.message}`;
          setAiResponse(data.message);  // typewriter the result into the output panel
          cmdInput.focus();  // ready for the next command (already cleared above)
          if (waitSpeech) await waitSpeech;  // let the "please wait" line finish

          await speakOut(await replyFor(data, hindiCmd));  // wait until Sara finishes talking...
          afterSpeech(data);  // music/video after her voice
          resumeAutoMic();               // ...then listen for the next question
        } catch (e) {
          clearTimeout(waitTimer);
          if (procBar) procBar.classList.remove("active");
          aiBrain.classList.remove("type-caret");
          statusEl.textContent = "ERROR: Comm link failed.";
          aiBrain.textContent = "Error: Comm link lost.";
          resumeAutoMic();
        }
      }

      // --- Today_News panel: headlines in English / Hindi -------------------
      const newsPanel = (() => {
        const list = document.getElementById("newsList");
        const updatedEl = document.getElementById("newsUpdated");
        const refreshBtn = document.getElementById("newsRefresh");
        const tabs = [...document.querySelectorAll(".news-tabs button")];
        const NEWS_KEY = "saraNewsLang";
        let lang;
        try { lang = localStorage.getItem(NEWS_KEY); } catch (e) {}
        if (lang !== "en" && lang !== "hi") lang = isHindiUI() ? "hi" : "en";

        function render(items) {
          const hindi = lang === "hi";
          if (!items.length) {
            const li = document.createElement("li");
            li.className = "news-empty";
            li.textContent = hindi ? "अभी खबरें उपलब्ध नहीं हैं।" : "No headlines available right now.";
            list.replaceChildren(li);
            return;
          }
          list.replaceChildren(...items.map((n, i) => {
            const li = document.createElement("li");
            li.className = "news-item";
            const a = document.createElement("a");
            a.href = n.link;
            a.target = "_blank";
            a.rel = "noopener";
            a.style.animationDelay = (i * 45) + "ms";
            const t = document.createElement("div");
            t.className = "news-title";
            t.textContent = n.title;
            const meta = document.createElement("div");
            meta.className = "news-meta";
            const src = document.createElement("b");
            src.textContent = n.source || "";
            const when = timeAgo(n.published, hindi);
            meta.append(src, when ? " · " + when : "");
            a.append(t, meta);
            stage.openInReader(a, n, { items, lang, topic: "" });
            li.append(a);
            return li;
          }));
          list.scrollTop = 0;
        }

        async function load() {
          tabs.forEach(b => {
            const on = b.dataset.lang === lang;
            b.classList.toggle("active", on);
            b.setAttribute("aria-selected", on);
          });
          refreshBtn.classList.add("spin");
          try {
            const r = await fetch("/news/headlines?lang=" + lang);
            const d = await r.json();
            render(d.items || []);
            const t = new Date();
            updatedEl.textContent = (lang === "hi" ? "अपडेट: " : "UPDATED ") +
              String(t.getHours()).padStart(2, "0") + ":" + String(t.getMinutes()).padStart(2, "0") +
              (d.stale ? (lang === "hi" ? " (पुरानी)" : " (CACHED)") : "");
          } catch (e) {
            updatedEl.textContent = lang === "hi" ? "कनेक्शन नहीं" : "OFFLINE";
          } finally {
            refreshBtn.classList.remove("spin");
          }
        }

        tabs.forEach(b => b.addEventListener("click", () => {
          if (b.dataset.lang === lang) return;
          lang = b.dataset.lang;
          try { localStorage.setItem(NEWS_KEY, lang); } catch (e) {}
          load();
        }));
        refreshBtn.addEventListener("click", load);
        setInterval(load, 10 * 60 * 1000);

        // Ticker: step to the next headline every 5 s (paused on hover/focus),
        // wrapping back to the top at the end.
        let paused = false;
        list.addEventListener("mouseenter", () => { paused = true; });
        list.addEventListener("mouseleave", () => { paused = false; });
        list.addEventListener("focusin", () => { paused = true; });
        list.addEventListener("focusout", () => { paused = false; });
        const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        setInterval(() => {
          if (paused || reduceMotion || document.hidden) return;
          if (list.scrollHeight <= list.clientHeight + 2) return;  // everything fits
          const items = [...list.children];
          const next = items.find(li => li.offsetTop - list.offsetTop > list.scrollTop + 2);
          const atEnd = list.scrollTop + list.clientHeight >= list.scrollHeight - 2;
          list.scrollTo({ top: atEnd || !next ? 0 : next.offsetTop - list.offsetTop, behavior: "smooth" });
        }, 5000);
        load();
        return { load };
      })();

      // --- Bluetooth screen -------------------------------------------------
      // Polls /bluetooth/state once a second while open. Paired devices show
      // instantly; the nearby search takes ~30 s (Windows' inquiry time).
      const btScreen = (() => {
        const screen = document.getElementById("btScreen");
        const radioEl = document.getElementById("btRadio");
        const scanBtn = document.getElementById("btScan");
        const bar = document.getElementById("btBar");
        const statusEl = document.getElementById("btStatus");
        const promptEl = document.getElementById("btPrompt");
        const pairedEl = document.getElementById("btPaired");
        const nearbyEl = document.getElementById("btNearby");
        const countEl = document.getElementById("btNearbyCount");
        const ICONS = { audio: "🎧", phone: "📱", computer: "💻", input: "⌨️", wearable: "⌚", tv: "📺", other: "🔷" };
        let timer = null, last = "", busy = null, lastPairStage = null, lastFocus = null, lastAuto = [];

        async function call(path) {
          try { return await (await fetch(path, { method: "POST" })).json(); }
          catch (e) { return { ok: false }; }
        }
        function el(tag, cls, text) {
          const e = document.createElement(tag);
          if (cls) e.className = cls;
          if (text != null) e.textContent = text;
          return e;
        }
        function signal(rssi) {
          const s = el("span", "bt-sig");
          const n = rssi == null ? 0 : rssi > -60 ? 4 : rssi > -70 ? 3 : rssi > -80 ? 2 : 1;
          for (let i = 0; i < 4; i++) {
            const b = el("i", i < n ? "lit" : "");
            b.style.height = (3 + i * 2) + "px";
            s.append(b);
          }
          s.title = rssi == null ? "" : rssi + " dBm";
          return s;
        }
        function button(label, cls, handler) {
          const b = el("button", "bt-act" + (cls ? " " + cls : ""), label);
          b.type = "button";
          b.addEventListener("click", async () => {
            b.disabled = true;
            busy = b;
            await handler();
            busy = null;
            refresh(true);
          });
          return b;
        }

        function row(d) {
          const li = el("li", "bt-item" + (d.connected ? " connected" : ""));
          li.append(el("span", "bt-ico", ICONS[d.kind] || ICONS.other));
          const info = el("div", "bt-info");
          info.append(el("div", "bt-name", d.name));
          const sub = el("div", "bt-sub");
          if (d.connected) sub.append(el("span", "bt-chip on", "CONNECTED"));
          else if (d.paired) sub.append(el("span", "bt-chip paired", "PAIRED"));
          if (!d.paired) sub.append(signal(d.rssi));
          sub.append(el("span", "", d.mac.toUpperCase() + (d.le ? " · LE" : "")));
          info.append(sub);
          const actions = el("div", "bt-row-actions");
          if (d.paired) {
            if (d.audio) {
              actions.append(d.connected
                ? button("Disconnect", "", () => call("/bluetooth/connect?on=false&mac=" + encodeURIComponent(d.mac)))
                : button("Connect", "", async () => {
                    statusEl.textContent = "Connecting to " + d.name + "... make sure it's on and nearby.";
                    await call("/bluetooth/connect?on=true&mac=" + encodeURIComponent(d.mac));
                  }));
            }
            if (d.audio) {
              // ★ Auto-connect: reconnect whenever it's switched on nearby.
              const isAuto = (lastAuto || []).includes(d.mac);
              const star = button(isAuto ? "★ Auto" : "☆ Auto", isAuto ? "auto on" : "auto", async () => {
                const r = await call("/bluetooth/auto?on=" + !isAuto + "&mac=" + encodeURIComponent(d.mac) +
                                     "&name=" + encodeURIComponent(d.name));
                statusEl.textContent = r.ok
                  ? (isAuto ? "Auto-connect off for " : "Auto-connect on: Sara will connect ") + d.name +
                    (isAuto ? "." : " whenever it's switched on nearby.")
                  : "Couldn't save auto-connect.";
              });
              star.title = isAuto ? "Auto-connect is on (click to turn off)" : "Auto-connect when it comes in range";
              star.setAttribute("aria-pressed", String(isAuto));
              actions.prepend(star);
            }
            actions.append(button("Forget", "ghost", async () => {
              if (confirm("Forget " + d.name + "? You'll need to pair it again.")) {
                await call("/bluetooth/unpair?id=" + encodeURIComponent(d.id));
              }
            }));
          } else {
            actions.append(button("Pair", "", () => call("/bluetooth/pair?id=" + encodeURIComponent(d.id))));
          }
          li.append(info, actions);
          return li;
        }

        function list(target, items, empty) {
          if (!items.length) { target.replaceChildren(el("li", "bt-empty", empty)); return; }
          target.replaceChildren(...items.map(row));
        }

        function renderPrompt(p, st) {
          const name = ((st.nearby || []).concat(st.paired || []).find(d => d.id === p.id) || {}).name || "the device";
          if (!p.stage || p.stage === "done" || p.stage === "failed") {
            promptEl.hidden = true;
            if (p.stage && p.stage !== lastPairStage) {
              statusEl.textContent = p.stage === "done" ? "Paired with " + name + "." : "Pairing failed (" + (p.status || "error") + ").";
            }
            lastPairStage = p.stage;
            return;
          }
          if (p.stage === lastPairStage && p.stage === "pairing" && !promptEl.hidden) return;
          lastPairStage = p.stage;
          promptEl.hidden = false;
          promptEl.replaceChildren();
          if (p.stage === "pairing") {
            promptEl.append(el("span", "", "Pairing with " + name + "... accept on the device if it asks."));
          } else if (p.stage === "display_pin") {
            promptEl.append(el("span", "", "Type this PIN on " + name + " and press Enter:"), el("b", "bt-pin", p.pin));
          } else if (p.stage === "confirm_pin") {
            const acts = el("div", "bt-row-actions");
            acts.append(
              button("Yes, it matches", "", () => call("/bluetooth/pair/respond?answer=yes")),
              button("No", "ghost", () => call("/bluetooth/pair/respond?answer=no")));
            promptEl.append(el("span", "", "Does " + name + " show this PIN?"), el("b", "bt-pin", p.pin), acts);
          } else if (p.stage === "provide_pin") {
            const input = el("input");
            input.placeholder = "PIN (often 0000)";
            input.setAttribute("aria-label", "PIN");
            const acts = el("div", "bt-row-actions");
            acts.append(input, button("OK", "", () => call("/bluetooth/pair/respond?answer=" + encodeURIComponent(input.value))));
            promptEl.append(el("span", "", "Enter the PIN for " + name + ":"), acts);
            setTimeout(() => input.focus(), 50);
          }
        }

        function render(st) {
          if (!st.available) {
            statusEl.textContent = "Bluetooth support isn't installed on the server. Install the project's bluetooth extra and restart.";
            scanBtn.disabled = true;
            return;
          }
          radioEl.checked = !!st.radio;
          scanBtn.disabled = st.scanning || st.radio === false;
          scanBtn.textContent = st.scanning ? "Searching..." : "⟳ Search";
          bar.style.width = (st.scanning ? Math.round(st.progress * 100) : 0) + "%";
          if (st.radio === false) statusEl.textContent = "Bluetooth is off. Turn it on to see devices.";
          else if (st.scanning) statusEl.textContent = "Searching for nearby devices... (about 30 seconds)";
          else if (!lastPairStage || lastPairStage === "pairing") statusEl.textContent = "Tap Connect or Pair on a device. ☆ Auto = connect it automatically when it's switched on nearby.";
          if (busy) return;  // don't swap buttons out from under a click
          lastAuto = st.auto || [];
          list(pairedEl, st.paired || [], st.radio === false ? "—" : "No paired devices yet.");
          list(nearbyEl, st.nearby || [], st.scanning ? "Looking..." : "Nothing found. Put the device in pairing mode and tap Search.");
          countEl.textContent = st.nearby && st.nearby.length ? "(" + st.nearby.length + ")" : "";
          renderPrompt(st.pairing || {}, st);
        }

        async function refresh(force) {
          let st;
          try { st = await (await fetch("/bluetooth/state")).json(); } catch (e) { return; }
          const key = JSON.stringify(st);
          if (!force && key === last) return;
          last = key;
          render(st);
        }

        function open(scan) {
          if (!screen.hidden) { if (scan) call("/bluetooth/scan"); return; }
          lastFocus = document.activeElement;
          screen.hidden = false;
          last = "";
          statusEl.textContent = "Loading devices...";
          if (scan) call("/bluetooth/scan");
          refresh(true);
          timer = setInterval(refresh, 1000);
          setTimeout(() => scanBtn.focus(), 50);
        }
        function close() {
          screen.hidden = true;
          clearInterval(timer);
          timer = null;
          if (lastFocus) lastFocus.focus();
        }

        scanBtn.addEventListener("click", async () => { await call("/bluetooth/scan"); refresh(true); });
        radioEl.addEventListener("change", async () => {
          statusEl.textContent = radioEl.checked ? "Turning Bluetooth on..." : "Turning Bluetooth off...";
          await call("/bluetooth/radio?on=" + radioEl.checked);
          refresh(true);
        });
        document.getElementById("btClose").addEventListener("click", close);
        screen.addEventListener("click", (e) => { if (e.target === screen) close(); });
        document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !screen.hidden) close(); });
        document.getElementById("btBtn").addEventListener("click", () => open(true));
        return { open, close };
      })();
      // --- AC screen: Blue Star Smart AC ("ac kholo", or the ❄ header button) ---
      const acScreen = (() => {
        const $ = (id) => document.getElementById(id);
        const screen = $("acScreen"), app = $("acCard"), setupEl = $("acSetup"), mainEl = $("acMain");
        const msgEl = $("acMsg"), pick = $("acPick"), live = $("acLive"), hero = $("acSecHero");
        const MIN = 16, MAX = 30;
        const MODES = [["cool", "❄️", "Cool", "Best for hot days"], ["dry", "💧", "Dry", "Reduces humidity"],
                       ["fan", "🌀", "Fan", "Air circulation"], ["auto", "✨", "Auto", "Smart comfort"]];
        const FANS = [["auto", "A", "Auto", "Adjusts itself"], ["low", "▂", "Low", "Quiet"], ["medium", "▄", "Medium", "Balanced"],
                      ["high", "▆", "High", "Strong airflow"], ["turbo", "█", "Turbo", "Maximum"]];
        const SWINGS = [["off", "▬", "Off", "Fixed"], ["auto", "≋", "Swing", "Up and down"], ["1", "1", "Level 1", "Top"],
                        ["2", "2", "Level 2", ""], ["3", "3", "Level 3", "Middle"], ["4", "4", "Level 4", ""], ["5", "5", "Level 5", "Down"]];
        const TIMERS = [[0, "⏻", "Off", "No timer"], [30, "⏱", "30 min", "Switch off"], [60, "⏱", "1 hour", "Switch off"],
                        [120, "⏱", "2 hours", "Switch off"], [240, "⏱", "4 hours", "Switch off"], [480, "⏱", "8 hours", "Switch off"]];
        const FEATS = [["turbo", "🚀", "Turbo", "Fastest cooling"], ["sleep", "🌙", "Sleep", "Night comfort"], ["display", "💡", "Display", "Panel light"],
                       ["buzzer", "🔔", "Beep", "Button sound"], ["hswing", "↔️", "H-Swing", "Side to side"], ["clean", "🧼", "Self clean", "Dry the coil"]];
        const ECOS = ["40", "60", "80", "100", "110"];
        const TABS = [["mode", "Mode"], ["fan", "Fan"], ["swing", "Swing"], ["timer", "Timer"], ["more", "More"]];
        let devs = [], curId = "", cur = null, timer = null, lastFocus = null, tab = "mode";
        let inflight = 0, tempTimer = null, pendingTemp = null, updatedAt = 0, timerSecs = null;
        let enDays = 1, hiDays = 1, repDays = 7, welcomed = false, tick = 0, setupKey = "";
        const hist = {}, reports = {};

        function el(tag, cls, text) {
          const e = document.createElement(tag);
          if (cls) e.className = cls;
          if (text != null) e.textContent = text;
          return e;
        }
        function say(text, err) { msgEl.textContent = text || ""; msgEl.classList.toggle("err", !!err); }
        async function post(path) {
          try { return await (await fetch(path, { method: "POST" })).json(); }
          catch (e) { return { ok: false, error: "Can't reach Sara's server." }; }
        }
        function fmtMin(m) { return m >= 60 ? Math.floor(m / 60) + " h " + (m % 60) + " min" : m + " min"; }
        function fmtLeft(s) { const m = Math.ceil(s / 60); return m >= 60 ? Math.floor(m / 60) + "h " + (m % 60) + "m" : m + " min"; }

        (() => {
          const g = $("acTicks");
          for (let t = MIN; t <= MAX; t++) {
            const a = (135 + (t - MIN) / (MAX - MIN) * 270) * Math.PI / 180;
            const r1 = t % 2 === 0 ? 80 : 83, r2 = 88;
            const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
            line.setAttribute("x1", 120 + r1 * Math.cos(a)); line.setAttribute("y1", 120 + r1 * Math.sin(a));
            line.setAttribute("x2", 120 + r2 * Math.cos(a)); line.setAttribute("y2", 120 + r2 * Math.sin(a));
            line.setAttribute("class", "tick"); line.setAttribute("stroke-width", t % 2 === 0 ? 2 : 1);
            g.append(line);
          }
        })();

        // ---- commands ----
        async function send(path, patch, label) {
          if (!cur) return;
          cur = Object.assign({}, cur, patch);
          render();
          inflight++;
          say("Sending · " + label + "...");
          const r = await post(path + (path.includes("?") ? "&" : "?") + "id=" + encodeURIComponent(cur.id));
          inflight--;
          if (r.ok) { if (r.device) cur = r.device; say("✓ " + label); render(); }
          else { say(r.error || "The AC didn't accept that.", true); refresh(true); }
        }
        const doMode = (v) => send("/ac/mode?value=" + v, { mode: v, power: true }, "Mode: " + v);
        const doFan = (v) => send("/ac/fan?value=" + v, { fan: v }, "Fan: " + v);
        const doSwing = (v) => send("/ac/swing?value=" + v, { vswing: v }, "Swing: " + (v === "off" ? "off" : v));
        const doEco = (v) => send("/ac/eco?value=" + v, { eco: v }, v === "off" ? "Eco off" : "Eco " + v + "%");
        const doFeat = (name, label) => {
          const on = !(cur && cur[name]);
          send("/ac/feature?name=" + name + "&on=" + on, { [name]: on }, label + (on ? " on" : " off"));
        };
        async function doTimer(min) {
          const r = await post("/ac/timer?minutes=" + min);
          if (r.ok) { timerSecs = r.timer_secs; say(min ? "✓ AC will switch off in " + fmtMin(min) : "✓ Timer cleared"); render(); }
          else say("Couldn't set the timer.", true);
        }
        function changeTemp(step) {
          if (!cur) return;
          const base = pendingTemp != null ? pendingTemp : Math.round(cur.target || 24);
          pendingTemp = Math.max(MIN, Math.min(MAX, base + step));
          render();
          clearTimeout(tempTimer);
          tempTimer = setTimeout(() => {
            const t = pendingTemp;
            pendingTemp = null;
            send("/ac/temp?value=" + t, { target: t, power: true }, "Temperature " + t + "°C");
          }, 600);
        }
        const cycle = (list, now) => list[(Math.max(0, list.indexOf(now)) + 1) % list.length];

        // ---- control tabs / tiles ----
        function tile(icon, title, sub, pressed, onClick, disabled) {
          const b = el("button", "ac-tile");
          b.type = "button";
          b.append(el("span", "ic", icon), el("b", "", title));
          if (sub) b.append(el("small", "", sub));
          b.setAttribute("aria-pressed", String(!!pressed));
          b.disabled = !!disabled;
          b.addEventListener("click", onClick);
          return b;
        }
        function renderTabs() {
          const box = $("acTabs");
          if (box.childElementCount) {
            for (const b of box.children) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
            return;
          }
          for (const [id, label] of TABS) {
            const b = el("button", "", label);
            b.type = "button"; b.dataset.tab = id; b.setAttribute("role", "tab");
            b.addEventListener("click", () => { tab = id; renderTabs(); renderTiles(); });
            box.append(b);
          }
          renderTabs();
        }
        function renderTiles() {
          const d = cur, box = $("acTiles"), off = !d.online;
          box.replaceChildren();
          box.className = "ac-tiles" + (tab === "more" || tab === "timer" ? " n3" : "");
          if (tab === "mode") for (const [v, i, t, s] of MODES) box.append(tile(i, t, s, d.power && d.mode === v, () => doMode(v), off));
          else if (tab === "fan") for (const [v, i, t, s] of FANS) box.append(tile(i, t, s, d.fan === v, () => doFan(v), off));
          else if (tab === "swing") for (const [v, i, t, s] of SWINGS) box.append(tile(i, t, s, d.vswing === v, () => doSwing(v), off));
          else if (tab === "timer") for (const [m, i, t, s] of TIMERS) box.append(tile(i, t, s, m === 0 ? !timerSecs : false, () => doTimer(m), false));
          else {
            for (const [n, i, t, s] of FEATS) box.append(tile(i, t, s, d[n], () => doFeat(n, t), off));
            for (const v of ECOS) box.append(tile("🌿", "Eco " + v + "%", "Convertible", d.eco === v, () => doEco(d.eco === v ? "off" : v), off));
          }
        }

        // ---- render ----
        function greeting() {
          const h = new Date().getHours();
          return h < 12 ? "Good Morning" : h < 17 ? "Good Afternoon" : "Good Evening";
        }
        function render() {
          if (!cur) return;
          const d = cur;
          const target = pendingTemp != null ? pendingTemp : Math.round(d.target || 24);
          app.classList.toggle("is-off", !d.power);
          hero.classList.toggle("on", !!(d.power && d.online));
          $("acTitle").textContent = greeting() + ",";
          $("acSub").textContent = !d.online ? d.name + " is offline. Check its power and Wi-Fi."
            : d.power ? d.name + " is keeping you comfortable." : d.name + " is on standby.";
          $("acTarget").textContent = target;
          $("acArc").setAttribute("stroke-dasharray", (75 * (target - MIN) / (MAX - MIN)).toFixed(2) + " 100");
          $("acOnText").textContent = !d.online ? "AC is OFFLINE" : d.power ? "AC is ON" : "AC is OFF";
          $("acModeText").textContent = d.power ? { cool: "Cooling Mode", dry: "Dry Mode", fan: "Fan Mode", auto: "Auto Mode" }[d.mode] || d.mode : "Standby";
          const room = d.room;
          $("acRoom").textContent = room != null ? room.toFixed(1) + "°C" : "--";
          const rn = $("acRoomNote");
          rn.textContent = room == null ? "--" : room <= 26 ? "Comfortable" : room <= 30 ? "Warm" : "Hot";
          rn.className = room != null && room > 26 ? "warn" : "";
          const gap = room != null ? room - target : null;
          $("acGap").textContent = gap == null || !d.power ? "--" : Math.abs(gap).toFixed(1) + "°";
          $("acGapNote").textContent = !d.power ? "AC is off" : gap == null ? "--" : gap > 0.5 ? "to reach target" : "Target reached";
          $("acMinus").disabled = !d.online || target <= MIN;
          $("acPlus").disabled = !d.online || target >= MAX;
          // quick bar
          $("qPowerV").textContent = d.power ? "On" : "Off";
          $("qModeV").textContent = d.mode;
          $("qFanV").textContent = d.fan;
          $("qSwingV").textContent = d.vswing === "off" ? "Off" : d.vswing === "auto" ? "Auto" : "Level " + d.vswing;
          $("qTimerV").textContent = timerSecs ? fmtLeft(timerSecs) : "Off";
          for (const b of $("acQuick").children) b.disabled = !d.online;
          // device info
          const rows = [["Brand", "Blue Star"], ["Name", d.name], ["Model", d.model || "--"], ["Serial No.", d.id || "--"],
                        ["MAC Address", d.mac || "--"], ["Firmware", d.version || "--"],
                        ["Wi-Fi", d.rssi == null ? "--" : (d.rssi > -60 ? "Strong" : d.rssi > -75 ? "Good" : "Weak") + " (" + d.rssi + " dBm)"],
                        ["Compressor", d.run_hours == null ? "--" : Number(d.run_hours).toLocaleString() + " h run"]];
          const dl = $("acDevList");
          dl.replaceChildren();
          for (const [k, v] of rows) { const r = el("div"); r.append(el("dt", "", k), el("dd", "", String(v))); r.lastChild.title = String(v); dl.append(r); }
          const bad = !d.online || !!d.error;
          $("acHealth").classList.toggle("bad", bad);
          $("acHealthT").textContent = !d.online ? "AC Offline" : d.error ? "Fault " + d.error : "Working Perfectly";
          $("acHealthS").textContent = !d.online ? "Check power / Wi-Fi" : d.error ? "Contact Blue Star service" : "No issues detected";
          $("acUpd").textContent = updatedAt ? "Updated " + new Date(updatedAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
          live.className = "ac-live " + (d.online ? "on" : "off");
          live.querySelector("b").textContent = d.online ? "Connected" : "Offline";
          renderTabs();
          renderTiles();
        }

        // ---- energy + history ----
        function makeRange(box, get, set) {
          for (const [days, label] of [[1, "Today"], [7, "7 Days"], [30, "30 Days"]]) {
            const b = el("button", "", label);
            b.type = "button";
            b.setAttribute("aria-pressed", String(get() === days));
            b.addEventListener("click", () => {
              set(days);
              for (const x of box.children) x.setAttribute("aria-pressed", String(x === b));
              paintHistory();
              loadHistory(days);
            });
            box.append(b);
          }
        }
        async function loadHistory(days) {
          try { hist[days] = await (await fetch("/ac/history?days=" + days)).json(); } catch (e) { return; }
          paintHistory();
        }
        function paintHistory() {
          const en = hist[enDays], hi = hist[hiDays];
          if (en) {
            $("acEnKwh").textContent = en.kwh.toFixed(1);
            $("acEnArc").setAttribute("stroke-dasharray", Math.min(100, en.on_minutes / (enDays * 1440) * 100).toFixed(1) + " 100");
            $("acEnCost").textContent = "₹ " + en.cost.toFixed(1);
            $("acEnAvg").textContent = en.avg_daily_kwh.toFixed(1) + " kWh";
            $("acEnEff").textContent = !en.on_minutes ? "--" : en.avg_target >= 24 ? "Good" : en.avg_target >= 22 ? "Fair" : "High use";
            const note = $("acEnNote");
            $("acEnArc").style.opacity = en.on_minutes ? 1 : 0;
            if (!en.since) note.textContent = "No usage recorded yet. Sara logs the AC every 5 minutes while she is running.";
            else if (!en.on_minutes) note.textContent = "Recording since " + new Date(en.since * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) + ". The AC hasn't been ON in this period.";
            else note.textContent = "Estimate: run time × " + en.watts + " W × ₹" + en.rate + "/kWh. Set BLUESTAR_WATTS / BLUESTAR_RATE in .env to match your AC.";
          }
          if (hi) {
            $("acSumOn").textContent = hi.on_minutes >= 60 ? Math.floor(hi.on_minutes / 60) + "h " + (hi.on_minutes % 60) + "m" : hi.on_minutes + "m";
            $("acSumTemp").textContent = hi.avg_target != null ? hi.avg_target.toFixed(0) + "°C" : "--";
            $("acSumKwh").textContent = hi.kwh.toFixed(1) + " kWh";
            drawChart(hi);
          }
        }
        function svgEl(tag, attrs) {
          const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
          for (const k in attrs) e.setAttribute(k, attrs[k]);
          return e;
        }
        function smoothPath(p, lo, hi) {
          let d = "M" + p[0][0].toFixed(1) + " " + p[0][1].toFixed(1);
          const cl = (v) => Math.max(lo, Math.min(hi, v));
          for (let i = 0; i < p.length - 1; i++) {
            const p0 = p[i - 1] || p[i], p1 = p[i], p2 = p[i + 1], p3 = p[i + 2] || p2;
            const c = [p1[0] + (p2[0] - p0[0]) / 6, cl(p1[1] + (p2[1] - p0[1]) / 6), p2[0] - (p3[0] - p1[0]) / 6, cl(p2[1] - (p3[1] - p1[1]) / 6), p2[0], p2[1]];
            d += " C" + c.map((v) => v.toFixed(1)).join(" ");
          }
          return d;
        }
        function drawChart(h) {
          const box = $("acChartBox");
          box.replaceChildren();
          const W = Math.max(260, Math.round(box.clientWidth) || 340), H = Math.max(150, Math.min(300, Math.round(W * 0.5)));
          const L = 46, R = 12, T = 12, B = 28;
          const day = h.days <= 1;
          const tEnd = day ? h.t0 + 86400 : h.t1;
          const pts = h.points.filter((p) => p[1] != null);
          let lo = 16, hi = 32;
          if (pts.length) {
            const vs = pts.map((p) => p[1]);
            lo = Math.floor((Math.min.apply(null, vs) - 0.01) / 4) * 4;
            hi = Math.ceil((Math.max.apply(null, vs) + 0.01) / 4) * 4;
            if (hi - lo < 8) hi = lo + 8;
          }
          const x = (t) => L + (t - h.t0) / Math.max(1, tEnd - h.t0) * (W - L - R);
          const y = (v) => T + (hi - v) / (hi - lo) * (H - T - B);
          const svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H, class: "ac-chart hist", role: "img", "aria-label": "Room temperature history" });
          const defs = svgEl("defs", {});
          const grad = svgEl("linearGradient", { id: "acHistGrad", x1: 0, y1: 0, x2: 0, y2: 1 });
          grad.append(svgEl("stop", { offset: 0, style: "stop-color: var(--ac-accent); stop-opacity: .5" }),
                      svgEl("stop", { offset: 1, style: "stop-color: var(--ac-accent); stop-opacity: 0" }));
          defs.append(grad);
          svg.append(defs);
          for (let v = lo; v <= hi; v += 4) {
            svg.append(svgEl("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: "grid" }));
            const tx = svgEl("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" });
            tx.textContent = v + "°C";
            svg.append(tx);
          }
          const ticks = day ? 6 : 4;
          for (let k = 0; k <= ticks; k++) {
            const t = h.t0 + (tEnd - h.t0) * k / ticks;
            let label;
            if (day) { const hr = (k * 4) % 24; label = (hr % 12 || 12) + (hr < 12 ? " AM" : " PM"); }
            else label = new Date(t * 1000).toLocaleDateString([], { month: "short", day: "numeric" });
            const tx = svgEl("text", { x: x(t), y: H - 7, "text-anchor": k === ticks ? "end" : "middle" });
            tx.textContent = label;
            svg.append(tx);
          }
          const gap = day ? 2700 : h.days <= 7 ? 10800 : 21600;
          const segs = [];
          for (const p of pts) {
            const last = segs[segs.length - 1];
            if (last && p[0] - last[last.length - 1][2] <= gap) last.push([x(p[0]), y(p[1]), p[0]]);
            else segs.push([[x(p[0]), y(p[1]), p[0]]]);
          }
          const base = H - B;
          for (const sg of segs) {
            if (sg.length < 2) continue;
            const line = smoothPath(sg, T, base);
            svg.append(svgEl("path", { d: line + " L" + sg[sg.length - 1][0].toFixed(1) + " " + base + " L" + sg[0][0].toFixed(1) + " " + base + " Z", fill: "url(#acHistGrad)" }));
            svg.append(svgEl("path", { d: line, class: "hline" }));
          }
          if (pts.length) {
            const lastSeg = segs[segs.length - 1], lp = lastSeg[lastSeg.length - 1];
            svg.append(svgEl("circle", { cx: lp[0], cy: lp[1], r: 3.2, class: "hdot" }));
          } else {
            const tx = svgEl("text", { x: (L + W - R) / 2, y: (T + H - B) / 2, class: "hnote" });
            tx.textContent = "No data yet. Sara records every 5 minutes.";
            svg.append(tx);
          }
          box.append(svg);
        }

        // ---- daily report ----
        async function loadReport(days) {
          try { reports[days] = await (await fetch("/ac/report?days=" + days)).json(); } catch (e) { return; }
          paintReport();
        }
        function makeReportRange() {
          const box = $("acRepRange");
          for (const [days, label] of [[7, "7 Days"], [14, "14 Days"], [30, "30 Days"]]) {
            const b = el("button", "", label);
            b.type = "button";
            b.setAttribute("aria-pressed", String(days === repDays));
            b.addEventListener("click", () => {
              repDays = days;
              for (const x of box.children) x.setAttribute("aria-pressed", String(x === b));
              paintReport();
              loadReport(days);
            });
            box.append(b);
          }
          $("acRepCsv").addEventListener("click", () => { window.location.href = "/ac/report.csv?days=" + repDays; });
        }
        function drawBars(r) {
          const box = $("acRepChart");
          box.replaceChildren();
          const n = r.days.length, W = 600, H = 240, L = 34, R = 8, T = 22, B = 32;
          const hrs = r.days.map((d) => d.on_minutes / 60);
          const top = Math.max(1, Math.ceil(Math.max.apply(null, hrs)));
          const step = top <= 4 ? 1 : Math.ceil(top / 4);
          const y = (v) => T + (top - v) / top * (H - T - B);
          const slot = (W - L - R) / n, bw = Math.max(4, slot * 0.62);
          const svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H, class: "ac-chart rep", role: "img", "aria-label": "Hours the AC ran each day" });
          for (let v = 0; v <= top; v += step) {
            svg.append(svgEl("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: "grid" }));
            const tx = svgEl("text", { x: L - 5, y: y(v) + 4, "text-anchor": "end" });
            tx.textContent = v + "h";
            svg.append(tx);
          }
          const every = Math.ceil(n / 8);
          r.days.forEach((d, i) => {
            const cx = L + (i + 0.5) * slot, h = hrs[i];
            const top0 = h > 0 ? Math.min(y(h), H - B - 2) : H - B;
            const bar = svgEl("rect", { x: cx - bw / 2, y: top0, width: bw, height: H - B - top0, rx: 4, class: "bar" + (i === n - 1 ? " today" : "") });
            const tip = svgEl("title", {});
            tip.textContent = d.label + ": " + fmtMin(d.on_minutes) + " · " + d.kwh + " kWh";
            bar.append(tip);
            svg.append(bar);
            if (h > 0 && n <= 14) {
              const v = svgEl("text", { x: cx, y: top0 - 5, class: "bval" });
              v.textContent = h.toFixed(1) + "h";
              svg.append(v);
            }
            if (i === n - 1 || (i % every === 0 && n - 1 - i >= Math.ceil(every / 2))) {
              const t = svgEl("text", { x: cx, y: H - 10, "text-anchor": "middle" });
              t.textContent = d.label.slice(0, 6);
              svg.append(t);
            }
          });
          box.append(svg);
        }
        function paintReport() {
          const r = reports[repDays];
          if (!r) return;
          $("acRepOn").textContent = fmtMin(r.total_minutes);
          $("acRepAvg").textContent = fmtMin(r.avg_minutes);
          $("acRepKwh").textContent = r.total_kwh.toFixed(1) + " kWh";
          $("acRepCost").textContent = "₹ " + r.total_cost.toFixed(1);
          const body = $("acRepBody");
          body.replaceChildren();
          for (const d of r.days.slice().reverse()) {
            const tr = el("tr", d === r.days[r.days.length - 1] ? "today" : "");
            const zero = d.on_minutes === 0;
            const cells = [d.label + (tr.className ? " (today)" : ""), zero ? "—" : fmtMin(d.on_minutes), zero ? "—" : d.kwh.toFixed(1) + " kWh",
                           zero ? "—" : "₹ " + d.cost.toFixed(1), d.avg_target != null ? d.avg_target.toFixed(0) + "°C" : "—",
                           d.min_room != null ? d.min_room.toFixed(0) + "–" + d.max_room.toFixed(0) + "°C" : "—"];
            cells.forEach((c, i) => tr.append(el("td", zero && i > 0 ? "zero" : "", c)));
            body.append(tr);
          }
          if (!r.total_minutes) {
            $("acRepChart").replaceChildren(el("div", "ac-empty", "No AC run-time recorded in this period yet. Sara logs the AC every 5 minutes while she is running and the AC is ON."));
          } else drawBars(r);
          $("acRepNote").textContent = !r.since ? "Nothing is recorded yet."
            : "Estimate: run time × " + r.watts + " W × ₹" + r.rate + "/kWh (set BLUESTAR_WATTS / BLUESTAR_RATE in .env). Days when Sara wasn't running are not recorded.";
        }

        // ---- schedule ----
        function renderRules(rules) {
          const ul = $("acRules");
          ul.replaceChildren();
          if (!rules.length) { ul.append(el("li", "ac-empty", "No schedules yet.")); return; }
          for (const r of rules) {
            const h = parseInt(r.start, 10);
            const li = el("li", "ac-rule");
            li.append(el("span", "ri", h >= 5 && h < 12 ? "☀️" : h >= 12 && h < 18 ? "🌇" : "🌙"));
            const info = el("div");
            info.append(el("b", "", r.name), el("small", "", r.start + " - " + r.end + "  |  " + r.temp + "°C · " + r.mode));
            const sw = el("label", "ac-sw");
            const cb = el("input");
            cb.type = "checkbox"; cb.checked = !!r.enabled; cb.setAttribute("aria-label", "Enable " + r.name);
            cb.addEventListener("change", async () => { renderRules((await post("/ac/schedule/toggle?id=" + r.id + "&on=" + cb.checked)).rules || rules); });
            sw.append(cb, el("span"));
            const del = el("button", "del", "✕");
            del.type = "button"; del.title = "Delete"; del.setAttribute("aria-label", "Delete " + r.name);
            del.addEventListener("click", async () => { renderRules((await post("/ac/schedule/delete?id=" + r.id)).rules || []); });
            li.append(info, sw, del);
            ul.append(li);
          }
        }
        async function loadRules() {
          try { renderRules((await (await fetch("/ac/schedule")).json()).rules || []); } catch (e) { /* offline */ }
        }

        // ---- assistant ----
        function bubble(text, cls) {
          const log = $("acLog");
          log.append(el("div", "ac-b" + (cls ? " " + cls : ""), text));
          log.scrollTop = log.scrollHeight;
        }
        function tips(d) {
          const out = [];
          if (!d.online) return ["The AC is offline. Check that it has power and is on Wi-Fi."];
          if (d.error) out.push("The AC reports fault code " + d.error + ". If it keeps showing, call Blue Star service with this code.");
          if (d.power && d.room != null && d.room - d.target > 0.5 && (d.mode === "cool" || d.mode === "auto"))
            out.push("Cooling from " + d.room.toFixed(1) + "° to " + Math.round(d.target) + "°.");
          if (d.power && d.target < 24) out.push("24°C is the BEE recommended setting; each degree higher saves about 6% power.");
          if (!d.power && d.room != null && d.room >= 28) out.push("It's " + d.room.toFixed(1) + "° inside. Say 'AC 24 pe chalao' to cool down.");
          return out;
        }
        // "sara ai", "back", "wapas jao", "ac screen band"... leave the AC screen for Sara AI.
        // (Plain "ac band karo" is not here: that still switches the AC off.)
        function wantsHome(text) {
          const t = " " + text.toLowerCase().replace(/[.!?,]/g, " ").split(" ").filter(Boolean).join(" ") + " ";
          const has = (...ws) => ws.some((w) => t.includes(" " + w + " "));
          if (has("sara", "sarah", "सारा") && !(has("ac", "एसी") && has("on", "off", "band", "chalu", "बंद", "चालू"))) return true;
          if (/[0-9]/.test(t)) return false;
          if (has("home", "back", "exit", "wapas", "vapas", "peeche", "होम", "वापस", "पीछे", "बाहर")) return true;
          return has("screen", "स्क्रीन") && has("band", "close", "hatao", "बंद");
        }
        async function ask(text) {
          text = text.trim();
          if (!text) return;
          bubble(text, "me");
          if (wantsHome(text)) { close(); return; }
          let r;
          try { r = await (await fetch("/ac/ask?text=" + encodeURIComponent(text), { method: "POST" })).json(); }
          catch (e) { r = { ok: false, message: "Can't reach Sara's server." }; }
          bubble(r.message || "Sorry, I didn't get that.", r.ok ? "" : "err");
          if (r.ok) refresh(true);
        }
        for (const q of ["Set temperature to 22°C", "Turn the AC off", "Show energy usage this week", "AC ka status batao"]) {
          const b = el("button", "", "✦ " + q);
          b.type = "button";
          b.addEventListener("click", () => ask(q));
          $("acChips").append(b);
        }
        const SRc = window.SpeechRecognition || window.webkitSpeechRecognition;
        const mic = $("acMic");
        if (!SRc) mic.hidden = true;
        else {
          let rec = null;
          mic.addEventListener("click", () => {
            if (rec) { rec.stop(); return; }
            rec = new SRc();
            const sel = document.getElementById("langSelect");
            rec.lang = (sel && sel.value) || "en-IN";
            rec.interimResults = false;
            mic.classList.add("rec");
            say("Listening...");
            rec.onresult = (e) => { say(""); ask(e.results[0][0].transcript); };
            rec.onerror = (e) => say(e.error === "not-allowed" ? "Allow the microphone to speak a command." : "Didn't catch that. Try again.", true);
            rec.onend = () => { rec = null; mic.classList.remove("rec"); };
            try { rec.start(); } catch (x) { rec = null; mic.classList.remove("rec"); }
          });
        }
        $("acAsk").addEventListener("submit", (e) => { e.preventDefault(); const i = $("acAskIn"); const v = i.value; i.value = ""; ask(v); });

        // ---- data ----
        function showSetup(st) {
          const key = (st.configured ? "1" : "0") + "|" + (st.error || "");
          if (key === setupKey && !setupEl.hidden && setupEl.querySelector("form")) return;
          setupKey = key;
          mainEl.hidden = true;
          setupEl.hidden = false;
          live.className = "ac-live off";
          live.querySelector("b").textContent = "Not linked";
          $("acTitle").textContent = greeting() + ",";
          $("acSub").textContent = "Link your Blue Star AC to start controlling it.";
          setupEl.replaceChildren();
          setupEl.append(el("h4", "", st.configured ? "Couldn't reach your AC" : "Link your Blue Star AC"));
          setupEl.append(el("p", "", st.configured ? (st.error || "No AC was found on this Blue Star account.")
            : "Log in with the same phone number (or email) and password you use in the Blue Star Smart app."));
          const form = el("form", "ac-login");
          const l1 = el("label", "", "Phone number or email");
          const user = el("input");
          user.id = "acUser"; user.type = "text"; user.autocomplete = "username"; user.maxLength = 80; user.required = true;
          user.placeholder = "9876543210 or name@email.com";
          l1.append(user);
          const l2 = el("label", "", "Password");
          const wrap = el("div", "ac-pw");
          const pass = el("input");
          pass.id = "acPass"; pass.type = "password"; pass.autocomplete = "current-password"; pass.maxLength = 100; pass.required = true;
          pass.placeholder = "Blue Star app password";
          const eye = el("button", "", "Show");
          eye.type = "button";
          eye.addEventListener("click", () => {
            const show = pass.type === "password";
            pass.type = show ? "text" : "password";
            eye.textContent = show ? "Hide" : "Show";
          });
          wrap.append(pass, eye);
          l2.append(wrap);
          const err = el("p", "err");
          err.setAttribute("role", "alert");
          const go = el("button", "go", "Connect AC");
          go.type = "submit";
          form.append(l1, l2, err, go);
          form.addEventListener("submit", async (e) => {
            e.preventDefault();
            go.disabled = true; go.textContent = "Connecting...";
            err.textContent = "";
            let r;
            try {
              r = await (await fetch("/ac/login", { method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ user: user.value, password: pass.value }) })).json();
            } catch (x) { r = { ok: false, error: "Can't reach Sara's server." }; }
            if (r.ok) { welcomed = false; say("✓ AC linked"); await refresh(true); loadExtras(); }
            else { err.textContent = r.error || "Login failed."; go.disabled = false; go.textContent = "Connect AC"; }
          });
          setupEl.append(form);
          setupEl.append(el("p", "note", "Your login is saved in this project's .env file on this computer and is only sent to Blue Star's login server."));
          if (st.configured) {
            const retry = el("button", "", "⟳ Try again");
            retry.type = "button";
            retry.addEventListener("click", () => { say("Connecting..."); refresh(true); });
            setupEl.append(retry);
          }
          setTimeout(() => user.focus(), 50);
        }
        async function refresh(force) {
          if (inflight || (pendingTemp != null && !force)) return;
          let st;
          try { st = await (await fetch("/ac/state")).json(); }
          catch (e) { say("Can't reach Sara's server.", true); return; }
          if (inflight) return;
          if (!st.configured || !st.devices || !st.devices.length) { cur = null; showSetup(st); return; }
          setupEl.hidden = true;
          setupKey = "";
          mainEl.hidden = false;
          devs = st.devices;
          timerSecs = st.timer_secs || null;
          updatedAt = Date.now();
          pick.hidden = devs.length < 2;
          if (devs.length > 1 && pick.options.length !== devs.length) {
            pick.replaceChildren(...devs.map((d) => { const o = el("option", "", d.name); o.value = d.id; return o; }));
          }
          cur = devs.find((d) => d.id === curId) || devs[0];
          curId = cur.id;
          pick.value = curId;
          if (st.error) say(st.error, true);
          render();
          if (!welcomed) {
            welcomed = true;
            bubble("Hi! I'm your Blue Star AI assistant. Ask me to change the temperature, mode or fan, or about usage.");
            for (const t of tips(cur)) bubble(t);
          }
        }
        function loadExtras() { loadHistory(enDays); if (hiDays !== enDays) loadHistory(hiDays); loadRules(); loadReport(repDays); }

        // ---- open / close ----
        function open() {
          if (!screen.hidden) { refresh(true); return; }
          lastFocus = document.activeElement;
          screen.hidden = false;
          screen.scrollTop = 0;
          document.body.style.overflow = "hidden";
          $("acTitle").textContent = greeting() + ",";
          say("Connecting to Blue Star...");
          refresh(true).then(() => { if (!msgEl.classList.contains("err")) say(""); });
          loadExtras();
          tick = 0;
          timer = setInterval(() => { refresh(); if (++tick % 20 === 0) loadExtras(); }, 3000);
          setTimeout(() => $("acClose").focus(), 50);
        }
        function close() {
          screen.hidden = true;
          document.body.style.overflow = "";
          clearInterval(timer);
          timer = null;
          if (lastFocus) lastFocus.focus();
        }

        makeRange($("acEnRange"), () => enDays, (d) => { enDays = d; });
        $("acHiSel").addEventListener("change", (e) => { hiDays = +e.target.value; paintHistory(); loadHistory(hiDays); });
        makeReportRange();
        $("acMinus").addEventListener("click", () => changeTemp(-1));
        $("acPlus").addEventListener("click", () => changeTemp(1));
        $("qPower").addEventListener("click", () => { if (cur) { const on = !cur.power; send("/ac/power?on=" + on, { power: on }, on ? "Power on" : "Power off"); } });
        $("qMode").addEventListener("click", () => { if (cur) doMode(cycle(MODES.map((m) => m[0]), cur.mode)); });
        $("qFan").addEventListener("click", () => { if (cur) doFan(cycle(FANS.map((m) => m[0]), cur.fan)); });
        $("qSwing").addEventListener("click", () => { if (cur) doSwing(cur.vswing === "off" ? "auto" : "off"); });
        $("qTimer").addEventListener("click", () => { tab = "timer"; renderTabs(); if (cur) renderTiles(); if (mainEl.dataset.view !== "all") setView("ctl"); });
        function setView(v) {
          mainEl.dataset.view = v;
          for (const x of $("acNav").children) x.classList.toggle("on", x.dataset.view === v);
          screen.scrollTop = 0;
          paintHistory();
          paintReport();
        }
        for (const b of $("acNav").children) b.addEventListener("click", () => setView(b.dataset.view));
        $("acAskGo").addEventListener("click", () => { setView("ai"); $("acAskIn").focus(); });
        $("acAddBtn").addEventListener("click", () => { $("acForm").hidden = false; $("acAddBtn").hidden = true; $("fName").focus(); });
        $("fCancel").addEventListener("click", () => { $("acForm").hidden = true; $("acAddBtn").hidden = false; });
        $("acForm").addEventListener("submit", async (e) => {
          e.preventDefault();
          const q = "name=" + encodeURIComponent($("fName").value) + "&start=" + $("fStart").value + "&end=" + $("fEnd").value +
                    "&temp=" + $("fTemp").value + "&mode=" + $("fMode").value;
          const r = await post("/ac/schedule/add?" + q);
          if (r.ok) { renderRules(r.rules); $("acForm").hidden = true; $("acAddBtn").hidden = false; $("fName").value = ""; say("✓ Schedule saved"); }
          else say(r.error || "Couldn't save the schedule.", true);
        });
        pick.addEventListener("change", () => { curId = pick.value; cur = devs.find((d) => d.id === curId) || cur; render(); });
        $("acClose").addEventListener("click", close);
        $("acHome").addEventListener("click", close);
        document.addEventListener("keydown", (e) => {
          if (screen.hidden) return;
          if (e.key === "Escape") close();
          else if ((e.key === "ArrowUp" || e.key === "ArrowDown") && !/^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) {
            e.preventDefault();
            changeTemp(e.key === "ArrowUp" ? 1 : -1);
          }
        });
        $("acBtn").addEventListener("click", open);
        return { open, close, refresh };
      })();
      async function startFlow() {
        logsEl.textContent = "OS_INIT: Establishing secure connection...\\n";
        afterId = 0;
        await pollLogs();
        pollTimer = setInterval(pollLogs, 1000);
      }

      // --- Connectivity Check ---
      const connStatus = document.getElementById("connStatus");
      const connDot = document.getElementById("connDot");
      setInterval(async () => {
        try {
          const r = await fetch("/health");
          if (r.ok) {
            connStatus.style.color = "var(--neon-green)";
            connStatus.textContent = "ONLINE";
            connDot.style.backgroundColor = "var(--neon-green)";
            connDot.style.boxShadow = "0 0 8px var(--neon-green)";
          }
        } catch (e) {
          connStatus.style.color = "var(--alert-red)";
          connStatus.textContent = "OFFLINE";
          connDot.style.backgroundColor = "var(--alert-red)";
          connDot.style.boxShadow = "0 0 8px var(--alert-red)";
        }
      }, 5000);

      async function runAiTask() {
        const text = aiInput.value.trim();
        if (!text) return;
        showThinking("AI is thinking");
        let waitSpeech = null;
        const waitTimer = setTimeout(() => {
          waitSpeech = speakOut(pleaseWaitLine(DEVANAGARI.test(text) || isHindiUI()));
        }, 1500);
        try {
          const resp = await fetch(`/run?text=${encodeURIComponent(text)}&in_page=true&reply_lang=${replyLangParam()}`, { method: "POST" });
          const data = await resp.json();
          clearTimeout(waitTimer);
          setAiResponse(data.message);
          aiInput.value = "";
          aiInput.focus();  // ready for the next question
          if (waitSpeech) await waitSpeech;
          await speakOut(await replyFor(data, DEVANAGARI.test(text) || isHindiUI()));
          afterSpeech(data);
          resumeAutoMic();
        } catch (e) {
          clearTimeout(waitTimer);
          if (procBar) procBar.classList.remove("active");
          aiBrain.classList.remove("type-caret");
          aiBrain.textContent = "Error: AI link lost.";
        }
      }

      runBtn.addEventListener("click", runCustomCommand);
      cmdInput.addEventListener("keypress", (e) => { if (e.key === "Enter") runCustomCommand(); });
      aiBtn.addEventListener("click", runAiTask);
      aiInput.addEventListener("keypress", (e) => { if (e.key === "Enter") runAiTask(); });

      // --- Voice Search ---
      const langSelect = document.getElementById("langSelect");
      const voiceOverlay = document.getElementById("voiceOverlay");
      const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;

      function endVoiceUI(msg) {
        voiceBtn.classList.remove("pulse");
        headerPanel.classList.remove("blur");
        mainPanel.classList.remove("blur");
        voiceOverlay.classList.remove("active");
        if (msg) statusEl.textContent = msg;
      }

      if (Recognition) {
        const recognition = new Recognition();
        let micTimeout;
        let listening = false;
        let heardSomething = false;  // did this listening round produce text?
        let userStopped = false;     // Stop button / mic click ends the round
        let silentRounds = 0;        // Auto Mic pauses after 3 silent rounds

        // Saying one of these turns Auto Mic off instead of running a command.
        const STOP_PHRASES = [
          "stop", "stop listening", "stop mic", "mic off", "mic band karo",
          "mic band kar do", "bas", "bas karo", "bas kar do", "chup", "chup ho jao",
          "ruk jao", "sunna band karo", "goodbye", "bye", "बस", "बस करो",
          "माइक बंद करो", "रुक जाओ", "चुप",
        ];
        function isStopPhrase(text) {
          const t = text.toLowerCase().replace(/[.,!?।]/g, "").trim();
          return STOP_PHRASES.includes(t);
        }

        // Notebook dictation needs the browser's speech engine to itself:
        // pause Sara's listening while it runs (resumed afterwards).
        window.saraVoice = {
          pause() { if (listening) { userStopped = true; try { recognition.abort(); } catch (e) {} } },
          resume() { resumeAutoMic(); },
        };
        startListening = () => {
          if (window.noteDictating) return false;
          if (listening) return true;
          userStopped = false;
          heardSomething = false;
          recognition.lang = langSelect.value;
          try {
            recognition.start();
            return true;
          } catch (e) {
            endVoiceUI("ERROR: could not start mic — " + e.message);
            return false;
          }
        };

        recognition.onresult = (e) => {
          const said = e.results[0][0].transcript;
          heardSomething = true;
          silentRounds = 0;
          if (autoMic && isStopPhrase(said)) {
            setAutoMic(false, "AUTO MIC: off (you said stop).");
            speakOut(isHindiUI() ? "ठीक है, माइक बंद कर दिया।" : "Okay, auto mic is off.");
            return;
          }
          cmdInput.value = said;
          runCustomCommand();  // re-opens the mic after the answer (Auto Mic)
        };

        recognition.onstart = () => {
          listening = true;
          aiVisual.setState("listening");
          voiceBtn.classList.add("pulse");
          // In the continuous Auto Mic conversation the avatar shows
          // "LISTENING"; skip the full-screen overlay so the page never blurs.
          if (!autoMic) {
            headerPanel.classList.add("blur");
            mainPanel.classList.add("blur");
            const langOpt = langSelect.options[langSelect.selectedIndex];
            const voiceLang = document.getElementById("voiceLang");
            if (voiceLang) voiceLang.textContent = "Mic live · " + (langOpt ? langOpt.text : langSelect.value);
            voiceOverlay.classList.add("active");
          }
          statusEl.textContent = autoMic
            ? "LISTENING... (auto mic — say 'stop' to end)"
            : "LISTENING... (speak now)";
          micTimeout = setTimeout(() => { try { recognition.stop(); } catch (e) {} }, 8000);
        };

        recognition.onend = () => {
          listening = false;
          clearTimeout(micTimeout);
          if (aiVisual.state === "listening") aiVisual.setState("idle");
          const wasListening = statusEl.textContent.includes("LISTENING");
          endVoiceUI(wasListening ? "SYS_READY: Awaiting input..." : null);
          // Auto Mic: nothing was said this round -> try again, but pause after
          // three silent rounds so the mic is not left on forever.
          if (autoMic && !heardSomething && !userStopped) {
            silentRounds += 1;
            if (silentRounds >= 3) {
              silentRounds = 0;
              statusEl.textContent = "AUTO MIC paused (no speech). Click the mic to talk again.";
            } else {
              setTimeout(() => startListening(), 300);
            }
          }
        };

        recognition.onerror = (e) => {
          listening = false;
          clearTimeout(micTimeout);
          // An "aborted" event means the user stopped on purpose (Stop button or
          // mic toggle) — that's a normal stop, not an error.
          if (e.error === "aborted") {
            endVoiceUI("SYS_READY: Voice input stopped.");
            return;
          }
          // Silence is normal in Auto Mic mode; onend decides whether to retry.
          if (e.error === "no-speech" && autoMic) return;
          if (e.error !== "no-speech") userStopped = true;  // real error: don't loop
          const reasons = {
            "not-allowed": "MIC BLOCKED: allow microphone access (click the padlock icon in the address bar).",
            "service-not-allowed": "MIC BLOCKED: allow microphone access in the browser.",
            "no-speech": "NO SPEECH HEARD: click the mic, then speak.",
            "audio-capture": "NO MIC FOUND: check your microphone is connected.",
            "network": "NETWORK ERROR: browser speech needs an internet connection.",
          };
          endVoiceUI("ERROR: " + (reasons[e.error] || ("voice failed (" + e.error + ").")));
        };

        voiceBtn.addEventListener("click", () => {
          if (listening) {
            userStopped = true;
            try { recognition.stop(); } catch (e) {}
            return;
          }
          stopSpeaking();  // interrupt Sara so she isn't heard by the mic
          silentRounds = 0;
          startListening();
        });

        // Dedicated Stop button on the listening overlay. Also turns Auto Mic
        // off, since the user clearly wants the mic closed.
        const voiceStopBtn = document.getElementById("voiceStopBtn");
        if (voiceStopBtn) {
          voiceStopBtn.addEventListener("click", () => {
            userStopped = true;
            try { recognition.abort(); } catch (e) {}
            endVoiceUI("SYS_READY: Voice input stopped.");
            if (autoMic) setAutoMic(false, "AUTO MIC: off.");
          });
          document.addEventListener("keydown", (e) => {
            if (e.key === "Escape" && voiceOverlay.classList.contains("active")) voiceStopBtn.click();
          });
        }

      } else {
        voiceBtn.title = "Voice not supported in this browser";
        voiceBtn.addEventListener("click", () => {
          statusEl.textContent = "VOICE UNSUPPORTED: use Chrome or Edge (not Firefox) for the mic button.";
        });
      }

      // --- Onboarding: ask name + gender, then greet --------------------------
      const onboardOverlay = document.getElementById("onboardOverlay");
      const onboardName = document.getElementById("onboardName");
      const onboardStart = document.getElementById("onboardStart");
      const onboardErr = document.getElementById("onboardErr");
      const genderMale = document.getElementById("genderMale");
      const genderFemale = document.getElementById("genderFemale");
      const brandTitle = document.getElementById("brandTitle");
      let pickedGender = null;

      function selectGender(g) {
        pickedGender = g;
        genderMale.classList.toggle("selected", g === "male");
        genderFemale.classList.toggle("selected", g === "female");
        genderMale.setAttribute("aria-checked", String(g === "male"));
        genderFemale.setAttribute("aria-checked", String(g === "female"));
        onboardErr.textContent = "";
        updateOnboardReady();
      }
      // Dim the Initialize button until both name and gender are filled in.
      function updateOnboardReady() {
        const ready = onboardName.value.trim() && pickedGender;
        onboardStart.classList.toggle("is-disabled", !ready);
      }
      genderMale.addEventListener("click", () => selectGender("male"));
      genderFemale.addEventListener("click", () => selectGender("female"));
      onboardName.addEventListener("input", () => { onboardErr.textContent = ""; updateOnboardReady(); });

      // Time-of-day greeting based on the local hour.
      function timeGreeting() {
        const h = new Date().getHours();
        if (h >= 5 && h < 12) return { en: "Good morning", hi: "सुप्रभात" };
        if (h >= 12 && h < 17) return { en: "Good afternoon", hi: "नमस्कार" };
        return { en: "Good evening", hi: "शुभ संध्या" }; // 17:00–04:59
      }

      // "Sara ki bhasha": Hindi (default) / English / Auto. Hindi or English
      // forces every reply into that language; Auto follows the command.
      const replyLangSel = document.getElementById("replyLangSelect");
      try { replyLangSel.value = localStorage.getItem("replyLang") || "hi"; } catch (e) { replyLangSel.value = "hi"; }
      replyLangSel.addEventListener("change", () => {
        try { localStorage.setItem("replyLang", replyLangSel.value); } catch (e) {}
      });
      function replyLangValue() {
        const el = document.getElementById("replyLangSelect");
        return (el && el.value) || "hi";
      }
      function replyLangParam() { const v = replyLangValue(); return v === "auto" ? "" : v; }
      function setReplyLang(v) {
        const el = document.getElementById("replyLangSelect");
        if (el) el.value = v;
        try { localStorage.setItem("replyLang", v); } catch (e) {}
      }
      function isHindiUI() {
        const v = replyLangValue();
        if (v === "hi") return true;
        if (v === "en") return false;
        const sel = document.getElementById("langSelect");
        return !!(sel && (sel.value || "").startsWith("hi"));
      }
      // "hindi mein baat karo" / "english mein baat karo": switch the reply language.
      const TO_HINDI = /(hindi|हिंदी|हिन्दी) *(me|mein|mai|में)? *(baat|bat|bolo|bol|jawab|reply|speak|talk|बात|बोलो)|speak (in )?hindi|talk (in )?hindi/i;
      const TO_ENGLISH = /(english|angrezi|इंग्लिश|अंग्रेज़ी) *(me|mein|mai|में)? *(baat|bat|bolo|bol|jawab|reply|speak|talk|बात|बोलो)|speak (in )?english|talk (in )?english/i;
      function languageSwitch(text) {
        if (TO_HINDI.test(text)) { setReplyLang("hi"); return "जी, अब मैं हिंदी में बात करूँगी।"; }
        if (TO_ENGLISH.test(text)) { setReplyLang("en"); return "Sure, I will speak in English now."; }
        return null;
      }

      // Visible welcome banner shown to every user on load.
      function showWelcome(name, greet) {
        const toast = document.getElementById("welcomeToast");
        if (!toast) return;
        toast.innerHTML = '<span class="wt-title"></span><span class="wt-sub">SARA_OS // session active</span>';
        // Use textContent for the name so user input can't inject markup.
        toast.querySelector(".wt-title").textContent = `👋 ${greet || "Welcome"}, ${name || "User"}!`;
        toast.classList.add("show");
        clearTimeout(toast._t);
        toast._t = setTimeout(() => toast.classList.remove("show"), 5000);
      }

      function greetUser() {
        const name = (profile && profile.name) ? profile.name : "";
        const g = timeGreeting();
        const hindi = isHindiUI();
        showWelcome(name, hindi ? g.hi : g.en);
        statusEl.textContent = `SYS_READY: ${g.en}, ${name || "user"}.`;
        const msg = hindi
          ? `${g.hi}${name ? " " + name : ""}! मैं सारा हूँ। मैं आपकी कैसे मदद कर सकती हूँ?`
          : `${g.en}${name ? ", " + name : ""}! I am Sara. How can I help you?`;
        // After the welcome, Auto Mic starts listening straight away.
        speakOut(msg).then(resumeAutoMic);
      }

      // Id of the current server run; a change means the server restarted.
      let serverRunId = null;
      async function fetchSession() {
        try {
          const r = await fetch("/session");
          const d = await r.json();
          serverRunId = d.run_id;
        } catch (e) { serverRunId = null; }
      }

      function finishOnboard() {
        const name = onboardName.value.trim();
        if (!name) { onboardErr.textContent = "Please enter your name."; onboardName.focus(); return; }
        if (!pickedGender) { onboardErr.textContent = "Please select Male or Female."; return; }
        profile = { name, gender: pickedGender, runId: serverRunId };
        saveProfile(profile);
        onboardOverlay.classList.remove("active");
        greetUser();  // show the welcome instantly
      }
      onboardStart.addEventListener("click", finishOnboard);
      onboardName.addEventListener("keypress", (e) => { if (e.key === "Enter") finishOnboard(); });

      function openOnboard() {
        onboardName.value = (profile && profile.name) || "";
        selectGender((profile && profile.gender) || null);
        onboardErr.textContent = "";
        onboardOverlay.classList.add("active");
        setTimeout(() => onboardName.focus(), 100);
      }
      // Click the SARA_OS title to change user / switch voice.
      if (brandTitle) brandTitle.addEventListener("click", openOnboard);

      async function maybeOnboard() {
        await fetchSession();
        // Re-ask the name on a new server run, or if no profile yet.
        const known = profile && profile.name && profile.gender
          && profile.runId === serverRunId;
        if (known) {
          greetUser();  // show the welcome instantly
        } else {
          openOnboard();
        }
      }

      // --- Hover explanations for the header icons -------------------------------
      (() => {
        const TIPS = {
          btBtn: "Bluetooth: paired devices, connect / disconnect, ☆ Auto-connect.",
          acBtn: "AC: Blue Star AC on/off, temperature, mode, fan, swing. Bol bhi sakte hain: 'AC 24 pe chalao'.",
          radioBtn: "FM Radio: Radio Mirchi, Vividh Bharati, Red FM aur bahut se live stations. Bol bhi sakte hain: 'radio mirchi chalao'.",
          quranBtn: "Quran: Sheikh Yasser Al-Dosari ki tilawat. Click karke surah chuniye.",
          phoneBtn: "Phone se input: phone se bolkar ya likhkar Sara ko command dijiye (QR / USB).",
          noteBtn: "Notebook: aaj ka kaam likhiye ya bolkar likhiye. Apne aap save + WhatsApp Send.",
          calcBtn: "Calculator: hisaab lagaiye. Bol bhi sakte hain: '12 plus 5 kitna hai'.",
          themeToggle: "Theme: dark / light mode badliye.",
        };
        for (const [id, tip] of Object.entries(TIPS)) {
          const b = document.getElementById(id);
          if (b) { b.dataset.tip = tip; b.removeAttribute("title"); }
        }
        const tipEl = document.createElement("div");
        tipEl.className = "hdr-tip";
        tipEl.setAttribute("role", "tooltip");
        document.body.append(tipEl);
        let showT = null;
        function show(btn) {
          tipEl.textContent = btn.dataset.tip;
          const r = btn.getBoundingClientRect();
          tipEl.classList.add("show");
          const w = tipEl.offsetWidth;
          const left = Math.max(8, Math.min(innerWidth - w - 8, r.left + r.width / 2 - w / 2));
          tipEl.style.left = left + "px";
          tipEl.style.top = (r.bottom + 10) + "px";
          tipEl.style.setProperty("--arrow", (r.left + r.width / 2 - left) + "px");
        }
        function hide() { clearTimeout(showT); tipEl.classList.remove("show"); }
        const bar = document.querySelector(".header-actions");
        if (bar) {
          bar.addEventListener("mouseover", (e) => {
            const btn = e.target.closest("[data-tip]");
            if (!btn) return;
            clearTimeout(showT);
            showT = setTimeout(() => show(btn), 250);
          });
          bar.addEventListener("mouseout", (e) => { if (!e.relatedTarget || !bar.contains(e.relatedTarget)) hide(); });
          bar.addEventListener("focusin", (e) => { const b = e.target.closest("[data-tip]"); if (b) show(b); });
          bar.addEventListener("focusout", hide);
          // Clicking updates the text (e.g. Auto Mic ON -> OFF) while still hovering.
          bar.addEventListener("click", (e) => { const b = e.target.closest("[data-tip]"); if (b) setTimeout(() => show(b), 30); });
        }
      })();

      // --- Floating tool windows: open / close / drag ---------------------------
      function toolWin(id, btnId, onOpen) {
        const win = document.getElementById(id), btn = document.getElementById(btnId);
        if (!win) return null;
        const open = () => { win.hidden = false; win.style.zIndex = String(++toolWin.z); if (onOpen) onOpen(); };
        const close = () => { win.hidden = true; };
        if (btn) btn.addEventListener("click", () => (win.hidden ? open() : close()));
        win.querySelector("[data-close]").addEventListener("click", close);
        win.addEventListener("pointerdown", () => { win.style.zIndex = String(++toolWin.z); });
        win.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } });
        // Drag by the title bar, kept on screen.
        const head = win.querySelector(".tw-head");
        head.addEventListener("pointerdown", (e) => {
          if (e.target.closest("button")) return;
          const r = win.getBoundingClientRect(), dx = e.clientX - r.left, dy = e.clientY - r.top;
          head.setPointerCapture(e.pointerId);
          const move = (ev) => {
            win.style.left = Math.max(0, Math.min(innerWidth - r.width, ev.clientX - dx)) + "px";
            win.style.top = Math.max(0, Math.min(innerHeight - 40, ev.clientY - dy)) + "px";
            win.style.right = "auto";
          };
          const up = () => { head.removeEventListener("pointermove", move); head.removeEventListener("pointerup", up); };
          head.addEventListener("pointermove", move);
          head.addEventListener("pointerup", up);
        });
        return { open, close, win };
      }
      toolWin.z = 1060;

      // --- Notebook: auto-save, date name by default, rename renames the file ----
      const notebook = (() => {
        const list = document.getElementById("noteList");
        const nameEl = document.getElementById("noteName");
        const textEl = document.getElementById("noteText");
        const statusEl = document.getElementById("noteStatus");
        const footEl = document.getElementById("noteFoot");
        if (!list) return null;
        let current = null;      // saved name of the open note (null = not saved yet)
        let saveTimer = null, saving = false, pending = false;

        // "01-Oct-2026 05:26 PM"
        const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
        const fmt = ts => {
          const d = new Date(ts * 1000);
          return String(d.getDate()).padStart(2, "0") + "-" + MON[d.getMonth()] + "-" + d.getFullYear() +
                 " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        };
        function status(text, cls) { statusEl.textContent = text; statusEl.className = "tw-status" + (cls ? " " + cls : ""); }
        const guideEl = document.getElementById("noteGuide");
        function updateGuide() {
          if (guideEl) guideEl.hidden = !!textEl.value.trim() || !!window.noteDictating;
        }
        function counts() {
          updateGuide();
          const t = textEl.value, words = (t.match(/[^ \\n\\t]+/g) || []).length;
          footEl.innerHTML = "";
          const a = document.createElement("span"); a.textContent = words + " words · " + t.length + " characters";
          const b = document.createElement("span"); b.textContent = "Saved in notes/" + (current || nameEl.value) + ".txt";
          footEl.append(a, b);
        }

        async function refreshList() {
          try {
            const d = await (await fetch("/notes")).json();
            list.replaceChildren();
            if (!d.notes.length) {
              const li = document.createElement("li"); li.className = "nl-empty"; li.textContent = "No notes yet.";
              list.append(li);
            }
            for (const n of d.notes) {
              const li = document.createElement("li"), b = document.createElement("button");
              b.type = "button";
              if (n.name === current) b.className = "active";
              const nm = document.createElement("span"); nm.className = "nl-name"; nm.textContent = n.name;
              const dt = document.createElement("span"); dt.className = "nl-date"; dt.textContent = fmt(n.modified);
              b.append(nm, dt);
              b.addEventListener("click", () => openNote(n.name));
              li.append(b); list.append(li);
            }
            return d;
          } catch (e) { status("Offline", "err"); return null; }
        }

        async function flush() {
          clearTimeout(saveTimer);
          if (saving) { pending = true; return; }
          if (current === null && !textEl.value.trim()) return;     // don't save empty new notes
          saving = true;
          status("Saving...");
          try {
            const body = { name: nameEl.value.trim(), text: textEl.value };
            if (current === null) body.is_new = true; else body.old_name = current;
            const d = await (await fetch("/notes/save", {
              method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
            })).json();
            if (!d.ok) throw new Error(d.error || "save failed");
            const renamed = d.name !== current;
            current = d.name;
            if (document.activeElement !== nameEl && nameEl.value !== d.name) nameEl.value = d.name;
            status("Saved ✓ " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }), "ok");
            counts();
            if (renamed) refreshList(); else markActive();
          } catch (e) {
            status("Not saved: " + e.message, "err");
          } finally {
            saving = false;
            if (pending) { pending = false; flush(); }
          }
        }
        function markActive() {
          list.querySelectorAll("button").forEach(b => b.classList.toggle("active", b.querySelector(".nl-name")?.textContent === current));
        }
        function schedule() { status("Editing..."); counts(); clearTimeout(saveTimer); saveTimer = setTimeout(flush, 800); }

        async function openNote(name) {
          await flush();
          try {
            const d = await (await fetch("/notes/get?name=" + encodeURIComponent(name))).json();
            if (!d.ok) throw new Error(d.error);
            current = d.name; nameEl.value = d.name; textEl.value = d.text;
            status("Opened", "ok"); counts(); markActive();
            textEl.focus();
          } catch (e) { status("Couldn't open note", "err"); }
        }
        async function newNote() {
          await flush();
          const d = await refreshList();
          current = null;
          nameEl.value = (d && d.new_name) || "Note";
          textEl.value = "";
          status("New note: starts saving when you type");
          counts(); markActive();
          textEl.focus();
        }

        textEl.addEventListener("input", schedule);
        nameEl.addEventListener("input", schedule);
        nameEl.addEventListener("blur", () => { if (!nameEl.value.trim() && current) nameEl.value = current; flush(); });
        nameEl.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); textEl.focus(); } });
        document.getElementById("noteNew").addEventListener("click", newNote);
        // Undo link in the title bar (instead of browser pop-ups).
        function undoStatus(text, onUndo) {
          statusEl.className = "tw-status ok";
          statusEl.textContent = text + " ";
          const a = document.createElement("button");
          a.type = "button"; a.className = "tw-undo"; a.textContent = "Undo";
          a.addEventListener("click", () => { clearTimeout(t); onUndo(); });
          statusEl.append(a);
          const t = setTimeout(() => { if (a.isConnected) a.remove(); }, 8000);
        }

        // Delete immediately (no pop-up); Undo restores the note for 8 s.
        document.getElementById("noteDel").addEventListener("click", async () => {
          if (current === null) { textEl.value = ""; counts(); return; }
          await flush();
          const gone = { name: current, text: textEl.value };
          await fetch("/notes/delete?name=" + encodeURIComponent(gone.name), { method: "POST" });
          current = null;
          const d = await refreshList();
          if (d && d.notes.length) await openNote(d.notes[0].name); else await newNote();
          undoStatus('Deleted "' + gone.name + '".', async () => {
            await fetch("/notes/save", { method: "POST", headers: { "Content-Type": "application/json" },
                                         body: JSON.stringify({ name: gone.name, text: gone.text, is_new: true }) });
            await refreshList();
            await openNote(gone.name);
            status("Restored ✓", "ok");
          });
        });
        window.addEventListener("beforeunload", () => { if (saveTimer) flush(); });

        let opening = false;   // show() handles which note to load
        const w = toolWin("noteWin", "noteBtn", async () => {
          if (opening) return;
          const d = await refreshList();
          if (current === null && !textEl.value) {
            if (d && d.notes.length) openNote(d.notes[0].name); else newNote();
          } else textEl.focus();
        });
        // Voice: open the window on ``name`` (the server may have just written
        // to it, so reload it instead of saving the old text over it).
        // Daily WhatsApp schedule banner under the note.
        const schedBox = document.getElementById("noteSched");
        async function refreshSchedule() {
          try {
            const d = await (await fetch("/schedule")).json();
            const job = (d.jobs || []).find(j => j.kind === "whatsapp_note");
            schedBox.hidden = !job;
            if (job) {
              const [h, m, s] = job.time.split(":").map(Number);
              const t = new Date(2000, 0, 1, h, m, s).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
              document.getElementById("noteSchedText").textContent =
                "📲 Every day at " + t + ", today's note is sent on WhatsApp to " + job.number_display + ".";
            }
          } catch (e) { schedBox.hidden = true; }
        }
        document.getElementById("noteSchedOff").addEventListener("click", async () => {
          // Turn off straight away; Undo puts the same schedule back.
          const d = await (await fetch("/schedule")).json().catch(() => ({}));
          const job = ((d && d.jobs) || []).find(j => j.kind === "whatsapp_note");
          await fetch("/schedule/clear?kind=whatsapp_note", { method: "POST" });
          refreshSchedule();
          if (job) undoStatus("Daily WhatsApp turned off.", async () => {
            await fetch("/schedule/whatsapp?number=" + encodeURIComponent(job.number) + "&at=" + encodeURIComponent(job.time), { method: "POST" });
            refreshSchedule();
            status("Daily WhatsApp is back on ✓", "ok");
          });
        });
        document.getElementById("noteBtn").addEventListener("click", refreshSchedule);

        // 🎤 Voice typing: what you say is typed at the end of the note (live
        // preview in the status line) and auto-saved. Sara's own listening is
        // paused meanwhile. Uses the language picked in the dashboard.
        const micBtn = document.getElementById("noteMic");
        const guideMic = document.getElementById("noteGuideMic");
        const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
        let dict = null, dictOn = false;
        function setMic(on) {
          dictOn = on;
          window.noteDictating = on;
          micBtn.classList.toggle("on", on);
          micBtn.setAttribute("aria-pressed", String(on));
          micBtn.title = on ? "Bolna band karne ke liye dabaiye" : "Click karke boliye: jo bolenge woh note mein likh jayega";
          document.getElementById("noteMicLabel").textContent = on ? "Rokein (sun rahi hoon...)" : "Bolkar likho";
          updateGuide();
        }
        function insertSpoken(text) {
          text = text.trim();
          if (!text) return;
          const v = textEl.value;
          const sep = !v || v.endsWith("\\n") ? "" : v.endsWith(" ") ? "" : " ";
          textEl.value = v + sep + text.charAt(0).toUpperCase() + text.slice(1);
          textEl.scrollTop = textEl.scrollHeight;
          schedule();                           // normal auto-save
        }
        function startDictation() {
          if (!SR) { status("Voice typing needs Chrome or Edge.", "err"); return; }
          if (window.saraVoice) window.saraVoice.pause();
          dict = new SR();
          dict.lang = (document.getElementById("langSelect") || {}).value || "en-IN";
          dict.continuous = true;
          dict.interimResults = true;
          dict.onresult = (e) => {
            let interim = "";
            for (let i = e.resultIndex; i < e.results.length; i++) {
              const r = e.results[i];
              if (r.isFinal) insertSpoken(r[0].transcript); else interim += r[0].transcript;
            }
            statusEl.className = "tw-status note-interim";
            statusEl.textContent = interim ? "🎤 " + interim : "🎤 Listening...";
          };
          dict.onerror = (e) => {
            if (e.error === "no-speech") return;   // keep listening
            status(e.error === "not-allowed" ? "Allow the microphone in the browser." : "Voice typing error: " + e.error, "err");
            stopDictation();
          };
          // Chrome ends continuous sessions after a pause: restart while on.
          dict.onend = () => { if (dictOn) { try { dict.start(); } catch (err) { stopDictation(); } } };
          setMic(true);
          try { dict.start(); status("🎤 Listening... speak now"); textEl.focus(); }
          catch (e) { stopDictation(); }
        }
        function stopDictation() {
          const was = dictOn;
          setMic(false);
          if (dict) { try { dict.stop(); } catch (e) {} dict = null; }
          if (was) { status("Voice typing off"); if (window.saraVoice) window.saraVoice.resume(); }
        }
        micBtn.addEventListener("click", () => (dictOn ? stopDictation() : startDictation()));
        guideMic.addEventListener("click", () => { if (!dictOn) startDictation(); });
        document.querySelector("#noteWin [data-close]").addEventListener("click", stopDictation);

        // 📲 Send: save, then send the open note on WhatsApp right now.
        const sendBtn = document.getElementById("noteSend");
        sendBtn.addEventListener("click", async () => {
          await flush();
          if (!textEl.value.trim()) { status("Write something first: the note is empty.", "err"); return; }
          if (current === null) { status("Saving first...", ""); await flush(); }
          // Sends straight to the saved number (the daily-schedule one); only
          // asks when no number has been saved yet.
          let num = "";
          try {
            const d = await (await fetch("/schedule")).json();
            const job = (d.jobs || []).find(j => j.kind === "whatsapp_note");
            num = job ? job.number_display : "";
          } catch (e) {}
          if (!num) {
            const asked = prompt("No WhatsApp number saved yet. Send this note to:", "");
            if (asked === null) return;
            num = asked.replace(/[^0-9+]/g, "");
            if (num.replace(/[^0-9]/g, "").length < 10) { status("Enter a valid 10-digit number.", "err"); return; }
          }
          sendBtn.disabled = true;
          status("Sending to " + num + "... WhatsApp opens; keep it in front for ~10 s.");
          try {
            const r = await (await fetch("/notes/send?name=" + encodeURIComponent(current || nameEl.value) +
                                         "&number=" + encodeURIComponent(num), { method: "POST" })).json();
            status(r.ok ? "✓ " + r.message : r.message, r.ok ? "ok" : "err");
          } catch (e) {
            status("Couldn't reach the server.", "err");
          } finally {
            setTimeout(() => { sendBtn.disabled = false; }, 4000);
          }
        });

        async function show(name) {
          refreshSchedule();
          if (!w) return;
          opening = true;
          w.open();
          opening = false;
          if (name && name === current) { clearTimeout(saveTimer); current = null; }
          const d = await refreshList();
          const exists = d && d.notes.some(n => n.name === name);
          if (name && exists) openNote(name);
          else if (name) { current = null; nameEl.value = name; textEl.value = ""; counts(); status("New note: starts saving when you type"); }
          else if (current === null && !textEl.value) {
            if (d && d.notes.length) openNote(d.notes[0].name); else newNote();
          }
        }
        return { open: w && w.open, show, flush };
      })();

      // --- 📱 Phone input window: how to connect, QR code, live status -----------
      window.phonePanel = (() => {
        const win = document.getElementById("phoneWin");
        if (!win) return null;
        const conn = document.getElementById("phConn"), connText = document.getElementById("phConnText");
        const urlEl = document.getElementById("phUrl"), qrBox = document.getElementById("phQr");
        const lanStep = document.getElementById("phLanStep"), lastEl = document.getElementById("phLast");
        let timer = null, qrUrl = "";

        win.querySelectorAll(".ph-tabs button").forEach(b => b.addEventListener("click", () => {
          win.querySelectorAll(".ph-tabs button").forEach(x => x.classList.toggle("on", x === b));
          win.querySelectorAll(".ph-pane").forEach(p => { p.hidden = p.dataset.pane !== b.dataset.tab; });
        }));

        function loadQrLib() {
          if (window.qrcode) return Promise.resolve(true);
          return new Promise(res => {
            const s = document.createElement("script");
            s.src = "https://cdnjs.cloudflare.com/ajax/libs/qrcode-generator/1.4.4/qrcode.min.js";
            s.onload = () => res(true); s.onerror = () => res(false);
            document.head.append(s);
          });
        }
        async function drawQr(url) {
          if (url === qrUrl) return;
          qrUrl = url;
          if (!(await loadQrLib())) { qrBox.textContent = "QR needs internet. Use the link instead."; return; }
          const qr = window.qrcode(0, "M");
          qr.addData(url); qr.make();
          const img = new Image(); img.alt = "QR code for " + url; img.src = qr.createDataURL(6, 0);
          qrBox.replaceChildren(img);
        }

        async function refresh() {
          try {
            const s = await (await fetch("/remote/status")).json();
            conn.classList.toggle("on", s.connected);
            connText.textContent = s.connected ? "Phone connected: " + (s.device || "phone") + " · ab phone se boliye" : "Phone connected nahi hai: neeche diye steps follow kijiye";
            const url = "http://" + s.lan_ip + ":" + s.port + "/remote";
            urlEl.textContent = url;
            document.getElementById("phAdb").textContent = "adb reverse tcp:" + s.port + " tcp:" + s.port;
            if (s.lan) {
              lanStep.classList.remove("warn");
              lanStep.innerHTML = "Sara LAN mode mein chal rahi hai ✓";
              drawQr(url);
            } else {
              lanStep.classList.add("warn");
              lanStep.innerHTML = "Sara abhi sirf is laptop ke liye chal rahi hai. Band karke aise chalaiye: <code>my-python-project serve --lan</code>";
              qrBox.textContent = "QR yahan dikhega jab Sara --lan mode mein chalegi.";
              qrUrl = "";
            }
          } catch (e) { connText.textContent = "Status nahi mila"; }
        }
        document.getElementById("phCopy").addEventListener("click", async () => {
          try { await navigator.clipboard.writeText(urlEl.textContent); document.getElementById("phoneStatus").textContent = "Link copied ✓"; } catch (e) {}
        });
        const w = toolWin("phoneWin", "phoneBtn", () => { refresh(); clearInterval(timer); timer = setInterval(() => { if (!win.hidden) refresh(); }, 4000); });
        win.querySelector("[data-close]").addEventListener("click", () => clearInterval(timer));
        return {
          refresh,
          last(text, reply) {
            lastEl.hidden = false;
            lastEl.textContent = "Last from phone: “" + text + "” → " + reply;
          },
        };
      })();

      // --- FM Radio window ----------------------------------------------------------
      // Stations from /radio/stations (Radio Browser). Plain MP3/AAC streams play
      // in an <audio>; HLS (.m3u8, e.g. All India Radio) through hls.js.
      // Sara's voice ducks the radio; Auto Mic stays off while it plays.
      window.radioBox = (() => {
        const win = document.getElementById("radioWin");
        if (!win) return null;
        const list = document.getElementById("radioList"), quick = document.getElementById("radioQuick");
        const nameEl = document.getElementById("radioName"), metaEl = document.getElementById("radioMeta");
        const stateEl = document.getElementById("radioState"), logo = document.getElementById("radioLogo");
        const playBtn = document.getElementById("radioPlay"), statusEl = document.getElementById("radioStatus");
        const vol = document.getElementById("radioVol"), volVal = document.getElementById("radioVolVal");
        const input = document.getElementById("radioQuery");
        const audio = new Audio();
        audio.preload = "none";
        let stations = [], current = null, hls = null, loadedQuery = null, wanted = false;
        window.radioPlaying = false;
        try { vol.value = localStorage.getItem("radioVol") || 80; } catch (e) {}
        volVal.textContent = vol.value + "%";

        function setState(text, live) {
          stateEl.textContent = text;
          win.classList.toggle("live", !!live);
          window.radioPlaying = !!live;
          playBtn.textContent = live || wanted ? "■" : "▶";
          statusEl.textContent = live ? "On air" : "";
          statusEl.className = "tw-status" + (live ? " ok" : "");
        }
        function el(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; }
        function favicon(box, s, fallback) {
          box.replaceChildren();
          if (s && s.favicon) {
            const img = el("img"); img.alt = ""; img.referrerPolicy = "no-referrer"; img.src = s.favicon;
            img.onerror = () => { box.textContent = fallback; };
            box.append(img);
          } else box.textContent = fallback;
        }
        function render() {
          if (!stations.length) { list.replaceChildren(el("li", "rd-empty", "Koi station nahi mila. Doosra naam try kijiye.")); return; }
          list.replaceChildren(...stations.map(s => {
            const li = el("li"), b = el("button", "rd-item" + (current && current.id === s.id ? " on" : ""));
            b.type = "button";
            const fav = el("span", "rd-fav"); favicon(fav, s, "📻");
            const txt = el("span", "rd-txt");
            txt.append(el("b", null, s.name), el("span", "rd-sub",
              [s.tags.slice(0, 3).join(" · "), s.bitrate ? s.bitrate + " kbps" : "", s.hls ? "HLS" : s.codec].filter(Boolean).join(" · ")));
            b.append(fav, txt);
            b.addEventListener("click", () => play(s));
            li.append(b);
            return li;
          }));
        }
        async function load(q) {
          q = (q || "").trim();
          if (q === loadedQuery && stations.length) return stations;
          list.replaceChildren(el("li", "rd-empty", q ? "'" + q + "' dhoondh rahi hoon..." : "Stations load ho rahe hain..."));
          try {
            const d = await (await fetch("/radio/stations?q=" + encodeURIComponent(q))).json();
            stations = d.stations || [];
            loadedQuery = q;
            if (!quick.children.length) {
              (d.quick || []).forEach(name => {
                const qb = el("button", null, name); qb.type = "button";
                qb.addEventListener("click", () => { input.value = name; search(name); });
                quick.append(qb);
              });
            }
            if (!d.ok) list.replaceChildren(el("li", "rd-empty", "Radio directory se connect nahi hua. Internet check kijiye."));
            else render();
          } catch (e) {
            list.replaceChildren(el("li", "rd-empty", "Server se stations nahi mile."));
          }
          return stations;
        }
        function search(q) {
          quick.querySelectorAll("button").forEach(b => b.classList.toggle("on", b.textContent === q));
          return load(q);
        }
        function loadHls() {
          if (window.Hls) return Promise.resolve(true);
          return new Promise(res => {
            const s = document.createElement("script");
            s.src = "https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.13/hls.min.js";
            s.onload = () => res(true); s.onerror = () => res(false);
            document.head.append(s);
          });
        }
        function teardown() {
          if (hls) { try { hls.destroy(); } catch (e) {} hls = null; }
          audio.pause(); audio.removeAttribute("src"); audio.load();
        }
        async function play(s) {
          if (!s) return;
          if (typeof stopSpeaking === "function") stopSpeaking();
          if (window.quranBox && window.quranBox.playing) window.quranBox.stop();     // one audio at a time
          if (typeof mediaPlayer !== "undefined" && mediaPlayer.playing) mediaPlayer.stop();
          current = s; wanted = true;
          teardown();
          nameEl.textContent = s.name;
          metaEl.textContent = [s.tags.slice(0, 3).join(" · "), s.bitrate ? s.bitrate + " kbps" : "", s.country].filter(Boolean).join(" · ");
          favicon(logo, s, "📻");
          render();
          setState("TUNING...", false);
          audio.volume = vol.value / 100;
          try {
            if (s.hls && !audio.canPlayType("application/vnd.apple.mpegurl")) {
              if (!(await loadHls()) || !window.Hls.isSupported()) throw new Error("hls");
              hls = new window.Hls({ lowLatencyMode: true });
              hls.loadSource(s.url); hls.attachMedia(audio);
              hls.on(window.Hls.Events.ERROR, (_e, data) => { if (data.fatal) failed(); });
            } else {
              audio.src = s.url;
            }
            await audio.play();
          } catch (e) { failed(); }
        }
        function failed() {
          if (!wanted) return;
          setState("⚠ YE STATION ABHI NAHI CHAL RAHA", false);
          statusEl.textContent = "Doosra station chuniye";
          statusEl.className = "tw-status err";
          wanted = false; playBtn.textContent = "▶";
          window.radioPlaying = false;
        }
        function stop() {
          wanted = false;
          teardown();
          setState(current ? "STOPPED" : "CHUNIYE", false);
        }
        audio.addEventListener("playing", () => setState("● LIVE", true));
        audio.addEventListener("waiting", () => { if (wanted) setState("BUFFERING...", true); });
        audio.addEventListener("error", () => { if (wanted && !hls) failed(); });
        audio.addEventListener("pause", () => { if (wanted && !audio.ended) return; });
        playBtn.addEventListener("click", () => {
          if (wanted) stop();
          else if (current) play(current);
          else if (stations[0]) play(stations[0]);
        });
        vol.addEventListener("input", () => {
          volVal.textContent = vol.value + "%";
          try { localStorage.setItem("radioVol", vol.value); } catch (e) {}
        });
        // Duck the radio while Sara speaks (and restore after).
        setInterval(() => {
          const target = vol.value / 100 * (aiVisual.state === "speaking" ? 0.2 : 1);
          if (Math.abs(audio.volume - target) > 0.01) audio.volume += (target - audio.volume) * 0.5;
        }, 120);
        document.getElementById("radioSearch").addEventListener("submit", (e) => { e.preventDefault(); search(input.value); });

        const w = toolWin("radioWin", "radioBtn", () => load(loadedQuery || ""));
        return {
          open: async () => { if (w) w.open(); await load(loadedQuery || ""); },
          // Voice: "radio mirchi chalao" -> search and play the best match.
          async tune(query) {
            if (w) w.open();
            input.value = query || "";
            const found = await search(query || "");
            if (found.length) play(found[0]);
            return found.length > 0;
          },
          stop,
          get playing() { return window.radioPlaying; },
        };
      })();

      // --- Skills window: everything Sara can do (from /skills) -------------------
      window.skillsBox = (() => {
        const win = document.getElementById("skillsWin");
        if (!win) return null;
        const grid = document.getElementById("skillsGrid"), cats = document.getElementById("skillsCats");
        const search = document.getElementById("skillsSearch"), countEl = document.getElementById("skillsCount");
        let data = null, cat = "all";
        const norm = (t) => (t || "").toLowerCase();

        async function load() {
          if (data) return data;
          try { data = await (await fetch("/skills")).json(); }
          catch (e) { grid.innerHTML = '<div class="sk-empty">Skills list load nahi hui. Sara ka server chal raha hai?</div>'; return null; }
          countEl.textContent = data.count + " skills · " + data.examples + " example commands";
          const chip = (id, label, n) => {
            const b = document.createElement("button"); b.type = "button"; b.className = "sk-cat" + (id === cat ? " on" : "");
            b.setAttribute("role", "tab"); b.dataset.cat = id; b.textContent = label;
            if (n != null) { const i = document.createElement("i"); i.textContent = n; b.append(i); }
            b.addEventListener("click", () => { cat = id; cats.querySelectorAll(".sk-cat").forEach(x => x.classList.toggle("on", x === b)); render(); });
            return b;
          };
          cats.replaceChildren(chip("all", "Sab", data.count), ...data.categories.map(c => chip(c.id, c.icon + " " + c.title, c.skills.length)));
          return data;
        }

        function useExample(ex) {
          const input = document.getElementById("cmdInput");
          if (!input) return;
          input.value = ex.text;
          if (ex.run && typeof runCustomCommand === "function") { runCustomCommand(); return; }
          input.focus();
          if (ex.edit) {                                   // select the part to replace (98XXXXXXXX, name@example.com ...)
            const m = ex.text.match(/98X+|name@example[.]com|realme buds/);
            if (m) input.setSelectionRange(m.index, m.index + m[0].length);
          } else input.setSelectionRange(input.value.length, input.value.length);
          input.classList.remove("sk-flash"); void input.offsetWidth; input.classList.add("sk-flash");
        }

        function render() {
          if (!data) return;
          const q = norm(search.value.trim());
          const out = [];
          for (const c of data.categories) {
            if (cat !== "all" && c.id !== cat && !q) continue;
            const skills = c.skills.filter(s => !q || norm(c.title + " " + s.title + " " + s.desc + " " + s.examples.map(e => e.text).join(" ")).includes(q));
            if (!skills.length) continue;
            const sec = document.createElement("div"); sec.className = "sk-sec";
            const ic = document.createElement("span"); ic.textContent = c.icon;
            sec.append(ic, c.title);
            out.push(sec);
            for (const s of skills) {
              const card = document.createElement("article"); card.className = "sk-card";
              const h = document.createElement("h5"); h.textContent = s.title;
              const p = document.createElement("p"); p.textContent = s.desc;
              const ex = document.createElement("div"); ex.className = "sk-ex";
              for (const e of s.examples) {
                const b = document.createElement("button"); b.type = "button";
                b.className = e.edit ? "edt" : e.run ? "" : "chk";
                b.title = e.edit ? "Command box mein aayega: hissa badal kar Enter dabaiye"
                        : e.run ? "Click karte hi chalega" : "Command box mein aayega: dekh kar Enter dabaiye";
                const i = document.createElement("i"); i.textContent = e.edit ? "✎" : e.run ? "▶" : "↵";
                const t = document.createElement("span"); t.textContent = e.text;
                b.append(i, t);
                b.addEventListener("click", () => useExample(e));
                ex.append(b);
              }
              if (!s.examples.length) {
                const em = document.createElement("em");
                em.textContent = "Phone ke browser mein " + location.origin + "/remote kholiye";
                ex.append(em);
              }
              card.append(h, p, ex);
              out.push(card);
            }
          }
          if (!out.length) {
            const d = document.createElement("div"); d.className = "sk-empty";
            d.textContent = "Is naam se koi skill nahi mili. Phir bhi Sara se seedha poochh sakte hain: wo AI se jawab degi.";
            out.push(d);
          }
          grid.replaceChildren(...out);
        }

        search.addEventListener("input", render);
        const w = toolWin("skillsWin", "skillsBtn", async () => { await load(); render(); setTimeout(() => search.focus(), 50); });
        return { open: async () => { if (w) w.open(); await load(); render(); } };
      })();

      // --- Quran window: audio tilawat (mp3quran.net), all 114 surahs ----------
      // 📖 button or "quran sunao" opens it; "surah yaseen sunao" plays here.
      // Plain MP3 audio (not YouTube), so it also plays where a web filter blocks
      // video sites. While it plays, Sara's Auto Mic and the globe music stay quiet.
      window.quranBox = (() => {
        const win = document.getElementById("quranWin");
        if (!win) return null;
        const $ = (id) => document.getElementById(id);
        const list = $("quranList"), hero = $("quranHero"), statusEl = $("quranStatus");
        const numEl = $("quranNum"), arEl = $("quranArabic"), nameEl = $("quranName"), metaEl = $("quranMeta");
        const msgEl = $("quranMsg"), resumeEl = $("quranResume");
        const seek = $("quranSeek"), curEl = $("quranCur"), durEl = $("quranDur");
        const playBtn = $("quranPlay"), reciterSel = $("quranReciter"), searchEl = $("quranSearch");
        const repeatBtn = $("quranRepeat"), autoBtn = $("quranAuto"), volEl = $("quranVol");
        const mini = $("quranMini"), miniState = $("quranMiniState"), miniTitle = $("quranMiniTitle");
        const miniPlay = $("quranMiniPlay"), miniBar = $("quranMiniBar");
        const ICON_PLAY = '<svg viewBox="0 0 24 24"><path d="M7 4v16l13-8z"/></svg>';
        const ICON_PAUSE = '<svg viewBox="0 0 24 24"><path d="M6 4h4v16H6zm8 0h4v16h-4z"/></svg>';
        const BISMILLAH = "بِسْمِ ٱللَّٰهِ ٱلرَّحْمَٰنِ ٱلرَّحِيمِ";   // Sara says this before a recitation
        const STAR = '<svg viewBox="0 0 100 100" aria-hidden="true"><polygon points="50,4 63.5,17.5 82.5,17.5 82.5,36.5 96,50 82.5,63.5 82.5,82.5 63.5,82.5 50,96 36.5,82.5 17.5,82.5 17.5,63.5 4,50 17.5,36.5 17.5,17.5 36.5,17.5"/><circle cx="50" cy="50" r="27"/></svg>';
        const DEFAULT_FAVS = [1, 18, 19, 23, 36, 55, 56, 67, 17];

        const store = {
          get(k, d) { try { const v = localStorage.getItem("sara.quran." + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
          set(k, v) { try { localStorage.setItem("sara.quran." + k, JSON.stringify(v)); } catch (e) {} },
        };
        let surahs = [], reciters = [], videos = [];      // videos: the YouTube favourites (voice compatibility)
        let current = 0, tab = "fav", loaded = null, lastSave = 0, switching = false;
        let favs = store.get("favs", DEFAULT_FAVS);
        let reciterId = store.get("reciter", "yasser");
        const audio = new Audio();
        audio.preload = "metadata";
        audio.volume = Math.min(1, Math.max(0, store.get("vol", 100) / 100));
        volEl.value = Math.round(audio.volume * 100);
        repeatBtn.setAttribute("aria-pressed", String(store.get("repeat", false)));
        autoBtn.setAttribute("aria-pressed", String(store.get("auto", true)));
        window.quranPlaying = false;

        const fmt = (s) => {
          if (!isFinite(s) || s < 0) return "0:00";
          s = Math.floor(s);
          const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = String(s % 60).padStart(2, "0");
          return h ? h + ":" + String(m).padStart(2, "0") + ":" + x : m + ":" + x;
        };
        const reciter = () => reciters.find(r => r.id === reciterId) || reciters[0];
        const surah = (n) => surahs[n - 1];
        const fill = (el, frac) => el.style.setProperty("--p", (Math.max(0, Math.min(1, frac)) * 100).toFixed(2) + "%");
        const pressed = (b) => b.getAttribute("aria-pressed") === "true";

        function setState(label, playing) {
          window.quranPlaying = !!playing;
          hero.classList.toggle("playing", !!playing && !audio.paused);
          const icon = audio.paused ? ICON_PLAY : ICON_PAUSE;
          playBtn.innerHTML = icon; miniPlay.innerHTML = icon;
          playBtn.setAttribute("aria-label", audio.paused ? "Play" : "Pause");
          statusEl.textContent = label || "";
          statusEl.className = "tw-status" + (playing ? " ok" : "");
          miniState.textContent = label ? label.toUpperCase() : "TILAWAT";
        }
        function showMsg(text, retry) {
          msgEl.hidden = !text;
          msgEl.textContent = text || "";
          if (text && retry) {
            const b = document.createElement("button"); b.type = "button"; b.textContent = "Dobara koshish";
            b.addEventListener("click", retry); msgEl.append(b);
          }
        }

        async function load() {
          if (loaded) return loaded;
          loaded = (async () => {
            try {
              const d = await (await fetch("/quran/catalog")).json();
              surahs = d.surahs || []; reciters = d.reciters || [];
              if (!reciters.some(r => r.id === reciterId)) reciterId = d.default_reciter || (reciters[0] && reciters[0].id);
            } catch (e) { surahs = []; }
            try { videos = (await (await fetch("/media/list?category=quran")).json()).songs || []; } catch (e) { videos = []; }
            reciterSel.replaceChildren(...reciters.map(r => {
              const o = document.createElement("option"); o.value = r.id; o.textContent = "Qari: " + r.name; return o;
            }));
            reciterSel.value = reciterId;
            if (!surahs.length) {
              loaded = null;
              showMsg("Surah list load nahi hui. Sara ka server chal raha hai?", () => { showMsg(""); load(); });
              return false;
            }
            showIdle(); render();
            return true;
          })();
          return loaded;
        }

        function showIdle() {
          numEl.textContent = "۞";
          arEl.textContent = BISMILLAH; arEl.classList.add("long");
          nameEl.textContent = "Tilawat ke liye koi surah chuniye";
          metaEl.textContent = "114 surahs · Sheikh " + (reciter() ? reciter().name : "");
          const last = store.get("last", null);
          if (last && surah(last.n)) {
            resumeEl.hidden = false;
            resumeEl.textContent = "▶ Jaari rakhein: Surah " + surah(last.n).name + (last.t > 5 ? " · " + fmt(last.t) + " se" : "");
          } else resumeEl.hidden = true;
        }
        function showSurah(n) {
          const s = surah(n);
          numEl.textContent = n;
          arEl.textContent = "سورة " + s.arabic; arEl.classList.toggle("long", s.arabic.length > 9);
          nameEl.textContent = "Surah " + s.name + (norm(s.meaning) === norm(s.name) ? "" : " · " + s.meaning);
          metaEl.textContent = s.ayahs + " ayat · Sheikh " + reciter().name;
          const v = videos.find(x => x.surah === n && x.video_id);
          if (v) {
            const a = document.createElement("a");
            a.href = "https://www.youtube.com/watch?v=" + encodeURIComponent(v.video_id);
            a.target = "_blank"; a.rel = "noopener"; a.textContent = "· YouTube par video ↗";
            metaEl.append(a);
          }
          resumeEl.hidden = true;
          miniTitle.textContent = "Surah " + s.name;
        }

        function norm(t) { return (t || "").toLowerCase().replace(/['’-]/g, "").replace(/ +/g, " ").trim(); }
        function render() {
          const q = norm(searchEl.value);
          let rows;
          if (q) {
            rows = surahs.filter(s => String(s.number) === q || norm(s.name).includes(q) || norm(s.name).replace(/^(al|an|ar|as|ash|at|ad|adh|az) /, "").startsWith(q)
              || s.arabic.includes(searchEl.value.trim()) || norm(s.meaning).includes(q));
          } else rows = tab === "fav" ? favs.map(surah).filter(Boolean) : surahs;
          if (!rows.length) {
            const li = document.createElement("li"); li.className = "qr-empty-list";
            li.textContent = q ? "Koi surah nahi mili." : "Abhi koi pasandida surah nahi. ☆ dabakar jodiye.";
            list.replaceChildren(li); return;
          }
          list.replaceChildren(...rows.map(s => {
            const li = document.createElement("li");
            const b = document.createElement("div");
            b.className = "qr-item" + (s.number === current ? " on" : "");
            b.setAttribute("role", "button"); b.tabIndex = 0;
            b.title = "Surah " + s.name + " chalayein";
            const n = document.createElement("span"); n.className = "qr-n";
            n.innerHTML = STAR;
            const nb = document.createElement("b"); nb.textContent = s.number; n.append(nb);
            const it = document.createElement("span"); it.className = "qr-it";
            const t = document.createElement("b"); t.textContent = s.name;
            const m = document.createElement("span");
            m.textContent = (norm(s.meaning) === norm(s.name) ? "Surah " + s.number : s.meaning) + " · " + s.ayahs + " ayat";
            it.append(t, m);
            const ar = document.createElement("span"); ar.className = "qr-ar"; ar.textContent = s.arabic;
            const star = document.createElement("button"); star.type = "button";
            const isFav = favs.includes(s.number);
            star.className = "qr-star" + (isFav ? " on" : ""); star.textContent = isFav ? "★" : "☆";
            star.title = isFav ? "Pasandida se hatayein" : "Pasandida mein jodein";
            star.setAttribute("aria-label", star.title);
            star.addEventListener("click", (e) => {
              e.stopPropagation();
              favs = favs.includes(s.number) ? favs.filter(x => x !== s.number) : [...favs, s.number];
              store.set("favs", favs); render();
            });
            b.append(n, it, ar, star);
            b.addEventListener("click", () => play(s.number));
            b.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); play(s.number); } });
            li.append(b);
            return li;
          }));
          const on = list.querySelector(".qr-item.on");
          if (on && !q) on.scrollIntoView({ block: "nearest" });
        }

        // opts.bismillah: Sara says Bismillah first (not before At-Tawbah, which has none).
        async function play(n, opts = {}) {
          if (!(await load())) return;
          n = Number(n);
          if (!surah(n)) return;
          const { bismillah = true, at = 0 } = opts;
          if (typeof stopSpeaking === "function") stopSpeaking();
          if (typeof mediaPlayer !== "undefined" && mediaPlayer.playing) mediaPlayer.stop();   // one audio at a time
          if (window.radioBox && window.radioBox.playing) window.radioBox.stop();
          switching = true;                                // ignore the old track's late "pause" event
          audio.pause();
          current = n;
          showMsg(""); showSurah(n); render();
          seek.disabled = true; seek.value = 0; fill(seek, 0); curEl.textContent = "0:00"; durEl.textContent = "0:00";
          if (bismillah && n !== 9 && typeof speakOut === "function") {
            setState("Bismillah", true);
            await speakOut(BISMILLAH);
            if (current !== n) return;                     // another surah was chosen meanwhile
          }
          switching = false;
          audio.src = reciter().server + String(n).padStart(3, "0") + ".mp3";
          if (at > 0) audio.addEventListener("loadedmetadata", () => { try { audio.currentTime = at; } catch (e) {} }, { once: true });
          setState("Load ho rahi hai…", true);
          try { await audio.play(); }
          catch (e) {
            if (current !== n) return;
            setState("", false);
            showMsg(e && e.name === "NotAllowedError" ? "Browser ne autoplay roka. ▶ dabaiye." : "Audio shuru nahi hui.", () => toggle());
          }
          mediaSession();
        }
        function toggle() {
          if (!current) { const last = store.get("last", null); return play(last ? last.n : (favs[0] || 1), { at: last ? last.t : 0 }); }
          if (audio.paused) { showMsg(""); audio.play().catch(() => showMsg("Audio shuru nahi hui.", () => play(current, { bismillah: false }))); }
          else audio.pause();
        }
        function stop() {
          audio.pause();
          if (current) store.set("last", { n: current, t: Math.floor(audio.currentTime || 0) });
          audio.removeAttribute("src"); audio.load();
          current = 0;
          setState("", false);
          seek.disabled = true; seek.value = 0; fill(seek, 0); curEl.textContent = "0:00"; durEl.textContent = "0:00";
          fill(miniBar, 0);
          showMsg(""); if (surahs.length) { showIdle(); render(); }
          mini.hidden = true;
        }
        const next = () => play(current ? (current % 114) + 1 : 1, { bismillah: false });
        const prev = () => {
          if (current && audio.currentTime > 5) { audio.currentTime = 0; return; }   // like any player: restart first
          play(current > 1 ? current - 1 : 114, { bismillah: false });
        };

        audio.addEventListener("playing", () => setState("Tilawat chal rahi hai", true));
        audio.addEventListener("pause", () => { if (current && !audio.ended && !switching) setState("Ruki hui", false); });
        audio.addEventListener("waiting", () => setState("Load ho rahi hai…", true));
        audio.addEventListener("loadedmetadata", () => { durEl.textContent = fmt(audio.duration); seek.disabled = false; });
        audio.addEventListener("timeupdate", () => {
          if (!audio.duration) return;
          const f = audio.currentTime / audio.duration;
          if (!seek.matches(":active")) { seek.value = Math.round(f * 1000); fill(seek, f); }
          curEl.textContent = fmt(audio.currentTime); fill(miniBar, f);
          const now = Date.now();
          if (now - lastSave > 5000) { lastSave = now; store.set("last", { n: current, t: Math.floor(audio.currentTime) }); }
        });
        audio.addEventListener("ended", () => {
          store.set("last", null);
          if (pressed(repeatBtn)) { audio.currentTime = 0; audio.play().catch(() => {}); return; }
          setState("Mukammal ✓", false);
          if (pressed(autoBtn)) setTimeout(() => { if (audio.ended) next(); }, 1500);
        });
        audio.addEventListener("error", () => {
          if (!current || !audio.getAttribute("src")) return;
          setState("", false);
          showMsg(navigator.onLine ? "Ye tilawat load nahi hui. Doosra qari chuniye ya dobara koshish karein."
                                   : "Internet nahi hai. Connection check karke dobara koshish karein.",
                  () => play(current, { bismillah: false, at: Math.floor(audio.currentTime || 0) }));
        });

        seek.addEventListener("input", () => {
          fill(seek, seek.value / 1000);
          if (audio.duration) curEl.textContent = fmt(audio.duration * seek.value / 1000);
        });
        seek.addEventListener("change", () => { if (audio.duration) audio.currentTime = audio.duration * seek.value / 1000; });
        volEl.addEventListener("input", () => { audio.volume = volEl.value / 100; fill(volEl, audio.volume); store.set("vol", Number(volEl.value)); });
        fill(volEl, audio.volume);
        for (const b of [repeatBtn, autoBtn]) b.addEventListener("click", () => {
          const on = !pressed(b); b.setAttribute("aria-pressed", String(on));
          store.set(b === repeatBtn ? "repeat" : "auto", on);
        });
        reciterSel.addEventListener("change", () => {
          reciterId = reciterSel.value; store.set("reciter", reciterId);
          if (current) play(current, { bismillah: false, at: 0 }); else showIdle();
        });
        searchEl.addEventListener("input", render);
        win.querySelectorAll(".qr-tabs button").forEach(b => b.addEventListener("click", () => {
          tab = b.dataset.tab;
          win.querySelectorAll(".qr-tabs button").forEach(x => { x.classList.toggle("on", x === b); x.setAttribute("aria-selected", String(x === b)); });
          render();
        }));
        resumeEl.addEventListener("click", () => { const l = store.get("last", null); if (l) play(l.n, { at: l.t }); });
        playBtn.addEventListener("click", toggle);
        miniPlay.addEventListener("click", toggle);
        $("quranPrev").addEventListener("click", prev);
        $("quranNext").addEventListener("click", next);
        win.querySelector("[data-close]").addEventListener("click", stop);   // closing ends the recitation
        // Keyboard (when the window has focus, not while typing): Space = play/pause, ←/→ = 10 s.
        win.addEventListener("keydown", (e) => {
          if (e.target.closest("input, select, textarea")) return;
          if (e.key === " " && !e.target.closest("button, [role=button]")) { e.preventDefault(); toggle(); }
          else if (e.key === "ArrowRight" && current) audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + 10);
          else if (e.key === "ArrowLeft" && current) audio.currentTime = Math.max(0, audio.currentTime - 10);
        });

        // Headset / keyboard media keys and the Windows media overlay.
        function mediaSession() {
          if (!("mediaSession" in navigator) || !current) return;
          const s = surah(current);
          try {
            navigator.mediaSession.metadata = new MediaMetadata({ title: "Surah " + s.name + " (" + s.arabic + ")", artist: "Sheikh " + reciter().name, album: "Al-Qur'an al-Kareem" });
            navigator.mediaSession.setActionHandler("play", toggle);
            navigator.mediaSession.setActionHandler("pause", toggle);
            navigator.mediaSession.setActionHandler("previoustrack", prev);
            navigator.mediaSession.setActionHandler("nexttrack", next);
            navigator.mediaSession.setActionHandler("seekto", (d) => { if (d.seekTime != null) audio.currentTime = d.seekTime; });
          } catch (e) {}
        }

        // ▁ Minimise: hide the window, keep the recitation playing, show the mini player.
        $("quranMin").addEventListener("click", () => {
          win.hidden = true;
          miniTitle.textContent = current ? "Surah " + surah(current).name : "Quran";
          mini.hidden = false;
        });
        $("quranMiniOpen").addEventListener("click", () => { if (w) w.open(); });
        $("quranMiniStop").addEventListener("click", stop);
        // ⛶ Full screen: the recitation stage (big Arabic name + controls).
        $("quranFull").addEventListener("click", () => {
          if (document.fullscreenElement) { document.exitFullscreen().catch(() => {}); return; }
          const st = $("quranStage");
          (st.requestFullscreen ? st.requestFullscreen() : Promise.reject()).catch(() => {});
        });
        setState("", false);

        const w = toolWin("quranWin", "quranBtn", () => { mini.hidden = true; load(); });
        return {
          open: async () => { if (w) w.open(); await load(); },
          playSurah: async (n, opts) => { if (w) w.open(); await play(n, opts); },
          // Older voice payloads name a YouTube favourite: play that surah's audio.
          async playVideo(videoId) {
            await load();
            const v = videos.find(s => s.video_id === videoId);
            if (v && v.surah) { if (w) w.open(); play(v.surah); }
            return !!(v && v.surah);
          },
          has: (videoId) => videos.some(s => s.video_id === videoId),
          load, stop, toggle,
          get playing() { return window.quranPlaying; },
        };
      })();
      if (window.quranBox) window.quranBox.load();

      // --- Calculator: keypad + keyboard; safe parser (no eval) -----------------
      (() => {
        const exprEl = document.getElementById("calcExpr");
        const outEl = document.getElementById("calcOut");
        const histEl = document.getElementById("calcHist");
        const keys = document.getElementById("calcKeys");
        if (!keys) return;
        let expr = "", justEvaluated = false;

        // Recursive-descent parser: + - * / ( ), unary minus, postfix % (= /100).
        function evaluate(src) {
          let i = 0;
          const peek = () => src[i], eat = (c) => (src[i] === c ? (i++, true) : false);
          function number() {
            const m = /^[0-9]*\\.?[0-9]+|^[0-9]+\\.?/.exec(src.slice(i));
            if (!m) throw new Error("Invalid");
            i += m[0].length;
            return parseFloat(m[0]);
          }
          function factor() {
            let v;
            if (eat("-")) return -factor();
            if (eat("+")) return factor();
            if (eat("(")) { v = expression(); if (!eat(")")) throw new Error("Missing )"); }
            else v = number();
            while (eat("%")) v /= 100;
            return v;
          }
          function term() {
            let v = factor();
            for (;;) {
              if (eat("*")) v *= factor();
              else if (eat("/")) { const d = factor(); if (d === 0) throw new Error("Can't divide by 0"); v /= d; }
              else return v;
            }
          }
          function expression() {
            let v = term();
            for (;;) {
              if (eat("+")) v += term();
              else if (eat("-")) v -= term();
              else return v;
            }
          }
          const v = expression();
          if (i < src.length) throw new Error("Invalid");
          if (!Number.isFinite(v)) throw new Error("Too large");
          return Math.round(v * 1e10) / 1e10;
        }
        const pretty = s => s.replace(/\\*/g, "×").replace(/\\//g, "÷").replace(/-/g, "−");
        function show(out, err) {
          exprEl.textContent = pretty(expr);
          outEl.textContent = out;
          outEl.classList.toggle("err", !!err);
        }
        function preview() {
          if (!expr) return show("0");
          try { show(String(evaluate(expr))); } catch (e) { show(pretty(expr)); }
        }
        function press(k) {
          if (k === "C") { expr = ""; justEvaluated = false; return preview(); }
          if (k === "Backspace") { expr = expr.slice(0, -1); return preview(); }
          if (k === "=" || k === "Enter") {
            if (!expr) return;
            try {
              const v = evaluate(expr);
              const line = document.createElement("div");
              line.textContent = pretty(expr) + " = " + v;
              line.addEventListener("click", () => { expr = String(v); preview(); });
              histEl.prepend(line);
              while (histEl.children.length > 8) histEl.lastChild.remove();
              exprEl.textContent = pretty(expr) + " =";
              outEl.textContent = String(v); outEl.classList.remove("err");
              expr = String(v); justEvaluated = true;
            } catch (e) { show(e.message, true); }
            return;
          }
          if (!/^[0-9.+\\-*/()%]$/.test(k)) return;
          if (justEvaluated && /[0-9.(]/.test(k)) expr = "";   // new calculation after "="
          justEvaluated = false;
          expr += k;
          preview();
        }
        keys.addEventListener("click", (e) => { const b = e.target.closest("button[data-k]"); if (b) press(b.dataset.k); });
        document.getElementById("calcWin").addEventListener("keydown", (e) => {
          if (e.key === "Escape") return;
          const k = e.key === "x" ? "*" : e.key === "Delete" ? "C" : e.key;
          if (/^[0-9.+\\-*/()%]$/.test(k) || ["Enter", "=", "Backspace", "C"].includes(k)) { e.preventDefault(); press(k); }
        });
        const w = toolWin("calcWin", "calcBtn", () => document.getElementById("calcWin").focus());
        document.getElementById("calcWin").tabIndex = -1;
        // Voice: open the calculator, optionally solving ``e`` ("250*4/8").
        window.saraCalc = {
          show(e) {
            if (w) w.open();
            if (e) { expr = String(e).replace(/[^0-9.+\\-*/()%]/g, ""); justEvaluated = false; press("="); }
          },
        };
      })();

      // --- Globe screen: date / time / location + map box ----------------------
      (() => {
        const dateEl = document.getElementById("giDate");
        const timeEl = document.getElementById("giTime");
        const placeEl = document.getElementById("giPlace");
        const mapBtn = document.getElementById("giMapBtn");
        const mapBox = document.getElementById("giMap");
        const frame = document.getElementById("giMapFrame");
        const openLink = document.getElementById("giMapOpen");
        const foot = document.getElementById("giMapFoot");
        if (!dateEl || !mapBox) return;
        const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
        const pad = n => String(n).padStart(2, "0");
        let where = null;   // { lat, lon, place, source }

        // DD-Mon-YYYY · HH:MM:SS (local time)
        function tick() {
          const d = new Date();
          dateEl.textContent = pad(d.getDate()) + "-" + MONTHS[d.getMonth()] + "-" + d.getFullYear();
          timeEl.textContent = pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
        }
        tick();
        setInterval(tick, 1000);

        // where.precise: true once the browser gave a real position; until then
        // the IP city is shown with "≈" (it's only the internet provider's hub).
        function setWhere(lat, lon, place, source, precise, detail) {
          where = { lat, lon, place: place || "", source, precise: !!precise, detail: detail || "" };
          placeEl.textContent = where.place ? (precise ? "" : "≈ ") + where.place : lat.toFixed(4) + ", " + lon.toFixed(4);
          placeEl.title = where.detail || where.place;
          if (!mapBox.hidden) loadMap();
        }
        async function preciseFix(p) {
          const lat = p.coords.latitude, lon = p.coords.longitude;
          const src = "GPS ±" + Math.round(p.coords.accuracy) + " m";
          setWhere(lat, lon, where && where.precise ? where.place : "", src, true);
          const a = await placeFor(lat, lon);
          // Full address; the landmark is added only if the address doesn't
          // already start with it (avoids "WORLD TRADE TOWER — WORLD TRADE TOWER, ...").
          const detail = a && a.landmark && !(a.display || "").startsWith(a.landmark)
            ? a.landmark + " — " + a.display : (a && a.display) || "";
          if (a) setWhere(lat, lon, a.short + (a.country_code ? ", " + a.country_code : ""), src, true, detail);
        }
        (async () => {
          try {
            const n = await (await fetch("/netinfo")).json();
            const place = [n.city, n.country_code].filter(Boolean).join(", ");
            if (!(where && where.precise)) {
              if (n.lat != null && n.lon != null) setWhere(n.lat, n.lon, place, "IP location (approx.)", false);
              else placeEl.textContent = place ? "≈ " + place : "Location unavailable";
            }
          } catch (e) { if (!where) placeEl.textContent = "Offline"; }
        })();
        // Precise position from the browser (Wi-Fi / GPS), kept up to date.
        if (navigator.geolocation) {
          navigator.geolocation.watchPosition(preciseFix, () => {},
            { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 });
        }

        function loadMap() {
          if (!where) { foot.textContent = "Locating..."; return; }
          const { lat, lon } = where, dx = 0.03, dy = 0.018;
          const src = "https://www.openstreetmap.org/export/embed.html?bbox=" +
            [lon - dx, lat - dy, lon + dx, lat + dy].map(v => v.toFixed(5)).join("%2C") +
            "&layer=mapnik&marker=" + lat.toFixed(5) + "%2C" + lon.toFixed(5);
          if (frame.src !== src) frame.src = src;
          openLink.href = "https://www.openstreetmap.org/?mlat=" + lat.toFixed(5) + "&mlon=" + lon.toFixed(5) + "#map=14/" + lat.toFixed(5) + "/" + lon.toFixed(5);
          foot.textContent = (where.detail || where.place || "").toUpperCase() + " · " +
            lat.toFixed(4) + "°, " + lon.toFixed(4) + "° · " + where.source;
          const credit = document.createElement("small");
          credit.textContent = "Map © OpenStreetMap contributors";
          foot.append(credit);
        }
        function toggleMap(show) {
          mapBox.hidden = !show;
          mapBtn.setAttribute("aria-expanded", String(show));
          if (show) loadMap();
        }
        mapBtn.addEventListener("click", () => toggleMap(mapBox.hidden));
        document.getElementById("giMapClose").addEventListener("click", () => toggleMap(false));
      })();

      // --- Globe sound: soft ambient hum + a radar ping per sweep --------------
      // Synthesised with Web Audio (no files). Starts after the first click or
      // key press (browser autoplay rule); plays only while the globe shows.
      window.saraGlobeSound = (() => {
        const btn = document.getElementById("giSoundBtn");
        const musicBtn = document.getElementById("giMusicBtn");
        const KEY = "saraGlobeSound", MUSIC_KEY = "saraGlobeMusic";
        let on = true, musicOn = true;
        try { on = localStorage.getItem(KEY) !== "off"; musicOn = localStorage.getItem(MUSIC_KEY) !== "off"; } catch (e) {}
        let ac = null, hum = null, music = null, echo = null;

        function paint() {
          if (btn) {
            btn.textContent = on ? "🔊" : "🔇";
            btn.classList.toggle("off", !on);
            btn.title = on ? "Sound effects: on" : "Sound effects: off";
          }
          if (musicBtn) {
            musicBtn.classList.toggle("off", !musicOn);
            musicBtn.title = musicOn ? "Soft music: on" : "Soft music: off";
            musicBtn.setAttribute("aria-pressed", String(musicOn));
          }
        }
        function globeVisible() {
          return noSignal.style.display !== "none" && !document.hidden;
        }
        function songPlaying() {
          try { return !!((mediaPlayer && mediaPlayer.playing) || window.quranPlaying || window.radioPlaying); } catch (e) { return !!(window.quranPlaying || window.radioPlaying); }
        }
        function ensure() {
          if (ac) return ac;
          const AC = window.AudioContext || window.webkitAudioContext;
          if (!AC) return null;
          ac = new AC();
          // Ambient hum: detuned low tones through a slowly breathing filter.
          const out = ac.createGain(); out.gain.value = 0; out.connect(ac.destination);
          const lp = ac.createBiquadFilter(); lp.type = "lowpass"; lp.frequency.value = 380; lp.Q.value = 4; lp.connect(out);
          for (const [f, type] of [[55, "sine"], [82.6, "triangle"], [110.3, "sine"]]) {
            const o = ac.createOscillator(); o.type = type; o.frequency.value = f;
            const g = ac.createGain(); g.gain.value = f > 100 ? 0.25 : 0.5;
            o.connect(g); g.connect(lp); o.start();
          }
          const lfo = ac.createOscillator(); lfo.frequency.value = 0.08;
          const lfoGain = ac.createGain(); lfoGain.gain.value = 160;
          lfo.connect(lfoGain); lfoGain.connect(lp.frequency); lfo.start();
          hum = out;

          // Music bus with a soft feedback echo for space.
          music = ac.createGain(); music.gain.value = 0; music.connect(ac.destination);
          echo = ac.createDelay(1.0); echo.delayTime.value = 0.42;
          const fb = ac.createGain(); fb.gain.value = 0.38;
          const wet = ac.createGain(); wet.gain.value = 0.5;
          const tone = ac.createBiquadFilter(); tone.type = "lowpass"; tone.frequency.value = 2200;
          echo.connect(tone); tone.connect(fb); fb.connect(echo); tone.connect(wet); wet.connect(music);
          return ac;
        }

        // --- Soft generative music: slow pad chords + gentle bells --------
        // A-minor feel; a new chord every 8 s, bells from a calm pentatonic.
        const hz = n => 440 * Math.pow(2, (n - 69) / 12);          // MIDI -> Hz
        const CHORDS = [[45, 52, 57, 60, 64], [41, 48, 57, 60, 64], [48, 55, 60, 64, 67], [43, 50, 59, 62, 67]];
        const BELLS = [69, 72, 74, 76, 79, 81, 84];                  // A C D E G A C
        let chordIdx = 0, nextChordAt = 0, nextBellAt = 0, lastBell = 3;

        function pad(notes, t0, dur) {
          for (const n of notes) {
            for (const det of [-4, 4]) {                               // two detuned voices
              const o = ac.createOscillator(), g = ac.createGain(), f = ac.createBiquadFilter();
              o.type = "triangle"; o.frequency.value = hz(n); o.detune.value = det;
              f.type = "lowpass"; f.frequency.value = 900;
              g.gain.setValueAtTime(0.0001, t0);
              g.gain.linearRampToValueAtTime(0.014, t0 + 2.5);          // slow swell
              g.gain.setValueAtTime(0.014, t0 + dur - 2);
              g.gain.linearRampToValueAtTime(0.0001, t0 + dur + 1.5);   // long release
              o.connect(f); f.connect(g); g.connect(music);
              o.start(t0); o.stop(t0 + dur + 1.6);
            }
          }
        }
        function bell(n, t0) {
          const o = ac.createOscillator(), o2 = ac.createOscillator(), g = ac.createGain();
          o.type = "sine"; o.frequency.value = hz(n);
          o2.type = "sine"; o2.frequency.value = hz(n) * 2.01;         // soft shimmer partial
          const g2 = ac.createGain(); g2.gain.value = 0.25;
          g.gain.setValueAtTime(0.0001, t0);
          g.gain.exponentialRampToValueAtTime(0.03, t0 + 0.02);
          g.gain.exponentialRampToValueAtTime(0.0001, t0 + 2.8);
          o.connect(g); o2.connect(g2); g2.connect(g);
          g.connect(music); g.connect(echo);
          o.start(t0); o2.start(t0); o.stop(t0 + 3); o2.stop(t0 + 3);
        }
        function schedule() {
          if (!ac || ac.state !== "running" || !music || !musicOn || !globeVisible() || songPlaying()) {
            nextChordAt = 0; nextBellAt = 0;
            return;
          }
          const now = ac.currentTime, ahead = now + 0.6;
          if (!nextChordAt) { nextChordAt = now + 0.1; nextBellAt = now + 2; }
          while (nextChordAt < ahead) {
            pad(CHORDS[chordIdx], nextChordAt, 8);
            chordIdx = (chordIdx + 1) % CHORDS.length;
            nextChordAt += 8;
          }
          while (nextBellAt < ahead) {
            // Mostly small steps up or down the scale: calm, melodic.
            lastBell = Math.max(0, Math.min(BELLS.length - 1, lastBell + [-2, -1, -1, 1, 1, 2][Math.floor(Math.random() * 6)]));
            bell(BELLS[lastBell], nextBellAt);
            nextBellAt += 1.4 + Math.random() * 1.8;
          }
        }

        function update() {
          if (!ac || !hum) return;
          const st = aiVisual.state, visible = globeVisible();
          hum.gain.setTargetAtTime(on && visible ? (st === "speaking" ? 0.006 : 0.018) : 0, ac.currentTime, 0.6);
          // Music: silent while listening (so the mic doesn't hear it) or when a
          // song plays; softer while Sara speaks.
          const m = musicOn && visible && !songPlaying() && st !== "listening" ? (st === "speaking" ? 0.35 : 1) : 0;
          music.gain.setTargetAtTime(m, ac.currentTime, m ? 1.2 : 0.25);
        }
        function ping() {
          if (!on || !ac || ac.state !== "running" || !globeVisible()) return;
          const t0 = ac.currentTime;
          // Two quick tones with an echo: a classic sonar "ping".
          [[1320, 0, 0.05], [1760, 0.09, 0.03], [1320, 0.32, 0.015]].forEach(([f, dt, vol]) => {
            const o = ac.createOscillator(), g = ac.createGain();
            o.type = "sine"; o.frequency.setValueAtTime(f, t0 + dt);
            o.frequency.exponentialRampToValueAtTime(f * 0.92, t0 + dt + 0.5);
            g.gain.setValueAtTime(0.0001, t0 + dt);
            g.gain.exponentialRampToValueAtTime(vol, t0 + dt + 0.01);
            g.gain.exponentialRampToValueAtTime(0.0001, t0 + dt + 0.7);
            o.connect(g); g.connect(ac.destination);
            o.start(t0 + dt); o.stop(t0 + dt + 0.75);
          });
        }
        function wake() {
          if (!on && !musicOn) return;
          ensure();
          if (ac && ac.state !== "running") ac.resume().catch(() => {});
          update();
        }
        ["pointerdown", "keydown"].forEach(ev => document.addEventListener(ev, wake, { passive: true }));
        if (btn) btn.addEventListener("click", (e) => {
          e.stopPropagation();
          on = !on;
          try { localStorage.setItem(KEY, on ? "on" : "off"); } catch (err) {}
          paint();
          if (on) wake(); else update();
        });
        if (musicBtn) musicBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          musicOn = !musicOn;
          try { localStorage.setItem(MUSIC_KEY, musicOn ? "on" : "off"); } catch (err) {}
          paint();
          if (musicOn) wake(); else update();
        });
        setInterval(update, 500);     // follow camera on/off, tab hidden, listening / speaking
        setInterval(schedule, 200);   // music look-ahead scheduler
        paint();
        return { ping };
      })();

      // --- Info card beside the face circle -----------------------------------
      // Built only from what the user types (link / name / question).
      const pcard = (() => {
        const wrap = document.querySelector(".video-wrap");
        const form = document.getElementById("pcardForm");
        const input = document.getElementById("pcardInput");
        const box = document.getElementById("pcard");
        if (!form || !box) return null;
        const SAVED = "saraCard:";

        function el(tag, cls, text) {
          const n = document.createElement(tag);
          if (cls) n.className = cls;
          if (text != null) n.textContent = text;
          return n;
        }
        function initials(name) {
          return (name || "?").split(/ +/).filter(Boolean).slice(0, 2).map(w => w[0].toUpperCase()).join("") || "?";
        }
        function saved(url) {
          try { return JSON.parse(localStorage.getItem(SAVED + url) || "null"); } catch (e) { return null; }
        }
        function save(url, data) {
          try { localStorage.setItem(SAVED + url, JSON.stringify(data)); } catch (e) {}
        }

        function open() { box.hidden = false; wrap.classList.add("has-card"); }
        function close() { box.hidden = true; box.replaceChildren(); wrap.classList.remove("has-card"); }

        function loading(q) {
          open();
          box.replaceChildren();
          const head = el("div", "pc-head");
          head.append(el("span", "pc-chip", "Looking up"), el("span", "pc-src", q.slice(0, 40)));
          box.append(head);
          for (const w of [70, 92, 55, 80]) { const s = el("div", "pc-skel"); s.style.width = w + "%"; box.append(s); }
        }

        function render(c) {
          // Details the user added for a blocked profile (e.g. LinkedIn).
          const mine = c.url ? saved(c.url) : null;
          if (mine) {
            c = { ...c, name: mine.name || c.name, headline: mine.headline || c.headline,
                  summary: mine.about || c.summary, notice: "",
                  facts: [["Company", mine.company], ["Location", mine.location]].filter(f => f[1]),
                  source: "Added by you" };
          }
          box.replaceChildren();
          const head = el("div", "pc-head");
          const chipText = c.kind === "answer" ? "AI Answer" : c.kind === "name" ? "Public info" : (c.platform || "Profile");
          head.append(el("span", "pc-chip " + (c.kind === "answer" ? "answer" : (c.platform || "")), chipText),
                      el("span", "pc-src", c.source || ""));
          const x = el("button", "pc-x", "×"); x.type = "button"; x.title = "Close"; x.onclick = close;
          head.append(x);
          box.append(head);

          const id = el("div", "pc-id");
          const shownName = c.name || (c.handle ? "@" + c.handle : "Profile");
          let av;
          if (c.image) {
            av = el("img", "pc-avatar"); av.src = c.image; av.alt = ""; av.referrerPolicy = "no-referrer";
            av.onerror = () => av.replaceWith(Object.assign(el("div", "pc-avatar", initials(shownName))));
          } else {
            av = el("div", "pc-avatar", c.kind === "answer" ? "AI" : initials(c.name || c.handle));
          }
          const who = el("div");
          who.append(el("div", "pc-name", c.kind === "answer" ? "Answer" : shownName));
          if (c.kind === "answer") who.append(el("div", "pc-headline", c.name));
          else if (c.headline) who.append(el("div", "pc-headline", c.headline));
          if (c.handle && c.name) who.append(el("div", "pc-handle", "@" + c.handle));
          id.append(av, who);
          box.append(id);

          if (c.summary) box.append(el("div", "pc-summary", c.summary));
          if (c.facts && c.facts.length) {
            const dl = el("dl", "pc-facts");
            for (const [k, v] of c.facts) dl.append(el("dt", null, k), el("dd", null, String(v)));
            box.append(dl);
          }
          if (c.notice) box.append(el("div", "pc-notice", c.notice));

          const actions = el("div", "pc-actions");
          if (c.url) {
            const a = el("a", "primary", c.kind === "profile" ? "Open profile ↗" : "Read more ↗");
            a.href = c.url; a.target = "_blank"; a.rel = "noopener";
            actions.append(a);
          }
          if (c.google) {
            const g = el("a", c.url ? "" : "primary", "Search on Google ↗");
            g.href = c.google; g.target = "_blank"; g.rel = "noopener";
            actions.append(g);
          }
          if (c.kind === "profile" && c.url) {
            const e = el("button", "", mine ? "Edit details" : "Add details"); e.type = "button";
            e.onclick = () => editForm(c, mine || {});
            actions.append(e);
          }
          box.append(actions);
          box.append(el("div", "pc-foot", "Shown from the text you entered — the face is not used for lookup."));
          if (c.blocked && !mine) editForm(c, {});
        }

        function editForm(c, cur) {
          box.querySelector(".pc-form")?.remove();
          const f = el("form", "pc-form");
          const fields = [["name", "Full name"], ["headline", "Headline, e.g. Python Developer"],
                          ["company", "Company"], ["location", "Location"]];
          for (const [k, ph] of fields) {
            const i = el("input"); i.name = k; i.placeholder = ph; i.value = cur[k] || ""; f.append(i);
          }
          const about = el("textarea"); about.name = "about"; about.placeholder = "About (optional)"; about.value = cur.about || "";
          f.append(about);
          const row = el("div", "pc-actions"); row.style.padding = "0";
          const ok = el("button", "primary", "Save to card"); ok.type = "submit";
          row.append(ok); f.append(row);
          f.onsubmit = (ev) => {
            ev.preventDefault();
            const data = Object.fromEntries(new FormData(f).entries());
            save(c.url, data);
            render(c);
          };
          box.querySelector(".pc-actions").after(f);
          f.querySelector("input").focus();
        }

        async function lookup(q) {
          q = (q || "").trim();
          if (!q) return;
          loading(q);
          try {
            const lang = isHindiUI() ? "hi" : "en";
            const r = await fetch("/profile/lookup?q=" + encodeURIComponent(q) + "&lang=" + lang);
            const d = await r.json();
            if (!d.ok) throw new Error(d.error || "lookup failed");
            render(d.card);
          } catch (e) {
            render({ kind: "name", name: q, facts: [], notice: "Couldn't fetch details right now. Try Search on Google.",
                     google: "https://www.google.com/search?q=" + encodeURIComponent(q) });
          }
        }

        // Search box: a round 🔍 button that opens on click and folds back
        // after 5 seconds without typing (any card stays open).
        const toggle = document.getElementById("pcardToggle");
        let foldTimer = null;
        function armFold() {
          clearTimeout(foldTimer);
          foldTimer = setTimeout(fold, 5000);
        }
        function unfold() {
          form.classList.remove("collapsed");
          toggle.setAttribute("aria-expanded", "true");
          toggle.setAttribute("aria-label", "Close search");
          setTimeout(() => input.focus(), 50);
          armFold();
        }
        function fold() {
          clearTimeout(foldTimer);
          form.classList.add("collapsed");
          toggle.setAttribute("aria-expanded", "false");
          toggle.setAttribute("aria-label", "Open search");
          input.blur();
        }
        toggle.addEventListener("click", () => (form.classList.contains("collapsed") ? unfold() : fold()));
        input.addEventListener("input", armFold);
        input.addEventListener("keydown", (e) => {
          if (e.key === "Escape") { e.stopPropagation(); fold(); } else armFold();
        });

        form.addEventListener("submit", (e) => { e.preventDefault(); lookup(input.value); armFold(); });
        document.addEventListener("keydown", (e) => {
          // Esc closes the card (the listening overlay handles its own Esc).
          if (e.key === "Escape" && !box.hidden && !voiceOverlay.classList.contains("active")) close();
        });
        return { lookup, close };
      })();

      startFlow();
      maybeOnboard();
    </script>
  </body>
</html>
"""

    @app.get("/favicon.ico")
    def favicon():
        # Prevent 404 logs in system monitor for favicon requests
        from fastapi import Response
        return Response(content=b"", media_type="image/x-icon")

    @app.get("/logs/backend")
    def logs_backend(after: int = 0):
        return {"logs": backend_log.since(after)}

    @app.get("/events")
    def events(after_id: int = 0):
        evs = list_events(after_id)
        return {
            "events": [{"id": e.id, "ts": e.ts, "kind": e.kind, "message": e.message} for e in evs],
        }

    @app.get("/log")
    def add_log(message: str):
        ev = emit("custom", message)
        return {"ok": True, "message": ev.message, "id": ev.id}

    @app.get("/tts")
    async def tts(text: str, lang: str = "en", gender: str = "female"):
        """Render text to a neural-voice MP3 (Edge TTS).

        Returns 503 if neural TTS is unavailable (not installed or no network)
        so the browser can fall back to its built-in speech synthesis.
        """
        from fastapi import Response

        from .neural_tts import speech_settings, synthesize

        if not text.strip():
            return Response(status_code=204)
        key = (text, lang, gender, speech_settings())   # new speed/volume -> fresh audio
        cached = _tts_cache.get(key)
        if cached is not None:
            return Response(content=cached, media_type="audio/mpeg")
        try:
            audio = await synthesize(text, lang=lang, gender=gender)
        except Exception:  # noqa: BLE001
            return Response(status_code=503)
        if len(_tts_cache) < _TTS_CACHE_MAX:
            _tts_cache[key] = audio
        return Response(content=audio, media_type="audio/mpeg")

    @app.post("/run")
    def run(
        text: str,
        async_run: bool = False,
        delay_seconds: float = 0.0,
        camera_seconds: float = 10.0,
        countdown: bool = True,
        in_page: bool = False,
        reply_lang: str = "",
    ):
        t = _normalize_text(text)

        def _maybe_countdown(delay: float) -> None:
            if delay <= 0:
                return
            steps = int(round(delay))
            current = 1
            while current <= steps:
                if t in {"open camera", "open cam", "start camera", "start cam"}:
                    emit("countdown", f"Signal in {current}s")
                sleep(1.0)
                current += 1

        if async_run:
            def _bg() -> None:
                set_muted(True)  # browser speaks; keep the server quiet
                emit("api", f"Job_Start: {text}")
                _maybe_countdown(delay_seconds)
                dispatch_command(text, camera_seconds=camera_seconds)

            threading.Thread(target=_bg, daemon=True).start()
            return {"ok": True, "message": "Job sent to background.", "text": text, "async": True, "kind": "queued"}

        _maybe_countdown(delay_seconds)
        set_muted(True)  # browser handles voice output for web requests
        try:
            r = dispatch_command(text, camera_seconds=camera_seconds, web_ui=in_page, reply_lang=reply_lang)
        finally:
            set_muted(False)
        return {"ok": r.ok, "message": r.message, "text": text, "async": False, "kind": r.kind, "data": r.data}

    # Face snapshots from the camera: saved as JPEG in captures/<YYYY-MM-DD>/
    # (on this laptop only) and listed in the Live detections log.
    captures_dir = Path(__file__).resolve().parents[2] / "captures"

    @app.post("/captures/save")
    async def captures_save(request: Request):
        import base64
        import re as _re
        from datetime import datetime as _dt

        body = await request.json()
        data_url = str(body.get("image") or "")
        if not data_url.startswith("data:image/jpeg;base64,"):
            return {"ok": False, "error": "expected a JPEG data URL"}
        raw = base64.b64decode(data_url.split(",", 1)[1])
        if len(raw) > 2_000_000:
            return {"ok": False, "error": "image too large"}
        label = _re.sub(r"[^A-Za-z0-9_]", "", str(body.get("label") or "FACE"))[:20] or "FACE"
        now = _dt.now()
        day = now.strftime("%Y-%m-%d")
        folder = captures_dir / day
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{now.strftime('%H-%M-%S')}_{label}.jpg"
        (folder / name).write_bytes(raw)
        emit("face", f"Face snapshot saved: captures/{day}/{name}")
        return {"ok": True, "path": f"captures/{day}/{name}", "url": f"/captures/file/{day}/{name}"}

    @app.get("/captures/file/{day}/{name}")
    def captures_file(day: str, name: str):
        import re as _re

        from fastapi.responses import FileResponse

        if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or not _re.fullmatch(r"[\w-]+\.jpg", name):
            return Response(status_code=404)
        p = captures_dir / day / name
        return FileResponse(p, media_type="image/jpeg") if p.is_file() else Response(status_code=404)

    # FM / internet radio (Radio Browser directory).
    @app.get("/radio/stations")
    def radio_stations(q: str = "", limit: int = 30):
        from . import radio

        try:
            return {"ok": True, "query": q, "stations": radio.stations(q, max(5, min(limit, 60))), "quick": radio.QUICK}
        except OSError as e:
            return {"ok": False, "error": str(e), "stations": [], "quick": radio.QUICK}

    # Playlists for the header buttons (e.g. 📖 Quran).
    @app.get("/media/list")
    def media_list(category: str = "quran"):
        from . import media

        items = media.CATEGORIES.get(category, [])
        return {"ok": bool(items), "category": category, "songs": [media.payload(s) for s in items]}

    # Skills window: everything Sara can do, with example commands.
    @app.get("/skills")
    def skills_catalog():
        from . import skills

        return skills.catalog()

    # Quran window: all 114 surahs + audio reciters (mp3quran.net).
    @app.get("/quran/catalog")
    def quran_catalog():
        from . import quran

        return quran.catalog()

    # --- Phone remote: the phone's browser sends voice / text commands ------
    # Reach it over USB (`adb reverse tcp:1309 tcp:1309`, phone opens
    # http://localhost:1309/remote) or on the same Wi-Fi / phone hotspot
    # (`serve --lan`, phone opens http://<laptop-ip>:1309/remote).
    phone_state = {"last_seen": 0.0, "device": ""}

    @app.get("/remote", response_class=HTMLResponse)
    def remote_page():
        return HTMLResponse(REMOTE_HTML)

    @app.post("/remote/ping")
    def remote_ping(device: str = ""):
        first = time.time() - phone_state["last_seen"] > 15
        phone_state.update(last_seen=time.time(), device=device[:60])
        if first:
            emit("phone", json.dumps({"event": "connected", "device": phone_state["device"]}))
        return {"ok": True}

    @app.get("/remote/status")
    def remote_status(request: Request):
        return {
            "connected": time.time() - phone_state["last_seen"] < 12,
            "device": phone_state["device"],
            "lan": os.environ.get("SARA_LAN") == "1",
            "lan_ip": _lan_ip(),
            "port": request.url.port or 1309,
        }

    @app.post("/remote/say")
    def remote_say(text: str, reply_lang: str = ""):
        """Run a phone command on the laptop; the dashboard acts on it and
        speaks the reply (via the "phone" event)."""
        text = (text or "").strip()[:500]
        if not text:
            return {"ok": False, "message": "Empty command."}
        phone_state["last_seen"] = time.time()
        set_muted(True)
        try:
            r = dispatch_command(text, web_ui=True, reply_lang=reply_lang)
        finally:
            set_muted(False)
        emit("phone", json.dumps({"event": "command", "text": text, "ok": r.ok,
                                  "message": r.message, "kind": r.kind, "data": r.data}, ensure_ascii=False))
        return {"ok": r.ok, "message": r.message, "kind": r.kind}

    return app


# Phone remote page (/remote): speak or type on the phone, Sara acts on the laptop.
REMOTE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b1220">
<title>Sara · Phone Remote</title>
<style>
  :root { --bg: #0b1220; --card: rgba(15, 23, 42, 0.85); --line: rgba(148, 163, 184, 0.16);
          --text: #e2e8f0; --muted: #94a3b8; --cyan: #22d3ee; --green: #34d399; --red: #f87171; }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--bg); color: var(--text);
    font-family: "Segoe UI", system-ui, -apple-system, Roboto, sans-serif; }
  body { min-height: 100dvh; display: flex; flex-direction: column;
    background: radial-gradient(600px 400px at 50% -10%, rgba(34, 211, 238, 0.12), transparent 70%), var(--bg); }
  header { display: flex; align-items: center; gap: 10px; padding: 14px 16px; border-bottom: 1px solid var(--line); }
  .logo { width: 34px; height: 34px; border-radius: 10px; display: grid; place-items: center; font-weight: 800;
    background: linear-gradient(135deg, #34d399, #10b981); color: #04121a; }
  .title b { display: block; font-size: 15px; letter-spacing: 1px; }
  .title span { font-size: 11px; color: var(--muted); }
  .pill { margin-left: auto; display: flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 999px;
    border: 1px solid var(--line); font-size: 11px; color: var(--muted); }
  .pill i { width: 7px; height: 7px; border-radius: 50%; background: #64748b; }
  .pill.on { color: #a7f3d0; border-color: rgba(52, 211, 153, 0.4); }
  .pill.on i { background: var(--green); box-shadow: 0 0 8px var(--green); }
  main { flex: 1; display: flex; flex-direction: column; padding: 16px; gap: 14px; max-width: 560px; width: 100%; margin: 0 auto; }
  .lang { display: flex; gap: 6px; justify-content: center; }
  .lang button { padding: 6px 14px; border-radius: 999px; border: 1px solid var(--line); background: transparent;
    color: var(--muted); font: inherit; font-size: 12px; }
  .lang button.on { background: var(--cyan); color: #04121a; border-color: var(--cyan); font-weight: 700; }
  .micwrap { display: flex; flex-direction: column; align-items: center; gap: 10px; padding: 10px 0 4px; }
  #mic { width: 132px; height: 132px; border-radius: 50%; border: 0; cursor: pointer;
    background: radial-gradient(circle at 35% 30%, #67e8f9, #0ea5e9 60%, #0369a1);
    box-shadow: 0 12px 40px rgba(14, 165, 233, 0.45); display: grid; place-items: center; color: #04121a; }
  #mic svg { width: 54px; height: 54px; }
  #mic.on { background: radial-gradient(circle at 35% 30%, #fca5a5, #ef4444 60%, #991b1b); color: #fff;
    animation: pulse 1.3s infinite; }
  @keyframes pulse { 0% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.6); } 70% { box-shadow: 0 0 0 22px rgba(239, 68, 68, 0); } 100% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0); } }
  #hint { font-size: 13px; color: var(--muted); text-align: center; min-height: 18px; }
  #live { font-size: 15px; color: #7dd3fc; text-align: center; min-height: 20px; font-style: italic; }
  .tip { font-size: 12px; line-height: 1.5; color: #fde68a; background: rgba(251, 191, 36, 0.08);
    border: 1px solid rgba(251, 191, 36, 0.3); border-radius: 10px; padding: 10px 12px; }
  .row { display: flex; gap: 8px; }
  #text { flex: 1; min-width: 0; height: 48px; padding: 0 14px; border-radius: 12px; border: 1px solid var(--line);
    background: rgba(0, 0, 0, 0.3); color: var(--text); font: inherit; font-size: 15px; outline: none; }
  #text:focus { border-color: var(--cyan); }
  #send { height: 48px; padding: 0 18px; border-radius: 12px; border: 0; font: inherit; font-weight: 700;
    background: linear-gradient(135deg, #34d399, #10b981); color: #04121a; }
  .chips { display: flex; flex-wrap: wrap; gap: 8px; }
  .chips button { padding: 8px 12px; border-radius: 999px; border: 1px solid var(--line); background: var(--card);
    color: var(--text); font: inherit; font-size: 12.5px; }
  .label { font-size: 10px; font-weight: 700; letter-spacing: 1.6px; color: var(--muted); text-transform: uppercase; }
  #log { display: flex; flex-direction: column; gap: 8px; }
  .msg { padding: 10px 12px; border-radius: 12px; font-size: 14px; line-height: 1.45; max-width: 88%; word-wrap: break-word; }
  .me { align-self: flex-end; background: rgba(34, 211, 238, 0.14); border: 1px solid rgba(34, 211, 238, 0.35); }
  .sara { align-self: flex-start; background: var(--card); border: 1px solid var(--line); }
  .sara.err { border-color: rgba(248, 113, 113, 0.45); }
  .msg small { display: block; font-size: 10px; color: var(--muted); margin-top: 4px; }
</style>
</head>
<body>
<header>
  <div class="logo">S</div>
  <div class="title"><b>SARA</b><span>Phone remote</span></div>
  <div class="pill" id="pill"><i></i><span id="pillText">Connecting...</span></div>
</header>
<main>
  <div class="lang" role="group" aria-label="Language">
    <button type="button" data-lang="hi-IN" class="on">हिंदी / Hinglish</button>
    <button type="button" data-lang="en-IN">English</button>
  </div>

  <div class="micwrap">
    <button id="mic" type="button" aria-label="Tap and speak">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0M12 17v4M8 21h8"/></svg>
    </button>
    <div id="hint">Tap the mic and speak · Mic dabaiye aur boliye</div>
    <div id="live"></div>
  </div>
  <div class="tip" id="tip" hidden>
    Is connection par phone ka direct mic browser allow nahi karta. Neeche box mein
    <b>keyboard ka 🎤 (voice typing)</b> dabaiye aur boliye, phir <b>Send</b>.
    Ya USB cable se jodiye (laptop par 📱 Phone window dekhiye), tab bada mic button chalega.
  </div>

  <div class="row">
    <input id="text" type="text" placeholder="Type or speak a command..." autocomplete="off" enterkeyhint="send">
    <button id="send" type="button">Send</button>
  </div>

  <div class="label">Quick commands</div>
  <div class="chips">
    <button type="button" data-cmd="notebook kholo">📒 Notebook kholo</button>
    <button type="button" data-fill="notebook me likho: ">✍️ Notebook me likho…</button>
    <button type="button" data-cmd="notes whatsapp pe bhejo">📤 Notes WhatsApp pe bhejo</button>
    <button type="button" data-cmd="quran sunao">📖 Quran</button>
    <button type="button" data-cmd="calculator kholo">🧮 Calculator</button>
    <button type="button" data-cmd="aaj ka mausam kaisa hai">🌤️ Mausam</button>
    <button type="button" data-cmd="news dikhao">📰 News</button>
  </div>

  <div class="label">Conversation</div>
  <div id="log"></div>
</main>
<script>
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const mic = document.getElementById("mic"), live = document.getElementById("live");
  const hint = document.getElementById("hint"), input = document.getElementById("text");
  const log = document.getElementById("log");
  let lang = localStorage.getItem("remoteLang") || "hi-IN", rec = null, listening = false;

  document.querySelectorAll(".lang button").forEach(b => {
    b.classList.toggle("on", b.dataset.lang === lang);
    b.onclick = () => {
      lang = b.dataset.lang; localStorage.setItem("remoteLang", lang);
      document.querySelectorAll(".lang button").forEach(x => x.classList.toggle("on", x === b));
    };
  });

  function add(cls, text, note) {
    const d = document.createElement("div");
    d.className = "msg " + cls;
    d.textContent = text;
    if (note) { const s = document.createElement("small"); s.textContent = note; d.append(s); }
    log.prepend(d);
  }

  async function say(text) {
    text = (text || "").trim();
    if (!text) return;
    add("me", text);
    input.value = "";
    try {
      const r = await (await fetch("/remote/say?text=" + encodeURIComponent(text) + "&reply_lang=" + (lang.startsWith("hi") ? "hi" : "en"), { method: "POST" })).json();
      add("sara" + (r.ok ? "" : " err"), r.message || "Done.", "Sara (laptop par bhi bol rahi hai)");
    } catch (e) {
      add("sara err", "Laptop se connection nahi hua. Sara chal rahi hai?");
    }
  }

  // Big mic: the phone's own speech recognition (needs a secure page:
  // http://localhost via USB works; plain http over Wi-Fi doesn't).
  const micOk = !!SR && window.isSecureContext;
  if (!micOk) {
    document.getElementById("tip").hidden = false;
    hint.textContent = "Keyboard ka 🎤 use karein (neeche box mein)";
    mic.style.opacity = 0.45;
  }
  mic.onclick = () => {
    if (!micOk) { input.focus(); return; }
    if (listening) { rec && rec.stop(); return; }
    rec = new SR();
    rec.lang = lang; rec.interimResults = true; rec.continuous = false;
    rec.onstart = () => { listening = true; mic.classList.add("on"); hint.textContent = "Sun rahi hoon... boliye"; live.textContent = ""; };
    rec.onresult = (e) => {
      let txt = "", final = false;
      for (const r of e.results) { txt += r[0].transcript; final = final || r.isFinal; }
      live.textContent = txt;
      if (final) { live.textContent = ""; say(txt); }
    };
    rec.onerror = (e) => { hint.textContent = e.error === "not-allowed" ? "Mic permission dijiye (browser settings)" : "Phir se try karein (" + e.error + ")"; };
    rec.onend = () => { listening = false; mic.classList.remove("on"); if (hint.textContent.startsWith("Sun")) hint.textContent = "Tap the mic and speak · Mic dabaiye aur boliye"; };
    rec.start();
  };

  document.getElementById("send").onclick = () => say(input.value);
  input.addEventListener("keydown", e => { if (e.key === "Enter") say(input.value); });
  document.querySelectorAll(".chips button").forEach(b => b.onclick = () => {
    if (b.dataset.cmd) say(b.dataset.cmd);
    else { input.value = b.dataset.fill; input.focus(); }
  });

  // Heartbeat: the laptop shows "Phone connected".
  const pill = document.getElementById("pill"), pillText = document.getElementById("pillText");
  const device = (navigator.userAgent.match(/\(([^;)]+);\s*([^;)]+)/) || [])[2] || "Phone";
  async function ping() {
    try {
      await fetch("/remote/ping?device=" + encodeURIComponent(device), { method: "POST" });
      pill.classList.add("on"); pillText.textContent = "Laptop connected";
    } catch (e) { pill.classList.remove("on"); pillText.textContent = "Not connected"; }
  }
  ping(); setInterval(ping, 4000);
</script>
</body>
</html>
"""


app = create_app()


# Redirect root (and the old /ui path) to the Sara dashboard for convenience.
@app.get("/", response_class=RedirectResponse)
def root_redirect():
    return RedirectResponse(url="/sara")


@app.get("/ui", response_class=RedirectResponse)
def ui_redirect():
    return RedirectResponse(url="/sara")

