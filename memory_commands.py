"""Instant memory commands for Jarvis (Phase 2).

Rule-based, so they are fast and reliable even with a small model, and they
never wait for the session-end extractor.

    handle_memory_command(text, conversation_id=None, message_id=None)
        -> a short spoken reply, or None if `text` is not a memory command.

Commands
  Remember   "remember that my sister's name is Ayesha"
             "remember I like chess and gaming"
             "remember that the meeting is on Friday"      (saved as a note)
  Recall     "what do you know about me"
             "what's my GPU" / "do you remember my favorite color"
  Forget     "forget my city"  /  "forget that" (undo the last change)
             "forget everything"  (asks for a yes/no confirmation first)
  Correct    "that's wrong, I live in Chittagong"
             "actually my name is Rafiq"  (only if a name is already saved)
  Past talks "what did we talk about last time" / "summarize our last conversation"
             "what have we talked about recently"   (Phase 4: reads the stored
             conversation summaries, which are written when a session ends)

Everything is stored through database.upsert_fact(), so changed facts keep
their history instead of being overwritten.
"""

import re
import time
import fact_rules
from datetime import datetime, timezone

import database

PENDING_TIMEOUT = 30            # seconds the "are you sure?" question stays open
MAX_VALUE_CHARS = 300
MAX_FACTS_SPOKEN = 8
MAX_SUMMARIES_SPOKEN = 3        # "what have we talked about recently"

_pending_wipe_until = 0.0       # time until which a "yes" confirms wiping memory
_last_change = None             # what "forget that" should undo


# ---------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------

_FILLER_RE = re.compile(
    r"^(?:(?:hey|hi|ok|okay|please|jarvis|can you|could you|would you|will you)[,\s]+)+",
    re.I,
)
_TRAILING_JARVIS_RE = re.compile(r"[,\s]+jarvis$", re.I)


def _clean_command(text):
    """Normalise a transcript: quotes, fillers, trailing punctuation."""
    t = str(text or "").replace("\u2019", "'").replace("\u2018", "'")
    t = re.sub(r"\s+", " ", t).strip()
    t = _FILLER_RE.sub("", t)
    t = t.strip().rstrip(".!?,;:").strip()
    t = _TRAILING_JARVIS_RE.sub("", t).strip()
    return t


def _join_words(items):
    items = [str(i) for i in items]
    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _speak_value(value):
    return _join_words(value) if isinstance(value, list) else str(value)


def _label(key):
    return key.replace("_", " ")


# ---------------------------------------------------------------
# Turning "my X is Y" into (category, key, value)
# ---------------------------------------------------------------

_KEY_ALIASES = {
    "graphics_card": "gpu", "video_card": "gpu", "graphics": "gpu",
    "processor": "cpu", "operating_system": "os",
    "job": "occupation", "profession": "occupation", "work": "occupation",
    "hometown": "city", "home_town": "city",
}

_HARDWARE = {"gpu", "cpu", "ram", "laptop", "pc", "computer", "monitor", "keyboard",
             "mouse", "phone", "ssd", "motherboard", "headphones", "microphone",
             "mic", "speakers", "screen", "tablet"}
_SOFTWARE = {"os", "editor", "ide", "browser", "software", "app"}
_PERSONAL = {"name", "age", "city", "country", "birthday", "occupation", "nickname",
             "sister", "brother", "mother", "father", "mom", "dad", "wife", "husband",
             "girlfriend", "boyfriend", "friend", "son", "daughter", "pet", "dog",
             "cat", "timezone", "email"}
_ROUTINES = {"routine", "schedule", "bedtime", "wakeup"}
_GOALS = {"goal", "goals", "dream"}
_PROJECTS = {"project", "projects"}
_ENVIRONMENT = {"room", "desk", "setup", "office"}


def _make_key(thing):
    """'sister's name' -> 'sister_name', 'graphics card' -> 'gpu'. None if unusable."""
    t = str(thing).lower().replace("'s", " ")
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    words = [w for w in t.split() if w not in {"the", "a", "an", "current", "own"}]
    words = ["favorite" if w == "favourite" else w for w in words]
    if not words or len(words) > 4:
        return None
    key = "_".join(words)
    return _KEY_ALIASES.get(key, key)


