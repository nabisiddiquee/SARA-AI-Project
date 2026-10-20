"""Bluetooth for Sara (Windows): list, search, pair, connect.

Paired devices come straight from Windows' device list, so they show up
instantly. A search for nearby devices takes ~30 s: Windows only reports
them once its classic-Bluetooth inquiry has finished.

Headsets and speakers are (dis)connected the way the Connect button in
Windows Settings does it: a one-shot property on the device's Bluetooth
audio kernel-streaming filter, whose interface path carries the device's
address. Other devices (phones, watches) connect from their own side.

Needs the ``bluetooth`` extra (pywinrt): ``pip install -e ".[bluetooth]"``.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import re
import threading
import time
import uuid

SCAN_SECONDS = 30
_CLASSIC = "{e0cbf06c-cd8b-4647-bb8a-263b43f0f974}"
_LE = "{bb7bb05e-5972-42b5-94fc-76eaa7084d49}"
_SCAN_PROPS = [
    "System.Devices.Aep.IsConnected",
    "System.Devices.Aep.SignalStrength",
    "System.Devices.Aep.Bluetooth.Cod.Major",
]

_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_nearby: dict[str, dict] = {}                  # mac -> device found by the last search
_scan = {"running": False, "started": 0.0, "seconds": SCAN_SECONDS}
_pairing: dict = {}                            # the current pairing job, if any
_paired_cache: tuple[float, list] = (0.0, [])


def available() -> bool:
    if os.name != "nt":
        return False
    try:
        import winrt.windows.devices.bluetooth  # noqa: F401
        import winrt.windows.devices.enumeration  # noqa: F401
    except ImportError:
        return False
    return True


def _run(coro, timeout: float = 30):
    """Run a coroutine on the module's private asyncio loop (own thread)."""
    global _loop
    with _lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, name="bluetooth", daemon=True).start()
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout)


# --- device classification -----------------------------------------------------

_MAJOR = {1: "computer", 2: "phone", 4: "audio", 5: "input", 7: "wearable"}
_NAME_HINTS = (
    ("audio", ("bud", "pods", "earphone", "headphone", "headset", "speaker", "fuze",
               "soundbar", "airdopes", "rockerz", "neckband", "earbud", "jbl", "boat")),
    ("tv", ("tv", "television", "firestick", "fire stick", "chromecast")),
    ("wearable", ("watch", "band", "fit", "halo", "ring")),
    ("phone", ("phone", "iphone", "oppo", "realme", "vivo", "redmi", "galaxy", "oneplus",
               "nord", "reno", "pixel", "poco", "moto")),
    ("computer", ("laptop", "desktop", "-pc", "macbook", "thinkpad")),
    ("input", ("keyboard", "mouse", "controller", "gamepad")),
)


def _kind(name: str, major: int | None) -> str:
    if major in _MAJOR:
        return _MAJOR[major]
    n = name.lower()
    for kind, hints in _NAME_HINTS:
        if any(h in n for h in hints):
            return kind
    return "other"


def _norm_mac(mac: str) -> str:
    return re.sub(r"[^0-9a-f]", "", mac.lower())


def _fmt_mac(addr: int) -> str:
    h = f"{addr:012x}"
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


# --- headset / speaker connect (kernel streaming) -------------------------------

class _GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16), ("d3", ctypes.c_uint16),
                ("d4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, text: str) -> "_GUID":
        g = cls()
        ctypes.memmove(ctypes.byref(g), uuid.UUID(text).bytes_le, 16)
        return g


class _IfaceData(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("guid", _GUID), ("flags", ctypes.c_uint32),
                ("reserved", ctypes.c_void_p)]


class _KsProperty(ctypes.Structure):
    _fields_ = [("set", _GUID), ("id", ctypes.c_uint32), ("flags", ctypes.c_uint32)]


_KSCATEGORY_AUDIO = "6994AD04-93EF-11D0-A3CC-00A0C9223196"
_KSPROPSETID_BTAUDIO = "7fa06c40-b8f6-4c7e-8556-e8c33a12e54d"
_IOCTL_KS_PROPERTY = 0x002F0003
_RECONNECT, _DISCONNECT = 0, 1


def _audio_paths() -> list[str]:
    """Device-interface paths of all present audio filters."""
    from ctypes import wintypes

    sa = ctypes.WinDLL("setupapi", use_last_error=True)
    sa.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    sa.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(_GUID), ctypes.c_wchar_p, ctypes.c_void_p, wintypes.DWORD]
    sa.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_GUID),
                                               wintypes.DWORD, ctypes.POINTER(_IfaceData)]
    sa.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_IfaceData), ctypes.c_void_p,
                                                    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    sa.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]

    guid = _GUID.of(_KSCATEGORY_AUDIO)
    handle = sa.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x2 | 0x10)  # PRESENT | DEVICEINTERFACE
    paths: list[str] = []
    if not handle or handle == ctypes.c_void_p(-1).value:
        return paths
    try:
        i = 0
        while True:
            data = _IfaceData()
            data.cbSize = ctypes.sizeof(data)
            if not sa.SetupDiEnumDeviceInterfaces(handle, None, ctypes.byref(guid), i, ctypes.byref(data)):
                break
            i += 1
            need = wintypes.DWORD()
            sa.SetupDiGetDeviceInterfaceDetailW(handle, ctypes.byref(data), None, 0, ctypes.byref(need), None)
            if not need.value:
                continue
            buf = ctypes.create_string_buffer(need.value)
            ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0] = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if sa.SetupDiGetDeviceInterfaceDetailW(handle, ctypes.byref(data), buf, need, None, None):
                paths.append(ctypes.wstring_at(ctypes.addressof(buf) + 4))
    finally:
        sa.SetupDiDestroyDeviceInfoList(handle)
    return paths


