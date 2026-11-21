"""Long-term memory extraction for Jarvis (Phase 3).

Qwen reads what you said in a session, together with what Jarvis already
knows, and proposes OPERATIONS: ADD, UPDATE or DELETE (an empty list means
NOTHING). Python validates every operation, checks it against the database,
and only then applies it. Qwen never writes to the database directly.

Architecture (session-end only):
  * Live conversation never issues memory-extraction requests.
  * After the user says goodbye or presses Ctrl+C, the main process calls
    process_pending_messages() once.
  * Pending user messages are sent to Qwen in small batches (one request per
    batch, not one per message), with the current facts as context.
  * Messages already handled by an instant memory command ("remember...",
    "forget...") are marked processed by memory_commands.py and never appear.
  * Handled messages are marked in memory_processed. Failures leave the
    messages unmarked so a later session can retry them.

Safety guards applied in Python, whatever Qwen says:
  * Newer wins: if the active fact came from a LATER message (for example you
    corrected it by voice), an operation from an older message is ignored.
  * Forgotten stays forgotten: an operation is ignored if the user deleted that
    fact, or a fact with the same value, after the message was spoken.
  * DELETE needs higher confidence, and a DELETE that names a value only
    removes that value (one list item, or a scalar that really matches).
  * Only categories, keys and confidence levels that pass validation are kept.
"""

import json
import os
import re
import requests
import fact_rules

import database
from memory_commands import parse_statement

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
#MODEL_NAME = "qwen3:4b-instruct"     # qwen3:8b judges updates/deletes better if VRAM allows
MODEL_NAME = "qwen3:8b"

BATCH_SIZE = 4                       # user messages per Qwen request (small is more reliable)
DEBUG = os.environ.get("JARVIS_MEMORY_DEBUG", "") not in ("", "0")   # print raw model replies
MIN_CONFIDENCE = 0.85
MIN_DELETE_CONFIDENCE = 0.90         # deleting is riskier than adding
MAX_KNOWN_FACTS = 60                 # facts shown to Qwen as context
MAX_FORGOTTEN_SHOWN = 15
MAX_TRANSCRIPT_CHARS = 6000

MEMORY_SYSTEM_PROMPT = """
You are the memory manager of a voice assistant. Read the CONVERSATION and list
the lasting facts the USER stated about themselves.

You are given KNOWN FACTS (already remembered) and FORGOTTEN facts (never add
these back). Only lines that start with "USER #<id>" count. Every other line is
context to help you understand them.

Operations:
  ADD     a new lasting fact: name, city, job, things they own, things they like
          or dislike, what they are learning or building, favorites, family.
  UPDATE  a known fact that changed ("I moved to Paris").
  DELETE  something that is no longer true ("I stopped learning Rust").

If a USER line states a lasting fact, you MUST return it. Return
{"operations":[]} only when the USER lines contain no lasting fact.

Do NOT store: moods or how they feel today, one-time events, questions, trivia,
commands, hypotheticals, jokes, secrets or passwords, health, politics, religion.

Details:
- Every operation needs "message_id": the number from the USER line that says it.
- Reuse key names from KNOWN FACTS for the same thing. No near-duplicates.
- Lists (likes, learning, current_projects) hold lists. ADD only the new items.
  To remove one item, DELETE with that item as the value. DELETE with no value
  removes the whole fact.
- confidence: 0.9 to 1.0 when the user was explicit. DELETE needs 0.9 or more.
- Allowed categories: personal, preferences, hardware, software, interests,
  projects, goals, routines, environment, other. Keys are short snake_case.

Return ONLY JSON like:
{"operations":[{"op":"ADD","category":"personal","key":"name","value":"Maya","confidence":0.95,"message_id":5}]}

Example 1
KNOWN FACTS: (none yet)
USER #5: my name is Maya
USER #6: I live in Toronto and I work as a nurse
USER #7: I'm so tired today
Answer: {"operations":[{"op":"ADD","category":"personal","key":"name","value":"Maya","confidence":0.95,"message_id":5},{"op":"ADD","category":"personal","key":"city","value":"Toronto","confidence":0.95,"message_id":6},{"op":"ADD","category":"personal","key":"occupation","value":"nurse","confidence":0.95,"message_id":6}]}

Example 2
KNOWN FACTS: personal.city = "Dhaka"
USER #31: I moved to Chittagong last month
Answer: {"operations":[{"op":"UPDATE","category":"personal","key":"city","value":"Chittagong","confidence":0.95,"message_id":31}]}

Example 3
KNOWN FACTS: interests.learning = ["Rust","Python"]
USER #40: I gave up on Rust
Answer: {"operations":[{"op":"DELETE","category":"interests","key":"learning","value":"Rust","confidence":0.92,"message_id":40}]}

Example 4
USER #60: what is the capital of France
USER #61: that was a long day
Answer: {"operations":[]}
""".strip()