def _infer_category(key):
    tokens = set(key.split("_"))
    if tokens & {"favorite", "preferred", "prefer"}:
        return "preferences"
    if tokens & _HARDWARE:
        return "hardware"
    if tokens & _SOFTWARE:
        return "software"
    if tokens & _PERSONAL:
        return "personal"
    if tokens & _ROUTINES:
        return "routines"
    if tokens & _GOALS:
        return "goals"
    if tokens & _PROJECTS:
        return "projects"
    if tokens & _ENVIRONMENT:
        return "environment"
    return "other"


def _as_list(text):
    parts = re.split(r",\s*|\s+and\s+", text, flags=re.I)
    return [p.strip() for p in parts if p.strip()]


# (pattern, category, key, is_list)
_STATEMENTS = [
    (re.compile(r"^my name is (.+)$", re.I), "personal", "name", False),
    (re.compile(r"^(?:you can |just )?call me (.+)$", re.I), "personal", "name", False),
    (re.compile(r"^i (?:currently )?live in (.+)$", re.I), "personal", "city", False),
    (re.compile(r"^i(?:'m| am) (?:currently )?(?:living|based) in (.+)$", re.I),
     "personal", "city", False),
    (re.compile(r"^i work as (?:a |an )?(.+)$", re.I), "personal", "occupation", False),
    (re.compile(r"^i(?:'m| am) (?:currently )?(?:learning|studying) (.+)$", re.I),
     "interests", "learning", True),
    (re.compile(r"^i(?:'m| am) (?:currently )?(?:building|making|developing|working on) (.+)$",
                re.I), "projects", "current_projects", True),
    (re.compile(r"^i (?:really )?(?:dislike|hate|(?:do not|don't|dont) like) (.+)$", re.I),
     "preferences", "dislikes", True),
    (re.compile(r"^i (?:really )?(?:like|love|enjoy) (.+)$", re.I),
     "interests", "likes", True),
]
_MY_X_IS_Y_RE = re.compile(r"^my (.+?) (?:is|are) (.+)$", re.I)


def parse_statement(text):
    """Parse a first-person fact. Returns a dict or None if it is not recognised."""
    text = _clean_command(text)

    for pattern, category, key, is_list in _STATEMENTS:
        match = pattern.match(text)
        if match:
            raw = match.group(1).strip()
            return {"category": category, "key": key,
                    "value": _as_list(raw) if is_list else raw}

    match = _MY_X_IS_Y_RE.match(text)
    if match:
        key = _make_key(match.group(1))
        value = match.group(2).strip()
        if key and value:
            return {"category": _infer_category(key), "key": key, "value": value}
    return None


def _build_note(text):
    """Fallback for 'remember that <anything>': a free-text note with a short key."""
    stop = {"the", "a", "an", "that", "is", "to", "of", "my", "i", "and", "on", "at"}
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in stop][:3]
    key = "note_" + "_".join(words) if words else "note"
    return {"category": "other", "key": key, "value": text.strip(), "is_note": True}


# ---------------------------------------------------------------
# Reading facts back
# ---------------------------------------------------------------

def _fact_sentence(fact):
    value = _speak_value(fact["value"])
    if fact["key"].startswith("note"):
        return f"A note: {value}"
    label = _label(fact["key"])
    if isinstance(fact["value"], list):
        return f"Your {label} include {value}"
    return f"Your {label} is {value}"


def _find_facts(thing):
    """Active facts matching 'thing': exact key first, else all key words present."""
    key = _make_key(thing)
    if not key:
        return []
    facts = database.get_active_facts()
    exact = [f for f in facts if f["key"] == key]
    if exact:
        return exact
    tokens = set(key.split("_"))
    return [f for f in facts if tokens <= set(f["key"].split("_"))]


def _remember_change(result, category, key):
    global _last_change
    _last_change = {"action": result["action"], "fact_id": result["fact_id"],
                    "old_value": result["old_value"], "category": category, "key": key}