def _audio_macs() -> set[str]:
    """Addresses of Bluetooth devices that have an audio (A2DP) filter."""
    out = set()
    for p in _audio_paths():
        m = re.search(r"&([0-9a-f]{12})_", p.lower()) if "bthenum" in p.lower() else None
        if m:
            out.add(m.group(1))
    return out


def _audio_command(mac: str, prop_id: int) -> bool:
    """Send (re)connect / disconnect to a headset/speaker. False if it has none."""
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CreateFileW.argtypes = [ctypes.c_wchar_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    k32.DeviceIoControl.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]

    key = _norm_mac(mac)
    sent = 0
    for path in _audio_paths():
        if key not in path.lower():
            continue
        h = k32.CreateFileW(path, 0xC0000000, 3, None, 3, 0, None)  # R/W, share R/W, OPEN_EXISTING
        if not h or h == ctypes.c_void_p(-1).value:
            continue
        try:
            prop = _KsProperty(_GUID.of(_KSPROPSETID_BTAUDIO), prop_id, 1)  # KSPROPERTY_TYPE_GET
            ret = wintypes.DWORD()
            if k32.DeviceIoControl(h, _IOCTL_KS_PROPERTY, ctypes.byref(prop), ctypes.sizeof(prop),
                                   None, 0, ctypes.byref(ret), None):
                sent += 1
        finally:
            k32.CloseHandle(h)
    return sent > 0


# --- paired devices ------------------------------------------------------------------

async def _paired_async() -> list[dict]:
    from winrt.windows.devices.bluetooth import (
        BluetoothConnectionStatus, BluetoothDevice, BluetoothLEDevice,
    )
    from winrt.windows.devices.enumeration import DeviceInformation

    audio = _audio_macs()
    out: list[dict] = []
    for cls, le in ((BluetoothDevice, False), (BluetoothLEDevice, True)):
        infos = await DeviceInformation.find_all_async_aqs_filter(
            cls.get_device_selector_from_pairing_state(True))
        for info in infos:
            try:
                dev = await cls.from_id_async(info.id)
            except Exception:  # noqa: BLE001
                dev = None
            if dev is None:
                continue
            mac = _fmt_mac(dev.bluetooth_address)
            if any(d["mac"] == mac for d in out):
                continue
            name = (dev.name or info.name or "").strip()
            major = None if le else int(dev.class_of_device.major_class)
            out.append({
                "id": info.id, "mac": mac, "name": name, "le": le,
                "paired": True,
                "connected": dev.connection_status == BluetoothConnectionStatus.CONNECTED,
                "kind": _kind(name, major), "rssi": None,
                "audio": _norm_mac(mac) in audio,
            })
    return out


def paired(max_age: float = 2.0) -> list[dict]:
    """Paired devices with live connection status (cached briefly)."""
    global _paired_cache
    if time.time() - _paired_cache[0] < max_age:
        return _paired_cache[1]
    items = _run(_paired_async())
    _paired_cache = (time.time(), items)
    return items


# --- nearby search -------------------------------------------------------------------

def _unbox(value, *unboxers):
    for fn in unboxers:
        try:
            return fn(value)
        except Exception:  # noqa: BLE001
            continue
    return None


