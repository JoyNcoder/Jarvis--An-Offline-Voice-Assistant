"""Jarvis local database (v2).

SQLite only: no server, no internet. The file is created beside this module.

Tables
  conversations, messages   chat log (same structure as v1, for a future UI)
  memory_processed          which user messages the session-end extractor handled
  facts                     structured long-term memory (replaces the JSON blob)
  conversation_summaries    one short summary + topic tags per finished conversation
                            (episodic memory, written at session end)

A fact is never silently overwritten. When a single-value fact changes
("city": Dhaka -> Chittagong) the old row is marked 'superseded' and a new
'active' row is created, so history and an audit trail are kept.
List-valued facts (interests, skills...) grow instead: new items are merged in.

Timestamps are stored in UTC (SQLite CURRENT_TIMESTAMP).
"""

import json
import fact_rules
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DATABASE_DIR = Path(__file__).resolve().parent
DATABASE_FILE = DATABASE_DIR / "jarvis_brain.db"

FACT_STATUSES = ("active", "superseded", "deleted")


@contextmanager
def _connect():
    """Open a connection, commit on success, roll back on error, always close."""
    connection = sqlite3.connect(DATABASE_FILE, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database():
    """Create all tables and indexes if they do not exist yet."""
    with _connect() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT 'New conversation',
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conversation_id)
                    REFERENCES conversations(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_messages_conversation_id
                ON messages(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_messages_created_at
                ON messages(created_at);

            CREATE TABLE IF NOT EXISTS memory_processed (
                message_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,                 -- JSON: a string or a list of strings
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'superseded', 'deleted')),
                confidence REAL NOT NULL DEFAULT 0.9,
                source TEXT NOT NULL DEFAULT 'extraction',  -- extraction | voice_command | manual
                source_message_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                mention_count INTEGER NOT NULL DEFAULT 1,
                superseded_by INTEGER,
                retired_at TEXT,
                FOREIGN KEY (source_message_id)
                    REFERENCES messages(id) ON DELETE SET NULL,
                FOREIGN KEY (superseded_by)
                    REFERENCES facts(id) ON DELETE SET NULL
            );

            -- Only one ACTIVE fact per (category, key); history rows are unlimited.
            CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_one_active
                ON facts(category, key) WHERE status = 'active';
            CREATE INDEX IF NOT EXISTS idx_facts_status ON facts(status);

            -- One row per reviewed conversation. summary = '' means "reviewed, nothing
            -- worth remembering" (so it is not reviewed again).
            CREATE TABLE IF NOT EXISTS conversation_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL UNIQUE,
                summary TEXT NOT NULL,
                topics TEXT NOT NULL DEFAULT '[]',   -- JSON list of short tags
                message_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conversation_id)
                    REFERENCES conversations(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_summaries_created
                ON conversation_summaries(created_at);
            """
        )
        _init_search_index(connection)
        _init_embedding_store(connection)
        _migrate_messages_route(connection)      # <- new



# How a turn was handled: chat = went to Qwen, tool = direct time/math/unit answer,
# system = PC command, memory = remember/forget/recall, voice = voice change,
# ignored = goodbye / stop / filler. NULL = old row, not tagged yet.
MESSAGE_ROUTES = ("chat", "tool", "system", "memory", "voice", "ignored")


def _migrate_messages_route(connection):
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(messages)")}
    if "route" not in columns:
        connection.execute("ALTER TABLE messages ADD COLUMN route TEXT")


# ---------------------------------------------------------------
# Conversations and messages (same behaviour as v1)
# ---------------------------------------------------------------

def create_conversation(title="New conversation"):
    """Start a new conversation session and return its database ID."""
    with _connect() as connection:
        cursor = connection.execute(
            "INSERT INTO conversations (title) VALUES (?)", (title,)
        )
        return cursor.lastrowid


def delete_conversation(conversation_id):
    """Permanently delete a conversation; messages and summary go with it (CASCADE)."""
    with _connect() as connection:
        cursor = connection.execute(
            "DELETE FROM conversations WHERE id = ?", (int(conversation_id),))
        return cursor.rowcount > 0


def add_message(conversation_id, role, content, route=None):
    """Save one message and update its conversation's timestamp."""
    if role not in {"user", "assistant", "system"}:
        raise ValueError(f"Unsupported message role: {role}")
    if route is not None and route not in MESSAGE_ROUTES:
        raise ValueError(f"Unsupported message route: {route}")

    content = str(content).strip()
    if not content:
        return None

    with _connect() as connection:
        cursor = connection.execute(
            "INSERT INTO messages (conversation_id, role, content, route) VALUES (?, ?, ?, ?)",
            (conversation_id, role, content, route),
        )
        connection.execute(
            "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (conversation_id,),
        )
        return cursor.lastrowid


def set_message_route(message_id, route):
    """Tag a saved message with how it was handled. Returns True if a row changed."""
    if message_id is None:
        return False
    if route not in MESSAGE_ROUTES:
        raise ValueError(f"Unsupported message route: {route}")
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE messages SET route = ? WHERE id = ?", (route, int(message_id)))
        return cursor.rowcount > 0


def count_messages_by_route():
    """{'chat': n, 'system': n, ..., 'untagged': n}"""
    with _connect() as connection:
        rows = connection.execute(
            "SELECT COALESCE(route, 'untagged') AS r, COUNT(*) AS n "
            "FROM messages GROUP BY r").fetchall()
        return {row["r"]: row["n"] for row in rows}


def get_conversations(limit=None):
    """Return conversations newest-first, including message counts."""
    query = """
        SELECT c.id, c.title, c.started_at, c.updated_at,
               COUNT(m.id) AS message_count
        FROM conversations AS c
        LEFT JOIN messages AS m ON m.conversation_id = c.id
        GROUP BY c.id
        ORDER BY c.updated_at DESC, c.id DESC
    """
    params = ()
    if limit is not None:
        query += " LIMIT ?"
        params = (max(0, int(limit)),)

    with _connect() as connection:
        return [dict(row) for row in connection.execute(query, params).fetchall()]


def get_conversation(conversation_id):
    """Return one conversation's metadata, or None if it does not exist."""
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT c.id, c.title, c.started_at, c.updated_at,
                   COUNT(m.id) AS message_count
            FROM conversations AS c
            LEFT JOIN messages AS m ON m.conversation_id = c.id
            WHERE c.id = ?
            GROUP BY c.id
            """,
            (conversation_id,),
        ).fetchone()
        return dict(row) if row else None


def get_conversation_messages(conversation_id, limit=None):
    """Return a conversation's messages in chronological order."""
    query = """
        SELECT id, role, content, created_at, route
        FROM messages
        WHERE conversation_id = ?
        ORDER BY id
    """
    params = [conversation_id]
    if limit is not None:
        query += " LIMIT ?"
        params.append(max(0, int(limit)))

    with _connect() as connection:
        return [dict(row) for row in connection.execute(query, params).fetchall()]


def get_recent_messages(limit=20):
    """Return the latest messages across all conversations, oldest-first."""
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT m.id, m.conversation_id, c.title AS conversation_title,
                   m.role, m.content, m.created_at
            FROM messages AS m
            JOIN conversations AS c ON c.id = m.conversation_id
            ORDER BY m.id DESC
            LIMIT ?
            """,
            (max(0, int(limit)),),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]


def _like_pattern(text):
    """Escape LIKE wildcards so '%' and '_' in user text are literal."""
    escaped = (text.replace("\\", "\\\\")
                   .replace("%", "\\%")
                   .replace("_", "\\_"))
    return f"%{escaped}%"


def search_messages(search_term, limit=20):
    """Search message text across conversations, newest matches first."""
    search_term = str(search_term).strip()
    if not search_term:
        return []

    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT m.id, m.conversation_id, c.title AS conversation_title,
                   m.role, m.content, m.created_at
            FROM messages AS m
            JOIN conversations AS c ON c.id = m.conversation_id
            WHERE m.content LIKE ? ESCAPE '\\'
            ORDER BY m.id DESC
            LIMIT ?
            """,
            (_like_pattern(search_term), max(0, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]


def get_unprocessed_user_messages(limit=20):
    """Return user messages not yet handled by the long-term memory worker."""
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT m.id, m.conversation_id, m.content, m.created_at, m.route
            FROM messages AS m
            LEFT JOIN memory_processed AS p ON p.message_id = m.id
            WHERE m.role = 'user' AND p.message_id IS NULL
            ORDER BY m.id
            LIMIT ?
            """,
            (max(0, int(limit)),),
        ).fetchall()
        return [dict(row) for row in rows]


def mark_memory_message_processed(message_id, status="processed"):
    """Mark a user message as handled so it is not sent to Qwen again."""
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO memory_processed (message_id, status)
            VALUES (?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                status = excluded.status,
                processed_at = CURRENT_TIMESTAMP
            """,
            (int(message_id), str(status)),
        )


# ---------------------------------------------------------------
# Facts: helpers
# ---------------------------------------------------------------

def _encode(value):
    return json.dumps(value, ensure_ascii=False)


def _decode(text):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def _row_to_fact(row):
    fact = dict(row)
    fact["value"] = _decode(fact["value"])
    return fact


def _clean_name(name, what):
    """Categories and keys are lowercase snake_case-ish identifiers."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"Fact {what} must be a non-empty string")
    cleaned = "_".join(name.strip().lower().split())
    return fact_rules.canonical_key(cleaned) if what == "key" else cleaned


def _clean_value(value):
    """A fact value is a non-empty string or a non-empty list of strings."""
    if isinstance(value, str):
        value = value.strip()
        if not value:
            raise ValueError("Fact value must not be empty")
        return value
    if isinstance(value, (list, tuple)):
        cleaned = []
        for item in value:
            if isinstance(item, str) and item.strip():
                item = item.strip()
                if _normalize(item) not in {_normalize(c) for c in cleaned}:
                    cleaned.append(item)
        if not cleaned:
            raise ValueError("Fact list value must contain at least one string")
        return cleaned
    raise TypeError("Fact value must be a string or a list of strings")


def _normalize(value):
    """Comparison form: case-insensitive, order-insensitive for lists."""
    if isinstance(value, str):
        return " ".join(value.casefold().split())
    if isinstance(value, list):
        return sorted(_normalize(v) for v in value)
    return value


# Keys that always hold a list, even if an old row stored a plain string.
LIST_KEYS = {"likes", "dislikes", "learning", "current_projects",
             "hobbies", "interests", "skills"}


def _as_list_if_needed(key, value):
    """Wrap a string in a list for keys that are always lists."""
    if key in LIST_KEYS and isinstance(value, str):
        return [value]
    return value


def _insert_fact(connection, category, key, value, confidence, source,
                 source_message_id):
    cursor = connection.execute(
        """
        INSERT INTO facts (category, key, value, confidence, source, source_message_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (category, key, _encode(value), float(confidence), source, source_message_id),
    )
    return cursor.lastrowid


# ---------------------------------------------------------------
# Facts: writing
# ---------------------------------------------------------------

def upsert_fact(category, key, value, confidence=0.9, source="extraction",
                source_message_id=None, replace=False):
    """Store a fact, handling repeats, list growth and changed values.

    Returns a dict: {"action", "fact_id", "old_value", "new_value"} where action is
      "added"       nothing active existed for (category, key)
      "confirmed"   same value again: mention_count and last_confirmed_at updated
      "merged"      both old and new are lists: new items were added to the old fact
      "superseded"  the value changed: the old row is kept as history, a new one is active
    """
    category = _clean_name(category, "category")
    key = _clean_name(key, "key")
    value = _clean_value(value)

    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM facts WHERE category = ? AND key = ? AND status = 'active'",
            (category, key),
        ).fetchone()

        if row is None:
            fact_id = _insert_fact(connection, category, key, value, confidence,
                                   source, source_message_id)
            return {"action": "added", "fact_id": fact_id,
                    "old_value": None, "new_value": value}

        old_value = _decode(row["value"])
        old_value = _as_list_if_needed(key, old_value)
        if isinstance(old_value, list) and isinstance(value, str):
            value = [value]                          # a lone item joins the list
        elif isinstance(old_value, str) and isinstance(value, list) and len(value) == 1:
            value = value[0]                         # ["Joy"] is just "Joy"

        if _normalize(old_value) == _normalize(value):
            connection.execute(
                """
                UPDATE facts
                SET mention_count = mention_count + 1,
                    last_confirmed_at = CURRENT_TIMESTAMP,
                    confidence = MAX(confidence, ?)
                WHERE id = ?
                """,
                (float(confidence), row["id"]),
            )
            return {"action": "confirmed", "fact_id": row["id"],
                    "old_value": old_value, "new_value": old_value}

        if isinstance(old_value, list) and isinstance(value, list) and not replace:
            known = {_normalize(item) for item in old_value}
            merged = old_value + [v for v in value if _normalize(v) not in known]
            connection.execute(
                """
                UPDATE facts
                SET value = ?, mention_count = mention_count + 1,
                    last_confirmed_at = CURRENT_TIMESTAMP,
                    confidence = MAX(confidence, ?)
                WHERE id = ?
                """,
                (_encode(merged), float(confidence), row["id"]),
            )
            return {"action": "merged", "fact_id": row["id"],
                    "old_value": old_value, "new_value": merged}

        # Value changed: retire the old row first (the unique index allows only
        # one active row per key), then insert the new one and link them.
        connection.execute(
            "UPDATE facts SET status = 'superseded', retired_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (row["id"],),
        )
        new_id = _insert_fact(connection, category, key, value, confidence,
                              source, source_message_id)
        connection.execute(
            "UPDATE facts SET superseded_by = ? WHERE id = ?", (new_id, row["id"])
        )
        return {"action": "superseded", "fact_id": new_id,
                "old_value": old_value, "new_value": value}


