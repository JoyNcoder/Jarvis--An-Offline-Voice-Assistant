"""Persistent user settings (currently the selected Kokoro voice)."""
import json
from pathlib import Path

SETTINGS_FILE = Path(__file__).with_name("settings.json")
DEFAULT_VOICE = "af_heart"

def _read_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}

def save_voice(voice):
    data = _read_settings()
    data["voice"] = voice
    try:
        SETTINGS_FILE.write_text(json.dumps(data, indent=2))
    except Exception as error:
        print("Could not save voice:", error)

def load_voice(available_voices=None):
    voice = _read_settings().get("voice")
    if available_voices is None:
        return voice or DEFAULT_VOICE
    return voice if voice in available_voices else DEFAULT_VOICE

DEFAULTS = {
    "voice": DEFAULT_VOICE, "speech_speed": 1.05, "mic_device": "Default microphone",
    "speech_threshold": 0.5, "silence_threshold": 0.85,
    "model_name": "qwen3:8b", "reply_length": "normal", "max_history_messages": 20,
    "accent": "#22d3ee", "compact_always_on_top": True, "start_in_compact": False,
}

def load_all():
    data = dict(DEFAULTS)
    data.update(_read_settings())
    return data

def get_setting(key, default=None):
    return _read_settings().get(key, DEFAULTS.get(key, default))

def save_setting(key, value):
    data = _read_settings()
    data[key] = value
    try:
        SETTINGS_FILE.write_text(json.dumps(data, indent=2))
    except Exception as error:
        print("Could not save setting:", error)
