"""Audio playback and interruption (barge-in) coordination."""
import queue
import threading
import time
import numpy as np
import sounddevice as sd
from collections import deque
from config import (SAMPLE_RATE, CHUNK, INTERRUPT_SPEECH_THRESHOLD,
                    DUCK_CHUNKS, INTERRUPT_CONFIRM_CHUNKS, INTERRUPT_PRE_ROLL_CHUNKS,
                    MAX_RECORD_CHUNKS, SILENCE_THRESHOLD, SILENCE_LIMIT, PLAYBACK_CHUNK,
                    SENTENCE_END)
from events import emit
from tts import tts_worker, clean_for_speech
from audio_in import MIC_DEVICE, emit_level, is_muted

def player_worker(audio_q, interrupt_event, duck_event, playback_started,
                  playback_finished, t0, played):

    stream = None

    try:
        first = True
        while not interrupt_event.is_set():
            try:
                item = audio_q.get(timeout=0.1)
            except queue.Empty:
                continue

            if item is None:
                break

            samples, sample_rate, sentence = item

            if first:
                print(f"Time to first audio: {time.perf_counter() - t0:.2f}s (counted from end of your speech)")
                emit("latency", int((time.perf_counter() - t0) * 1000))
                emit("state", "speaking")
                first = False

            if stream is None:
                # latency="low" keeps the output buffer small, so audio stops
                # soon after we stop writing.
                stream = sd.OutputStream(samplerate=sample_rate, channels=1,
                                         dtype="float32", latency="low")
                stream.start()
                playback_started.set()

            # Write short blocks instead of a whole sentence. This bounds how
            # long Jarvis can continue playing after an interruption is detected.
            written = 0
            for offset in range(0, len(samples), PLAYBACK_CHUNK):
                # Ducked = the monitor heard something that might be you.
                # Stop writing (the stream plays silence) until it decides.
                while duck_event.is_set() and not interrupt_event.is_set():
                    time.sleep(0.01)
                if interrupt_event.is_set():
                    break
                block = samples[offset:offset + PLAYBACK_CHUNK]
                emit_level(block)
                stream.write(block.reshape(-1, 1))
                written = offset + len(block)

            # Remember what was actually heard (for the conversation history).
            if sentence:
                if written >= len(samples):
                    played.append(sentence)
                elif written > 0:
                    words = sentence.split()
                    keep = int(len(words) * written / len(samples))
                    if keep:
                        played.append(" ".join(words[:keep]))

            if interrupt_event.is_set():
                break

    except Exception as error:
        if not interrupt_event.is_set():
            print("\nAudio playback error:", error)

    finally:
        playback_finished.set()
        if stream is not None:
            try:
                if interrupt_event.is_set():
                    stream.abort()      # drop buffered audio immediately
                else:
                    stream.stop()       # let the last words finish playing
            except Exception:
                pass
            finally:
                stream.close()
