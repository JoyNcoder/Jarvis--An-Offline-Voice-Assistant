"""Central configuration for Jarvis."""
import re

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
OLLAMA_OPTIONS = {
    "num_predict": 300,
    "num_ctx": 4096,
    "temperature": 0.7,
    "top_p": 0.8,
    "top_k": 20,
    "repeat_penalty": 1.05,
}
MODEL_NAME = "qwen3:4b-instruct"
#MODEL_NAME = "qwen3:8b"

SYSTEM_PROMPT = (
    "You are Qwen model but you name is Jarvis. You are a friendly offline voice assistant. "
    "Address the user as 'sir' often, in a natural way, such as at the end of a sentence or when greeting. Do not add it to every reply. "
    #"Reply in one to three short, natural spoken sentences. "
    "Use plain words only: no markdown, lists, emojis, symbols, or code blocks. "
    "Write numbers and abbreviations the way they are spoken. "
    "Never start with filler like 'Sure' or 'Certainly'. "
    "If the user asks for details, an explanation, or a step-by-step answer, "
    "you may give a longer reply "
    "Your only abilities are: chatting and answering questions, telling the time and "
    "date, math and unit conversions, remembering things about the user and forgetting "
    "them on request, changing your voice, and controlling the user's PC (apps, websites, "
    "volume, brightness, windows, screenshots, power). You cannot set reminders, timers "
    "or alarms, play music, send messages, or look anything up online. If asked to do "
    "something that is not in your abilities, say plainly that you can't do that yet. "
    "Never claim an ability that is not listed here. Also mention that they can "
    " ask 'what can you do on my PC' for the PC details. "
    "Use the earlier messages in this conversation to answer follow-up questions. "
    "If you do not know something about the user, meaning they never told you in "
    "this conversation and it is not in your notes, say you don't know. Never guess. "
    "If the user only reacts (okay, thanks, good job), reply with at most one short sentence. "
    "Never repeat or continue your previous answer unless asked. "
    "You have no internet access. You cannot look anything up, "
    " or check current information, and you must never say or imply that you will. "
    "Answer only from what you already know. If you are not sure, or the answer needs "
    "current information, say plainly that you don't know or can't check that offline. "
)

MAX_HISTORY_MESSAGES = 20       # about 10 question/answer pairs
MAX_TOOL_ROUNDS = 3             # tool-call round trips allowed per reply

# Keys never injected into the system prompt even if they are in the stored profile.
_PROFILE_SKIP_KEYS = {
    "mood", "self_correction", "assistant_name", "preferred_assistant_name",
    "emoji_usage", "initial_interaction", "compliments", "communication",
}
MIC_DEVICE = 1                 # None = system default; list devices with: python -m sounddevice
SAMPLE_RATE = 16000
CHUNK = 512                    # 512 samples at 16 kHz = 32 ms (Silero requires this size)

SPEECH_THRESHOLD = 0.5         # probability above this = speech
SILENCE_THRESHOLD = 0.35       # probability below this = silence
SILENCE_LIMIT = 27             # 27 chunks = about 0.85 s of silence ends the utterance
MIN_SPEECH_CHUNKS = 8          # about 0.25 s; shorter sounds (coughs, clicks) are ignored
PRE_ROLL_CHUNKS = 8            # about 0.25 s of audio kept from before speech was detected
MAX_RECORD_CHUNKS = 1875       # about 60 s safety limit

# --- Interruption tuning (used only while Jarvis is talking) ---
INTERRUPT_SPEECH_THRESHOLD = 0.7   # stricter: his own voice leaks into the mic
DUCK_CHUNKS = 3                    # score needed to PAUSE Jarvis (about 0.1 s of speech)
INTERRUPT_CONFIRM_CHUNKS = 8       # score needed to STOP him for good (about 0.25 s of speech)
INTERRUPT_PRE_ROLL_CHUNKS = 16     # about 0.5 s of audio kept from before the interruption

# Said on their own, these silence Jarvis without triggering a new reply.
STOP_PHRASES = {
    "stop", "wait", "hold on", "quiet", "be quiet", "never mind", "nevermind",
    "enough", "ok stop", "okay stop", "stop talking", "shut up", "that's enough",
    "thats enough",
}
FILLER_SOUNDS = {"uh", "um", "hmm", "mm", "mhm", "uh huh", "ah", "oh", "huh"}
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

SPEECH_SPEED = 1.05
SENTENCE_PAUSE = 0.25          # seconds of silence between sentences
PLAYBACK_CHUNK = 1024          # small writes make playback interruption responsive

