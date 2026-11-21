"""One turn's thread wiring, conversation history and phrase classification."""
import queue
import threading
import time
import re
import database
from config import MAX_HISTORY_MESSAGES, STOP_PHRASES, FILLER_SOUNDS
from events import emit
from llm import qwen_worker
import audio_in
from tts import tts_worker, clean_for_speech
from playback import player_worker, interrupt_monitor_worker

def save_database_message(conversation_id, role, content, route=None):
    """Save a message without allowing a database problem to crash Jarvis."""
    if conversation_id is None:
        return None
    try:
        return database.add_message(conversation_id, role, content, route=route)
    except Exception as error:
        print(f"Database save error ({role}):", error)
        emit("log", f"Database save error ({role}): {error}")
        return None


def set_route(message_id, route):
    """Tag a saved message with how it was handled. Never raises."""
    if message_id is None:
        return
    try:
        database.set_message_route(message_id, route)
    except Exception as error:
        print("Database route error:", error)

_active = {"interrupt": None, "holder": None, "lock": None}

def interrupt_current():
    """Stop Jarvis's current reply (used by the UI stop button)."""
    event = _active["interrupt"]
    if event is None or event.is_set():
        return False
    event.set()
    lock, holder = _active["lock"], _active["holder"]
    if lock and holder:
        with lock:
            response = holder.get("response")
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
    return True


def _silent_sink(text_q, interrupt_event, played):
    """Muted mode: show the reply as text only. No speech, no audio."""
    while not interrupt_event.is_set():
        try:
            sentence = text_q.get(timeout=0.1)
        except queue.Empty:
            continue
        if sentence is None:
            break
        played.append(sentence)

def run_turn(question, history, t0, vad, route, conversation_id=None, system_prompt=None):

    text_q = queue.Queue()
    audio_q = queue.Queue()
    interrupt_event = threading.Event()
    duck_event = threading.Event()
    playback_started = threading.Event()
    playback_finished = threading.Event()
    response_holder = {"response": None}
    response_lock = threading.Lock()
    interrupted_audio = []
    played = []                     # sentences (or partial sentences) you actually heard
    outcome = {"failed": False}
    _active.update(interrupt=interrupt_event, holder=response_holder, lock=response_lock)

    qwen_thread = threading.Thread(
        target=qwen_worker,
        args=(question, history, route, text_q, interrupt_event, response_holder,
              response_lock, outcome, system_prompt),
        daemon=True,
    )

    if audio_in.is_muted():
        # Chat only: no TTS, no player, no mic monitor
        threads = [
            qwen_thread,
            threading.Thread(target=_silent_sink,
                             args=(text_q, interrupt_event, played), daemon=True),
        ]
    else:
        threads = [
            qwen_thread,
            threading.Thread(target=tts_worker,
                             args=(text_q, audio_q, interrupt_event), daemon=True),
            threading.Thread(target=player_worker,
                             args=(audio_q, interrupt_event, duck_event, playback_started,
                                   playback_finished, t0, played), daemon=True),
            threading.Thread(target=interrupt_monitor_worker,
                             args=(vad, interrupt_event, duck_event, playback_started,
                                   playback_finished, interrupted_audio,
                                   response_holder, response_lock), daemon=True),
        ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    _active.update(interrupt=None, holder=None, lock=None)

    # Record the exchange using only what you actually heard. If you cut Jarvis
    # off mid-sentence, the history holds the part you heard, not the full text.
    heard = " ".join(played).strip()

    # The user message is saved by main() before this turn starts.
    if heard:
        save_database_message(conversation_id, "assistant", heard,
                              route="tool" if route["answer"] else "chat")

    if not outcome["failed"]:
        history.append({"role": "user", "content": question})
        if heard:
            history.append({"role": "assistant", "content": heard})
        del history[:-MAX_HISTORY_MESSAGES]
        while history and history[0]["role"] != "user":
            history.pop(0)

    return interrupted_audio[0] if interrupted_audio else None
def is_goodbye(text):
    cleaned = text.lower().replace("'", "").replace("\u2019", "")
    cleaned = re.sub(r"[^a-z\s]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # Remove common words Whisper may include around the command.
    cleaned = re.sub(r"^(hey|hi|okay|ok|please)\s+", "", cleaned)
    cleaned = re.sub(r"\s+jarvis$", "", cleaned)

    goodbye_phrases = {
        "goodbye", "good bye", "bye", "bye bye", "see you", "see ya",
        "see you later", "dismissed", "disengage", "talk to you later",
        "catch you later", "farewell", "i am leaving", "im leaving",
        "thats all", "thats it",
    }
    return cleaned in goodbye_phrases
def is_stop_or_filler(text):
    """True for 'stop', 'wait', 'never mind', 'hmm'... : no new reply is needed."""
    cleaned = text.lower().replace("-", " ").replace("'", "")
    cleaned = re.sub(r"[^a-z\s]", "", cleaned)
    cleaned = re.sub(r"\bjarvis\b", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned in STOP_PHRASES or cleaned in FILLER_SOUNDS