def _record(info) -> None:
    from winrt.system import unbox_boolean, unbox_int32, unbox_uint8, unbox_uint16, unbox_uint32

    name = (info.name or "").strip()
    if not any(ch.isalnum() for ch in name):  # unnamed / garbled advertisers
        return
    props = info.properties

    def prop(key, *unboxers):
        return _unbox(props.lookup(key), *unboxers) if props.has_key(key) else None

    le = info.id.startswith("BluetoothLE#")
    mac = info.id[-17:].lower()
    rec = {
        "id": info.id, "mac": mac, "name": name, "le": le,
        "paired": bool(info.pairing.is_paired),
        "can_pair": bool(info.pairing.can_pair),
        "connected": bool(prop("System.Devices.Aep.IsConnected", unbox_boolean)),
        "rssi": prop("System.Devices.Aep.SignalStrength", unbox_int32),
        "kind": _kind(name, prop("System.Devices.Aep.Bluetooth.Cod.Major",
                                 unbox_uint8, unbox_uint16, unbox_uint32)),
    }
    with _lock:
        old = _nearby.get(mac)
        # Devices can show up over both classic and LE; pairing over
        # classic is what audio devices need, so keep that entry.
        if old and not old["le"] and le:
            old["rssi"] = rec["rssi"] if rec["rssi"] is not None else old["rssi"]
            return
        _nearby[mac] = rec


def start_scan(seconds: int = SCAN_SECONDS) -> bool:
    """Start a background search for nearby devices. False if one is running."""
    with _lock:
        if _scan["running"]:
            return False
        _scan.update(running=True, started=time.time(), seconds=seconds)
        _nearby.clear()
    threading.Thread(target=_scan_thread, args=(seconds,), name="bt-scan", daemon=True).start()
    return True


def _scan_thread(seconds: int) -> None:
    from winrt.windows.devices.enumeration import DeviceInformation, DeviceInformationKind

    infos: dict[str, object] = {}
    done = threading.Event()
    pending = [2]

    def added(_w, info):
        infos[info.id] = info
        _record(info)

    def updated(_w, upd):
        info = infos.get(upd.id)
        if info is not None:
            info.update(upd)
            _record(info)

    def completed(_w, _o):
        with _lock:
            pending[0] -= 1
            if pending[0] <= 0:
                done.set()

    watchers = []
    try:
        for proto in (_CLASSIC, _LE):
            w = DeviceInformation.create_watcher_with_kind_aqs_filter_and_additional_properties(
                f'System.Devices.Aep.ProtocolId:="{proto}"', _SCAN_PROPS,
                DeviceInformationKind.ASSOCIATION_ENDPOINT)
            w.add_added(added)
            w.add_updated(updated)
            w.add_enumeration_completed(completed)
            watchers.append(w)
            w.start()
        done.wait(seconds + 15)
    finally:
        for w in watchers:
            try:
                w.stop()
            except Exception:  # noqa: BLE001
                pass
        with _lock:
            _scan["running"] = False


# --- pairing -------------------------------------------------------------------------

def pair(device_id: str) -> bool:
    """Start pairing in the background; follow it with ``state()["pairing"]``."""
    if _pairing.get("stage") in ("pairing", "confirm_pin", "provide_pin", "display_pin"):
        return False
    _pairing.clear()
    _pairing.update(id=device_id, stage="pairing", pin=None, status=None, answer=None,
                    event=threading.Event())
    threading.Thread(target=_pair_thread, args=(device_id,), name="bt-pair", daemon=True).start()
    return True


def respond(answer: str) -> None:
    """Answer a PIN prompt: "yes"/"no" to confirm a match, or the PIN itself."""
    _pairing["answer"] = (answer or "").strip()
    ev = _pairing.get("event")
    if ev:
        ev.set()


def _pair_thread(device_id: str) -> None:
    try:
        ok, status = _run(_pair_async(device_id), timeout=150)
    except Exception as e:  # noqa: BLE001
        ok, status = False, str(e)
    _pairing.update(stage="done" if ok else "failed", status=status)
    if ok:
        global _paired_cache
        _paired_cache = (0.0, [])
        rec = next((d for d in _nearby.values() if d["id"] == device_id), None)
        if rec:
            rec["paired"] = True
            if rec["kind"] == "audio":
                time.sleep(1.5)
                _audio_command(rec["mac"], _RECONNECT)