def retire_fact(fact_id, status="deleted", retired_at=None):
    """Retire an active fact ("forget that"). Returns True if something changed.

    The row is kept (status 'deleted') so there is an audit trail and an
    accidental "forget" can be undone with restore_fact().
    """
    if status not in ("deleted", "superseded"):
        raise ValueError("status must be 'deleted' or 'superseded'")
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE facts SET status = ?, retired_at = COALESCE(?, CURRENT_TIMESTAMP) "
            "WHERE id = ? AND status = 'active'",
            (status, retired_at, int(fact_id)),
        )
        return cursor.rowcount > 0


def retire_fact_by_key(category, key):
    """Retire the active fact for (category, key). Returns True if one existed."""
    category = _clean_name(category, "category")
    key = _clean_name(key, "key")
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE facts SET status = 'deleted', retired_at = CURRENT_TIMESTAMP "
            "WHERE category = ? AND key = ? AND status = 'active'",
            (category, key),
        )
        return cursor.rowcount > 0


def restore_fact(fact_id):
    """Bring a deleted fact back, unless something active already replaced it."""
    with _connect() as connection:
        row = connection.execute(
            "SELECT category, key, status FROM facts WHERE id = ?", (int(fact_id),)
        ).fetchone()
        if row is None or row["status"] != "deleted":
            return False
        clash = connection.execute(
            "SELECT 1 FROM facts WHERE category = ? AND key = ? AND status = 'active'",
            (row["category"], row["key"]),
        ).fetchone()
        if clash:
            return False
        connection.execute(
            "UPDATE facts SET status = 'active', retired_at = NULL, "
            "last_confirmed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (int(fact_id),),
        )
        return True


