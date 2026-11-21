"""Conversation summaries for Jarvis (Phase 4: episodic memory).

After the facts have been extracted at session end, every finished conversation
that has not been reviewed yet gets ONE Qwen request. Qwen writes a 2-3 sentence
summary and a few topic tags; Python validates them and stores them in the
conversation_summaries table. Qwen never writes to the database directly.

Rules
  * One row per conversation. Conversations with too few real user messages are
    recorded as "reviewed, nothing to remember" without calling the model.
  * A summary of '' also means "reviewed, nothing worth remembering" (greetings,
    small talk). Such rows are never spoken back and never reviewed again.
  * Messages that were instant memory commands ("remember...", "forget...",
    "what do you know about me") are left out of the transcript, so a fact you
    told Jarvis to forget cannot come back through a summary.
  * At most MAX_PER_SESSION model calls per session, newest conversation first;
    anything left stays pending for the next session. If the model cannot be
    reached, the model calls stop for this session and nothing is marked reviewed.

Used by j.py:
    jarvis_summaries.process_pending_summaries()  -> list of result dicts
"""

import json
import os
import re
import requests

import database

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
MODEL_NAME = "qwen3:8b"

DEBUG = os.environ.get("JARVIS_SUMMARY_DEBUG", "") not in ("", "0")

MIN_USER_MESSAGES = 3            # real user messages needed before a summary is attempted
MAX_PER_SESSION = 5              # model calls per session end
MAX_PENDING_SCAN = 200           # conversations looked at per session end
MAX_LINE_CHARS = 300             # one transcript line
MAX_TRANSCRIPT_CHARS = 7000      # whole transcript; the middle is cut if longer
MAX_SUMMARY_CHARS = 600
MAX_TOPICS = 5
MAX_TOPIC_CHARS = 40

SUMMARY_SYSTEM_PROMPT = """
You write a short memory note about a voice conversation between a USER and
their assistant JARVIS. Jarvis will read the note out loud later, so write it
as Jarvis remembering the conversation.

Return ONLY JSON: {"summary": "...", "topics": ["...", "..."]}

summary:
- 2 or 3 short sentences in plain spoken words. No markdown, lists or emojis.
- Start with "We talked about" or "We discussed". Always say "you" for the user.
  Never use the user's name, and never say "he", "she" or "the user".
- Say what the user asked for or wanted, what was decided, and anything left
  unfinished.
- Do not include passwords, secrets, or details about health, politics or religion.
- Ignore greetings, thanks, goodbyes, small talk, stop words, and simple commands
  such as opening apps, changing the volume or the voice.
- Do not list personal details the user told about themselves (name, city, likes,
  computer, family). Those are saved elsewhere. If the conversation is only the
  user stating such details, return an empty summary.

If the conversation has nothing worth remembering, return {"summary": "", "topics": []}.

Example
JARVIS and USER conversation:
USER: my python script keeps crashing when it reads the csv file
JARVIS: Check that the file path is right and that the file is not open in another program.
USER: it was open in excel, that fixed it
USER: now how do I sort the rows by date
JARVIS: Use the sort_values method on the date column.
Answer: {"summary": "We talked about your Python script crashing while reading a CSV file. It turned out the file was open in Excel, and then you asked how to sort the rows by date.", "topics": ["python", "csv", "debugging"]}
""".strip()

_NOISE = {
    "stop", "wait", "hold on", "quiet", "be quiet", "never mind", "nevermind",
    "enough", "ok stop", "okay stop", "stop talking", "shut up", "thats enough",
    "goodbye", "good bye", "bye", "bye bye", "see you", "see ya", "see you later",
    "uh", "um", "hmm", "mm", "mhm", "uh huh", "ah", "oh", "huh",
}


# ---------------------------------------------------------------
# Building the transcript
# ---------------------------------------------------------------