def _save(statement, message_id, confidence=1.0):
    """Store a parsed statement and return (result, spoken reply)."""
    value = statement["value"]
    if fact_rules.is_secret(statement["key"], value):
        return None, "I won't store passwords or other secrets."
    if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
        return None, "That is too long to remember. Try a shorter fact."

    result = database.upsert_fact(
        statement["category"], statement["key"], value,
        confidence=confidence, source="voice_command", source_message_id=message_id,
    )
    _remember_change(result, statement["category"], statement["key"])

    label = _label(statement["key"])
    new = _speak_value(result["new_value"])
    old = _speak_value(result["old_value"]) if result["old_value"] is not None else ""
    is_note = statement.get("is_note", False)

    action = result["action"]
    if action == "added":
        if is_note:
            return result, "Got it, I'll remember that."
        if isinstance(result["new_value"], list):
            return result, f"Got it. Your {label} include {new}."
        return result, f"Got it. Your {label} is {new}."
    if action == "confirmed":
        return result, "I already knew that."
    if action == "merged":
        return result, f"Added. Your {label} now include {new}."
    # superseded
    if is_note:
        return result, "Updated that note."
    return result, f"Updated. Your {label} was {old}, now it's {new}."


# ---------------------------------------------------------------
# Command patterns
# ---------------------------------------------------------------

_TALK_VERB = (r"(?:talk(?:ed|ing)?|discuss(?:ed|ing)?|chat(?:ted|ting)?|"
              r"speak(?:ing)?|spoke)")
_ASK_PREFIX = r"(?:(?:remind me|tell me|do you remember|do you recall|can you tell me)\s+)?"

_LAST_TALK_RE = re.compile(
    r"^" + _ASK_PREFIX + r"what\s+(?:(?:did|have|were|was)\s+)?we\s+" + _TALK_VERB +
    r"(?:\s+about)?\s*(?:last time|the last time|last session|previously|before|"
    r"yesterday|last night|"
    r"in (?:our |the )?(?:last|previous) (?:conversation|session|chat|talk))$",
    re.I,
)
_SUMMARIZE_LAST_RE = re.compile(
    r"^(?:summari[sz]e|recap|sum up)(?: (?:our|the))? (?:last|previous) "
    r"(?:conversation|session|chat|talk)$",
    re.I,
)
_RECENT_TALKS_RE = re.compile(
    r"^" + _ASK_PREFIX + r"what\s+(?:(?:did|have|were)\s+)?we\s+" + _TALK_VERB +
    r"(?:\s+about)?\s*(?:recently|lately|these days|"
    r"(?:over|in) the (?:last|past) (?:few|couple of) (?:days|sessions|conversations|chats))$",
    re.I,
)

_RECALL_ALL_RE = re.compile(
    r"^(?:what do you (?:know|remember)(?: about me)?|"
    r"(?:tell me )?(?:what|everything) you (?:know|remember) about me|"
    r"what(?:'s| is) in your memory|what have you learned about me)$",
    re.I,
)

_WIPE_RE = re.compile(
    r"^(?:(?:forget|delete|erase|wipe|clear)(?: out)? (?:everything|all(?: of it)?)"
    r"(?: (?:that )?you know)?(?: about me)?(?: from (?:your |my )?memory)?"
    r"|(?:wipe|clear|erase|reset|forget) (?:out )?(?:your|my|all) (?:long[- ]term )?memory)$",
    re.I,
)

_UNDO_RE = re.compile(
    r"^(?:forget|undo|scratch|delete)(?: about)? (?:that|it|this|what i just said)$", re.I
)

_FORGET_RE = re.compile(
    r"^(?:forget|delete|erase|remove)(?: about)?(?: (my|the))? (.+?)"
    r"(?: from (?:your |my )?memory)?$",
    re.I,
)

_REMEMBER_RE = re.compile(
    r"^(?:remember|make a note|note down|keep in mind|don't forget|dont forget|do not forget)"
    r"(?: that| this)?[,:.\s]+(.+)$",
    re.I,
)