async def _pair_async(device_id: str) -> tuple[bool, str]:
    from winrt.windows.devices.enumeration import (
        DeviceInformation, DevicePairingKinds, DevicePairingResultStatus,
    )

    info = await DeviceInformation.create_from_id_async(device_id)
    if info.pairing.is_paired:
        return True, "AlreadyPaired"
    custom = info.pairing.custom
    event = _pairing["event"]

    def requested(_sender, args):
        kind = args.pairing_kind
        if kind == DevicePairingKinds.CONFIRM_ONLY:
            args.accept()
            return
        if kind == DevicePairingKinds.DISPLAY_PIN:
            _pairing.update(stage="display_pin", pin=args.pin)  # type it on the device
            args.accept()
            return
        # Needs the user: wait (up to 90 s) for their answer from the dashboard.
        deferral = args.get_deferral()
        confirm = kind == DevicePairingKinds.CONFIRM_PIN_MATCH
        _pairing.update(stage="confirm_pin" if confirm else "provide_pin",
                        pin=args.pin if confirm else None)
        event.wait(90)
        answer = _pairing.get("answer") or ""
        if confirm and answer.lower() == "yes":
            args.accept()
        elif not confirm and answer:
            args.accept_with_pin(answer)
        _pairing["stage"] = "pairing"
        deferral.complete()

    token = custom.add_pairing_requested(requested)
    try:
        kinds = (DevicePairingKinds.CONFIRM_ONLY | DevicePairingKinds.DISPLAY_PIN
                 | DevicePairingKinds.CONFIRM_PIN_MATCH | DevicePairingKinds.PROVIDE_PIN)
        result = await custom.pair_async(kinds)
    finally:
        custom.remove_pairing_requested(token)
    ok = result.status in (DevicePairingResultStatus.PAIRED, DevicePairingResultStatus.ALREADY_PAIRED)
    return ok, result.status.name


def unpair(device_id: str) -> tuple[bool, str]:
    async def go():
        from winrt.windows.devices.enumeration import DeviceInformation, DeviceUnpairingResultStatus

        info = await DeviceInformation.create_from_id_async(device_id)
        r = await info.pairing.unpair_async()
        ok = r.status in (DeviceUnpairingResultStatus.UNPAIRED, DeviceUnpairingResultStatus.ALREADY_UNPAIRED)
        return ok, r.status.name

    global _paired_cache
    _paired_cache = (0.0, [])
    return _run(go())


# --- connect / disconnect ------------------------------------------------------------

# Phones / tablets are audio *sources* (A2DP profile 110a): they send sound to
# the PC. Windows can't reconnect those through the headset filter above; it
# needs an AudioPlaybackConnection (Windows 10 2004+), which also makes the
# PC play the phone's media audio, like "Connect" in Windows Settings. The
# connection object must stay alive, so it's kept here per device.
_playback: dict[str, object] = {}


def _source_macs() -> set[str]:
    """Paired devices that stream audio to this PC (phones, tablets)."""
    out = set()
    for p in _audio_paths():
        pl = p.lower()
        if "bthenum" in pl and "0000110a" in pl:
            m = re.search(r"&([0-9a-f]{12})_", pl)
            if m:
                out.add(m.group(1))
    return out


async def _playback_connect_async(mac: str) -> bool:
    from winrt.windows.devices.enumeration import DeviceInformation
    from winrt.windows.media.audio import (
        AudioPlaybackConnection, AudioPlaybackConnectionOpenResultStatus, AudioPlaybackConnectionState,
    )

    key = _norm_mac(mac)
    old = _playback.get(key)
    if old is not None and old.state == AudioPlaybackConnectionState.OPENED:
        return True
    infos = await DeviceInformation.find_all_async_aqs_filter(AudioPlaybackConnection.get_device_selector())
    info = next((i for i in infos if key in i.id.lower()), None)
    if info is None:
        return False
    conn = AudioPlaybackConnection.try_create_from_id(info.id)
    if conn is None:
        return False
    await conn.start_async()
    result = await conn.open_async()
    if result.status == AudioPlaybackConnectionOpenResultStatus.SUCCESS:
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001
                pass
        _playback[key] = conn
        return True
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    return False


def _playback_disconnect(mac: str) -> bool:
    conn = _playback.pop(_norm_mac(mac), None)
    if conn is None:
        return False
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    return True


def is_source(mac: str) -> bool:
    return _norm_mac(mac) in _source_macs()


def connect(mac: str) -> bool:
    """Connect a paired device: phones via AudioPlaybackConnection,
    headsets / speakers via their audio filter. True only on success for
    phones; for headsets it means the request was sent."""
    if is_source(mac):
        try:
            ok = _run(_playback_connect_async(mac), timeout=25)
        except Exception:  # noqa: BLE001 - off / out of range / API missing
            ok = False
        global _paired_cache
        _paired_cache = (0.0, [])
        return ok
    return _audio_command(mac, _RECONNECT)


