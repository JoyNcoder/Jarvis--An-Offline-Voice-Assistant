"""Kokoro text-to-speech, voice selection and playback synthesis."""
import difflib
import re
import time
import unicodedata
import queue
import threading
import pocket_voice
import numpy as np
import sounddevice as sd
from kokoro_onnx import Kokoro
from config import SPEECH_SPEED, SENTENCE_PAUSE
from settings_store import load_voice, save_voice, DEFAULT_VOICE
from events import emit

kokoro = Kokoro("kokoro-v1.0.onnx", "voices-v1.0.bin")
ALL_VOICES = pocket_voice.available_voices() + sorted(kokoro.get_voices())
_voice = load_voice(ALL_VOICES)

def get_voice():
    return _voice

def set_voice(voice):
    global _voice
    if voice not in ALL_VOICES:
        raise ValueError(f"Unknown voice: {voice}")
    _voice = voice
    save_voice(voice)
    emit("settings_changed", {"key": "voice", "value": voice})

def clean_for_speech(text):
    """Remove emoji, symbols and joiners so Kokoro never reads them out."""
    cleaned = "".join(
        char for char in text
        if not unicodedata.category(char).startswith("S")
        and char not in ("\u200d", "\ufe0f", "\ufe0e")
    )
    return re.sub(r"\s+", " ", cleaned).strip()
def voice_lang(voice):
    return "en-gb" if voice.startswith("b") else "en-us"

def synth(text, voice=None):
    """(float32 samples, sample rate) from Kokoro or a cloned voice."""
    voice = voice or get_voice()
    if pocket_voice.is_clone(voice):
        try:
            return pocket_voice.create(text, voice)
        except Exception as error:
            print("Cloned voice failed, using Kokoro for this sentence:", error)
            voice = DEFAULT_VOICE
    return kokoro.create(text, voice=voice, speed=SPEECH_SPEED, lang=voice_lang(voice))

def voice_name(voice):
    return voice.split("_", 1)[1]
LANGUAGES = {"a": "american", "b": "british", "e": "spanish", "f": "french", "h": "hindi",
             "i": "italian", "j": "japanese", "p": "portuguese", "z": "chinese", "c": "cloned", "q": "natural"}
def display_label(voice):
    """'af_heart' -> 'Heart (American, female)'"""
    if pocket_voice.is_clone(voice):
        return pocket_voice.label(voice)
    lang = LANGUAGES.get(voice[0], "").capitalize()
    sex = "female" if voice[1] == "f" else "male"
    return f"{voice_name(voice).capitalize()} ({lang}, {sex})"
SPOKEN = {}      # voice id -> the unique name Jarvis says
_ALIASES = {}    # everything you may say -> voice id
for _v in ALL_VOICES:
    if pocket_voice.is_clone(_v):
        continue
    _base, _lang = voice_name(_v), LANGUAGES.get(_v[0], "")
    SPOKEN[_v] = _base if _base not in _ALIASES else f"{_base} {_lang}"
    _ALIASES.setdefault(_base, _v)
    _ALIASES[f"{_base} {_lang}"] = _v

# Cloned voices. Aliases must be what is left AFTER voice_commands strips the words
# "voice" and "jarvis", so "use movie jarvis" arrives here as just "movie".
SPOKEN["clone_me"] = "your clone"
SPOKEN["clone_jarvis"] = "movie jarvis"
for _alias in ("my clone", "your clone", "clone", "my"):
    _ALIASES[_alias] = "clone_me"
for _alias in ("movie", "iron man"):
    _ALIASES[_alias] = "clone_jarvis"
for _v in ALL_VOICES:
    if _v.startswith("q_"):
        _n = _v[2:].replace("_", " ")
        SPOKEN[_v] = _n
        _ALIASES[_n] = _v
        _ALIASES[_n.split()[0]] = _v      # "stuart bell" also answers to "stuart"

def find_voice(spoken):
    """'nicole', 'nicole voice', 'santa portuguese', even slightly misheard -> voice id."""
    spoken = re.sub(r"\b(?:the|voice)\b", " ", spoken.lower())
    spoken = re.sub(r"\s+", " ", spoken).strip()
    if spoken in _ALIASES:
        return _ALIASES[spoken]
    close = difflib.get_close_matches(spoken, list(_ALIASES), n=1, cutoff=0.7)
    return _ALIASES[close[0]] if close else None

def voices_in_language(word):
    letter = next((k for k, v in LANGUAGES.items() if v == word), None)
    return [v for v in ALL_VOICES if v[0] == letter]
def say_now(text, label=None):
    """Speak one short line right away. `label` is printed the moment audio starts."""
    emit("reply_chunk", clean_for_speech(text))
    emit("state", "speaking")
    samples, rate = synth(clean_for_speech(text))
    sd.play(samples, rate)          # starts playing and returns immediately
    if label:
        print(label)
    sd.wait()

def preview_voice(voice):
    """Say a short sample in `voice` without changing the saved voice."""
    if voice not in ALL_VOICES:
        return
    name = SPOKEN[voice].title() if pocket_voice.is_clone(voice) else voice_name(voice).capitalize()
    samples, rate = synth(f"Hi, I'm {name}.", voice)
    sd.play(samples, rate)
    sd.wait()

def tts_worker(text_q, audio_q, interrupt_event):

    try:
        while not interrupt_event.is_set():
            sentence = text_q.get()
            if sentence is None:
                break
            if interrupt_event.is_set():
                break

            start = time.perf_counter()
            samples, sample_rate = synth(sentence)
            print(f"Kokoro: {time.perf_counter() - start:.2f}s for \"{sentence[:40]}\"")
            emit("log", f"Kokoro synthesized: {sentence[:40]}")

            if interrupt_event.is_set():
                break

            # Each item carries its sentence text so the player can report
            # what was actually heard. The pause item has no text.
            audio_q.put((np.asarray(samples, dtype=np.float32).flatten(), sample_rate, sentence))
            audio_q.put((np.zeros(int(sample_rate * SENTENCE_PAUSE), dtype=np.float32), sample_rate, None))

    except Exception as error:
        if not interrupt_event.is_set():
            print("\nKokoro error:", error)

    finally:
        audio_q.put(None)
