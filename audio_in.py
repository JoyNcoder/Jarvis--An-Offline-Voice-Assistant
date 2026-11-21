"""Microphone input, VAD and utterance recording."""
from collections import deque
import time
import numpy as np
import onnxruntime as ort
import sounddevice as sd
from config import (MIC_DEVICE, SAMPLE_RATE, CHUNK, SPEECH_THRESHOLD, SILENCE_THRESHOLD,
                    SILENCE_LIMIT, MIN_SPEECH_CHUNKS, PRE_ROLL_CHUNKS, MAX_RECORD_CHUNKS)
from events import emit

_mic_muted = False

def set_muted(muted):
    global _mic_muted
    _mic_muted = bool(muted)

def is_muted():
    return _mic_muted

_last_level = 0.0

def emit_level(chunk):
    """Send an audio level (0..1) to the UI, about 15 times per second."""
    global _last_level
    now = time.perf_counter()
    if now - _last_level < 0.066:
        return
    _last_level = now
    rms = float(np.sqrt(np.mean(np.square(chunk, dtype=np.float32))))
    emit("level", min(1.0, rms * 10))     # raise or lower 10 to taste

def _pick_mic(device):
    """Return `device` if it is a real input, else None (system default)."""
    try:
        sd.query_devices(device, "input")
        return device
    except Exception:
        print(f"MIC_DEVICE {device} is not an input device; using the system default mic.")
        return None
MIC_DEVICE = _pick_mic(MIC_DEVICE)

def mic_available():
    """True if a working input device exists."""
    try:
        sd.query_devices(MIC_DEVICE, "input")
        return True
    except Exception:
        return False

class SileroVAD:
    """Silero VAD (ONNX). Call it with one 512-sample chunk to get a speech probability."""

    def __init__(self, path="silero_vad.onnx"):
        self.session = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
        self.reset()

    def reset(self):
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, 64), dtype=np.float32)   # last 64 samples of previous chunk

    def __call__(self, chunk):
        x = np.concatenate((self.context, chunk.reshape(1, -1)), axis=1)
        outputs = self.session.run(
            None,
            {
                "input": x,
                "state": self.state,
                "sr": np.array(SAMPLE_RATE, dtype=np.int64),
            },
        )
        self.state = outputs[1]
        self.context = x[:, -64:]
        return float(outputs[0][0][0])
def record_until_silence(vad, abort=None):
    """Wait for speech, record it, and stop after a short silence."""

    if _mic_muted:
        time.sleep(0.1)
        return np.array([], dtype=np.float32)

    vad.reset()
    print("\nJarvis is listening...")
    emit("state", "idle")

    pre_roll = deque(maxlen=PRE_ROLL_CHUNKS)
    recorded = []
    started = False
    speech_chunks = 0
    silent_run = 0

    # The mic stream is opened fresh each turn on purpose: audio that piled up
    # while Jarvis was speaking (including his own voice) is never read.
    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        device=MIC_DEVICE,
        blocksize=CHUNK,
    ) as stream:

        for _ in range(MAX_RECORD_CHUNKS):
            if _mic_muted or (abort and abort()):
                return np.array([], dtype=np.float32)

            chunk, overflowed = stream.read(CHUNK)
            chunk = chunk.flatten()
            probability = vad(chunk)
            if started or probability >= 0.3:
                emit_level(chunk)

            if not started:
                pre_roll.append(chunk)
                if probability >= SPEECH_THRESHOLD:
                    started = True
                    emit("state", "listening")      # <- speech detected
                    recorded.extend(pre_roll)
                    speech_chunks = 1
                continue

            recorded.append(chunk)

            if probability >= SPEECH_THRESHOLD:
                speech_chunks += 1

            if probability < SILENCE_THRESHOLD:
                silent_run += 1
            else:
                silent_run = 0

            if silent_run >= SILENCE_LIMIT:
                break

    if not started:
        return np.array([], dtype=np.float32)

    if speech_chunks < MIN_SPEECH_CHUNKS:
        print("(Too short, ignoring.)")
        return np.array([], dtype=np.float32)

    return np.concatenate(recorded)
