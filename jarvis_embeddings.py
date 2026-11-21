"""Local embeddings for Jarvis (Phase 6): matching by MEANING.

Phase 5 finds facts that share WORDS with your question. This module turns facts,
past-conversation summaries and your question into vectors with a small local
model (through Ollama), so "what do I do on weekends" can find a "hobbies" fact
even though they share no word.

What lives where
  * database.py stores the vectors (table `embeddings`, float32 bytes).
  * This file asks Ollama for vectors, keeps them in memory, and compares them.
  * jarvis_retrieval.py mixes the result with the word search (hybrid search).

Rules it follows
  * A vector is made once per fact. If a fact changes, its text fingerprint changes
    and only that fact is embedded again. Nothing needs to be run by hand.
  * Ollama problems NEVER stop Jarvis. After a failure this module leaves Ollama
    alone for RETRY_AFTER seconds (so no turn waits for a timeout) and retrieval
    silently uses the Phase 5 word search.
  * Set ENABLED = False to switch vectors off completely.

Setup (once):   ollama pull nomic-embed-text
"""

import hashlib
import time

import numpy as np
import requests

import database

ENABLED = True
EMBED_MODEL = "nomic-embed-text"
OLLAMA_EMBED_URL = "http://127.0.0.1:11434/api/embed"
KEEP_ALIVE = -1                   # keep the (small) model loaded, like the chat model
BATCH_SIZE = 32                   # texts per Ollama request
TIMEOUT = (3, 30)                 # connect, read (seconds)
RETRY_AFTER = 120                 # seconds to leave Ollama alone after a failure
MAX_QUERY_CHARS = 500
TEXT_VERSION = 1                  # bump to force every vector to be made again


class EmbeddingError(Exception):
    """Ollama could not produce embeddings (message is safe to show to the user)."""


_unavailable_until = 0.0
_last_error = ""
_cache = {}                       # kind -> {"signature", "ids", "matrix"}


# ---------------------------------------------------------------
# Availability
# ---------------------------------------------------------------

def is_available():
    return ENABLED and time.time() >= _unavailable_until


def last_error():
    return _last_error


def _mark_unavailable(reason):
    global _unavailable_until, _last_error
    _last_error = str(reason)
    _unavailable_until = time.time() + RETRY_AFTER


def reset():
    """Forget the failure state and the in-memory vectors (used by tests and warm_up)."""
    global _unavailable_until, _last_error
    _unavailable_until = 0.0
    _last_error = ""
    _cache.clear()


# ---------------------------------------------------------------
# Talking to Ollama
# ---------------------------------------------------------------

def _doc_prefix():
    # nomic-embed-text is trained with task prefixes; other models do not use them.
    return "search_document: " if EMBED_MODEL.lower().startswith("nomic") else ""


def _query_prefix():
    return "search_query: " if EMBED_MODEL.lower().startswith("nomic") else ""


def _request_embeddings(texts):
    """One request to Ollama. Returns a list of vectors (lists of floats)."""
    try:
        response = requests.post(
            OLLAMA_EMBED_URL,
            json={"model": EMBED_MODEL, "input": texts,
                  "keep_alive": KEEP_ALIVE, "truncate": True},
            timeout=TIMEOUT,
        )
    except requests.RequestException as error:
        raise EmbeddingError(f"cannot reach Ollama ({error.__class__.__name__})")

    if response.status_code == 404 or "not found" in response.text.lower()[:200]:
        raise EmbeddingError(
            f"the embedding model '{EMBED_MODEL}' is not installed (run: ollama pull {EMBED_MODEL})")
    if not response.ok:
        raise EmbeddingError(f"Ollama answered HTTP {response.status_code}: {response.text[:100]}")

    try:
        vectors = response.json().get("embeddings")
    except ValueError:
        raise EmbeddingError("Ollama's reply was not valid JSON")
    if (not isinstance(vectors, list) or len(vectors) != len(texts)
            or not all(isinstance(v, list) and v for v in vectors)
            or len({len(v) for v in vectors}) != 1):
        raise EmbeddingError("Ollama returned embeddings in an unexpected shape")
    return vectors


def _normalize(matrix):
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (matrix / norms).astype(np.float32)


def embed_texts(texts):
    """Vectors for documents (facts, summaries): float32 matrix, one unit-length row each."""
    prefix = _doc_prefix()
    rows = []
    for start in range(0, len(texts), BATCH_SIZE):
        batch = [prefix + t for t in texts[start:start + BATCH_SIZE]]
        rows.extend(_request_embeddings(batch))
    return _normalize(np.asarray(rows, dtype=np.float32))


def embed_query(question):
    """Unit-length vector for the question, or None if vectors are unavailable."""
    if not is_available():
        return None
    text = " ".join(str(question or "").split())[:MAX_QUERY_CHARS]
    if not text:
        return None
    try:
        rows = _request_embeddings([_query_prefix() + text])
    except EmbeddingError as error:
        _mark_unavailable(error)
        return None
    return _normalize(np.asarray(rows, dtype=np.float32))[0]


# ---------------------------------------------------------------
# What gets embedded
# ---------------------------------------------------------------

def _join(value):
    if isinstance(value, list):
        items = [str(v).strip() for v in value if str(v).strip()]
        if len(items) <= 2:
            return " and ".join(items)
        return ", ".join(items[:-1]) + f", and {items[-1]}"
    return str(value).strip()


