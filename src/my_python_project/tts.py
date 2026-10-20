from __future__ import annotations

import threading

# Per-thread mute switch. The web server sets this so browser-side TTS handles
# voice output (avoiding a double voice when the server runs on the same
# machine as the browser). The CLI leaves it unmuted.
_mute = threading.local()


def set_muted(value: bool) -> None:
    _mute.value = bool(value)


def _is_muted() -> bool:
    return getattr(_mute, "value", False)


def speak(text: str, lang: str = "en") -> bool:
    """
    Speak text aloud on the local machine.
    Uses pyttsx3 with a simple PowerShell fallback for Windows.
    """
    if _is_muted():
        return False
    # 1. Try pyttsx3 (Primary)
    try:
        import os

        import pyttsx3
        
        if os.name == 'nt':
            engine = pyttsx3.init('sapi5')
        else:
            engine = pyttsx3.init()
            
        engine.setProperty("rate", engine.getProperty("rate") - 50)
        
        voices = engine.getProperty("voices")
        target_voice = None
        
        if voices:
            if lang == "hi":
                # Search for Hindi/India voices
                for v in voices:
                    v_low = (v.name + v.id).lower()
                    if any(x in v_low for x in ["hindi", "hi-in", "india", "hemant", "kalpana"]):
                        target_voice = v.id
                        break
                if not target_voice:
                    # If Hindi voice not found in SAPI5, we'll try the fallback
                    raise Exception("Hindi voice not found in pyttsx3")
            
            if not target_voice:
                # English voice search
                for v in voices:
                    v_low = (v.name + v.id).lower()
                    if any(x in v_low for x in ["english", "en-us", "en-gb", "david", "zira"]):
                        target_voice = v.id
                        break
            
            if target_voice:
                engine.setProperty("voice", target_voice)

        engine.say(text)
        engine.runAndWait()
        return True
        
    except Exception as e:
        print(f"TTS Notice: {e}. using system fallback...")

        import os
        import shutil
        import subprocess
        import sys

        def _run_speak(cmd: list[str]) -> bool:
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                return r.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                return False

        # 2. Windows: System.Speech via PowerShell
        if os.name == "nt":
            try:
                import base64

                safe_text = text.replace('"', "'")
                ps_script = f"""
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$v = $synth.GetInstalledVoices() | Where-Object {{ $_.VoiceInfo.Culture.Name -match "hi" -or $_.VoiceInfo.Name -match "Hindi" -or $_.VoiceInfo.Name -match "India" }} | Select-Object -First 1
if ($v) {{ $synth.SelectVoice($v.VoiceInfo.Name) }}
$synth.Speak("{safe_text}")
"""
                encoded_script = base64.b64encode(ps_script.encode("utf-16-le")).decode("utf-8")
                if _run_speak(["powershell", "-EncodedCommand", encoded_script]):
                    return True
            except Exception as ps_e:  # noqa: BLE001
                print(f"TTS Windows fallback failed: {ps_e}")

        # 3. macOS: `say` (optional Hindi voice if installed)
        if sys.platform == "darwin" and shutil.which("say"):
            if lang == "hi" and _run_speak(["say", "-v", "Lekha", text]):
                return True
            if _run_speak(["say", text]):
                return True

        # 4. Linux / other Unix: espeak-ng, espeak, or speech-dispatcher
        if os.name == "posix":
            hi_args: list[str] = ["-v", "hi"] if lang == "hi" else []
            for exe in ("espeak-ng", "espeak"):
                path = shutil.which(exe)
                if path:
                    if _run_speak([path, *hi_args, text]):
                        return True
            if shutil.which("spd-say"):
                spd_cmd = ["spd-say", "-l", "hi", text] if lang == "hi" else ["spd-say", text]
                if _run_speak(spd_cmd):
                    return True

    return False
