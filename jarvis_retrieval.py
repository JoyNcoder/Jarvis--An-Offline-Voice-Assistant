"""Per-turn memory retrieval for Jarvis (Phase 5).

Instead of putting the same 12 facts in the system prompt on every turn, this
module looks at what you just said and builds a small block of memory for THAT
turn:

    core facts      always: name, city, occupation... (the basics)
    relevant facts  the few facts whose words match the question
    relevant past   up to two earlier-conversation summaries that match

Used by j.py:
    ctx = jarvis_retrieval.retrieve(question)
    ctx["text"]     the block to append to the system prompt ("" if nothing)
    ctx["report"]   one short line for the console

How matching works
  1. Remove filler words ("what", "is", "my", "do"...), keep the content words.
  2. Add a few synonyms ("live" also looks for city and country).
  3. Search with SQLite FTS5 (stemming: "lives" matches "live"), best match first.
     If FTS5 is missing or a query fails, a plain keyword matcher does the same job.
  Words must overlap to match, so "what's the capital of France" brings in no
  facts at all. Matching by MEANING ("what's inside my computer" -> gpu) is
  Phase 6 (embeddings).

Phase 6 adds a second way to find things, by MEANING (jarvis_embeddings.py):
the question and every fact are turned into vectors by a small local model, and
the closest ones are mixed with the word-search results (hybrid search). If the
embedding model is not available, everything above still works on its own.

With few facts (up to INJECT_ALL_UP_TO) everything is injected, because
searching a tiny profile gains nothing. Set INJECT_ALL_UP_TO = 0 to always search.
"""

import json
import re
import time

import database

try:                                   # vectors are optional: Phase 5 search works without
    import numpy as np
    import jarvis_embeddings as emb
except Exception:
    np = None
    emb = None

try:                                   # reuse the speech-friendly summary helpers
    from memory_commands import _when_phrase, _speakable
except Exception:                      # never let a cosmetic helper break retrieval
    def _when_phrase(started_at, now=None):
        return None

    def _speakable(summary):
        return str(summary)

# ---------------------------------------------------------------
# Settings
# ---------------------------------------------------------------

INJECT_ALL_UP_TO = 8          # at most this many facts: inject them all, no search
MAX_CORE_FACTS = 6
MAX_RELEVANT_FACTS = 6
MAX_PROFILE_FACTS = 12        # "tell me about myself": a broad profile instead
MAX_SUMMARIES = 2
MAX_FACT_CHARS = 120
MAX_SUMMARY_CHARS = 300

# Phase 6 (meaning search). Cosine similarity runs from 0 (unrelated) to 1 (same).
FACT_MIN_SIM = 0.53           # a fact must be at least this close to count
SUMMARY_MIN_SIM = 0.55
SIM_MARGIN = 0.08             # ...and within this distance of the best match
WORD_ONLY_MIN_SIM = 0.45      # a word match this far from the question in meaning is a coincidence
MAX_VECTOR_HITS = 12
RRF_K = 60                    # rank fusion constant (higher = flatter blending)

# Always-on facts, in this order (the rest of the profile is found by search).
CORE_KEYS = ("name", "nickname", "city", "country", "occupation", "age")

# Keys never injected, even if stored (same idea as _PROFILE_SKIP_KEYS in j.py).
SKIP_KEYS = {
    "mood", "self_correction", "assistant_name", "preferred_assistant_name",
    "emoji_usage", "initial_interaction", "compliments", "communication",
}

_CATEGORY_ORDER = ("personal", "hardware", "projects", "preferences", "interests")

_SELF_RE = re.compile(
    r"\b(?:about (?:me|myself)|who am i|know me|my profile|my life)\b", re.I)

# ---------------------------------------------------------------
# Words: filler to ignore, and synonyms to add
# ---------------------------------------------------------------

_STOPWORDS = set("""
a an the and or but if so of to in on at by for with about as into than then that this
these those there here is am are was were be been being do does did done doing have has
had having will would shall should can could may might must i me my mine myself you your
yours we us our they them their he him his she her it its what whats which who whom whose
when where why how not no yes please tell say said give make get got go going know think
want need help let just really very also too still ever some any all more most much many
few lot lots other another again now today tomorrow yesterday tonight okay ok hey hi hello
jarvis thanks thank sure well oh um uh remember remind recall cant dont didnt isnt wont im
ive id ill youre thats theres lets doesnt wasnt arent couldnt wouldnt shouldnt
""".split())