# ---------------------------------------------------------------
# Facts: reading
# ---------------------------------------------------------------

def get_fact(fact_id):
    """Return one fact (any status) or None."""
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM facts WHERE id = ?", (int(fact_id),)
        ).fetchone()
        return _row_to_fact(row) if row else None


def get_active_facts(category=None, limit=None):
    """Return active facts ordered by category then key."""
    query = "SELECT * FROM facts WHERE status = 'active'"
    params = []
    if category is not None:
        query += " AND category = ?"
        params.append(_clean_name(category, "category"))
    query += " ORDER BY category, key"
    if limit is not None:
        query += " LIMIT ?"
        params.append(max(0, int(limit)))

    with _connect() as connection:
        return [_row_to_fact(r) for r in connection.execute(query, params).fetchall()]


def get_fact_history(category, key):
    """Every version of one fact (active, superseded, deleted), oldest first."""
    category = _clean_name(category, "category")
    key = _clean_name(key, "key")
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM facts WHERE category = ? AND key = ? ORDER BY id",
            (category, key),
        ).fetchall()
        return [_row_to_fact(r) for r in rows]


def search_facts(term, limit=10):
    """Simple text search over ACTIVE facts (category, key and value).

    Spaces in the term also match underscores in keys, so "favorite color"
    finds the key favorite_color. (Smarter retrieval comes in a later phase.)
    """
    term = str(term).strip()
    if not term:
        return []

    plain = _like_pattern(term)
    underscored = _like_pattern("_".join(term.lower().split()))

    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM facts
            WHERE status = 'active'
              AND (value LIKE ? ESCAPE '\\'
                   OR key LIKE ? ESCAPE '\\'
                   OR category LIKE ? ESCAPE '\\')
            ORDER BY last_confirmed_at DESC, id DESC
            LIMIT ?
            """,
            (plain, underscored, plain, max(0, int(limit))),
        ).fetchall()
        return [_row_to_fact(r) for r in rows]


def count_facts():
    """Return {'active': n, 'superseded': n, 'deleted': n}."""
    counts = {status: 0 for status in FACT_STATUSES}
    with _connect() as connection:
        for row in connection.execute(
            "SELECT status, COUNT(*) AS n FROM facts GROUP BY status"
        ):
            counts[row["status"]] = row["n"]
    return counts


# ---------------------------------------------------------------
# Phase 3 helpers (used by the session-end extractor in jarvis_memory.py)
# ---------------------------------------------------------------

def get_message(message_id):
    """Return one message (id, conversation_id, role, content, created_at) or None."""
    with _connect() as connection:
        row = connection.execute(
            "SELECT id, conversation_id, role, content, created_at, route "
            "FROM messages WHERE id = ?",
            (int(message_id),),
        ).fetchone()
        return dict(row) if row else None


def get_active_fact(category, key):
    """Return the active fact for (category, key), or None."""
    category = _clean_name(category, "category")
    key = _clean_name(key, "key")
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM facts WHERE category = ? AND key = ? AND status = 'active'",
            (category, key),
        ).fetchone()
        return _row_to_fact(row) if row else None


def get_deleted_facts(limit=20):
    """Facts the user asked to forget, most recently forgotten first."""
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM facts WHERE status = 'deleted' "
            "ORDER BY retired_at DESC, id DESC LIMIT ?",
            (max(0, int(limit)),),
        ).fetchall()
        return [_row_to_fact(r) for r in rows]


def _value_tokens(value):
    """Normalised strings inside a fact value (one for a string, many for a list)."""
    if value is None:
        return set()
    items = value if isinstance(value, list) else [value]
    return {_normalize(str(v)) for v in items if str(v).strip()}


def was_forgotten_since(category, key, value, since):
    """True if the user deleted this fact, or one with the same value, at or after `since`.

    `since` is the time of the message being processed, in the same UTC text
    format as the database. A fact forgotten AFTER a message was spoken must
    not be brought back by processing that older message.
    """
    category = _clean_name(category, "category")
    key = _clean_name(key, "key")
    wanted = _value_tokens(value)

    with _connect() as connection:
        rows = connection.execute(
            "SELECT category, key, value FROM facts "
            "WHERE status = 'deleted' AND retired_at >= ?",
            (str(since),),
        ).fetchall()

    for row in rows:
        if row["category"] == category and row["key"] == key:
            return True
        if wanted & _value_tokens(_decode(row["value"])):
            return True
    return False


def remove_fact_list_item(category, key, item, confidence=0.9, source="extraction",
                          source_message_id=None, retired_at=None):
    """Remove one item from a list-valued active fact ("I stopped learning Rust").

    Returns {"action", "fact_id", "old_value", "new_value"} where action is
      "item_removed"  the list shrank: old row kept as history, new row active
      "retired"       it was the last item, so the whole fact was forgotten
      "not_found"     no active list fact, or the item is not in it
    """
    category = _clean_name(category, "category")
    key = _clean_name(key, "key")
    target = _normalize(str(item))
    if not target:
        raise ValueError("Item must not be empty")

    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM facts WHERE category = ? AND key = ? AND status = 'active'",
            (category, key),
        ).fetchone()
        missing = {"action": "not_found", "fact_id": None,
                   "old_value": None, "new_value": None}
        if row is None:
            return missing

        old_value = _decode(row["value"])
        old_value = _as_list_if_needed(key, old_value)
        if not isinstance(old_value, list):
            return missing

        def matches(existing):
            existing = _normalize(existing)
            if existing == target:
                return True
            # "rust programming" and "rust" count as the same thing
            return len(target) >= 3 and len(existing) >= 3 and (
                target in existing or existing in target)

        remaining = [v for v in old_value if not matches(v)]
        if len(remaining) == len(old_value):
            return {**missing, "fact_id": row["id"], "old_value": old_value}

        if not remaining:
            connection.execute(
                "UPDATE facts SET status = 'deleted', "
                "retired_at = COALESCE(?, CURRENT_TIMESTAMP) WHERE id = ?",
                (retired_at, row["id"]),
            )
            return {"action": "retired", "fact_id": row["id"],
                    "old_value": old_value, "new_value": None}

        connection.execute(
            "UPDATE facts SET status = 'superseded', retired_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (row["id"],),
        )
        new_id = _insert_fact(connection, category, key, remaining, confidence,
                              source, source_message_id)
        connection.execute(
            "UPDATE facts SET superseded_by = ? WHERE id = ?", (new_id, row["id"])
        )
        return {"action": "item_removed", "fact_id": new_id,
                "old_value": old_value, "new_value": remaining}


# ---------------------------------------------------------------
# Phase 4: conversation summaries (episodic memory)
# ---------------------------------------------------------------

def save_conversation_summary(conversation_id, summary, topics=None, message_count=0):
    """Store (or replace) the summary of one conversation and return its row id.

    An empty summary is allowed: it records "reviewed, nothing worth remembering".
    """
    summary = " ".join(str(summary or "").split())
    clean_topics = []
    for topic in topics or []:
        topic = " ".join(str(topic).split())
        if topic and topic.casefold() not in {t.casefold() for t in clean_topics}:
            clean_topics.append(topic)

    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO conversation_summaries
                (conversation_id, summary, topics, message_count)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                summary = excluded.summary,
                topics = excluded.topics,
                message_count = excluded.message_count,
                created_at = CURRENT_TIMESTAMP
            """,
            (int(conversation_id), summary, _encode(clean_topics), int(message_count)),
        )
        row = connection.execute(
            "SELECT id FROM conversation_summaries WHERE conversation_id = ?",
            (int(conversation_id),),
        ).fetchone()
        return row["id"]