_EXPLICIT_CORRECTION_RE = re.compile(
    r"^(?:(?:no|nope)[,\s]+)?(?:that(?:'s| is| was)|thats|it(?:'s| is)) "
    r"(?:wrong|incorrect|not right|not correct|not true)[,:.\s]+(.+)$|"
    r"^correction[,:.\s]+(.+)$",
    re.I,
)
_SOFT_CORRECTION_RE = re.compile(r"^(?:actually|no|nope|sorry)[,.\s]+(.+)$", re.I)

_RECALL_EXPLICIT_RE = re.compile(
    r"^(?:do you (?:remember|know)|what do you know about|tell me) my (.+)$", re.I
)
_RECALL_TOPIC_RE = re.compile(
    r"^(?:(?:what do you (?:know|remember)|tell me what you (?:know|remember)) about"
    r"|what(?:'s| is| are)|whats) my (.+)$", re.I)
_RECALL_IMPLICIT_RE = re.compile(
    r"^(?:what is|what's|whats|what are|who is|who's) my (.+)$", re.I
)

_YES = {"yes", "yeah", "yep", "yup", "sure", "confirm", "do it", "go ahead",
        "yes please", "yes i am sure", "i am sure", "im sure", "yes do it"}
_NO = {"no", "nope", "cancel", "stop", "dont", "do not", "never mind", "nevermind",
       "no thanks", "no dont"}


# ---------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------

def _handle_pending(text):
    """Answer to 'are you sure you want me to forget everything?'. None = not an answer."""
    global _pending_wipe_until, _last_change
    if not _pending_wipe_until:
        return None

    expired = time.time() > _pending_wipe_until
    _pending_wipe_until = 0.0
    if expired:
        return None

    answer = re.sub(r"[^a-z\s]", "", _clean_command(text).lower().replace("'", "")).strip()
    if answer in _YES:
        count = 0
        for fact in database.get_active_facts():
            if database.retire_fact(fact["id"]):
                count += 1
        _last_change = None
        return f"Done. I've forgotten {count} thing{'s' if count != 1 else ''} about you."
    if answer in _NO:
        return "Okay, I'll keep everything."
    return None                       # something else: not an answer, carry on normally

MAX_RECALL_WORDS = 70           # "what do you know about me" stays about this long
LIST_LIMIT = 4                  # items read from one list ("you like a, b, c, and d")

_GROUP_NAMES = {"identity": "basics", "interests": "interests", "favorites": "favorites",
                "setup": "computer setup", "projects": "projects",
                "other": "other details", "notes": "notes"}

# "what do you know about my <word>" -> which group to read out
_TOPICS = {"setup": "setup", "hardware": "setup", "computer": "setup", "pc": "setup",
           "specs": "setup", "system": "setup",
           "interests": "interests", "hobbies": "interests", "likes": "interests",
           "favorites": "favorites", "favourites": "favorites", "preferences": "favorites",
           "projects": "projects", "goals": "projects"}


def _article(text):
    text = str(text).strip()
    if re.match(r"(?:a|an|the)\s", text, re.I):
        return text
    return ("an " if text[:1].lower() in "aeiou" else "a ") + text


def _items(value, limit):
    items = value if isinstance(value, list) else [value]
    items = [str(i).strip() for i in items if str(i).strip()]
    if len(items) > limit:
        return ", ".join(items[:limit]) + ", and more"
    return _join_words(items)


def _sentence(bits):
    text = _join_words(bits)
    return text[:1].upper() + text[1:] + "."


def _fact_bit(fact):
    text = _fact_sentence(fact)
    return text[:1].lower() + text[1:]


