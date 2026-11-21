"""Shared rules for what may become a fact (Phase 7)."""
import re

KEY_ALIASES = {
    "job": "occupation", "job_title": "occupation", "profession": "occupation",
    "work": "occupation", "career": "occupation",
    "hometown": "city", "home_town": "city", "location": "city", "town": "city",
    "residence": "city", "current_city": "city",
    "full_name": "name", "first_name": "name", "user_name": "name",
    "graphics_card": "gpu", "video_card": "gpu", "graphics": "gpu",
    "processor": "cpu", "operating_system": "os",
    "birth_date": "birthday", "date_of_birth": "birthday", "dob": "birthday",
    "hobby": "hobbies", "interest": "interests", "skill": "skills",
    "like": "likes", "dislike": "dislikes", "current_project": "current_projects",
}

# The category a well-known key always lives in (stops personal.likes vs interests.likes).
CATEGORY_OF = {
    "name": "personal", "nickname": "personal", "age": "personal",
    "birthday": "personal", "city": "personal", "country": "personal",
    "occupation": "personal",
    "gpu": "hardware", "cpu": "hardware", "ram": "hardware", "os": "software",
    "likes": "interests", "hobbies": "interests", "interests": "interests",
    "skills": "interests", "learning": "interests",
    "dislikes": "preferences", "current_projects": "projects",
}

TIME_WORDS = {"today", "tonight", "tomorrow", "yesterday", "now", "later", "soon",
              "currently", "temporarily"}
STATE_WORDS = {"hot", "cold", "warm", "slow", "laggy", "lagging", "broken", "crashing",
               "crashed", "frozen", "stuck", "dead", "overheating", "working", "low",
               "full", "empty", "tired", "busy", "fine", "good", "bad", "great", "okay",
               "ok", "terrible", "annoying", "weird", "sleeping", "noisy", "loud",
               "dirty", "charging", "updating", "loading", "failing"}
FILLER = {"not", "very", "so", "too", "a", "bit", "little", "really", "quite", "pretty",
          "kinda", "sort", "of", "is", "are", "be", "being", "just", "still", "always",
          "never"}
NOT_NAMES = {"later", "back", "tomorrow", "tonight", "anything", "whatever", "sir",
             "jarvis", "buddy", "man", "dude", "when", "if"}

SECRET_RE = re.compile(
    r"\b(?:password|passcode|passphrase|pin(?: code| number)?|cvv|ssn|social security|"
    r"credit card|card number|bank account|api key|secret key|private key)\b", re.I)
SENSITIVE_KEY_RE = re.compile(
    r"religio|politic|health|medical|diagnos|disease|medication|illness|sexual|ethnic", re.I)
SENSITIVE_VALUE_RE = re.compile(
    r"\b(?:muslim|islam|hindu|hinduism|buddhis[tm]|jewish|judaism|atheis[tm]|democrat|"
    r"republican|depress\w*|anxiety|diabet\w*|cancer|pregnan\w*|suicid\w*)\b", re.I)


def canonical_key(key):
    key = str(key).strip().lower().replace("favourite", "favorite")
    key = "_".join(key.split())
    return KEY_ALIASES.get(key, key)


def canonical_pair(category, key):
    key = canonical_key(key)
    return CATEGORY_OF.get(key, category), key


def _text(value):
    items = value if isinstance(value, list) else [value]
    return " ".join(str(i) for i in items)


def is_secret(key, value):
    return bool(SECRET_RE.search(f"{key} {_text(value)}".replace("_", " ")))


def rejection_reason(category, key, value):
    """Why an AUTOMATIC save (rules or model) must not store this, or None.
    Explicit 'remember ...' commands only use is_secret()."""
    if is_secret(key, value):
        return "secret"
    text = _text(value)
    if SENSITIVE_KEY_RE.search(key.replace("_", " ")) or SENSITIVE_VALUE_RE.search(text):
        return "sensitive topic"
    items = value if isinstance(value, list) else [value]
    for item in items:
        words = re.findall(r"[a-z0-9']+", str(item).lower())
        if not words:
            return "empty"
        if set(words) & TIME_WORDS:
            return "temporary"
        if set(words) <= (STATE_WORDS | FILLER):
            return "temporary"
        if key == "name" and (len(words) > 3 or set(words) & NOT_NAMES):
            return "not a name"
    return None