def _row_to_summary(row):
    item = dict(row)
    topics = _decode(item.get("topics"))
    item["topics"] = topics if isinstance(topics, list) else []
    return item


def get_conversation_summary(conversation_id):
    """Return the summary row for one conversation (with started_at), or None."""
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT s.id, s.conversation_id, s.summary, s.topics, s.message_count,
                   s.created_at, c.started_at
            FROM conversation_summaries AS s
            JOIN conversations AS c ON c.id = s.conversation_id
            WHERE s.conversation_id = ?
            """,
            (int(conversation_id),),
        ).fetchone()
        return _row_to_summary(row) if row else None


def get_recent_summaries(limit=3):
    """Latest NON-EMPTY summaries, newest conversation first."""
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT s.id, s.conversation_id, s.summary, s.topics, s.message_count,
                   s.created_at, c.started_at
            FROM conversation_summaries AS s
            JOIN conversations AS c ON c.id = s.conversation_id
            WHERE s.summary <> ''
            ORDER BY c.started_at DESC, c.id DESC
            LIMIT ?
            """,
            (max(0, int(limit)),),
        ).fetchall()
        return [_row_to_summary(r) for r in rows]


def get_conversations_without_summary(limit=200):
    """Conversations that have not been reviewed yet, newest first."""
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT c.id, c.title, c.started_at, c.updated_at
            FROM conversations AS c
            LEFT JOIN conversation_summaries AS s ON s.conversation_id = c.id
            WHERE s.id IS NULL
            ORDER BY c.id DESC
            LIMIT ?
            """,
            (max(0, int(limit)),),
        ).fetchall()
        return [dict(r) for r in rows]


def get_memory_command_message_ids(conversation_id):
    """Ids of user messages that were instant memory commands ("remember...", "forget...").

    The summarizer leaves these out, so a fact you told Jarvis to forget does not
    come back through a conversation summary.
    """
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT p.message_id
            FROM memory_processed AS p
            JOIN messages AS m ON m.id = p.message_id
            WHERE m.conversation_id = ? AND p.status = 'memory_command'
            """,
            (int(conversation_id),),
        ).fetchall()
        return [r["message_id"] for r in rows]