ALLOWED_CATEGORIES = {
    "personal", "preferences", "hardware", "software", "interests",
    "projects", "goals", "routines", "environment", "other",
}

# Keys that are almost always noise or temporary; rejected even if the model emits them.
BLOCKED_KEYS = {
    "mood", "feeling", "energy", "tired", "happy", "sad", "stressed",
    "assistant_name", "assistant", "bot_name", "ai_name",
    "current_mood", "temporary", "right_now",
    "hypothetical", "maybe", "perhaps",
}

# Values that indicate the model captured a voice-command side-effect or noise.
BLOCKED_VALUE_SUBSTRINGS = (
    "nicole", "af_", "bf_",  # common kokoro voice ids / names from voice switching
)

KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")

# Messages that should never be sent to the extractor at all.
_STOP_OR_GOODBYE = {
    "stop", "wait", "hold on", "quiet", "be quiet", "never mind", "nevermind",
    "enough", "ok stop", "okay stop", "stop talking", "shut up", "that's enough",
    "thats enough",
    "goodbye", "good bye", "bye", "bye bye", "see you", "see ya", "see you later",
    "dismissed", "disengage", "talk to you later", "catch you later", "farewell",
    "i am leaving", "im leaving", "thats all", "that's all", "thats it", "that's it",
    "uh", "um", "hmm", "mm", "mhm", "uh huh", "ah", "oh", "huh",
}

_VOICE_COMMAND_RE = re.compile(
    r"^\s*(?:hey|hi|ok|okay|please|jarvis)?\s*"
    r"(?:(?:change|switch)(?:\s+your)?\s+voice(?:\s+to)?|"
    r"next\s+voice|previous\s+voice|last\s+voice|"
    r"use|speak\s+as|switch\s+to|change\s+to)"
    r".*$",
    re.I,
)

_PURE_QUESTION_RE = re.compile(
    r"^\s*(?:what|who|when|where|why|how|which|can you|could you|would you|"
    r"tell me|do you know|is there|are there)\b",
    re.I,
)


# ---------------------------------------------------------------
# Pre-filter: messages that never need the model
# ---------------------------------------------------------------