def _plain(text):
    text = str(text or "").lower().replace("'", "").replace("\u2019", "")
    text = re.sub(r"[^a-z\s]", " ", text)
    text = re.sub(r"\bjarvis\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# "change your voice to heart", "next voice": a command, not conversation. Needs the
# word "voice" so that "use a lambda function" is never mistaken for one.
_VOICE_COMMAND_RE = re.compile(
    r"^\s*(?:(?:hey|hi|ok|okay|please|jarvis|can you|could you)[,\s]+)*"
    r"(?:(?:change|switch)(?:\s+your)?\s+voice\b|(?:next|previous|last)\s+voice\b|"
    r"(?:use|speak\s+as)\b.*\bvoice\b)",
    re.I,
)


def _is_noise(content):
    cleaned = _plain(content)
    return len(cleaned) < 3 or cleaned in _NOISE


_CHAT_ROUTES = (None, "chat")        # None = old row that was never tagged


def conversation_lines(conversation_id):
    """Return (lines, user_count) for one conversation.

    Only real chat turns are kept. PC commands, voice changes, memory commands,
    direct time/math answers and goodbyes (and Jarvis's replies to them) are left out.
    user_count is the number of real user messages that remain.
    """
    messages = database.get_conversation_messages(conversation_id)
    command_ids = set(database.get_memory_command_message_ids(conversation_id))

    lines = []
    user_count = 0
    skip_reply = False
    for message in messages:
        text = " ".join(str(message["content"]).split())[:MAX_LINE_CHARS]
        route = message.get("route")
        if message["role"] == "user":
            skip_reply = (route not in _CHAT_ROUTES
                          or message["id"] in command_ids
                          or bool(_VOICE_COMMAND_RE.match(message["content"])))
            if skip_reply or _is_noise(message["content"]):
                continue
            user_count += 1
            lines.append(f"USER: {text}")
        elif message["role"] == "assistant":
            if skip_reply:
                skip_reply = False
                continue
            if route not in _CHAT_ROUTES:
                continue
            if text:
                lines.append(f"JARVIS: {text}")
    return lines, user_count


def build_transcript(lines):
    """Join the lines; if too long keep the start and (more of) the end."""
    text = "\n".join(lines)
    if len(text) <= MAX_TRANSCRIPT_CHARS:
        return text
    head_size = MAX_TRANSCRIPT_CHARS // 4
    head = text[:head_size].rsplit("\n", 1)[0]
    tail = text[-(MAX_TRANSCRIPT_CHARS - head_size):].split("\n", 1)[-1]
    return head + "\n[... part of the conversation left out ...]\n" + tail


# ---------------------------------------------------------------
# Talking to the model, and checking what it says
# ---------------------------------------------------------------

def _parse_json(text):
    """Parse JSON, tolerating a fenced block or surrounding prose."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _ask_model(transcript):
    """One request to local Qwen. Returns the parsed JSON payload."""
    response = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL_NAME,
            "messages": [
                {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                {"role": "user",
                 "content": "JARVIS and USER conversation:\n" + transcript
                            + "\n\nReturn the JSON only."},
            ],
            "stream": False,
            "think": False,
            "keep_alive": -1,
            "format": "json",
            "options": {"temperature": 0.2, "top_p": 0.8, "num_ctx": 4096,
                        "num_predict": 300},
        },
        timeout=(10, 180),
    )
    response.raise_for_status()
    content = response.json().get("message", {}).get("content", "")
    if DEBUG:
        print("[summary debug] raw model reply:", content)
    return _parse_json(content)


def _clean_summary(value):
    if not isinstance(value, str):
        return None
    text = re.sub(r"[*_#`~]", "", value)
    text = " ".join(text.split())
    if len(text) > MAX_SUMMARY_CHARS:
        cut = text[:MAX_SUMMARY_CHARS]
        last_stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        text = cut[:last_stop + 1] if last_stop > 100 else cut.rstrip() + "..."
    return text


def _clean_topics(value):
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        return []
    topics = []
    for item in value:
        if not isinstance(item, str):
            continue
        tag = re.sub(r"[^a-z0-9\s\-+#.]", "", item.lower())
        tag = " ".join(tag.split())[:MAX_TOPIC_CHARS].strip()
        if tag and tag not in topics:
            topics.append(tag)
        if len(topics) >= MAX_TOPICS:
            break
    return topics


def validate_summary(payload):
    """Return (summary, topics). summary == '' means nothing worth remembering."""
    if not isinstance(payload, dict) or "summary" not in payload:
        raise ValueError("Qwen response does not contain a summary")
    summary = _clean_summary(payload.get("summary"))
    if summary is None:
        raise ValueError("summary is not text")
    if not summary:
        return "", []
    return summary, _clean_topics(payload.get("topics"))


# ---------------------------------------------------------------
# Session-end driver
# ---------------------------------------------------------------

def _result(conversation_id, status, summary="", topics=None, error=None):
    out = {"conversation_id": conversation_id, "status": status,
           "summary": summary, "topics": topics or []}
    if error:
        out["error"] = error
    return out

def _use_you(summary):
    """Replace the user's own name (any saved version of it) with 'you'."""
    try:
        names = {str(f["value"]).strip()
                 for f in database.get_fact_history("personal", "name")
                 if isinstance(f["value"], str)}
    except Exception:
        return summary
    for name in sorted((n for n in names if len(n) >= 2), key=len, reverse=True):
        summary = re.sub(rf"\b{re.escape(name)}'s\b", "your", summary)
        summary = re.sub(rf"\b{re.escape(name)}\b", "you", summary)
    return summary


def summarize_conversation(conversation_id, call_model=True):
    """Review one conversation and store the outcome.

    Status: "saved" (summary stored), "empty" (reviewed, nothing worth keeping),
    "too_short" (not enough real messages), "error" (model failed: nothing stored,
    so a later session retries). With call_model=False a conversation that would
    need the model is left pending (status "pending").
    """
    lines, user_count = conversation_lines(conversation_id)

    if user_count < MIN_USER_MESSAGES:
        database.save_conversation_summary(conversation_id, "", [], user_count)
        return _result(conversation_id, "too_short")

    if not call_model:
        return _result(conversation_id, "pending")

    try:
        payload = _ask_model(build_transcript(lines))
        summary, topics = validate_summary(payload)
        summary = _use_you(summary)
    except Exception as error:
        return _result(conversation_id, "error", error=str(error))

    database.save_conversation_summary(conversation_id, summary, topics, user_count)
    if not summary:
        return _result(conversation_id, "empty")
    return _result(conversation_id, "saved", summary, topics)


def process_pending_summaries(limit=MAX_PER_SESSION):
    """Review every unreviewed conversation (at most `limit` model calls).

    Called once after the facts have been extracted at session end.
    """
    results = []
    model_calls = 0
    model_down = False

    for conversation in database.get_conversations_without_summary(MAX_PENDING_SCAN):
        cid = conversation["id"]
        try:
            allow_model = not model_down and model_calls < limit
            result = summarize_conversation(cid, call_model=allow_model)
        except Exception as error:               # a database problem on one conversation
            results.append(_result(cid, "error", error=str(error)))
            continue

        if result["status"] in ("saved", "empty", "error"):
            model_calls += 1
        if result["status"] == "error":
            model_down = True                    # probably Ollama: stop calling it
        if result["status"] != "pending":
            results.append(result)

    return results