# ---------------------------------------------------------------
# Phase 5: full-text search (SQLite FTS5)
#
# facts_fts / summaries_fts hold one row per ACTIVE fact / non-empty summary,
# with rowid = the id of the row in the real table. Triggers keep them in sync
# whatever code changes a fact, and the index is rebuilt every time the
# database is opened, so an existing database needs no migration.
# If this SQLite build has no FTS5, fts_available() is False and the retrieval
# module falls back to plain keyword matching in Python.
# ---------------------------------------------------------------

_FTS_OK = None          # None = not checked yet, True/False after initialize_database()

_FACT_TEXT = "replace({r}.key, '_', ' ') || ' ' || {r}.category || ' ' || {r}.value"
_SUMMARY_TEXT = "{r}.summary || ' ' || {r}.topics"


def _init_search_index(connection):
    """Create the FTS5 tables and triggers, then fill them from the real tables."""
    global _FTS_OK
    new_fact, new_summary = _FACT_TEXT.format(r="NEW"), _SUMMARY_TEXT.format(r="NEW")
    try:
        connection.executescript(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
                USING fts5(search_text, tokenize = 'porter unicode61');
            CREATE VIRTUAL TABLE IF NOT EXISTS summaries_fts
                USING fts5(search_text, tokenize = 'porter unicode61');

            DROP TRIGGER IF EXISTS facts_fts_insert;
            DROP TRIGGER IF EXISTS facts_fts_update;
            DROP TRIGGER IF EXISTS facts_fts_delete;
            CREATE TRIGGER facts_fts_insert AFTER INSERT ON facts
            WHEN NEW.status = 'active'
            BEGIN
                INSERT INTO facts_fts (rowid, search_text) VALUES (NEW.id, {new_fact});
            END;
            CREATE TRIGGER facts_fts_update AFTER UPDATE ON facts
            BEGIN
                DELETE FROM facts_fts WHERE rowid = OLD.id;
                INSERT INTO facts_fts (rowid, search_text)
                    SELECT NEW.id, {new_fact} WHERE NEW.status = 'active';
            END;
            CREATE TRIGGER facts_fts_delete AFTER DELETE ON facts
            BEGIN
                DELETE FROM facts_fts WHERE rowid = OLD.id;
            END;

            DROP TRIGGER IF EXISTS summaries_fts_insert;
            DROP TRIGGER IF EXISTS summaries_fts_update;
            DROP TRIGGER IF EXISTS summaries_fts_delete;
            CREATE TRIGGER summaries_fts_insert AFTER INSERT ON conversation_summaries
            WHEN NEW.summary <> ''
            BEGIN
                INSERT INTO summaries_fts (rowid, search_text) VALUES (NEW.id, {new_summary});
            END;
            CREATE TRIGGER summaries_fts_update AFTER UPDATE ON conversation_summaries
            BEGIN
                DELETE FROM summaries_fts WHERE rowid = OLD.id;
                INSERT INTO summaries_fts (rowid, search_text)
                    SELECT NEW.id, {new_summary} WHERE NEW.summary <> '';
            END;
            CREATE TRIGGER summaries_fts_delete AFTER DELETE ON conversation_summaries
            BEGIN
                DELETE FROM summaries_fts WHERE rowid = OLD.id;
            END;
            """
        )
    except sqlite3.OperationalError:
        _FTS_OK = False
        return
    _FTS_OK = True
    _rebuild_search_index(connection)


def _rebuild_search_index(connection):
    connection.execute("DELETE FROM facts_fts")
    connection.execute(
        "INSERT INTO facts_fts (rowid, search_text) SELECT id, "
        + _FACT_TEXT.format(r="facts") + " FROM facts WHERE status = 'active'"
    )
    connection.execute("DELETE FROM summaries_fts")
    connection.execute(
        "INSERT INTO summaries_fts (rowid, search_text) SELECT id, "
        + _SUMMARY_TEXT.format(r="conversation_summaries")
        + " FROM conversation_summaries WHERE summary <> ''"
    )


def rebuild_search_index():
    """Refill the search index from the real tables. Returns False if FTS5 is missing."""
    if not _FTS_OK:
        return False
    with _connect() as connection:
        _rebuild_search_index(connection)
    return True


def fts_available():
    """True if the FTS5 search index exists in this database."""
    return bool(_FTS_OK)


def count_searchable():
    """(active facts, non-empty summaries): what the search index covers."""
    with _connect() as connection:
        facts = connection.execute(
            "SELECT COUNT(*) FROM facts WHERE status = 'active'").fetchone()[0]
        summaries = connection.execute(
            "SELECT COUNT(*) FROM conversation_summaries WHERE summary <> ''").fetchone()[0]
    return facts, summaries


def search_facts_ranked(match_query, limit=10):
    """Active facts matching an FTS5 query, best match first. Needs fts_available().

    `match_query` is an FTS5 expression such as '"city" OR "london"'. A bad
    expression raises sqlite3.OperationalError; the caller decides what to do.
    """
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT f.* FROM facts_fts
            JOIN facts AS f ON f.id = facts_fts.rowid
            WHERE facts_fts MATCH ? AND f.status = 'active'
            ORDER BY bm25(facts_fts)
            LIMIT ?
            """,
            (str(match_query), max(0, int(limit))),
        ).fetchall()
        return [_row_to_fact(r) for r in rows]


