# my_python_project — "Sara" voice assistant

A bilingual (English + Hindi/Hinglish) voice assistant with a cyberpunk web
dashboard. Sara listens (mic or text), runs system commands, fetches **live**
weather/time/news, sends WhatsApp messages, and falls back to a real AI brain
for anything else. Built on a clean `src` layout with pytest + ruff.

## Quickstart (Windows PowerShell)

Create and activate a virtual environment:

```powershell
cd "e:\New WOrk Space\python\my_python_project"
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install with the extras you want (see the table below). For the full experience:

```powershell
python -m pip install -U pip
python -m pip install -e ".[dev,web,neural,time,whatsapp,ai]"
```

Run the web dashboard (opens automatically in your browser):

```powershell
python -m my_python_project serve
# -> http://127.0.0.1:1309/sara
```

### Access from your phone / another laptop

By default the dashboard is reachable only on this computer. Add `--lan` to make
it reachable from other devices on the **same Wi-Fi**:

```powershell
python -m my_python_project serve --lan
```

It prints both URLs — a local one and a `http://<this-PC-IP>:1309/sara` you can
open on your phone or laptop. Both devices must be on the same network; allow
Python through the Windows firewall if prompted.

### Lock it with a PIN

When serving on the network, protect the dashboard with a PIN so only you can
use it. Set `SARA_PIN` in your `.env`:

```
SARA_PIN=1234
```

Now every visitor must enter the PIN to unlock the dashboard (a login cookie
keeps you signed in for 30 days; visit `/logout` to sign out). Without
`SARA_PIN` the dashboard is open — fine for local-only use, **not** recommended
with `--lan`. The `serve` command prints whether the PIN lock is ON or OFF.

Or use the CLI:

```powershell
python -m my_python_project --help
python -m my_python_project hello --name "Nabi"
pytest
```

## The web dashboard (`serve`)

The main interface is **SARA_OS** at `http://127.0.0.1:1309/sara` (the old
`/ui` path redirects there). From there you
can type a command in the terminal box, press the mic to speak, or use the
"Neural_Net_Link" box to ask the AI directly. The dashboard is **responsive** —
it reflows for tablets and phones.

### First-run onboarding (name + gender)

On every server start, Sara asks for your **name** and **gender** (saved in the
browser). She then:

- Greets you by name with a time-of-day **welcome banner** — *Good morning /
  afternoon / evening* (Hindi: *सुप्रभात / नमस्कार / शुभ संध्या*), based on the
  language selected in the UI dropdown.
- Speaks in the **opposite-gender** voice: a male user hears a female voice and
  vice-versa.

Click the **SARA_OS** title any time to change your name/voice. A page refresh
within the same server run keeps your profile; restarting the server asks again.

### AI visual

When the camera is off, the video panel shows Sara's animated AI core — a
glowing orb with rotating rings, orbiting particles and a voice wave. It reacts
to Sara: listening (cyan), thinking (violet), speaking (the wave follows her
voice).

### Voice output — Neural TTS

Sara replies out loud. With the `neural` extra she uses Microsoft Edge's
high-quality **neural voices** (free, no API key) — `en-IN`/`hi-IN` male & female
— served from the app and played in the browser. Without it (or offline), she
falls back to the browser's built-in voices. Toggle voice with the 🔊 button.

### Voice input

Click the mic to speak (Chrome/Edge). A **Stop Listening** button on the
listening overlay cancels mic input cleanly.

## Bilingual: Hinglish → Hindi

Type in romanized Hindi (Hinglish) and Sara replies in **Hindi**, in a Hindi
neural voice. Detection covers Devanagari plus common Hinglish words (`kya`,
`kaise`, `mausam`, `samay`, `khabar`, `naam`, …). Plain English stays English.

## Live data (real, not the LLM)

The AI brain has no real-time data, so these are answered from live sources
**before** falling back to the AI:

| Ask (examples) | Source | Extra needed |
| --- | --- | --- |
| `Darbhanga ka mausam`, `weather in London` | Open-Meteo (free, no key) | none |
| `Delhi, Mumbai aur Patna ka mausam`, `sabhi shahar ka mausam` | Open-Meteo, many cities at once | none |
| `nifty kitna hai`, `reliance aur tcs ka share price`, `apple stock` | Yahoo Finance (free, no key) | none |
| `abhi kya time hai`, `time in New York` | local clock / city timezone | `time` (for city timezones) |
| `aaj ki taza khabar`, `cricket ki news` | Google News RSS (free, no key) | none |