def _recall_groups(facts, limit=LIST_LIMIT):
    """([(label, spoken text)], trimmed) in speaking order.

    `trimmed` is True when some items were left out to keep it short.
    """
    rest = list(facts)
    groups, trimmed = [], False

    def take(*keys):
        found = {}
        for key in keys:
            for fact in rest:
                if fact["key"] == key:
                    found[key] = fact["value"]
                    rest.remove(fact)
                    break
        return found

    # who you are
    p = take("name", "nickname", "age", "birthday", "city", "country", "occupation")
    bits = []
    if "name" in p:
        bits.append(f"your name is {_speak_value(p['name'])}")
    if "nickname" in p:
        bits.append(f"you also go by {_speak_value(p['nickname'])}")
    if "age" in p:
        age = _speak_value(p["age"])
        bits.append(f"you're {age} years old" if age.isdigit() else f"your age is {age}")
    if "birthday" in p:
        bits.append(f"your birthday is {_speak_value(p['birthday'])}")
    if "city" in p:
        place = _speak_value(p["city"])
        if "country" in p:
            place += ", " + _speak_value(p["country"])
        bits.append(f"you live in {place}")
    elif "country" in p:
        bits.append(f"you live in {_speak_value(p['country'])}")
    if "occupation" in p:
        bits.append(f"you work as {_article(_speak_value(p['occupation']))}")
    if bits:
        groups.append(("identity", _sentence(bits)))

    # what you like and learn
    p = take("likes", "hobbies", "interests", "learning", "skills", "dislikes")
    bits = []
    for key, template in (("likes", "you like {}"), ("hobbies", "your hobbies include {}"),
                          ("interests", "you're interested in {}"),
                          ("learning", "you're learning {}"),
                          ("skills", "you're skilled at {}"),
                          ("dislikes", "you don't like {}")):
        if key in p:
            if isinstance(p[key], list) and len(p[key]) > limit:
                trimmed = True
            bits.append(template.format(_items(p[key], limit)))
    if bits:
        groups.append(("interests", _sentence(bits)))

    # favorites
    favorites = [f for f in rest if f["key"].startswith("favorite")]
    for fact in favorites:
        rest.remove(fact)
    if len(favorites) > limit + 1:
        trimmed = True
    bits = [_fact_bit(f) for f in favorites[:limit + 1]]
    if bits:
        groups.append(("favorites", _sentence(bits)))

    # computer setup
    setup = [f for f in rest if f["category"] in ("hardware", "software")]
    for fact in setup:
        rest.remove(fact)
    if len(setup) > limit:
        trimmed = True
    bits = [_fact_bit(f) for f in setup[:limit]]
    if bits:
        groups.append(("setup", _sentence(bits)))

    # projects and goals
    projects = [f for f in rest if f["category"] in ("projects", "goals")]
    for fact in projects:
        rest.remove(fact)
    bits = []
    for fact in projects[:limit]:
        if fact["key"] == "current_projects":
            bits.append(f"you're working on {_items(fact['value'], limit)}")
        else:
            bits.append(_fact_bit(fact))
    trimmed = trimmed or len(projects) > limit
    if bits:
        groups.append(("projects", _sentence(bits)))

    # everything else (family, routines...), then notes
    notes = [f for f in rest if f["key"].startswith("note")]
    others = [f for f in rest if not f["key"].startswith("note")]
    trimmed = trimmed or len(others) > limit
    bits = [_fact_bit(f) for f in others[:limit]]
    if bits:
        groups.append(("other", _sentence(bits)))
    if notes:
        trimmed = trimmed or len(notes) > 2
        texts = [" ".join(str(_speak_value(n["value"])).split())[:80] for n in notes[:2]]
        groups.append(("notes", " ".join(
            f"You asked me to remember that {t.rstrip('.')}." for t in texts)))

    return groups, trimmed

def _handle_recall_all():
    from jarvis_retrieval import _ordered, SKIP_KEYS      # import here, see note below
    facts = [f for f in _ordered(database.get_active_facts()) if f["key"] not in SKIP_KEYS]
    if not facts:
        return "I don't know anything about you yet. Tell me to remember something."

    groups, trimmed = _recall_groups(facts)
    spoken, left_out, words = [], [], 0
    for label, text in groups:
        size = len(text.split())
        if spoken and words + size > MAX_RECALL_WORDS:
            left_out.append(label)
            continue
        spoken.append(text)
        words += size

    reply = "Here's what I know about you. " + " ".join(spoken)
    if left_out:
        names = _join_words([_GROUP_NAMES[label] for label in left_out])
        reply += f" I also know about your {names}. Just ask."
    elif trimmed:
        reply += " I have a few smaller details too. Just ask."
    return reply

def _topic_label(word):
    word = re.sub(r"\s+", " ", word.strip().lower())
    for candidate in (word, word.replace(" ", ""), word.split()[-1] if word else ""):
        if candidate in _TOPICS:
            return _TOPICS[candidate]
    return None