def search_summaries_ranked(match_query, limit=10):
    """Conversation summaries matching an FTS5 query, best match first."""
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT s.id, s.conversation_id, s.summary, s.topics, s.message_count,
                   s.created_at, c.started_at
            FROM summaries_fts
            JOIN conversation_summaries AS s ON s.id = summaries_fts.rowid
            JOIN conversations AS c ON c.id = s.conversation_id
            WHERE summaries_fts MATCH ?
            ORDER BY bm25(summaries_fts)
            LIMIT ?
            """,
            (str(match_query), max(0, int(limit))),
        ).fetchall()
        return [_row_to_summary(r) for r in rows]


# ---------------------------------------------------------------
# Phase 6: stored embeddings (vectors)
#
# One row per (kind, ref_id, model): kind is 'fact' or 'summary', ref_id the id of
# that row, model the embedding model that produced the vector. text_hash is a
# fingerprint of the text that was embedded, so a changed fact is re-embedded.
# The vector is float32 bytes. The database only stores them; jarvis_embeddings.py
# creates and compares them.
# ---------------------------------------------------------------

def _init_embedding_store(connection):
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS embeddings (
            kind TEXT NOT NULL CHECK (kind IN ('fact', 'summary')),
            ref_id INTEGER NOT NULL,
            model TEXT NOT NULL,
            text_hash TEXT NOT NULL,
            dim INTEGER NOT NULL,
            vector BLOB NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (kind, ref_id, model)
        );
        """
    )


