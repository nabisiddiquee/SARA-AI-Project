# Phone Mic AI

Use an **Android phone as a live microphone** for a **Windows laptop**, over
**Bluetooth** (primary) or a **USB cable** (fallback). No Wi-Fi, no shared
network and no internet needed. Python receives the audio live, and it can be
exposed to every Windows app as a normal microphone.

```
Android phone mic ──(AudioRecord, PCM 16-bit mono 16 kHz)
      │
      ├── Bluetooth RFCOMM (Serial Port style, ~256 kbit/s)  ← primary
      └── USB cable via `adb forward` (TCP over the cable)   ← fallback
      ▼
python-server/main.py  (Windows)
      ├── live status, level meter, speech detection, auto-reconnect
      ├── --virtual-mic "CABLE Input"  →  Windows apps use "CABLE Output" as a mic
      ├── --record file.wav
      └── --stt whisper|vosk|openai  [--ai openai|ollama]  →  text → AI reply
```

---

## 1. Why this design (what Bluetooth can and can't do)

| Option | Works for "phone mic → Windows"? | Why |
|---|---|---|
| Normal pairing only | ❌ | Pairing just creates a trust link; no audio flows by itself. |
| **A2DP** | ❌ | Carries the phone's *media playback* (music), never the microphone. Windows can receive it (Settings → "Connect" a phone), but that's not mic audio. |
| **HFP / HSP** | ❌ (on Windows) | The mic direction exists only during calls, and Windows supports HFP only as the *phone side* (Audio Gateway, for headsets). It can't act as the headset for a phone. |
| Windows driver | Not needed | Would only be required for a native mic device, which needs signed kernel drivers. A free virtual audio cable gives the same result. |
| **Android app + Bluetooth RFCOMM** | ✅ **chosen** | RFCOMM is a reliable serial byte stream available on every Android and Windows PC. The app sends raw PCM; Python reads it. |
| **Android app + USB (`adb forward`)** | ✅ fallback | Lowest latency, most stable, no radio at all. Needs USB debugging. |
| Virtual audio device | ✅ optional | [VB-Audio Virtual Cable](https://vb-audio.com/Cable/) (free) makes the stream appear as a microphone for *all* Windows apps. |
| Python socket server | ✅ | Python is the receiver (RFCOMM server for Bluetooth, TCP client for USB). |

So: **an Android app is required** (Android has no built-in "be a microphone"
mode), **no Windows driver is required**, and a **virtual cable is optional**.

### Comparison

| Method | Latency | Quality | Stability | Complexity | Internet | Same network |
|---|---|---|---|---|---|---|
| **Bluetooth RFCOMM** (this project) | ~60–150 ms | PCM 16 kHz lossless (speech-grade); 44.1 kHz possible | Good within ~5–10 m, auto-reconnects | Medium | No | **No** |
| **USB** (`adb forward`, this project) | ~20–50 ms | Lossless, any rate | Excellent | Medium (needs adb) | No | **No** |
| Bluetooth HFP/HSP | ~100–200 ms | 8/16 kHz, compressed (CVSD/mSBC) | Call-only | Not possible as phone→PC on Windows | No | No |
| Bluetooth A2DP | ~150–250 ms | High (music) | Good | Not usable (no mic) | No | No |
| Wi-Fi (TCP/UDP) | ~20–80 ms | Lossless | Depends on Wi-Fi | Low | No | **Yes** |
| WebRTC | ~100–300 ms | Opus, very good | Good | High (signalling, TURN server) | **Usually yes** | No (via internet) |

---

## 2. Project structure

```
phone-mic-ai/
├── README.md
├── android-app/                      Kotlin app (Android 8.0+)
│   ├── settings.gradle.kts, build.gradle.kts, gradle.properties
│   └── app/
│       ├── build.gradle.kts
│       └── src/main/
│           ├── AndroidManifest.xml   permissions + foreground service
│           ├── java/com/phonemic/ai/
│           │   ├── MainActivity.kt        UI: Bluetooth/USB, laptop picker, Start/Stop, level
│           │   ├── MicStreamService.kt    foreground service: record → send, auto-reconnect
│           │   ├── AudioCapture.kt        AudioRecord, PCM 16-bit mono 16 kHz, 20 ms chunks
│           │   ├── StreamProtocol.kt      UUID, port, 12-byte header
│           │   └── transport/
│           │       ├── Transport.kt
│           │       ├── BluetoothTransport.kt  RFCOMM client → laptop
│           │       └── UsbTransport.kt        127.0.0.1:5000 server (adb forward)
│           └── res/ (layout, strings, theme)
└── python-server/                    Python 3.11+
    ├── main.py             CLI: transport, virtual mic, record, STT / AI
    ├── audio_receiver.py   BluetoothReceiver (WinRT RFCOMM server), TcpReceiver (USB)
    ├── audio_processor.py  level, speech detection, status, virtual mic, WAV, utterances
    ├── protocol.py         wire format (shared with the app)
    ├── stt.py              speech-to-text + AI reply pipeline (optional)
    ├── tools/fake_phone.py simulated phone for testing without a phone
    └── requirements.txt
```

### Wire format
Every connection starts with a 12-byte header, then raw PCM until it closes:

| bytes | field |
|---|---|
| 0–3 | `PMIC` |
| 4 | version = 1 |
| 5 | channels = 1 |
| 6 | bits = 16 |
| 7 | reserved |
| 8–11 | sample rate (uint32 LE), 16000 by default |

Audio: signed 16-bit little-endian PCM, mono, 16 kHz (≈ 32 KB/s, 256 kbit/s),
sent in 20 ms chunks.

---

## 3. Windows setup (laptop)

### 3.1 Python
```powershell
cd phone-mic-ai\python-server
py -3.11 -m venv .venv          # or any Python 3.11+
.venv\Scripts\activate
pip install -r requirements.txt
```
Requires Windows 10 version 2004+ or Windows 11 (WinRT Bluetooth APIs).

### 3.2 Bluetooth pairing (once)
1. Windows: **Settings → Bluetooth & devices → Bluetooth: On**.
2. Phone: **Settings → Bluetooth**, tap your laptop's name, confirm the code on both.
3. That's all. Don't "Connect" it as an audio device; the app opens its own channel.

### 3.3 Optional: make it a Windows microphone (virtual cable)
1. Download **VB-Audio Virtual Cable** from https://vb-audio.com/Cable/ (free), run
   `VBCABLE_Setup_x64.exe` as administrator, reboot.
2. Run the receiver with `--virtual-mic "CABLE Input"`.
3. In any app (Zoom, Teams, Discord, browser, Windows voice typing), choose
   **"CABLE Output (VB-Audio Virtual Cable)"** as the microphone.
4. List output devices with `python main.py --list-devices`.

### 3.4 USB fallback: adb
1. Install **Android Platform-Tools** from https://developer.android.com/tools/releases/platform-tools
   and add the folder to `PATH`.
2. Phone: **Settings → About phone →** tap *Build number* 7× → **Developer options → USB debugging: On**.
3. Connect the cable, allow the "USB debugging" prompt on the phone, then:
```powershell
adb devices                       # should list your phone as "device"
adb forward tcp:5000 tcp:5000     # repeat after each re-plug
```

---

## 4. Android app

### Build and install
**Android Studio (recommended):** *File → Open →* `phone-mic-ai/android-app`, let Gradle
sync (it generates the Gradle wrapper and downloads the SDK), connect the phone,
press **Run ▶**.

**Command line** (needs JDK 17 + Android SDK + Gradle 8.7):
```powershell
cd phone-mic-ai\android-app
gradle wrapper --gradle-version 8.7
.\gradlew assembleDebug
adb install -r app\build\outputs\apk\debug\app-debug.apk
```

### Permissions (in `AndroidManifest.xml`, requested at runtime)
| Permission | Why |
|---|---|
| `RECORD_AUDIO` | microphone (runtime prompt) |
| `BLUETOOTH_CONNECT` (Android 12+) | connect to the paired laptop (runtime prompt) |
| `BLUETOOTH`, `BLUETOOTH_ADMIN` (≤ Android 11) | legacy Bluetooth |
| `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_MICROPHONE` | keep streaming with the screen off |
| `POST_NOTIFICATIONS` (Android 13+) | the "streaming" notification with a Stop button |
| `INTERNET` | only for the local 127.0.0.1 socket in USB mode (no network traffic) |
| `WAKE_LOCK` | keep the CPU awake while streaming |

No location permission is needed: the app only connects to an already-paired
laptop and never scans.

---

## 5. Run it

### Bluetooth (primary)
```powershell
# laptop
python main.py
#   [INFO] Bluetooth server ready: 'PhoneMic AI' (8f4c2a10-...). Open the app on the phone and tap Start.
```
Phone: choose **Bluetooth**, pick your laptop, tap **Start**.
```
[CONNECTED] Phone microphone via Bluetooth (DESKTOP-ABC): 16000 Hz · 16-bit · mono
[LIVE] Bluetooth · 16000 Hz · 16-bit · mono ·  -24.1 dBFS ███████████········· SPEAKING · 256 kbit/s
```

### USB (fallback)
```powershell
adb forward tcp:5000 tcp:5000
python main.py --transport usb
```
Phone: choose **USB cable**, tap **Start**.

### Useful options
```powershell
python main.py --virtual-mic "CABLE Input"   # expose as a Windows microphone
python main.py --record meeting.wav           # save the audio
python main.py --threshold -45                # more sensitive speech detection
python main.py --seconds 60                   # stop after 60 s
```

---

## 6. Speech-to-text and AI

Every finished phrase (speech detection with 0.7 s hangover) is passed to
`stt.Pipeline` in a background thread, so the stream never stalls:

```
Phone mic → Python → utterance PCM → STT → text → AI → reply
```

```powershell
# offline, accurate (downloads the model on first use)
pip install faster-whisper
python main.py --stt whisper

# offline, very light
pip install vosk
$env:VOSK_MODEL = "C:\models\vosk-model-small-en-in-0.4"
python main.py --stt vosk

# cloud STT + cloud AI reply
pip install openai
$env:OPENAI_API_KEY = "sk-..."
python main.py --stt openai --ai openai

# local AI reply with Ollama (https://ollama.com, then `ollama pull llama3.2`)
python main.py --stt whisper --ai ollama
```
Output:
```
[YOU said · 2.3 s] what's the weather like today
[AI] I can't check live weather, but ...
```
To use it in your own code, pass any function to
`AudioProcessor(on_utterance=lambda pcm, fmt: ...)`.

---

## 7. Testing

### Without a phone (simulated, verified)
```powershell
# terminal 1: fake phone, drops the connection every 6 s
python tools\fake_phone.py --port 5600 --drop 6
# terminal 2
python main.py --transport tcp --port 5600 --record test.wav --seconds 20
```
Expected: `CONNECTED` → `LIVE ... SPEAKING` (1 s tone / 1 s silence) →
`DISCONNECTED ... Will reconnect automatically` → `CONNECTED` again, and a valid
`test.wav`.

### With the phone
1. Bluetooth: `python main.py`, tap **Start** on the phone, speak: the level and
   `SPEAKING` react. Turn the phone's Bluetooth off: you get `DISCONNECTED`; turn
   it on again: the app reconnects within ~2 s.
2. USB: same with `--transport usb`; unplug and re-plug (then re-run `adb forward`).
3. Virtual mic: Windows **Settings → System → Sound → Input → CABLE Output →
   Test your microphone** shows the phone's level.

---

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| Phone: "Laptop not reachable over Bluetooth" | `main.py` must be running *before* tapping Start. Check that the laptop is paired (not just "seen"). Toggle Bluetooth on both and retry. |
| Laptop: `Bluetooth error: ... Retrying` | Bluetooth is off or the adapter is busy: Settings → Bluetooth & devices → On. |
| Laptop: `WinRT Bluetooth packages missing` | `pip install -r requirements.txt` inside the venv. |
| Connects, then drops every few seconds | Distance or 2.4 GHz interference: move closer, or use USB. Disable Battery saver / "optimize battery" for the app on the phone. |
| USB: stays on "Waiting for the phone" | Run `adb devices` (must say `device`, not `unauthorized`), then `adb forward tcp:5000 tcp:5000`, and tap Start in **USB cable** mode. |
| No sound in other apps | Use `--virtual-mic "CABLE Input"` and choose **CABLE Output** as the mic in the app. Check names with `--list-devices`. |
| Level stays at -90 dBFS | Another app is using the phone mic (calls, recorder). Close it and press Stop/Start. |
| Always `SPEAKING` (noisy room) | Raise the threshold: `--threshold -32`. |
| Stops when the phone screen turns off | Allow the notification and set the app's battery usage to **Unrestricted**. |
| Choppy audio on Bluetooth | Keep 16 kHz (256 kbit/s); 44.1 kHz needs ~706 kbit/s, which is too much for some phones' RFCOMM. Use USB for 44.1 kHz. |