def _normalize_for_filter(text):
    text = (text or "").lower().replace("-", " ").replace("'", "")
    text = re.sub(r"[^a-z\s]", " ", text)
    text = re.sub(r"\bjarvis\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def should_skip_message(content):
    """Return True for messages that must never go to the memory model."""
    if not content or not str(content).strip():
        return True

    cleaned = _normalize_for_filter(content)

    if len(cleaned) < 4:
        return True

    if cleaned in _STOP_OR_GOODBYE:
        return True

    if cleaned.rstrip(".") in _STOP_OR_GOODBYE:
        return True

    if _VOICE_COMMAND_RE.match(content.strip()):
        return True

    # Pure questions with no self-statement about the user.
    # "Tell me a fun fact" contains "me" but is not a self-fact.
    if _PURE_QUESTION_RE.match(cleaned):
        has_self_fact = re.search(
            r"\b(i|i'm|im|i am|my|mine)\b", cleaned
        ) and not re.search(
            r"\b(tell|give|show|remind|ask|help|can you|could you|would you)\s+me\b",
            cleaned,
        )
        if not has_self_fact:
            return True

    return False


# ---------------------------------------------------------------
# Validation of what Qwen returns
# ---------------------------------------------------------------

def _extract_json(text):
    """Parse JSON, tolerating a single fenced block or surrounding prose."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _valid_value(value):
    if isinstance(value, str):
        value = value.strip()
        if not value or len(value) > 500:
            return None
        low = value.lower()
        if any(bad in low for bad in BLOCKED_VALUE_SUBSTRINGS):
            return None
        return value
    if isinstance(value, list):
        cleaned = []
        for item in value:
            if isinstance(item, str) and item.strip() and len(item.strip()) <= 200:
                low = item.strip().lower()
                if any(bad in low for bad in BLOCKED_VALUE_SUBSTRINGS):
                    continue
                if item.strip() not in cleaned:
                    cleaned.append(item.strip())
        return cleaned or None
    return None


def _coerce_message_id(raw):
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        raw = raw.strip().lstrip("#")
        if raw.isdigit():
            return int(raw)
    return None


def _check_operation(item, valid_message_ids):
    """Return (clean operation, None) or (None, the reason it was rejected)."""
    if not isinstance(item, dict):
        return None, "not an object"

    op = str(item.get("op", "")).strip().upper()
    if op not in ("ADD", "UPDATE", "DELETE"):
        return None, f"unknown op {op!r}"

    category = item.get("category")
    key = item.get("key")
    if category not in ALLOWED_CATEGORIES:
        return None, f"bad category {category!r}"
    if not isinstance(key, str) or not KEY_RE.fullmatch(key) or key in BLOCKED_KEYS:
        return None, f"bad or blocked key {key!r}"
    category, key = fact_rules.canonical_pair(category, key)

    message_id = _coerce_message_id(item.get("message_id"))
    if message_id is None or message_id not in valid_message_ids:
        return None, f"unknown message_id {item.get('message_id')!r}"

    confidence = item.get("confidence", 0)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None, f"bad confidence {confidence!r}"
    floor = MIN_DELETE_CONFIDENCE if op == "DELETE" else MIN_CONFIDENCE
    if confidence < floor or confidence > 1:
        return None, f"confidence {confidence} not accepted for {op}"

    raw_value = item.get("value")
    if op == "DELETE" and raw_value in (None, "", []):
        value = None                      # delete the whole fact
    else:
        value = _valid_value(raw_value)
        if value is None:
            return None, f"unusable value {raw_value!r}"

    if op != "DELETE":
        reason = fact_rules.rejection_reason(category, key, value)
        if reason:
            return None, f"{reason}: {category}.{key}"
    return {"op": op, "category": category, "key": key, "value": value,
            "confidence": float(confidence), "message_id": message_id,
            "via": "model"}, None


def validate_operations(payload, valid_message_ids):
    """Return (operations sorted oldest message first, list of rejection reasons)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("operations"), list):
        raise ValueError("Qwen response does not contain an operations list")

    operations, rejected = [], []
    for item in payload["operations"]:
        if isinstance(item, dict) and str(item.get("op", "")).strip().upper() == "NOTHING":
            continue
        clean, reason = _check_operation(item, valid_message_ids)
        if clean is None:
            rejected.append(reason)
        else:
            operations.append(clean)

    operations.sort(key=lambda o: o["message_id"])    # older messages first: newer wins
    return operations, rejected


# ---------------------------------------------------------------
# Rule-based fast path
# Clear first-person statements ("my name is Sam", "I live in London") are
# understood by the same parser as the instant voice commands. They do not
# depend on the small model noticing them. Anything unclear goes to Qwen.
# ---------------------------------------------------------------

_TRAILING_FILLER_RE = re.compile(
    r"\s+(?:again|too|a lot|so much|very much|as well|now|these days|lately|recently)$",
    re.I,
)
_NOT_A_VALUE_START = {"i", "my", "me", "it", "this", "that", "these", "those", "to",
                      "you", "we", "they", "he", "she", "there", "when", "how", "what",
                      "if", "not"}