def get_embeddings(kind, model):
    """Return {ref_id: (text_hash, dim, vector_bytes)} for one kind and model."""
    with _connect() as connection:
        rows = connection.execute(
            "SELECT ref_id, text_hash, dim, vector FROM embeddings "
            "WHERE kind = ? AND model = ?",
            (kind, model),
        ).fetchall()
        return {r["ref_id"]: (r["text_hash"], r["dim"], bytes(r["vector"])) for r in rows}


def save_embeddings(kind, model, rows):
    """Store or replace vectors. rows = [(ref_id, text_hash, dim, vector_bytes), ...]."""
    rows = list(rows)
    if not rows:
        return 0
    with _connect() as connection:
        connection.executemany(
            """
            INSERT INTO embeddings (kind, ref_id, model, text_hash, dim, vector)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(kind, ref_id, model) DO UPDATE SET
                text_hash = excluded.text_hash,
                dim = excluded.dim,
                vector = excluded.vector,
                created_at = CURRENT_TIMESTAMP
            """,
            [(kind, int(ref), model, str(h), int(d), sqlite3.Binary(v))
             for ref, h, d, v in rows],
        )
    return len(rows)


def delete_embeddings(kind, model, ref_ids):
    """Remove the vectors of rows that no longer exist or are no longer active."""
    ref_ids = [int(r) for r in ref_ids]
    if not ref_ids:
        return 0
    with _connect() as connection:
        connection.executemany(
            "DELETE FROM embeddings WHERE kind = ? AND model = ? AND ref_id = ?",
            [(kind, model, r) for r in ref_ids],
        )
    return len(ref_ids)