def disconnect(mac: str) -> bool:
    if is_source(mac) and _playback_disconnect(mac):
        return True
    return _audio_command(mac, _DISCONNECT)


def find_paired(name: str) -> dict | None:
    """The paired device whose name best matches ``name`` ("realme buds")."""
    words = [w for w in re.split(r"\W+", name.lower()) if w]
    best, best_score = None, 0
    for d in paired():
        n = d["name"].lower()
        score = sum(1 for w in words if w in n)
        if score > best_score:
            best, best_score = d, score
    return best if best_score else None


# --- radio ---------------------------------------------------------------------------

def radio(on: bool | None = None) -> bool | None:
    """Bluetooth radio state; switches it first when ``on`` is given.
    None when the PC has no Bluetooth adapter."""
    async def go():
        from winrt.windows.devices.radios import Radio, RadioKind, RadioState

        radios = await Radio.get_radios_async()
        bt = next((r for r in radios if r.kind == RadioKind.BLUETOOTH), None)
        if bt is None:
            return None
        if on is not None:
            await Radio.request_access_async()
            await bt.set_state_async(RadioState.ON if on else RadioState.OFF)
        return bt.state == RadioState.ON

    return _run(go())


# --- auto-connect --------------------------------------------------------------------
# Devices the user starred in the dashboard are reconnected whenever they come
# in range (switched on nearby). Only audio devices can be connected from the
# PC side, so only those can be starred. The list lives in a small JSON file.

AUTO_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                         ".sara_bt_auto.json")
AUTO_EVERY = 15        # seconds between checks
AUTO_RETRY = 30        # seconds between reconnect attempts per device
_auto_started = False


def auto_list() -> dict[str, str]:
    """Starred devices: {mac: name}."""
    try:
        import json

        with open(AUTO_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def set_auto(mac: str, name: str, on: bool) -> dict[str, str]:
    import json

    items = auto_list()
    if on:
        items[mac] = name or items.get(mac, mac)
    else:
        items.pop(mac, None)
    with open(AUTO_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2)
    return items


def start_auto_watch(emit) -> None:
    """Background thread: reconnect starred devices and report changes via
    ``emit(kind, message)`` ("bt_connected" / "bt_disconnected")."""
    global _auto_started
    if _auto_started or not available():
        return
    _auto_started = True

    def loop() -> None:
        was: dict[str, bool] = {}          # mac -> connected at the last check
        last_try: dict[str, float] = {}
        while True:
            try:
                auto = auto_list()
                if auto and radio() is not False:
                    devices = {d["mac"]: d for d in paired(max_age=0)}
                    for mac, name in auto.items():
                        d = devices.get(mac)
                        if d is None:
                            continue                      # forgotten / unpaired
                        name = d["name"] or name
                        now_on = bool(d["connected"])
                        before = was.get(mac)
                        if before is not None and now_on != before:
                            emit("bt_connected" if now_on else "bt_disconnected", name)
                        was[mac] = now_on
                        if not now_on and d.get("audio") and time.time() - last_try.get(mac, 0) > AUTO_RETRY:
                            last_try[mac] = time.time()
                            connect(mac)                  # succeeds once it's on and in range
            except Exception:  # noqa: BLE001 - keep watching whatever happens
                pass
            time.sleep(AUTO_EVERY)

    threading.Thread(target=loop, name="bt-auto", daemon=True).start()


# --- snapshot for the dashboard ------------------------------------------------------

def state() -> dict:
    if not available():
        return {"available": False}
    try:
        radio_on = radio()
    except Exception:  # noqa: BLE001
        radio_on = None
    items = paired() if radio_on is not False else []
    paired_macs = {d["mac"] for d in items}
    with _lock:
        nearby = [dict(d) for d in _nearby.values() if d["mac"] not in paired_macs and not d["paired"]]
        scan = dict(_scan)
    nearby.sort(key=lambda d: (d["rssi"] is None, -(d["rssi"] or 0), d["name"].lower()))
    elapsed = time.time() - scan["started"]
    return {
        "available": True,
        "radio": radio_on,
        "scanning": scan["running"],
        "progress": min(1.0, elapsed / scan["seconds"]) if scan["running"] else (1.0 if scan["started"] else 0.0),
        "paired": items,
        "nearby": nearby,
        "auto": list(auto_list()),
        "pairing": {k: v for k, v in _pairing.items() if k not in ("event", "answer")},
    }
