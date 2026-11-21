"""python tune_retrieval.py
Ollama must be running. Reads your real database and changes nothing."""
import numpy as np

import database
import jarvis_embeddings as emb
import jarvis_retrieval as r

# (question, facts that SHOULD be found). An empty set = unrelated, nothing should be added.
TESTS = [
    ("what do I do on weekends", {"likes"}),
    ("what's inside my computer", {"gpu", "cpu", "ram", "monitor"}),
    ("do you know my sister's name", {"sister_name"}),
    ("what's my dog called", {"dog_name"}),
    ("when do I go to sleep", {"bedtime"}),
    ("what graphics card do I have", {"gpu"}),
    ("which code editor do I use", {"editor"}),
    ("what music do I enjoy", {"favorite_band", "dislikes"}),
    ("what am I working on", {"current_projects", "learning"}),
    ("when is my birthday", {"birthday"}),
    ("what do I want to achieve in life", {"dream"}),
    ("what do I like to eat", {"favorite_food"}),
    ("what's the capital of France", set()),
    ("tell me a joke", set()),
    ("how does photosynthesis work", set()),
    ("what's the tallest mountain", set()),
    ("who won the world cup", set()),
    ("explain how black holes work", set()),
    ("what should I cook tonight", {"favorite_food", "likes"}),
    ("how old am I", {"age"}),
]

database.initialize_database()
emb.reset()
facts = [f for f in database.get_active_facts() if f["key"] not in r.SKIP_KEYS]
index = emb.get_index("fact", facts)
if index is None:
    raise SystemExit(f"Vectors unavailable: {emb.last_error()}")
ids, matrix = index
key_of = {f["id"]: f["key"] for f in facts}
keys = [key_of[i] for i in ids]
core_ids = {f["id"] for f in r._core_facts(facts)}

pos_ok = neg_ok = pos_n = neg_n = 0
worst_good, worst_bad = 1.0, 0.0
for question, expected in TESTS:
    qvec = emb.embed_query(question)
    if qvec is None:
        raise SystemExit(f"Vectors unavailable: {emb.last_error()}")
    sims = matrix @ qvec
    top = np.argsort(-sims)[:6]
    words = [f["key"] for f in r._rank_facts(
        r.search_terms(r.question_words(question)), facts)][:5]
    chosen = r.retrieve(question, force_search=True)["facts"]
    extra = [f["key"] for f in chosen if f["id"] not in core_ids]

    print(f"\nQ: {question}")
    print("  meaning:", " | ".join(f"{keys[i]} {sims[i]:.2f}" for i in top))
    print("  words:  ", ", ".join(words) or "-")
    if expected:
        pos_n += 1
        ok = bool(expected & {f["key"] for f in chosen})
        pos_ok += ok
        best = max((float(sims[i]) for i, k in enumerate(keys) if k in expected), default=0.0)
        worst_good = min(worst_good, best)
    else:
        neg_n += 1
        ok = not extra
        neg_ok += ok
        worst_bad = max(worst_bad, max(float(s) for i, s in enumerate(sims)
                                       if ids[i] not in core_ids))
    print(f"  injected besides core: {', '.join(extra) or '-'}  -> {'PASS' if ok else 'FAIL'}")

print(f"\nSummary: {pos_ok}/{pos_n} questions found their fact, "
      f"{neg_ok}/{neg_n} unrelated questions stayed clean.")
print(f"Lowest similarity of a CORRECT fact:       {worst_good:.2f}")
print(f"Highest similarity on an UNRELATED question: {worst_bad:.2f}")
print(f"Highest similarity of a NON-core fact on an UNRELATED question: {worst_bad:.2f}")