def _handle_recall_topic(word):
    """'what do you know about my setup' -> that group in full, or None if not a topic."""
    word = word.strip().lower()
    label = _topic_label(word)
    if label is None:
        return None
    from jarvis_retrieval import SKIP_KEYS
    facts = [f for f in database.get_active_facts() if f["key"] not in SKIP_KEYS]
    groups, _ = _recall_groups(facts, limit=10)
    text = next((t for l, t in groups if l == label), None)
    if text is None:
        return f"I don't have anything saved about your {word} yet."
    return "Here's what I know. " + text


def _handle_recall_one(thing, explicit):
    thing = re.sub(r"\s+(?:again|is|are)$", "", thing.strip(), flags=re.I)
    matches = _find_facts(thing)
    if matches:
        return " ".join(_fact_sentence(f) + "." for f in matches[:3])
    return None            # not a saved key: let Qwen answer, using memory search


def _when_phrase(started_at, now=None):
    """'earlier today', 'yesterday', '3 days ago' or 'on March 5' for a stored UTC time."""
    try:
        started = (datetime.strptime(str(started_at), "%Y-%m-%d %H:%M:%S")
                   .replace(tzinfo=timezone.utc).astimezone())
    except ValueError:
        return None
    now = now or datetime.now().astimezone()
    days = (now.date() - started.date()).days
    if days <= 0:
        return "earlier today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    return f"on {started:%B} {started.day}"


def _swap(text, pattern, replacement):
    """Regex replace that keeps a capital first letter ("The user" -> "You")."""
    def fix(match):
        word = replacement
        return word[0].upper() + word[1:] if match.group(0)[0].isupper() else word
    return re.sub(pattern, fix, text, flags=re.I)


def _speakable(summary):
    """Stored summaries may say "the user"; Jarvis should say "you" when speaking."""
    text = str(summary)
    if not re.search(r"\bthe user\b", text, re.I):
        return text
    text = _swap(text, r"\bthe user's\b", "your")
    text = _swap(text, r"\bthe user\b", "you")
    # With "the user" as the subject, "they/their" in the same note mean the user too.
    text = _swap(text, r"\bthemselves\b", "yourself")
    text = _swap(text, r"\btheir\b", "your")
    text = _swap(text, r"\bthey\b", "you")
    for wrong, right in (("you was", "you were"), ("you is", "you are"),
                         ("you has", "you have"), ("you does", "you do"),
                         ("you doesn't", "you don't")):
        text = _swap(text, r"\b" + wrong + r"\b", right)
    return text

def _lead_in(lead, text):
    """'Earlier today' + 'We talked about X' -> 'Earlier today, we talked about X'."""
    if text.startswith(("We ", "We'")):
        return f"{lead}, {text[0].lower()}{text[1:]}"
    return f"{lead}. {text}"

def _handle_past_talks(count):
    """Speak the latest conversation summaries (count=1: just the last one)."""
    summaries = database.get_recent_summaries(count)
    if not summaries:
        return "I don't have a summary of any earlier conversation yet."

    if count == 1:
        item = summaries[0]
        when = _when_phrase(item["started_at"])
        intro = f"Our last conversation was {when}." if when else "Here is our last conversation."
        return f"{intro} {_speakable(item['summary'])}"

    parts = ["Here is what we covered recently."]
    last_when = None
    for item in summaries:
        when = _when_phrase(item["started_at"])
        if when and when == last_when:
            lead = "Before that"                 # same day as the previous one
        elif when:
            lead = when[0].upper() + when[1:]
        else:
            lead = None
        last_when = when
        text = _speakable(item["summary"])
        parts.append(_lead_in(lead, text) if lead else text)
    return " ".join(parts)


def _handle_undo():
    global _last_change
    change, _last_change = _last_change, None
    if not change:
        return "Tell me which fact to forget, for example: forget my city."

    label = _label(change["key"])
    if change["action"] == "added":
        database.retire_fact(change["fact_id"])
        return "Okay, forgotten."
    if change["action"] == "superseded" and change["old_value"] is not None:
        database.upsert_fact(change["category"], change["key"], change["old_value"],
                             confidence=1.0, source="voice_command")
        return f"Okay, I put your {label} back to {_speak_value(change['old_value'])}."
    return "I can't undo that one. Tell me what to forget, for example: forget my city."