City timezones need the `time` extra (`tzdata`) on Windows. Weather/time accept
a city name; news accepts an optional topic. With no city, weather uses
`SARA_CITY` from `.env`. "Sabhi shahar" shows 12 major Indian cities.

Stocks understand indices (Nifty, Sensex, Bank Nifty, Nasdaq, Dow), gold/silver,
Bitcoin, the dollar rate, company names, and tickers (`TCS.NS`, `AAPL`). With no
name ("share market kaisa hai") Sara gives Nifty + Sensex.

## Email

```
abc@gmail.com ko email bhejo kal meeting 5 baje hai
boss ko email karo ki main kal chhutti par rahunga     # needs email_contacts.json
```

Claude writes the subject and body, Sara shows the draft, and sends it **only
after you say "haan"/"yes"** (anything else cancels). Setup in `.env` (Gmail):

```
SMTP_USER=you@gmail.com
SMTP_PASSWORD=abcd efgh ijkl mnop   # a Gmail App Password, not your normal password
```

Create the App Password at Google Account → Security → 2-Step Verification →
App passwords. Save names in `email_contacts.json`:
`{"boss": "boss@company.com"}`. Other providers: set `SMTP_HOST`/`SMTP_PORT`.

## WhatsApp

Open WhatsApp or send a message to a phone number / saved contact.

```
whatsapp kholo
open whatsapp
9876543210 ko message karo hello kaise ho
send whatsapp to 9123456789 meeting at 5 pm
mummy ko message karo aa raha hu        # needs contacts.json
```

How it works:

- Prefers the **desktop app deep link** (`whatsapp://send?...`) which opens the
  native app directly on the chat, focused, with the text pre-filled; falls back
  to `https://wa.me/...` (WhatsApp Web) if the app isn't available.
- 10-digit numbers default to India (**+91**); override with
  `WHATSAPP_COUNTRY_CODE`.
- Names are resolved from an optional `contacts.json` in the project folder —
  copy `contacts.json.example` and fill in `{"name": "91XXXXXXXXXX"}`.

**Auto-send** (actually press Send): install the `whatsapp` extra (`pyautogui`).
For safety Sara only presses **Enter while WhatsApp is the foreground window**,
so a message is never typed into another app if you click away. Without the
extra, the chat opens pre-filled and you press Enter yourself.

Requires the WhatsApp desktop app (or WhatsApp Web) to be **logged in**.

## Other commands

| Say | Action |
| --- | --- |
| `open camera` / `कैमरा खोलो` | Open the webcam (needs `camera` extra) |
| `close camera` | Stop the camera |
| `create folder <name>` / `folder banao` | Make a folder on the Desktop by default |
| `Desktop par folder banao <name>`, `create folder in D drive named X` | Make a folder at a named location |
| `create file <name>` / `file banao` | Make a file (Desktop by default) |
| `Desktop dikhao`, `D drive ke folders dikhao`, `list C:\Users` | List a folder's contents |
| `open youtube` | Open YouTube |
| `open notepad++`, `open intellij`, `open openvpn` | Launch apps |
| `hello`, `namaste`, `who are you`, `welcome` | Greetings / identity |

## Sara's own trained model (intent model)

Sara has a small AI model of her own that learns **what you mean** — open an
app, close an app, shutdown, weather, or just a question for the AI. It stops
questions that only *mention* an action word from triggering it:

| You say | Before | Now |
| --- | --- | --- |
| `exit code 1 kya hai` | tried to close an app | asks the AI |
| `shutdown kaise karte hain` | armed a shutdown | asks the AI |
| `notepad band kr do` | closed Notepad | closed Notepad |

It's trained on built-in English/Hinglish/Hindi examples **plus the apps
installed on your PC** (from the Start Menu), runs fully offline, and trains in
a few seconds on the CPU — no GPU or extra packages.

```powershell
python -m my_python_project train --evaluate     # retrain (e.g. after installing new apps)
python -m my_python_project predict "chrome band karo"
python -m my_python_project teach "tv band karo" close_app   # teach your own phrase
```

