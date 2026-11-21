"""Speech-to-text model and transcription."""
from faster_whisper import WhisperModel

whisper = WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=8)

def transcribe(audio):
    segments, _ = whisper.transcribe(
        audio,
        beam_size=1,
        language="en",                    # skips language detection (faster)
        condition_on_previous_text=False,
        without_timestamps=True,          # skips timestamp tokens, a bit faster
    )
    return " ".join(segment.text.strip() for segment in segments).strip()
