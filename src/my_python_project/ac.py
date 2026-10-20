"""Blue Star Smart AC control through the Blue Star cloud (same as the phone app).

How it talks to the AC (reverse-engineered from the Blue Star Smart Android app;
see https://github.com/ArjunVRa/bluestar_smartac, MIT):

* ``POST /auth/login`` on the Blue Star API with the app login (phone or email +
  password) returns a session token plus base64 ``mi`` = ``endpoint::key::secret``,
  i.e. AWS IoT credentials for the account.
* ``GET /things`` lists the ACs with their last reported state.
* A command is a SigV4-signed ``POST /things/<id>/shadow`` to AWS IoT
  (``{"state": {"desired": {...}}}``); the AC picks it up within a second or two.

Only the standard library is used. Set ``BLUESTAR_USER`` (the phone number or
email you log in to the Blue Star Smart app with) and ``BLUESTAR_PASSWORD`` in
``.env``. ``BLUESTAR_AC`` optionally picks which AC voice commands control (part
of its name) when the account has more than one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

API_BASE = "https://api-smart.bluestarindia.com"
_REGION = "ap-south-1"
_SERVICE = "iotdata"  # AWS signing name of the IoT Data Plane
_TIMEOUT = 15

MIN_TEMP, MAX_TEMP = 16, 30

# Blue Star codes <-> names (from the app).
MODES = {"fan": 0, "cool": 2, "dry": 3, "auto": 4}
FANS = {"low": 2, "medium": 3, "high": 4, "turbo": 6, "auto": 7}
# Vertical louver: 6 = off (fixed), 0 = continuous swing, 1-5 = fixed angle.
VSWING = {"off": 6, "auto": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5}
# Eco capacity (5-in-1 / 6-in-1 convertible models).
ECO = {"off": 0, "110": 5, "100": 1, "80": 2, "60": 3, "40": 4}
# Simple on/off features: name -> (state key, value when on, value when off).
FEATURES = {
    "turbo": ("turbo", 3, 0),
    "sleep": ("sleep", 1, 0),
    "display": ("display", 1, 0),
    "buzzer": ("m_buz", 1, 0),
    "hswing": ("hswing", 0, 6),
    "clean": ("s_clean", 1, 0),
}
ERROR_CODES = {
    1: "L1", 2: "L2", 3: "DH", 4: "DL", 5: "HE", 6: "E5", 7: "HP", 8: "PC", 9: "P3",
    10: "IP", 11: "LO", 12: "P8", 13: "L3", 14: "F5", 15: "F4", 16: "F3", 17: "E4",
    18: "E1", 19: "EC", 20: "H6", 21: "E6", 22: "F1", 23: "F2", 24: "E2", 25: "EE",
    26: "E0", 27: "LC", 28: "P7", 29: "P5", 30: "PH", 31: "P4", 32: "E8", 33: "L4",
    48: "U7", 49: "U8", 50: "E3", 51: "E7",
}

_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "com.bluestarindia.bluesmart",
    "X-APP-VER": "v5.1.2-183",
    "X-OS-NAME": "Android",
    "X-OS-VER": "14-34",
}


class ACError(RuntimeError):
    """A user-facing problem (not configured, login failed, AC offline...)."""


def _inv(d: dict) -> dict:
    return {v: k for k, v in d.items()}


def sigv4_headers(
    method: str, host: str, path: str, body: bytes, access_key: str, secret_key: str,
    *, region: str = _REGION, service: str = _SERVICE, now: datetime | None = None,
) -> dict[str, str]:
    """AWS Signature Version 4 headers for a request with no query string."""
    now = now or datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()
    # Non-S3 services sign the path URI-encoded once more.
    canonical_uri = urllib.parse.quote(path, safe="/-_.~")
    canonical = "\n".join([
        method, canonical_uri, "",
        f"host:{host}\nx-amz-date:{amz_date}\n",
        "host;x-amz-date", payload_hash,
    ])
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest(),
    ])

    def _h(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k = _h(("AWS4" + secret_key).encode(), day)
    for part in (region, service, "aws4_request"):
        k = _h(k, part)
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "x-amz-date": amz_date,
        "Authorization": (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                          f"SignedHeaders=host;x-amz-date, Signature={sig}"),
    }


def parse_state(thing: dict, st: dict) -> dict:
    """Turn a Blue Star thing + its raw reported state into the UI/voice shape."""
    rep = st.get("reported") or st.get("state") or {}
    eco = rep.get("eco", 0)
    eco = eco.get("value", 0) if isinstance(eco, dict) else eco

    def _num(key, default=None):
        try:
            return float(rep[key])
        except (KeyError, TypeError, ValueError):
            return default

    err = rep.get("err", 0) or 0
    return {
        "id": thing.get("thing_id"),
        "version": thing.get("version"),
        "model": thing.get("model") or thing.get("model_no") or thing.get("product"),
        "mac": thing.get("mac") or thing.get("mac_address"),
        "name": thing.get("name") or f"AC-{str(thing.get('thing_id', ''))[-4:]}",
        "online": bool(st.get("connected", True)),
        "power": rep.get("pow", 0) == 1,
        "mode": _inv(MODES).get(rep.get("mode"), "cool"),
        "target": _num("stemp", 24.0),
        "room": _num("ctemp"),
        "fan": _inv(FANS).get(rep.get("fspd"), "auto"),
        "vswing": _inv(VSWING).get(rep.get("vswing"), "off"),
        "eco": _inv(ECO).get(eco, "off"),
        **{name: rep.get(key, off) == on for name, (key, on, off) in FEATURES.items()},
        "rssi": rep.get("rssi"),
        "run_hours": rep.get("c_runhour"),
        "error": ERROR_CODES.get(err, str(err)) if err else None,
    }


class BlueStarClient:
    def __init__(self, user: str, password: str) -> None:
        self.user = user.strip()
        self.password = password
        self._session: str | None = None
        self._aws: tuple[str, str, str] | None = None  # endpoint, key, secret
        self._lock = threading.Lock()

    # --- Blue Star API ---
    def _api(self, method: str, path: str, payload: dict | None = None) -> dict:
        headers = dict(_HEADERS)
        if self._session:
            headers["Authorization"] = f"Bearer {self._session}"
            headers["X-APP-SESSION"] = self._session
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(API_BASE + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8") or "{}")

    def login(self) -> None:
        payload = {
            "auth_id": self.user,
            "auth_type": 0 if "@" in self.user else 1,  # 0 = email, 1 = phone
            "password": self.password,
        }
        self._session = None
        try:
            data = self._api("POST", "/auth/login", payload)
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 404):
                raise ACError("Blue Star login failed: check BLUESTAR_USER / BLUESTAR_PASSWORD.") from e
            raise
        self._session = data.get("session")
        mi = data.get("mi") or (data.get("user") or {}).get("mi") or ""
        parts = base64.b64decode(mi).decode("utf-8").split("::") if mi else []
        if not self._session or len(parts) < 3:
            raise ACError("Blue Star login worked but returned no control credentials.")
        self._aws = (parts[0], parts[1], parts[2])

    def _ensure_login(self) -> None:
        if not self._session or not self._aws:
            self.login()

    def devices(self) -> list[dict]:
        """All ACs on the account with their current state."""
        with self._lock:
            self._ensure_login()
            try:
                data = self._api("GET", "/things")
            except urllib.error.HTTPError as e:
                if e.code not in (401, 403):
                    raise
                self.login()
                data = self._api("GET", "/things")
        states = data.get("states") or {}
        return [parse_state(t, states.get(t.get("thing_id"), {}))
                for t in data.get("things") or [] if t.get("thing_id")]

    # --- AWS IoT shadow ---
    def send(self, thing_id: str, updates: dict) -> None:
        desired = {**updates, "src": "anmq", "ts": int(time.time() * 1000)}
        body = json.dumps({"state": {"desired": desired}}).encode()
        with self._lock:
            self._ensure_login()
            for attempt in (1, 2):
                endpoint, key, secret = self._aws  # type: ignore[misc]
                path = f"/things/{urllib.parse.quote(thing_id, safe='')}/shadow"
                headers = {"Content-Type": "application/json",
                           **sigv4_headers("POST", endpoint, path, body, key, secret)}
                req = urllib.request.Request(f"https://{endpoint}{path}", data=body,
                                             headers=headers, method="POST")
                try:
                    with urllib.request.urlopen(req, timeout=_TIMEOUT):
                        return
                except urllib.error.HTTPError as e:
                    if e.code in (401, 403) and attempt == 1:
                        self.login()  # credentials rotated: log in again once
                        continue
                    raise ACError(f"The AC cloud rejected the command (HTTP {e.code}).") from e


# --- module-level API used by the server and voice commands -----------------

_client: BlueStarClient | None = None
_cache: tuple[float, list[dict]] | None = None
_CACHE_TTL = 2.5
# What the last command changed, per AC: shown until the cloud reports it.
_pending: dict[str, tuple[float, dict]] = {}
_PENDING_SECS = 8.0
_mod_lock = threading.Lock()


def configured() -> bool:
    return bool(os.environ.get("BLUESTAR_USER") and os.environ.get("BLUESTAR_PASSWORD"))


def _get_client() -> BlueStarClient:
    global _client
    if not configured():
        raise ACError("AC not set up: add BLUESTAR_USER and BLUESTAR_PASSWORD to .env.")
    user, pw = os.environ["BLUESTAR_USER"], os.environ["BLUESTAR_PASSWORD"]
    with _mod_lock:
        if _client is None or (_client.user, _client.password) != (user.strip(), pw):
            _client = BlueStarClient(user, pw)
        return _client


def devices(*, fresh: bool = False) -> list[dict]:
    global _cache
    if not fresh and _cache and time.monotonic() - _cache[0] < _CACHE_TTL:
        return _cache[1]
    try:
        devs = _get_client().devices()
    except urllib.error.URLError as e:
        raise ACError("Can't reach the Blue Star cloud. Check the internet connection.") from e
    now = time.monotonic()
    for d in devs:
        p = _pending.get(d["id"])
        if p and now - p[0] < _PENDING_SECS:
            d.update(p[1])
    _cache = (now, devs)
    _note_change(devs)
    return devs


def _pick(devs: list[dict]) -> dict:
    """The AC named in BLUESTAR_AC, else the first one."""
    want = os.environ.get("BLUESTAR_AC", "").strip().lower()
    if want:
        for d in devs:
            if want in d["name"].lower():
                return d
    return devs[0]


_last_seen: tuple | None = None


def _note_change(devs: list[dict]) -> None:
    """Log a history sample the moment the AC switches on/off or goes offline."""
    global _last_seen
    if not devs:
        return
    d = _pick(devs)
    key = (d["power"], d["online"])
    if key != _last_seen:
        _last_seen = key
        try:
            record_sample(d)
        except OSError:
            pass


def find(ac_id: str | None = None) -> dict:
    """The AC with ``ac_id``; else the one named in BLUESTAR_AC; else the first."""
    devs = devices()
    if not devs:
        raise ACError("No AC found on this Blue Star account.")
    if ac_id:
        for d in devs:
            if d["id"] == ac_id:
                return d
        raise ACError("That AC isn't on this Blue Star account.")
    return _pick(devs)


def state() -> dict:
    """Everything the AC screen needs. Never raises."""
    if not configured():
        return {"configured": False, "devices": []}
    try:
        return {"configured": True, "devices": devices(), "timer_secs": timer_left()}
    except ACError as e:
        return {"configured": True, "devices": [], "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"configured": True, "devices": [], "error": f"{type(e).__name__}: {e}"}


def _apply(ac: dict, updates: dict, local: dict) -> dict:
    """Send ``updates`` to the AC and reflect ``local`` in the cache right away
    (the cloud reports the new state a moment later)."""
    if not ac["online"]:
        raise ACError(f"{ac['name']} is offline. Check its Wi-Fi / power.")
    try:
        _get_client().send(ac["id"], updates)
    except urllib.error.URLError as e:
        raise ACError("Can't reach the AC cloud. Check the internet connection.") from e
    prev = _pending.get(ac["id"])
    merged = {**prev[1], **local} if prev and time.monotonic() - prev[0] < _PENDING_SECS else local
    _pending[ac["id"]] = (time.monotonic(), merged)
    ac.update(local)
    return ac


def _mode_cmd(ac: dict, mode: str, target: float | None = None) -> tuple[dict, dict]:
    t = target if target is not None else (ac["target"] or 24.0)
    fan = FANS.get(ac["fan"], 7)
    upd = {"pow": 1, "lcmd": "mode",
           "mode": {"value": MODES[mode], "stemp": f"{float(t):.1f}", "fspd": fan}}
    return upd, {"power": True, "mode": mode, "target": float(t)}


def power(on: bool, ac_id: str | None = None) -> dict:
    ac = find(ac_id)
    if not on:
        return _apply(ac, {"pow": 0, "lcmd": "pow"}, {"power": False})
    # Switching on goes through a mode command, as the app does.
    return _apply(ac, *_mode_cmd(ac, ac["mode"]))


def set_mode(mode: str, ac_id: str | None = None, target: float | None = None) -> dict:
    if mode not in MODES:
        raise ACError(f"Unknown mode {mode!r}. Use one of: {', '.join(MODES)}.")
    ac = find(ac_id)
    return _apply(ac, *_mode_cmd(ac, mode, None if target is None else clamp_temp(target)))


def clamp_temp(t: float) -> int:
    return int(max(MIN_TEMP, min(MAX_TEMP, round(t))))


def set_temp(temp: float, ac_id: str | None = None) -> dict:
    ac = find(ac_id)
    t = clamp_temp(temp)
    if not ac["power"]:
        return _apply(ac, *_mode_cmd(ac, ac["mode"], t))  # also switches it on
    return _apply(ac, {"stemp": f"{t:.1f}", "lcmd": "stemp"}, {"target": float(t)})


def set_fan(fan: str, ac_id: str | None = None) -> dict:
    if fan not in FANS:
        raise ACError(f"Unknown fan speed {fan!r}. Use one of: {', '.join(FANS)}.")
    return _apply(find(ac_id), {"fspd": FANS[fan], "lcmd": "fspd"}, {"fan": fan})


def set_swing(swing: str, ac_id: str | None = None) -> dict:
    if swing not in VSWING:
        raise ACError(f"Unknown swing {swing!r}. Use one of: {', '.join(VSWING)}.")
    return _apply(find(ac_id), {"vswing": VSWING[swing], "lcmd": "vswing"}, {"vswing": swing})


def set_eco(level: str, ac_id: str | None = None) -> dict:
    if level not in ECO:
        raise ACError(f"Unknown eco level {level!r}. Use one of: {', '.join(ECO)}.")
    ac = find(ac_id)
    upd = {"eco": {"value": ECO[level], "fspd": FANS.get(ac["fan"], 3)}, "lcmd": "eco"}
    return _apply(ac, upd, {"eco": level})


def set_feature(name: str, on: bool, ac_id: str | None = None) -> dict:
    if name not in FEATURES:
        raise ACError(f"Unknown feature {name!r}. Use one of: {', '.join(FEATURES)}.")
    key, on_val, off_val = FEATURES[name]
    return _apply(find(ac_id), {key: on_val if on else off_val, "lcmd": key}, {name: on})


def _write_env(path: Path, updates: dict[str, str]) -> None:
    """Set KEY=VALUE lines in a .env file, replacing active ones, keeping the rest."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    lines = [ln for ln in lines if ln.split("=", 1)[0].strip() not in updates]
    lines += [f"{k}={v}" for k, v in updates.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def save_credentials(user: str, password: str) -> int:
    """Check the Blue Star login, then keep it in .env and the environment.
    Returns how many ACs the account has."""
    global _client, _cache
    user = user.strip()
    if not user or not password:
        raise ACError("Enter your phone number (or email) and password.")
    if any(c in user + password for c in "\r\n"):
        raise ACError("The login can't contain line breaks.")
    client = BlueStarClient(user, password)
    try:
        client.login()
        count = len(client.devices())
    except urllib.error.URLError as e:
        raise ACError("Can't reach the Blue Star cloud. Check the internet connection.") from e
    _write_env(_ROOT / ".env", {"BLUESTAR_USER": user, "BLUESTAR_PASSWORD": password})
    os.environ["BLUESTAR_USER"], os.environ["BLUESTAR_PASSWORD"] = user, password
    with _mod_lock:
        _client, _cache = client, None
    return count


# --- voice commands ----------------------------------------------------------

_AC = r"(?:^|\s)(?:ac|a\.c\.?|a c|एसी|ए\.सी\.?|air ?conditioner|एयर ?कंडीशनर)(?=\s|$|[,.!?।])"
_ON = r"(?:^|\s)(?:on|chalu|chaalu|chalao|chala do|start|turn on|switch on|चालू|चलाओ|ऑन)(?:\s|$)"
_OFF = r"(?:^|\s)(?:off|band|bandh|stop|turn off|switch off|बंद|ऑफ)(?:\s|$)"
_DOWN = r"(?:kam|ghatao|ghata do|down|decrease|lower|reduce|thanda|thandi|cooler|कम|घटाओ|ठंडा)"
_UP = r"(?:badhao|badha do|zyada|jyada|up|increase|raise|higher|garam|warmer|बढ़ाओ|ज़्यादा|गरम)"
_STATUS = r"(?:kitna|kitni|kya hai|kaisa|status|batao|bataiye|room temp|what is|what's|how)"
_REPORT = r"(?:report|usage|energy|bijli|kharcha|bill|kitna chala|kitni der|kitne ghante|kitne ghante|how long|how much|उपयोग|बिजली|खर्च)"
_FEATURE_WORDS = {
    "turbo": r"turbo|टर्बो",
    "sleep": r"sleep|neend|स्लीप",
    "display": r"display|light|led|डिस्प्ले|लाइट",
    "buzzer": r"buzzer|beep|sound|awaaz|बीप",
    "clean": r"self ?clean|clean|सफ़ाई|क्लीन",
}


def mentions_ac(text: str) -> bool:
    return re.search(_AC, " ".join(text.lower().split())) is not None


def parse_command(text: str) -> dict | None:
    """What an AC voice command asks for, e.g. ``{"action": "temp", "value": 24}``.
    None when the text isn't about the AC."""
    t = " ".join(text.lower().split()).rstrip(".!?।")
    if not mentions_ac(t):
        return None
    on = re.search(_ON, t) is not None
    off = re.search(_OFF, t) is not None
    onoff = False if off else True if on else None

    # Features first, so "turbo on karo" doesn't switch the whole AC.
    for name, words in _FEATURE_WORDS.items():
        if re.search(rf"(?:^|\s)(?:{words})(?:\s|$)", t):
            return {"action": "feature", "name": name, "on": onoff is not False}
    if re.search(r"(?:^|\s)(?:swing|louver|स्विंग)(?:\s|$)", t):
        return {"action": "swing", "value": "off" if off else "auto"}
    if re.search(r"(?:^|\s)(?:eco|economy|इको)(?:\s|$)", t):
        m = re.search(r"\b(110|100|80|60|40)\s*%?", t)
        return {"action": "eco", "value": m.group(1) if m and not off else ("off" if off else "60")}

    # Fan speed ("fan high", "fan tez karo"); "fan mode" is a mode.
    fm = re.search(r"(?:fan|pankha|पंखा)\s*(?:speed\s*)?(?:ko\s*)?(low|medium|high|auto|turbo|kam|dheere|tez|full|max)", t)
    if fm and not re.search(r"fan\s*(?:mode|only)", t):
        word = fm.group(1)
        fan = {"kam": "low", "dheere": "low", "tez": "high", "full": "turbo", "max": "turbo"}.get(word, word)
        return {"action": "fan", "value": fan}

    for mode, words in (("cool", r"cool|cooling|कूल"), ("dry", r"dry|dehumidify|ड्राई"),
                        ("fan", r"fan mode|fan only|फैन मोड"), ("auto", r"auto mode|auto")):
        if re.search(rf"(?:^|\s)(?:{words})(?:\s|$)", t) and not off:
            return {"action": "mode", "value": mode}

    # Relative change: "2 degree kam karo", "thoda thanda karo".
    for words, sign in ((_DOWN, -1), (_UP, 1)):
        m = re.search(rf"(?<!\S)(?:(\d)\s*(?:degree|डिग्री)?\s+)?{words}(?!\S)", t)
        if m:
            return {"action": "delta", "value": sign * int(m.group(1) or 1)}

    m = re.search(r"(?<!\d)(1[6-9]|2\d|30)(?!\d)", t)
    if m:
        return {"action": "temp", "value": int(m.group(1))}
    if onoff is not None:
        return {"action": "power", "on": onoff}
    if re.search(_REPORT, t):
        days = 30 if re.search(r"month|mahin|मह", t) else 7 if re.search(r"week|hafta|roz|har din|daily|every day|report|हफ|रोज", t) else 1
        return {"action": "report", "days": days}
    if re.search(_STATUS, t):
        return {"action": "status"}
    return {"action": "open"}


# --- history, energy estimate, schedule, off-timer ----------------------------
# The Blue Star cloud reports no usage history, so Sara keeps her own while the
# server runs: a sample every 5 minutes, schedule rules, and an off-timer.

_ROOT = Path(__file__).resolve().parents[2]
HISTORY_FILE = _ROOT / ".sara_ac_history.jsonl"
SCHEDULE_FILE = _ROOT / ".sara_ac_schedule.json"
_SAMPLE_SECS = 300
_KEEP_DAYS = 30
_file_lock = threading.Lock()
_timer_at: float | None = None
_fired: set[str] = set()
_started = False


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def record_sample(d: dict, *, now: float | None = None) -> None:
    row = {"t": int(now or time.time()), "on": 1 if d["power"] and d["online"] else 0,
           "room": d["room"], "target": d["target"], "mode": d["mode"], "online": d["online"]}
    with _file_lock, HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def _read_history(since: float) -> list[dict]:
    rows = []
    try:
        with HISTORY_FILE.open(encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("t", 0) >= since:
                    rows.append(r)
    except OSError:
        pass
    return rows


def _trim_history() -> None:
    rows = _read_history(time.time() - _KEEP_DAYS * 86400)
    with _file_lock:
        HISTORY_FILE.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _avg(rows: list[dict], key: str) -> float | None:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def _on_segments(rows: list[dict], now: float) -> list[tuple[float, float]]:
    """(start, end) spans the AC was ON, from consecutive samples. A gap longer
    than 3 samples (Sara wasn't running) isn't counted beyond that."""
    spans = []
    for a, b in zip(rows, rows[1:], strict=False):
        if a["on"]:
            spans.append((a["t"], a["t"] + min(b["t"] - a["t"], 3 * _SAMPLE_SECS)))
    if rows and rows[-1]["on"]:
        spans.append((rows[-1]["t"], rows[-1]["t"] + min(max(0, now - rows[-1]["t"]), _SAMPLE_SECS)))
    return spans


def history(days: int = 1, *, now: float | None = None) -> dict:
    """Chart points plus run-time / energy estimate for the last ``days`` days
    (1 = since midnight)."""
    now = now or time.time()
    if days <= 1:
        t0 = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    else:
        t0 = now - days * 86400
    rows = _read_history(t0)
    on_secs = sum(e - s for s, e in _on_segments(rows, now))
    on_rows = [r for r in rows if r["on"]]
    watts = _float_env("BLUESTAR_WATTS", 900)
    rate = _float_env("BLUESTAR_RATE", 8)
    kwh = on_secs / 3600 * watts / 1000
    step = max(1, len(rows) // 160)
    return {
        "since": rows[0]["t"] if rows else None, "t0": int(t0), "t1": int(now), "days": days,
        "points": [[r["t"], r["room"], r["target"], r["on"]] for r in rows[::step]],
        "on_minutes": round(on_secs / 60), "avg_target": _avg(on_rows, "target"),
        "avg_room": _avg(on_rows, "room"), "kwh": round(kwh, 2), "cost": round(kwh * rate, 1),
        "avg_daily_kwh": round(kwh / max(1, days), 2), "watts": watts, "rate": rate,
    }


def _hm(minutes: int) -> str:
    return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"


def daily_report(days: int = 7, *, now: float | None = None) -> dict:
    """How long the AC ran on each of the last ``days`` days (today included),
    with an energy / cost estimate and the room temperature range."""
    now = now or time.time()

    def midnight(ts: float) -> datetime:
        return datetime.fromtimestamp(ts).replace(hour=0, minute=0, second=0, microsecond=0)

    first = midnight(now) - timedelta(days=days - 1)
    rows = _read_history(first.timestamp())
    secs: dict[str, float] = {}
    for s, e in _on_segments(rows, now):
        t = s
        while t < e:
            d0 = midnight(t)
            seg = min(e, (d0 + timedelta(days=1)).timestamp()) - t
            if seg <= 0:
                break
            key = d0.strftime("%Y-%m-%d")
            secs[key] = secs.get(key, 0) + seg
            t += seg
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(datetime.fromtimestamp(r["t"]).strftime("%Y-%m-%d"), []).append(r)
    watts, rate = _float_env("BLUESTAR_WATTS", 900), _float_env("BLUESTAR_RATE", 8)
    out = []
    for i in range(days):
        d = first + timedelta(days=i)
        key = d.strftime("%Y-%m-%d")
        day_rows = by_day.get(key, [])
        rooms = [r["room"] for r in day_rows if r.get("room") is not None]
        kwh = secs.get(key, 0) / 3600 * watts / 1000
        out.append({
            "date": key, "label": d.strftime("%a %d %b"), "on_minutes": round(secs.get(key, 0) / 60),
            "kwh": round(kwh, 2), "cost": round(kwh * rate, 1),
            "avg_target": _avg([r for r in day_rows if r["on"]], "target"),
            "min_room": min(rooms) if rooms else None, "max_room": max(rooms) if rooms else None,
            "samples": len(day_rows),
        })
    total_min = sum(d["on_minutes"] for d in out)
    total_kwh = round(sum(d["kwh"] for d in out), 2)
    return {"days": out, "total_minutes": total_min, "total_kwh": total_kwh,
            "total_cost": round(sum(d["cost"] for d in out), 1), "avg_minutes": round(total_min / days),
            "since": rows[0]["t"] if rows else None, "watts": watts, "rate": rate}


def report_csv(days: int = 30) -> str:
    rep = daily_report(days)
    lines = ["date,on_minutes,energy_kwh_estimate,cost_estimate,avg_set_temp,min_room_temp,max_room_temp"]
    for d in rep["days"]:
        lines.append(",".join("" if v is None else str(v) for v in
                              (d["date"], d["on_minutes"], d["kwh"], d["cost"], d["avg_target"], d["min_room"], d["max_room"])))
    return "\n".join(lines) + "\n"


def report_text(days: int = 7) -> str:
    """Plain-text daily report for the chat box."""
    rep = daily_report(days)
    if not rep["total_minutes"]:
        return "No AC run-time is recorded yet. Sara logs it every 5 minutes while she is running and the AC is ON."
    used = [d for d in rep["days"] if d["on_minutes"]]
    head = (f"Last {days} days: {_hm(rep['total_minutes'])} total, about {rep['total_kwh']} kWh "
            f"(\u20b9{rep['total_cost']}), {_hm(rep['avg_minutes'])} per day.")
    return "\n".join([head] + [f"{d['label']}: {_hm(d['on_minutes'])} \u00b7 {d['kwh']} kWh" for d in used[-10:]])


def energy_summary(days: int = 7) -> str:
    h = history(days)
    span = "Today" if days <= 1 else f"In the last {days} days"
    if not h["on_minutes"]:
        return "No AC run-time is recorded yet. Sara logs it while the server is running."
    return (f"{span} the AC ran about {_hm(h['on_minutes'])}, "
            f"roughly {h['kwh']} kWh (about \u20b9{h['cost']}). This is an estimate.")


# schedule rules: {"id", "name", "start": "HH:MM", "end": "HH:MM", "temp", "mode", "enabled"}
def _load_rules() -> list[dict]:
    try:
        return json.loads(SCHEDULE_FILE.read_text(encoding="utf-8")).get("rules", [])
    except (OSError, ValueError):
        return []


def _save_rules(rules: list[dict]) -> None:
    with _file_lock:
        SCHEDULE_FILE.write_text(json.dumps({"rules": rules}, indent=1), encoding="utf-8")


def _hhmm(v: str) -> str:
    try:
        h, m = v.split(":")
        if 0 <= int(h) <= 23 and 0 <= int(m) <= 59:
            return f"{int(h):02d}:{int(m):02d}"
    except ValueError:
        pass
    raise ACError("Time must look like 06:30.")


def schedule_list() -> list[dict]:
    return sorted(_load_rules(), key=lambda r: r["start"])


def schedule_add(name: str, start: str, end: str, temp: float, mode: str) -> list[dict]:
    if mode not in MODES:
        raise ACError(f"Unknown mode {mode!r}.")
    rules = _load_rules()
    if len(rules) >= 12:
        raise ACError("That's the limit of 12 schedules.")
    rules.append({"id": uuid.uuid4().hex[:8], "name": (name or "Schedule").strip()[:30], "start": _hhmm(start),
                  "end": _hhmm(end), "temp": clamp_temp(temp), "mode": mode, "enabled": True})
    _save_rules(rules)
    return schedule_list()


def schedule_update(rule_id: str, *, enabled: bool | None = None, delete: bool = False) -> list[dict]:
    rules = _load_rules()
    if delete:
        rules = [r for r in rules if r["id"] != rule_id]
    else:
        for r in rules:
            if r["id"] == rule_id and enabled is not None:
                r["enabled"] = enabled
    _save_rules(rules)
    return schedule_list()


def set_timer(minutes: int) -> int | None:
    """Switch the AC off after ``minutes`` (0 clears). Held in memory only."""
    global _timer_at
    _timer_at = time.time() + minutes * 60 if minutes > 0 else None
    return timer_left()


def timer_left() -> int | None:
    return max(0, int(_timer_at - time.time())) if _timer_at else None


def _tick(now: datetime | None = None) -> None:
    """Run due schedule rules and the off-timer (called about twice a minute)."""
    global _timer_at
    now = now or datetime.now()
    if _timer_at and time.time() >= _timer_at:
        _timer_at = None
        power(False)
    hm, day = now.strftime("%H:%M"), now.strftime("%Y-%m-%d")
    for r in _load_rules():
        if not r.get("enabled"):
            continue
        for kind in ("start", "end"):
            key = f"{day}:{r['id']}:{kind}"
            if r[kind] == hm and key not in _fired:
                _fired.add(key)
                if kind == "start":
                    set_mode(r["mode"], None, r["temp"])
                else:
                    power(False)
    if len(_fired) > 200:
        _fired.clear()


def start(emit=None) -> None:
    """Background worker: 5-minute history samples, schedule rules, off-timer."""
    global _started
    if _started:
        return
    _started = True
    try:
        _trim_history()
    except OSError:
        pass

    def loop() -> None:
        last_sample = 0.0
        while True:
            try:
                if configured():
                    _tick()
                    d = find()  # also notices on/off changes between samples
                    if time.time() - last_sample >= _SAMPLE_SECS:
                        last_sample = time.time()
                        record_sample(d)
            except Exception as e:  # noqa: BLE001 - never kill the worker
                if emit:
                    emit("ac", f"AC background task: {e}")
            time.sleep(30)

    threading.Thread(target=loop, name="ac-worker", daemon=True).start()