`teach` saves your phrases to `my_training_data.json` and retrains. Intents:
`open_app`, `close_app`, `power`, `create_fs`, `list_fs`, `whatsapp`, `camera`,
`weather`, `time`, `news`, `game`, `animation`, `greeting`, `identity`, `chat`.
The trained model is saved in `models/intent_model.json`.

## Offline AI brain (Ollama)

With [Ollama](https://ollama.com) installed, Sara can answer questions with no
internet and no API key:

```powershell
winget install Ollama.Ollama
ollama pull gemma3:4b
```

Sara uses Ollama automatically when no API key is set, and as a **fallback**
when Claude fails (no internet, invalid key, server busy). To always use it,
set `AI_PROVIDER=ollama`. Choose another model with `OLLAMA_MODEL`. On a laptop
without a graphics card, replies take a few seconds.

## AI brain — Claude

Anything Sara doesn't recognise is answered by **Claude (Anthropic)**, with the
offline Ollama brain as a backup (see above).

1. Install the extra: `python -m pip install -e ".[ai]"`
2. Create a key at https://console.anthropic.com/settings/keys and add it to
   `.env` (git-ignored):

   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```

   > A Claude Pro subscription does **not** include API access — the API is
   > billed separately (prepaid credit on console.anthropic.com).

The default model is `claude-sonnet-5` (override with `ANTHROPIC_MODEL`).
ChatGPT still works as an alternative (`OPENAI_API_KEY`, `AI_PROVIDER=openai`).
Without a working key, Sara answers with Ollama; without either, the other
commands keep working.

## Optional extras

| Extra | Installs | Enables |
| --- | --- | --- |
| `dev` | pytest, ruff | tests + linting |
| `web` | fastapi, uvicorn | the `serve` web dashboard |
| `neural` | edge-tts | high-quality neural voices |
| `tts` | pyttsx3 | local (CLI) text-to-speech |
| `time` | tzdata | city-specific timezones |
| `whatsapp` | pyautogui | WhatsApp auto-send |
| `voice` | sounddevice, vosk | offline mic → text (CLI `listen`) |
| `camera` | opencv-python | webcam commands |
| `notify` | winotify | Windows toast notifications |
| `ai` / `chatgpt` | anthropic / openai | AI brain provider (Claude / ChatGPT) |

## CLI commands

```powershell
python -m my_python_project hello --name "Nabi"
python -m my_python_project serve --host 127.0.0.1 --port 1309
python -m my_python_project serve --lan   # reachable from phone/laptop on the same Wi-Fi
python -m my_python_project listen --vosk-model "path\to\vosk-model" --seconds 4
```

`listen` does offline mic → text with Vosk (needs the `voice` extra and a
downloaded model from https://alphacephei.com/vosk/models), then runs the
recognised command once.

## Environment variables

All optional; set in `.env` (git-ignored) or the real environment.

| Variable | Default | Purpose |
| --- | --- | --- |
| `SARA_PIN` | — | lock the web dashboard behind a PIN login |
| `AI_PROVIDER` | auto | `anthropic` / `openai` / `ollama` |
| `OLLAMA_MODEL` / `OLLAMA_HOST` | `gemma3:4b` / `http://127.0.0.1:11434` | offline AI model and server |
| `SARA_INTENT_MODEL` | `1` | `0` = turn off the trained intent model |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | — | AI brain keys |
| `ANTHROPIC_MODEL` / `OPENAI_MODEL` | `claude-sonnet-5` / `gpt-4o-mini` | override the model |
| `SARA_CITY` | — | default city for weather |
| `SMTP_USER` / `SMTP_PASSWORD` | — | email account + App Password |
| `SMTP_HOST` / `SMTP_PORT` | `smtp.gmail.com` / `465` | email server |
| `SARA_USER_NAME` | email name | name used to sign emails |
| `WHATSAPP_COUNTRY_CODE` | `91` | default country code for 10-digit numbers |
| `WHATSAPP_AUTOSEND` | `1` | `0` = open the chat pre-filled but don't press Send |
| `WHATSAPP_SEND_DELAY` | `6` (app) / `12` (web) | seconds to wait before pressing Enter |
| `WHATSAPP_REQUIRE_FOCUS` | `1` | `0` = press Enter even if WhatsApp isn't focused |

## Tests & linting

```powershell
pytest
ruff check .
```
