"""Live settings: check the value, save it to settings.json, apply it to the running Jarvis."""
import re
import sys
import requests
import config
import threading
import settings_store

CHUNK_SECONDS = config.CHUNK / config.SAMPLE_RATE      # 0.032 s per audio chunk

RANGES = {"speech_speed": (0.7, 1.4), "speech_threshold": (0.2, 0.9),
          "silence_threshold": (0.4, 1.6), "max_history_messages": (4, 30)}
CHOICES = {"reply_length": ("short", "normal", "long")}
BOOLS = {"compact_always_on_top", "start_in_compact"}
HINTS = {"short": "Answer in one to three short spoken sentences.",
         "normal": "",
         "long": "Answer in detail, about six to ten spoken sentences, in plain spoken words."}
TOKENS = {"short": 150, "normal": 300, "long": 500}


def clean(key, value):
    """Return a safe value or raise ValueError."""
    if key in RANGES:
        lo, hi = RANGES[key]
        try:
            number = min(hi, max(lo, float(value)))
        except (TypeError, ValueError):
            raise ValueError("not a number")
        return int(round(number)) if key == "max_history_messages" else number
    if key in CHOICES:
        if value not in CHOICES[key]:
            raise ValueError("not an allowed choice")
        return value
    if key in BOOLS:
        if not isinstance(value, bool):
            raise ValueError("not true/false")
        return value
    if key == "accent":
        if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            raise ValueError("bad colour")
        return value
    if key == "model_name":
        if not isinstance(value, str) or not re.fullmatch(r"[\w.\-:/]{1,60}", value):
            raise ValueError("bad model name")
        return value
    if key == "mic_device":
        return str(value)[:100]
    if key == "voice":
        import tts
        if value not in tts.ALL_VOICES:
            raise ValueError("unknown voice")
        return value
    raise ValueError("unknown setting")


def _push(name, value, modules=()):
    """Set config.NAME and the copy that other modules made with `from config import NAME`."""
    setattr(config, name, value)
    for module_name in modules:
        module = sys.modules.get(module_name)
        if module is not None:
            setattr(module, name, value)


def _swap_model(old, new):
    """Unload the old model from Ollama, then load the new one (runs in the background)."""
    try:
        if old and old != new:
            requests.post(config.OLLAMA_URL.replace("/api/chat", "/api/generate"),
                          json={"model": old, "keep_alive": 0}, timeout=30)
        requests.post(config.OLLAMA_URL,
                      json={"model": new, "messages": [{"role": "user", "content": "hi"}],
                            "think": False, "stream": False, "keep_alive": -1,
                            "options": {"num_predict": 1}},
                      timeout=180)
    except Exception as error:
        print(f"[settings] model swap failed: {error}")

def list_mics():
    """Real input devices, from the default host API only (avoids duplicate entries)."""
    import sounddevice as sd
    host = sd.default.hostapi
    return [d["name"] for d in sd.query_devices()
            if d["max_input_channels"] > 0 and d["hostapi"] == host]


def _mic_index(name):
    import sounddevice as sd
    host = sd.default.hostapi
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and d["hostapi"] == host and d["name"] == name:
            return i
    return None                      # "Default microphone" or an unplugged device

def apply(key, value):
    if key == "voice":
        import tts
        tts.set_voice(value)                       # also saves it
    elif key == "speech_speed":
        _push("SPEECH_SPEED", value, ("tts", "llm"))
    elif key == "speech_threshold":
        _push("SPEECH_THRESHOLD", value, ("audio_in",))
        _push("SILENCE_THRESHOLD", max(0.15, round(value - 0.15, 2)), ("audio_in", "playback"))
    elif key == "silence_threshold":
        _push("SILENCE_LIMIT", max(5, round(value / CHUNK_SECONDS)), ("audio_in", "playback"))
    elif key == "max_history_messages":
        _push("MAX_HISTORY_MESSAGES", int(value), ("turn",))
    elif key == "mic_device":
        _push("MIC_DEVICE", _mic_index(value), ("audio_in", "playback"))
    elif key == "model_name":
        old = config.MODEL_NAME
        _push("MODEL_NAME", value, ("llm", "jarvis_memory", "jarvis_summaries"))
        if old != value:
            threading.Thread(target=_swap_model, args=(old, value), daemon=True).start()
    elif key == "reply_length":
        config.REPLY_LENGTH_HINT = HINTS[value]
        config.OLLAMA_OPTIONS["num_predict"] = TOKENS[value]   # same dict llm.py uses
    # accent, mic_device, compact_* : saved only


def _model_installed(name):
    try:
        reply = requests.get("http://127.0.0.1:11434/api/tags", timeout=2).json()
        names = {m["name"] for m in reply.get("models", [])}
    except Exception:
        return True                                # cannot check: allow it
    return name in names or f"{name}:latest" in names


def set_setting(key, value):
    """Called by the UI. Returns the cleaned value, raises ValueError if rejected."""
    value = clean(key, value)
    if key == "model_name" and not _model_installed(value):
        raise ValueError(f"model {value} is not installed (run: ollama pull {value})")
    if key != "voice":
        settings_store.save_setting(key, value)
    apply(key, value)
    return value


def apply_all():
    """Apply everything saved in settings.json (called once when Jarvis starts)."""
    for key, value in settings_store.load_all().items():
        if key == "voice":
            continue                               # tts.py loads it itself
        try:
            apply(key, clean(key, value))
        except Exception as error:
            print(f"[settings] ignored saved {key}: {error}")