def _stem(word):
    """Very light stemmer, used for overlap checks: crashes/crashing/crashed -> crash."""
    for suffix in ("ing", "ed", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[:-len(suffix)]
            break
    if word.endswith("e") and len(word) > 3:
        word = word[:-1]
    return word


_RAW_SYNONYMS = {
    "live": ("city", "country", "home"),
    "home": ("city", "country"),
    "located": ("city", "country"),
    "location": ("city", "country"),
    "address": ("city", "country"),
    "based": ("city", "country"),
    "work": ("occupation", "job"),
    "job": ("occupation", "work"),
    "career": ("occupation", "job"),
    "profession": ("occupation", "job"),
    "employed": ("occupation", "job"),
    "employer": ("occupation", "job"),
    "company": ("occupation", "job"),
    "old": ("age", "birthday"),
    "age": ("birthday",),
    "born": ("age", "birthday"),
    "birthday": ("age", "born"),
    "name": ("nickname",),
    "called": ("name", "nickname"),
    "computer": ("hardware", "gpu", "cpu", "ram", "laptop", "pc"),
    "machine": ("hardware", "gpu", "cpu", "ram", "laptop", "pc"),
    "rig": ("hardware", "gpu", "cpu", "ram", "pc"),
    "hardware": ("gpu", "cpu", "ram", "laptop", "pc"),
    "specs": ("gpu", "cpu", "ram", "laptop", "pc"),
    "spec": ("gpu", "cpu", "ram", "laptop", "pc"),
    "setup": ("hardware", "gpu", "cpu", "monitor"),
    "graphics": ("gpu",),
    "card": ("gpu",),
    "processor": ("cpu",),
    "favorite": ("favorite",),
    "favourite": ("favorite",),
    "prefer": ("favorite",),
    "preferred": ("favorite",),
    "love": ("likes", "interests"),
    "enjoy": ("likes", "interests"),
    "hobby": ("hobbies", "likes", "interests"),
    "hobbies": ("hobbies", "likes", "interests"),
    "interest": ("interests", "likes"),
    "interests": ("interests", "likes"),
    "hate": ("dislikes",),
    "dislike": ("dislikes",),
    "learn": ("learning",),
    "study": ("learning",),
    "course": ("learning",),
    "build": ("projects", "project"),
    "project": ("projects",),
    "family": ("sister", "brother", "mother", "father", "mom", "dad", "wife", "husband",
               "son", "daughter"),
    "sibling": ("sister", "brother"),
    "kid": ("son", "daughter"),
    "child": ("son", "daughter"),
    "pet": ("dog", "cat"),
    "editor": ("ide", "software"),
    "coding": ("editor", "ide", "software"),
    "programming": ("editor", "ide", "software"),
    "routine": ("schedule", "bedtime"),
    "schedule": ("routine", "bedtime"),
    "sleep": ("bedtime", "routine"),
    "goal": ("goals", "dream"),
    "dream": ("goals", "goal"),
    "weekend": ("hobbies", "likes", "interests"),
    "fun": ("hobbies", "likes", "interests"),
    "bored": ("hobbies", "likes", "interests"),
    "relax": ("hobbies", "likes", "interests"),
}
_SYNONYMS = {_stem(word): extra for word, extra in _RAW_SYNONYMS.items()}


def question_words(question):
    """Content words of a question: lowercase, no filler, no duplicates."""
    text = str(question or "").lower().replace("\u2019", "'")
    text = re.sub(r"'s\b", "", text).replace("'", "")
    words = []
    for word in re.findall(r"[a-z0-9]+", text):
        if len(word) < 2 or word in _STOPWORDS or word in words:
            continue
        words.append(word)
    return words


def search_terms(words):
    """The content words plus their synonyms."""
    terms = list(words)
    for word in words:
        for extra in _SYNONYMS.get(_stem(word), ()):
            if extra not in terms:
                terms.append(extra)
    return terms


def _match_query(terms):
    """FTS5 expression: every term quoted (so no search syntax can sneak in), OR-ed."""
    return " OR ".join(f'"{t}"' for t in terms if re.fullmatch(r"[a-z0-9]+", t))


# ---------------------------------------------------------------
# Finding facts and summaries
# ---------------------------------------------------------------

def _fact_text(fact):
    return (fact["key"].replace("_", " ") + " " + fact["category"] + " "
            + json.dumps(fact["value"], ensure_ascii=False))


def _stems(text):
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", str(text).lower())}


def _rank_facts_python(terms, facts):
    """Keyword fallback: facts sharing the most term stems with the question."""
    wanted = {_stem(t) for t in terms}
    scored = []
    for fact in facts:
        hits = len(wanted & _stems(_fact_text(fact)))
        if hits:
            scored.append((-hits, -fact.get("mention_count", 1), fact["id"], fact))
    scored.sort(key=lambda item: item[:3])
    return [item[3] for item in scored]


def _rank_facts(terms, facts):
    if not terms:
        return []
    if database.fts_available():
        try:
            found = database.search_facts_ranked(_match_query(terms),
                                                 limit=MAX_RELEVANT_FACTS * 4)
            return [f for f in found if f["key"] not in SKIP_KEYS]
        except Exception:
            pass                                   # fall through to the Python matcher
    return _rank_facts_python(terms, facts)


def _summary_candidates(terms):
    if terms and database.fts_available():
        try:
            return database.search_summaries_ranked(_match_query(terms), limit=10)
        except Exception:
            pass
    return database.get_recent_summaries(200)       # keyword fallback scans them all


def _vector_rank(kind, items, qvec, min_sim):
    """Items closest in MEANING to the question: [(item, similarity)], best first.

    Returns [] when nothing is close enough, and None when vectors are not
    available (the caller then keeps the plain word-search result).
    """
    if qvec is None or emb is None:
        return None
    if not items:
        return []
    index = emb.get_index(kind, items)
    if index is None:
        return None
    ids, matrix = index
    if not ids or matrix.shape[1] != qvec.shape[0]:
        return None if ids else []
    sims = matrix @ qvec
    order = np.argsort(-sims)
    best = float(sims[order[0]])
    if best < min_sim:
        return []
    cutoff = max(min_sim, best - SIM_MARGIN)
    by_id = {item["id"]: item for item in items}
    return [(by_id[ids[i]], float(sims[i])) for i in order[:MAX_VECTOR_HITS]
            if sims[i] >= cutoff]

def _similarities(kind, items, qvec):
    """{item id: similarity} for every item, or None when vectors are unavailable."""
    if qvec is None or emb is None or not items:
        return None
    index = emb.get_index(kind, items)
    if index is None:
        return None
    ids, matrix = index
    if not ids or matrix.shape[1] != qvec.shape[0]:
        return None
    sims = matrix @ qvec
    return {item_id: float(sims[i]) for i, item_id in enumerate(ids)}

def _fuse(*ranked_lists):
    """Blend ranked lists (reciprocal rank fusion): items high in several lists win."""
    scores, items = {}, {}
    for ranked in ranked_lists:
        for position, item in enumerate(ranked):
            scores[item["id"]] = scores.get(item["id"], 0.0) + 1.0 / (RRF_K + position + 1)
            items.setdefault(item["id"], item)
    return [items[i] for i in sorted(scores, key=lambda i: (-scores[i], i))]


def _word_summaries(words, terms):
    """Summaries sharing enough real words with the question, best first.

    A single shared word is enough for a short question, but a question with
    four or more content words must share two, so one common word such as
    "python" does not drag in every coding chat.
    """
    if not words:
        return []
    needed = 2 if len(words) >= 4 else 1
    wanted = {_stem(w) for w in words}
    scored = []
    for position, item in enumerate(_summary_candidates(terms)):
        overlap = len(wanted & _stems(item["summary"] + " " + " ".join(item["topics"])))
        if overlap >= needed:
            scored.append((-overlap, position, item))
    scored.sort(key=lambda entry: entry[:2])
    return [entry[2] for entry in scored]


def _relevant_summaries(words, terms, qvec=None, all_summaries=None):
    """Past conversations that match by words and, if available, by meaning.

    Returns (summaries, used_vectors).
    """
    by_words = _word_summaries(words, terms)
    by_meaning = _vector_rank("summary", all_summaries or [], qvec, SUMMARY_MIN_SIM)
    if by_meaning is None:
        return by_words[:MAX_SUMMARIES], False
    return _fuse(by_words, [item for item, _ in by_meaning])[:MAX_SUMMARIES], True


def _ordered(facts):
    """Personal first, then hardware, projects, preferences, interests, the rest."""
    def rank(fact):
        category = fact["category"]
        position = (_CATEGORY_ORDER.index(category) if category in _CATEGORY_ORDER
                    else len(_CATEGORY_ORDER))
        return (position, fact["key"])
    return sorted(facts, key=rank)


def _core_facts(facts):
    by_key = {f["key"]: f for f in facts if f["category"] == "personal"}
    return [by_key[k] for k in CORE_KEYS if k in by_key][:MAX_CORE_FACTS]


# ---------------------------------------------------------------
# Building the prompt block
# ---------------------------------------------------------------

def _join(value):
    if isinstance(value, list):
        items = [str(v).strip() for v in value if str(v).strip()]
        if len(items) <= 2:
            return " and ".join(items)
        return ", ".join(items[:-1]) + f", and {items[-1]}"
    return str(value).strip()


def _fact_line(fact):
    label = "note" if fact["key"].startswith("note") else fact["key"].replace("_", " ")
    text = _join(fact["value"])
    if len(text) > MAX_FACT_CHARS:
        text = text[:MAX_FACT_CHARS - 3].rstrip() + "..."
    return f"- {label}: {text}"


def _summary_line(item):
    text = _speakable(item["summary"])
    if len(text) > MAX_SUMMARY_CHARS:
        text = text[:MAX_SUMMARY_CHARS - 3].rstrip() + "..."
    when = _when_phrase(item.get("started_at"))
    return f"- {when}: {text}" if when else f"- {text}"


def _build_text(facts, summaries):
    parts = []
    if facts:
        parts.append("What you know about the user from previous sessions "
                     "(use only when relevant; do not recite this list):\n"
                     + "\n".join(_fact_line(f) for f in facts))
    if summaries:
        parts.append("Earlier conversations that may be relevant "
                     "(mention one only if it helps; do not recite):\n"
                     + "\n".join(_summary_line(s) for s in summaries))
    return "\n\n".join(parts)


def retrieve(question, force_search=False, use_vectors=True):
    """Pick the memory for this turn.

    Returns {"mode", "facts", "summaries", "text", "report", "vectors"} where mode is
      "all"      few facts: all of them
      "profile"  "tell me about myself": a broad slice of the profile
      "search"   core facts plus the facts that match the question
    `force_search=True` skips the "all" shortcut (used by debug_retrieval.py).
    `use_vectors=False` uses the word search only. "vectors" says whether the
    meaning search contributed to this answer.
    """
    began = time.perf_counter()
    facts = [f for f in database.get_active_facts() if f["key"] not in SKIP_KEYS]
    words = question_words(question)
    terms = search_terms(words)

    all_mode = len(facts) <= INJECT_ALL_UP_TO and not force_search
    profile_mode = not all_mode and bool(_SELF_RE.search(str(question or "")))
    search_mode = not all_mode and not profile_mode

    all_summaries = []
    qvec = None
    if use_vectors and emb is not None and words:
        all_summaries = database.get_recent_summaries(500)
        if search_mode or all_summaries:                 # otherwise nothing to compare
            qvec = emb.embed_query(question)

    summaries, summary_vectors = _relevant_summaries(words, terms, qvec, all_summaries)
    fact_vectors = False

    if all_mode:
        mode, chosen = "all", _ordered(facts)
    elif profile_mode:
        mode, chosen = "profile", _ordered(facts)[:MAX_PROFILE_FACTS]
    else:
        mode = "search"
        core = _core_facts(facts)
        core_ids = {f["id"] for f in core}
        by_words = _rank_facts(terms, facts)
        sims = _similarities("fact", facts, qvec)
        if sims is not None:
            by_words = [f for f in by_words if sims.get(f["id"], 1.0) >= WORD_ONLY_MIN_SIM]
        by_meaning = _vector_rank("fact", facts, qvec, FACT_MIN_SIM)
        if by_meaning is None:
            ranked = by_words
        else:
            fact_vectors = True
            ranked = _fuse(by_words, [item for item, _ in by_meaning])
        relevant = [f for f in ranked if f["id"] not in core_ids]
        chosen = core + relevant[:MAX_RELEVANT_FACTS]

    used_vectors = fact_vectors or summary_vectors
    text = _build_text(chosen, summaries)
    elapsed_ms = (time.perf_counter() - began) * 1000
    report = (f"(Memory context: {len(chosen)} of {len(facts)} facts "
              f"[{mode}{'+vec' if used_vectors else ''}], "
              f"{len(summaries)} past conversation(s), {elapsed_ms:.0f} ms)")
    return {"mode": mode, "facts": chosen, "summaries": summaries,
            "text": text, "report": report, "vectors": used_vectors}


def build_context(question):
    """Just the text block for the system prompt ("" when there is nothing)."""
    return retrieve(question)["text"]


def vector_warm_up():
    """Prepare meaning search at startup (loads the model, embeds new facts).

    Returns one status line for the console. Never raises.
    """
    if emb is None:
        return "Vector memory unavailable (numpy or jarvis_embeddings missing). Using word search only."
    try:
        facts = [f for f in database.get_active_facts() if f["key"] not in SKIP_KEYS]
        return emb.warm_up(facts, database.get_recent_summaries(500))
    except Exception as error:
        return f"Vector memory unavailable: {error}. Using word search only."


def startup_report():
    """One console line for startup: what the search covers."""
    facts, summaries = database.count_searchable()
    engine = "FTS5 search" if database.fts_available() else "keyword matching (no FTS5)"
    return (f"Memory search ready: {facts} fact(s), {summaries} conversation "
            f"summary(ies) indexed ({engine}).")
