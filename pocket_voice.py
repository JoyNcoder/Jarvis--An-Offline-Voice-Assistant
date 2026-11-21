"""Pocket TTS voice cloning. create() returns (float32 samples, sample rate) like Kokoro."""
import os
os.environ.setdefault("HF_HUB_OFFLINE", "1")  

import threading
from pathlib import Path
import numpy as np

BASE = Path(__file__).resolve().parent

# voice id -> (label, saved voice state file)
CLONES = {
    "clone_me":     ("My voice",     "my_voice.safetensors"),
    "clone_jarvis": ("Jarvis movie", "jarvis_movie.safetensors"),
}
PRESETS = ["alba", "fantine", "cosette", "eponine", "azelma", "anna", "eve",
           "javert", "charles", "stuart_bell"]
for _n in PRESETS:
    CLONES["q_" + _n] = (_n.replace("_", " ").title(), f"voices/{_n}.safetensors")

_model = None
_states = {}
_lock = threading.Lock()
_error = ""


def is_clone(voice):
    return voice in CLONES


def available_voices():
    """Clone ids whose saved state file exists."""
    return [v for v, (_, f) in CLONES.items() if (BASE / f).exists()]


def label(voice):
    suffix = " (natural)" if voice.startswith("q_") else " (cloned)"
    return CLONES[voice][0] + suffix


def last_error():
    return _error


def _load_model():
    global _model, _error
    if _model is not None:
        return True
    if _error:
        return False
    try:
        from pocket_tts import TTSModel
        _model = TTSModel.load_model()
        return True
    except Exception as error:
        _error = str(error)
        print("Pocket TTS could not load:", error)
        return False


def create(text, voice):
    """Speak `text` in a cloned voice. Returns (samples, sample_rate)."""
    with _lock:
        if not _load_model():
            raise RuntimeError("Pocket TTS is not available: " + _error)
        if voice not in _states:
            path = BASE / CLONES[voice][1]
            _states[voice] = _model.get_state_for_audio_prompt(str(path))
        audio = _model.generate_audio(_states[voice], text)
        samples = audio.detach().cpu().numpy().astype(np.float32).flatten()
        rate = _model.sample_rate
        loud = np.where(np.abs(samples) > 0.01)[0]
        if loud.size:                                   # keep a little padding so words don't clip
            start = max(0, loud[0] - int(0.04 * rate))
            end = min(len(samples), loud[-1] + int(0.15 * rate))
            samples = samples[start:end]
        return samples, rate