def interrupt_monitor_worker(vad, interrupt_event, duck_event, playback_started,
                             playback_finished, interrupted_audio,
                             response_holder, response_lock):
    """Listen while Jarvis talks.

    Speech score goes up 1 per speech chunk and down 2 per non-speech chunk:
      score >= DUCK_CHUNKS               -> pause Jarvis (can still resume)
      score == 0                         -> resume Jarvis
      score >= INTERRUPT_CONFIRM_CHUNKS  -> stop Jarvis for good, capture your speech
    """

    # Wait for the first audio block, but don't hang if the reply fails
    # before playback ever begins.
    while not playback_started.wait(timeout=0.1):
        if playback_finished.is_set():
            return

    vad.reset()
    pre_roll = deque(maxlen=INTERRUPT_PRE_ROLL_CHUNKS)
    recorded = []
    detected = False
    score = 0
    silent_run = 0

    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            device=MIC_DEVICE,
            blocksize=CHUNK,
        ) as mic_stream:

            while True:
                # Playback ended normally and nobody is talking: nothing to do.
                if not detected and playback_finished.is_set() and score < DUCK_CHUNKS:
                    break
                if len(recorded) >= MAX_RECORD_CHUNKS:
                    break

                chunk, overflowed = mic_stream.read(CHUNK)
                chunk = chunk.flatten()
                probability = vad(chunk)

                if not detected:
                    pre_roll.append(chunk)

                    if probability >= INTERRUPT_SPEECH_THRESHOLD:
                        score += 1
                    else:
                        score = max(0, score - 2)

                    # Pause quickly, resume only once the score has fully decayed
                    # (hysteresis, so playback does not stutter on VAD dips).
                    if score >= DUCK_CHUNKS and not duck_event.is_set():
                        duck_event.set()
                    elif score == 0 and duck_event.is_set():
                        duck_event.clear()

                    reply_over = playback_finished.is_set()
                    if score >= INTERRUPT_CONFIRM_CHUNKS or (reply_over and score >= DUCK_CHUNKS):
                        detected = True
                        recorded = list(pre_roll)

                        if reply_over:
                            # You started talking just as Jarvis finished: keep it.
                            print("\nSpeech caught at the end of the reply.")
                        else:
                            interrupt_event.set()
                            print("\nVoice interruption detected. Stopping Jarvis...")
                            emit("state", "interrupted")

                            # Closing the active HTTP response interrupts Ollama's stream.
                            with response_lock:
                                response = response_holder.get("response")
                            if response is not None:
                                try:
                                    response.close()
                                except Exception:
                                    pass
                    continue

                recorded.append(chunk)

                if probability < SILENCE_THRESHOLD:
                    silent_run += 1
                else:
                    silent_run = 0

                if silent_run >= SILENCE_LIMIT:
                    break

    except Exception as error:
        print("\nInterruption microphone error:", error)
        emit("error", "Lost access to the microphone")      # <- orb goes red 
        if not playback_finished.is_set():
            interrupt_event.set()               # stop speaking
            with response_lock:
                response = response_holder.get("response")
            if response is not None:
                try:
                    response.close()            # stop the Ollama stream too
                except Exception:
                    pass

    finally:
        # Never leave the player paused if the monitor exits for any reason.
        duck_event.clear()

    if detected and recorded:
        interrupted_audio.append(np.concatenate(recorded))
        print("Speech captured. Processing your new command...")
        emit("state", "processing_interruption")
def say_interruptible(text, vad, t0=None, label=None):
    """Speak a canned reply (memory recall, summaries) with full interruption support.

    Returns (interrupting_audio, heard):
      interrupting_audio  what you said over him (to process next), or None
      heard               the part of the reply you actually heard
    """
    if t0 is None:
        t0 = time.perf_counter()

    sentences = [s for s in SENTENCE_END.split(clean_for_speech(text)) if s.strip()]
    if not sentences:
        return None, ""

    if is_muted():
        for sentence in sentences:
            emit("reply_chunk", sentence)
        emit("state", "idle")
        return None, " ".join(sentences)

    text_q = queue.Queue()
    audio_q = queue.Queue()
    for sentence in sentences:
        text_q.put(sentence)
        emit("reply_chunk", sentence)
    text_q.put(None)

    interrupt_event = threading.Event()
    duck_event = threading.Event()
    playback_started = threading.Event()
    playback_finished = threading.Event()
    response_holder = {"response": None}      # no Qwen stream to cancel here
    response_lock = threading.Lock()
    interrupted_audio = []
    played = []

    threads = [
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
    if label:
        def _announce():
            # Print the text the moment audio starts (or when it gives up).
            while not playback_started.wait(timeout=0.1):
                if playback_finished.is_set():
                    break
            print(label)
        threads.append(threading.Thread(target=_announce, daemon=True))

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    return (interrupted_audio[0] if interrupted_audio else None), " ".join(played).strip()