_MULTI_CLAUSE_RE = re.compile(
    r"\b(?:but|because|since|although|though|which|while|unless|if|so|i)\b", re.I)


def _clean_item(text):
    """True if `text` looks like one simple thing, not a whole second sentence."""
    words = text.split()
    if not words or len(words) > 4:
        return False
    if words[0].lower() in _NOT_A_VALUE_START:
        return False
    return not _MULTI_CLAUSE_RE.search(text)


def parse_explicit_statement(text):
    """Return {category, key, value} for a clear, simple statement, else None."""
    if not text or "?" in text:
        return None
    statement = parse_statement(text)
    if statement is None or statement["category"] == "other":
        return None

    raw = statement["value"]
    items = raw if isinstance(raw, list) else [raw]
    cleaned = []
    for item in items:
        item = _TRAILING_FILLER_RE.sub("", item.strip())
        if not _clean_item(item):
            return None
        cleaned.append(item)
    if not cleaned:
        return None
    
    value = cleaned if isinstance(raw, list) else cleaned[0]
    key = fact_rules.canonical_key(statement["key"])
    if fact_rules.rejection_reason(statement["category"], key, value):
        return None                      # temporary, sensitive or a secret: not saved
    return {"category": statement["category"], "key": statement["key"], "value": value}


def _rule_operations(batch):
    """Split a batch into (operations found by rules, messages left for Qwen)."""
    operations, remaining = [], []
    for message in batch:
        statement = parse_explicit_statement(message["content"])
        op = None
        if statement:
            op, _ = _check_operation(
                {"op": "UPDATE", "category": statement["category"],
                 "key": statement["key"], "value": statement["value"],
                 "confidence": 0.95, "message_id": message["id"]},
                {message["id"]},
            )
        if op:
            op["via"] = "rules"
            operations.append(op)
        else:
            remaining.append(message)
    return operations, remaining


# ---------------------------------------------------------------
# Building the request
# ---------------------------------------------------------------

def _short(value, limit=80):
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _known_facts_block():
    facts = [f for f in database.get_active_facts() if not f["key"].startswith("note")]
    if not facts:
        return "(none yet)"
    lines = [f"- {f['category']}.{f['key']} = {_short(f['value'])}"
             for f in facts[:MAX_KNOWN_FACTS]]
    return "\n".join(lines)


def _forgotten_block():
    deleted = [f for f in database.get_deleted_facts(MAX_FORGOTTEN_SHOWN)
               if not f["key"].startswith("note")]
    if not deleted:
        return "(none)"
    return "\n".join(f"- {f['category']}.{f['key']} = {_short(f['value'])}" for f in deleted)


def _build_transcript(batch):
    """The conversation around this batch. Only pending user lines carry an id."""
    pending_ids = {m["id"] for m in batch}
    first, last = batch[0]["id"], batch[-1]["id"]
    messages = database.get_conversation_messages(batch[0]["conversation_id"])

    start = next((i for i, m in enumerate(messages) if m["id"] >= first), 0)
    start = max(0, start - 2)             # a couple of earlier lines for context

    lines = []
    for m in messages[start:]:
        if m["id"] > last:
            break
        if m["id"] not in pending_ids and m.get("route") not in (None, "chat"):
            continue                      # commands are not conversation
        text = " ".join(m["content"].split())
        if m["role"] == "user" and m["id"] in pending_ids:
            lines.append(f"USER #{m['id']}: {text[:600]}")
        elif m["role"] == "user":
            lines.append(f"USER (context only): {text[:200]}")
        else:
            lines.append(f"ASSISTANT (context only): {text[:200]}")

    while len("\n".join(lines)) > MAX_TRANSCRIPT_CHARS and len(lines) > 1:
        lines.pop(0)
    return "\n".join(lines)