def _handle_forget(thing, has_my):
    matches = _find_facts(thing)
    if not matches:
        if has_my:
            return f"I don't have anything saved about your {thing}."
        return None                   # "forget the previous question": not a memory command
    if len(matches) > 1:
        names = _join_words([_label(f["key"]) for f in matches[:4]])
        return f"I have a few matches: {names}. Which one should I forget?"

    fact = matches[0]
    database.retire_fact(fact["id"])
    return f"Okay, I've forgotten your {_label(fact['key'])}."


def _handle_remember(rest, message_id):
    rest = rest.strip()
    if rest.lower().startswith("your "):
        return "I only remember facts about you."

    statement = parse_statement(rest)
    if statement is None:
        if len(rest.split()) < 3:
            return "What should I remember?"
        statement = _build_note(rest)

    _, reply = _save(statement, message_id)
    return reply


def _handle_correction(rest, message_id, explicit):
    statement = parse_statement(rest)
    if statement is None:
        return None

    if not explicit:
        # "actually my cat is sleeping" must not become a fact: only accept a soft
        # correction when it changes something Jarvis already knows.
        existing = database.get_fact_history(statement["category"], statement["key"])
        current = [f for f in existing if f["status"] == "active"]
        if not current:
            return None

    result, reply = _save(statement, message_id)
    if result and result["action"] == "superseded" and not statement.get("is_note"):
        return "Fixed. " + reply
    return reply


def _route(text, message_id):
    global _pending_wipe_until

    pending = _handle_pending(text)
    if pending is not None:
        return pending

    t = _clean_command(text)
    if not t:
        return None

    if _LAST_TALK_RE.match(t) or _SUMMARIZE_LAST_RE.match(t):
        return _handle_past_talks(1)

    if _RECENT_TALKS_RE.match(t):
        return _handle_past_talks(MAX_SUMMARIES_SPOKEN)

    if _RECALL_ALL_RE.match(t):
        return _handle_recall_all()

    if _WIPE_RE.match(t):
        _pending_wipe_until = time.time() + PENDING_TIMEOUT
        count = len(database.get_active_facts())
        if count == 0:
            _pending_wipe_until = 0.0
            return "My memory about you is already empty."
        return (f"Are you sure? That will make me forget all {count} "
                f"thing{'s' if count != 1 else ''} I know about you. Say yes or no.")

    if _UNDO_RE.match(t):
        return _handle_undo()

    match = _FORGET_RE.match(t)
    if match:
        return _handle_forget(match.group(2).strip(), has_my=bool(match.group(1) and
                                                                  match.group(1).lower() == "my"))

    match = _EXPLICIT_CORRECTION_RE.match(t)
    if match:
        return _handle_correction(match.group(1) or match.group(2), message_id, explicit=True)

    match = _REMEMBER_RE.match(t)
    if match:
        return _handle_remember(match.group(1), message_id)

    match = _RECALL_EXPLICIT_RE.match(t)
    if match:
        return _handle_recall_one(match.group(1), explicit=True)

    match = _RECALL_TOPIC_RE.match(t)
    if match:
        reply = _handle_recall_topic(match.group(1))
        if reply is not None:
            return reply

    match = _RECALL_IMPLICIT_RE.match(t)
    if match:
        return _handle_recall_one(match.group(1), explicit=False)

    match = _SOFT_CORRECTION_RE.match(t)
    if match:
        return _handle_correction(match.group(1), message_id, explicit=False)

    return None


def handle_memory_command(text, conversation_id=None, message_id=None):
    """Return a spoken reply if `text` is a memory command, otherwise None.

    When a command is handled, its user message is marked as processed so the
    session-end extractor does not process it a second time.
    """
    reply = _route(text, message_id)
    if reply is not None and message_id is not None:
        try:
            database.mark_memory_message_processed(message_id, "memory_command")
        except Exception:
            pass
    return reply