def count_embeddings(kind=None, model=None):
    """How many vectors are stored (optionally for one kind and/or model)."""
    query, params = "SELECT COUNT(*) FROM embeddings WHERE 1 = 1", []
    if kind is not None:
        query += " AND kind = ?"
        params.append(kind)
    if model is not None:
        query += " AND model = ?"
        params.append(model)
    with _connect() as connection:
        return connection.execute(query, params).fetchone()[0]



def count_facts_by_conversation():
    """{conversation_id: number of facts that came from its messages}."""
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT m.conversation_id AS cid, COUNT(DISTINCT f.id) AS n
            FROM facts AS f JOIN messages AS m ON m.id = f.source_message_id
            GROUP BY m.conversation_id
            """
        ).fetchall()
        return {r["cid"]: r["n"] for r in rows}


def get_facts_for_ui():
    """Active and forgotten facts (superseded history is hidden)."""
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM facts WHERE status IN ('active', 'deleted') "
            "ORDER BY category, key, id DESC"
        ).fetchall()
        return [_row_to_fact(r) for r in rows]


def edit_fact_value(fact_id, text):
    """Manual edit from the UI. The old value is kept as history."""
    fact = get_fact(fact_id)
    text = str(text).strip()
    if not fact or fact["status"] != "active" or not text:
        return False
    value = text
    if isinstance(fact["value"], list) or fact["key"] in LIST_KEYS:
        value = [p.strip() for p in text.split(",") if p.strip()]
    upsert_fact(fact["category"], fact["key"], value, confidence=1.0,
                source="manual", replace=True)
    return True


# ---------------------------------------------------------------
# Compatibility layer
# j10.py and jarvis_memory.py still call these v1 names, so they keep
# working without any edits while we upgrade them in later phases.
# ---------------------------------------------------------------

def get_user_profile():
    """Active facts as {category: {key: value}} (the old profile shape)."""
    profile = {}
    for fact in get_active_facts():
        profile.setdefault(fact["category"], {})[fact["key"]] = fact["value"]
    return profile


def merge_user_profile(updates, source="extraction", source_message_id=None):
    """Apply {category: {key: value}} through upsert_fact; return the profile."""
    if not isinstance(updates, dict):
        raise TypeError("updates must be a dictionary")

    for category, entries in updates.items():
        if not isinstance(entries, dict):
            continue
        for key, value in entries.items():
            if value is None:
                continue
            upsert_fact(category, key, value, source=source,
                        source_message_id=source_message_id)
    return get_user_profile()


def update_user_profile(profile):
    """v1 name kept for compatibility."""
    return merge_user_profile(profile)