_HINTS = {
    "likes": "things they like and enjoy, hobbies, what they do for fun, weekends and free time",
    "hobbies": "hobbies and free time",
    "interests": "interests and things they enjoy",
    "dislikes": "things they dislike or avoid",
    "learning": "things they are studying or learning",
    "current_projects": "what they are building or working on",
    "occupation": "their job and work",
    "city": "the city where they live",
    "country": "the country they live in",
    "age": "how old they are",
    "birthday": "their date of birth",
    "name": "what they are called",
    "gpu": "graphics card",
    "cpu": "processor",
    "ram": "memory",
    "os": "operating system",
    "monitor": "screen or display",
    "editor": "code editor, the program they write code in",
    "bedtime": "when they go to sleep",
    "dream": "their biggest goal and ambition in life",
    "sister_name": "their sister",
    "dog_name": "their pet dog",
}


def fact_text(fact):
    """'The user's gpu (graphics card): Intel Arc B580'. Notes skip the category."""
    key = fact["key"]
    value = _join(fact["value"])
    if key.startswith("note"):
        return f"A note: {value}"
    label = key.replace("_", " ")
    hint = _HINTS.get(key)
    if hint is None and key.startswith("favorite"):
        hint = "their favorite " + label.replace("favorite", "").strip()
    return f"The user's {label} ({hint}): {value}" if hint else f"The user's {label}: {value}"


def summary_text(item):
    topics = ", ".join(item.get("topics") or [])
    return f"{item['summary']} Topics: {topics}" if topics else str(item["summary"])


def _fingerprint(text):
    return hashlib.sha1(f"v{TEXT_VERSION}|{text}".encode("utf-8")).hexdigest()


# ---------------------------------------------------------------
# The index: every vector for one kind, loaded or created as needed
# ---------------------------------------------------------------

def _load_or_embed(kind, texts, hashes):
    """Return {ref_id: vector}, embedding only what is missing or changed."""
    stored = database.get_embeddings(kind, EMBED_MODEL)

    vectors, need = {}, []
    for ref_id in texts:
        entry = stored.get(ref_id)
        if entry and entry[0] == hashes[ref_id] and len(entry[2]) == entry[1] * 4:
            vectors[ref_id] = np.frombuffer(entry[2], dtype=np.float32)
        else:
            need.append(ref_id)

    if len({v.shape[0] for v in vectors.values()}) > 1:      # mixed sizes: start over
        vectors, need = {}, list(texts)

    if need:
        fresh = embed_texts([texts[r] for r in need])
        database.save_embeddings(
            kind, EMBED_MODEL,
            [(r, hashes[r], fresh.shape[1], fresh[i].tobytes()) for i, r in enumerate(need)])
        for i, ref_id in enumerate(need):
            vectors[ref_id] = fresh[i]
        if any(v.shape[0] != fresh.shape[1] for v in vectors.values()):
            raise EmbeddingError("the embedding model's vector size changed; restart Jarvis")

    stale = [r for r in stored if r not in texts]            # forgotten or replaced rows
    if stale:
        database.delete_embeddings(kind, EMBED_MODEL, stale)
    return vectors


def get_index(kind, items):
    """(ids, matrix) for these facts ('fact') or summaries ('summary'), or None.

    `items` are the rows to search (dicts with an "id"). The matrix has one
    unit-length row per id. Returns None when vectors are unavailable.
    """
    if not is_available():
        return None

    text_of = fact_text if kind == "fact" else summary_text
    texts = {item["id"]: text_of(item) for item in items}
    hashes = {ref_id: _fingerprint(text) for ref_id, text in texts.items()}
    signature = (EMBED_MODEL, tuple(sorted(hashes.items())))

    cached = _cache.get(kind)
    if cached and cached["signature"] == signature:
        return cached["ids"], cached["matrix"]

    try:
        vectors = _load_or_embed(kind, texts, hashes)
    except EmbeddingError as error:
        _mark_unavailable(error)
        return None

    ids = sorted(vectors)
    matrix = (np.vstack([vectors[i] for i in ids]).astype(np.float32)
              if ids else np.zeros((0, 0), dtype=np.float32))
    _cache[kind] = {"signature": signature, "ids": ids, "matrix": matrix}
    return ids, matrix


# ---------------------------------------------------------------
# Startup
# ---------------------------------------------------------------

def warm_up(facts=None, summaries=None):
    """Load the embedding model and make every missing vector. Returns a status line.

    jarvis_retrieval.vector_warm_up() passes the same rows retrieval searches, so
    the first question does not have to rebuild anything.
    """
    if not ENABLED:
        return "Vector memory is switched off (ENABLED = False). Using word search only."

    reset()
    start = time.perf_counter()
    try:
        _request_embeddings([_query_prefix() + "warm up"])
        if facts is None:
            facts = database.get_active_facts()
        if summaries is None:
            summaries = database.get_recent_summaries(500)
        if get_index("fact", facts) is None or get_index("summary", summaries) is None:
            raise EmbeddingError(last_error() or "could not build the vector index")
    except EmbeddingError as error:
        _mark_unavailable(error)
        return (f"Vector memory unavailable: {error}. "
                "Jarvis will use word search only.")
    except Exception as error:                      # a database problem, for example
        _mark_unavailable(error)
        return f"Vector memory unavailable: {error}. Jarvis will use word search only."

    return (f"Vector memory ready: {EMBED_MODEL}, {len(facts)} fact(s) and "
            f"{len(summaries)} summary(ies) embedded ({time.perf_counter() - start:.2f}s).")