def _build_user_prompt(batch):
    return (
        "KNOWN FACTS:\n" + _known_facts_block() + "\n\n"
        "FORGOTTEN (never add back):\n" + _forgotten_block() + "\n\n"
        "CONVERSATION:\n" + _build_transcript(batch) + "\n\n"
        "Return the operations as JSON only."
    )


def _ask_model(user_prompt):
    """One request to local Qwen. Returns the parsed JSON payload."""
    response = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL_NAME,
            "messages": [
                {"role": "system", "content": MEMORY_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "think": False,
            "keep_alive": -1,
            "format": "json",
            "options": {
                "temperature": 0.05,
                "top_p": 0.7,
                "num_ctx": 4096,
                "num_predict": 600,
            },
        },
        timeout=(10, 180),
    )
    response.raise_for_status()
    content = response.json().get("message", {}).get("content", "")
    if DEBUG:
        print("[memory debug] raw model reply:", content)
    return _extract_json(content)


def extract_operations(batch):
    """Ask Qwen about one batch. Returns (valid operations, rejected count)."""
    prompt = _build_user_prompt(batch)
    if DEBUG:
        print("[memory debug] prompt sent to the model:\n" + prompt + "\n")
    payload = _ask_model(prompt)
    return validate_operations(payload, {m["id"] for m in batch})


# ---------------------------------------------------------------
# Applying operations safely
# ---------------------------------------------------------------

def _norm(value):
    return " ".join(str(value).casefold().split())


def _is_newer(fact, message):
    """True if the active fact came from a LATER message than the one being processed."""
    source = fact.get("source_message_id")
    if source is not None:
        return source > message["id"]
    return fact["created_at"] > message["created_at"]


def _ignored(text):
    return {"applied": False, "text": text}


def _applied(text):
    return {"applied": True, "text": text}


def _apply_operation(op, message):
    """Apply one validated operation. Returns {"applied": bool, "text": str}."""
    category, key = op["category"], op["key"]
    label = f"{category}.{key}"
    active = database.get_active_fact(category, key)

    if active is not None and _is_newer(active, message):
        return _ignored(f"{op['op']} {label}: a newer value is already saved")

    if op["op"] in ("ADD", "UPDATE"):
        if database.was_forgotten_since(category, key, op["value"], message["created_at"]):
            return _ignored(f"{op['op']} {label}: you asked me to forget it")

        result = database.upsert_fact(
            category, key, op["value"], confidence=op["confidence"],
            source="extraction", source_message_id=message["id"],
        )
        action = result["action"]
        if action == "added":
            return _applied(f"added {label} = {_short(result['new_value'])}")
        if action == "superseded":
            return _applied(f"updated {label}: {_short(result['old_value'])} -> "
                            f"{_short(result['new_value'])}")
        if action == "merged":
            return _applied(f"extended {label} -> {_short(result['new_value'])}")
        return _applied(f"confirmed {label}")

    # DELETE
    if active is None:
        return _ignored(f"DELETE {label}: nothing saved to delete")

    value = op["value"]
    if value is None:
        database.retire_fact(active["id"], retired_at=message["created_at"])
        return _applied(f"forgot {label}")

    items = value if isinstance(value, list) else [value]
    if isinstance(active["value"], list):
        removed = []
        for item in items:
            result = database.remove_fact_list_item(
                category, key, item, source="extraction",
                source_message_id=message["id"], retired_at=message["created_at"],
            )
            if result["action"] in ("item_removed", "retired"):
                removed.append(item)
        if not removed:
            return _ignored(f"DELETE {label}: {_short(value)} is not in the saved list")
        return _applied(f"removed {_short(removed)} from {label}")

    # Scalar fact: only delete if the named value really is the saved one.
    if len(items) == 1 and _norm(items[0]) == _norm(active["value"]):
        database.retire_fact(active["id"], retired_at=message["created_at"])
        return _applied(f"forgot {label}")
    return _ignored(f"DELETE {label}: the saved value is different")


# ---------------------------------------------------------------
# Session-end driver
# ---------------------------------------------------------------

def _result(message_id, status, updates=None, applied=None, ignored=None, error=None):
    out = {"message_id": message_id, "status": status,
           "updates": updates or {}, "applied": applied or [], "ignored": ignored or []}
    if error:
        out["error"] = error
    return out


def _batches(messages):
    """Group messages by conversation, then cut each group into BATCH_SIZE chunks."""
    by_conversation = {}
    for message in messages:
        by_conversation.setdefault(message["conversation_id"], []).append(message)
    for group in by_conversation.values():
        for i in range(0, len(group), BATCH_SIZE):
            yield group[i:i + BATCH_SIZE]


def _process_batch(batch):
    rule_ops, remaining = _rule_operations(batch)

    model_ops, rejected, model_error = [], [], None
    if remaining:                         # no model call at all if rules handled everything
        try:
            model_ops, rejected = extract_operations(remaining)
        except Exception as error:
            model_error = str(error)

    by_id = {m["id"]: m for m in batch}
    state = {m["id"]: {"updates": {}, "applied": [], "ignored": [], "error": None}
             for m in batch}

    for op in sorted(rule_ops + model_ops, key=lambda o: o["message_id"]):
        entry = state[op["message_id"]]
        try:
            outcome = _apply_operation(op, by_id[op["message_id"]])
        except Exception as error:
            entry["error"] = str(error)
            continue
        tag = f" [{op['via']}]"
        if outcome["applied"]:
            entry["applied"].append(outcome["text"] + tag)
            if op["op"] != "DELETE":
                entry["updates"].setdefault(op["category"], {})[op["key"]] = op["value"]
        else:
            entry["ignored"].append(outcome["text"] + tag)

    for reason in rejected:
        state[remaining[0]["id"]]["ignored"].append(f"model suggestion rejected: {reason}")

    # If Qwen failed, only the messages that needed Qwen stay pending for a retry.
    needs_retry = {m["id"] for m in remaining} if model_error else set()

    results = []
    for message in batch:
        entry = state[message["id"]]
        if message["id"] in needs_retry:
            results.append(_result(message["id"], "error", error=model_error))
            continue
        if entry["error"]:
            results.append(_result(message["id"], "error", error=entry["error"]))
            continue
        status = "memories_saved" if entry["applied"] else "no_memories"
        try:
            database.mark_memory_message_processed(message["id"], status)
        except Exception as error:
            results.append(_result(message["id"], "error", error=str(error)))
            continue
        results.append(_result(message["id"], status, entry["updates"],
                               entry["applied"], entry["ignored"]))
    return results


def process_pending_messages(limit=20):
    """Process pending user messages. Called once after the voice session ends."""
    results = []
    to_extract = []

    for message in database.get_unprocessed_user_messages(limit=limit):
        not_chat = message.get("route") not in (None, "chat")
        if not_chat or should_skip_message(message.get("content", "")):
            try:
                database.mark_memory_message_processed(message["id"], "skipped")
                results.append(_result(message["id"], "skipped"))
            except Exception as error:
                results.append(_result(message["id"], "error", error=str(error)))
        else:
            to_extract.append(message)

    for batch in _batches(to_extract):
        results.extend(_process_batch(batch))

    results.sort(key=lambda r: r["message_id"])
    return results


def save_statement_now(message_id):
    """Live fast path: save a clear statement ("I live in London") right away.

    Pure regex, no model call. Returns a short log line, or None if the
    message was not a clear statement (Qwen handles it at session end).
    """
    if message_id is None:
        return None

    message = database.get_message(message_id)
    if not message:
        return None

    statement = parse_explicit_statement(message["content"])
    if not statement:
        return None

    op, _ = _check_operation(
        {"op": "UPDATE", "category": statement["category"],
         "key": statement["key"], "value": statement["value"],
         "confidence": 0.95, "message_id": message["id"]},
        {message["id"]},
    )
    if not op:
        return None
    op["via"] = "live"

    outcome = _apply_operation(op, message)
    # Mark it handled so the session-end extractor skips it.
    database.mark_memory_message_processed(message_id, "live_rules")
    return outcome["text"]