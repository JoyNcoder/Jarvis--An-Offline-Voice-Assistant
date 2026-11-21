"""Web server: serves ui/ and bridges events.py <-> WebSocket."""
import env_setup
import asyncio, json, os, threading, database, uvicorn, events
from contextlib import asynccontextmanager
from pathlib import Path
import settings_store, settings_runtime, tts

from collections import deque
from datetime import datetime, timezone


from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from session import JarvisSession          

BASE = Path(__file__).resolve().parent
UI_DIR = BASE / "ui"
SETTINGS_FILE = BASE / "settings.json"
PORT = 8765
DEBUG = os.environ.get("JARVIS_EVENT_DEBUG", "") not in ("", "0")

loop = None
clients = set()
session = None
session_thread = None
snapshot = {"state": "idle", "muted": False}   # sent to a UI that connects late
reply_buffer = ""
ready = False
_lock = threading.Lock()
log_rows = deque(maxlen=200)     # activity log (in memory)
log_lines = deque(maxlen=60)     # system log tail


# ---------- backend event -> UI message ----------

def _flush_reply():
    """Close the reply bubble (typing cursor off). Returns messages to send."""
    global reply_buffer
    if not reply_buffer:
        return []
    out = [("assistant_text", {"text": reply_buffer, "done": True})]
    reply_buffer = ""
    return out


def exit_soon(delay=1.5):
    """Close the whole app after a short delay (so the UI receives the last message)."""
    threading.Timer(delay, os._exit, args=(0,)).start()


def to_ui(name, data):
    global reply_buffer
    text = data if isinstance(data, str) else (data or {}).get("text", "") if isinstance(data, dict) else ""

    if name == "level":
        return [("level", {"v": float(data or 0)})]

    if name == "conversation_reset":
        reply_buffer = ""
        snapshot["error"] = False
        return [("conversation_reset", {})]

    if name == "conversation_busy":
        return [("conversation_busy", {})]

    if name == "state":
        if snapshot.get("error"):
            return []
        backend = data
        if backend in ("idle", "online"):
            state = "idle"
        elif backend in ("listening", "interrupted"):
            state = "listening"
        elif backend in ("thinking", "speaking"):
            state = backend
        else:
            return []                       # muted, stopping, processing_interruption...
        out = _flush_reply() if state in ("idle", "listening") else []
        if snapshot["muted"]:
            state = "paused"
        snapshot["state"] = state
        extra = [("level", {"v": 0})] if state in ("idle", "thinking", "paused") else []
        return out + [("state", {"state": state})] + extra

    if name == "user_text":
        if not text.strip():
            return []
        log_rows.append({"t": f"{datetime.now():%H:%M:%S}", "text": text, "route": "-", "ms": 0})
        out = _flush_reply() + [("user_said", {"text": text})]
        if not snapshot["muted"] and not snapshot.get("error"):
            snapshot["state"] = "thinking"
            out.append(("state", {"state": "thinking"}))
        return out

    if name == "reply_chunk":
        if not text.strip():
            return []
        reply_buffer = (reply_buffer + " " + text).strip()
        return [("assistant_text", {"text": reply_buffer, "done": False})]

    if name == "muted":
        snapshot["muted"] = bool(data)
        snapshot["state"] = "paused" if data else "idle"
        return [("muted", {"on": bool(data)}), ("state", {"state": snapshot["state"]})]

    if name == "error":
        msg = data.get("message") if isinstance(data, dict) else str(data)
        out = _flush_reply()              # closes the half-written reply bubble
        snapshot["error"] = True
        snapshot["state"] = "error"
        return out + [("error", {"message": msg}),
                      ("state", {"state": "error"}),
                      ("level", {"v": 0})]

    if name == "log":
        log_lines.append(f"{datetime.now():%H:%M:%S}  {data}")
        return []
    if name == "facts_changed":
        return [("facts_changed", {})]

    if name == "conversations_changed":
        return [("conversations_changed", {})]

    if name == "route":
        if log_rows:
            log_rows[-1]["route"] = str(data)
        return []

    if name == "latency":
        if log_rows:
            log_rows[-1]["ms"] = int(data or 0)
        return []

    if name == "settings_changed":
        return [("settings_changed", data)]

    if name == "shutdown_progress":
        d = data if isinstance(data, dict) else {}
        snapshot["quit"] = d                  # so a refreshed page can resume the screen
        if d.get("done"):
            exit_soon(1.5)
        return [("shutdown_progress", d)]

    return []                               


def on_event(name, data=None):
    global ready
    if name == "error" or (name == "state" and data == "online"):
        ready = True
    if DEBUG and name != "level":
        print(f"[event] {name!r} {data!r}")
    if loop is None or not loop.is_running():
        return
    with _lock:
        messages = to_ui(name, data)
    for ui_name, ui_data in messages:
        asyncio.run_coroutine_threadsafe(
            broadcast({"event": ui_name, "data": ui_data}), loop)


async def broadcast(message):
    text = json.dumps(message)
    for ws in list(clients):
        try:
            await ws.send_text(text)
        except Exception:
            clients.discard(ws)


# ---------- UI command -> Jarvis ----------
def handle_command(cmd, args):
    global session_thread
    if cmd == "mute":
        (session.mute if args.get("on") else session.unmute)()
    elif cmd == "stop":
        session.interrupt()
    elif cmd == "send_text":
        session.submit_text(args.get("text", ""))
    elif cmd == "forget_fact":
        database.retire_fact(args["id"]);            events.emit("facts_changed")
    elif cmd == "delete_conversation":
        cid = args.get("id")
        if cid is None or cid == getattr(session, "conversation_id", None):
            return                                   # never delete the live conversation
        database.delete_conversation(cid)
        events.emit("conversations_changed")
    elif cmd == "restore_fact":
        database.restore_fact(args["id"]);           events.emit("facts_changed")
    elif cmd == "edit_fact":
        database.edit_fact_value(args["id"], args["value"]); events.emit("facts_changed")
    elif cmd == "new_conversation":
        if session_thread and session_thread.is_alive():
            if not session.new_conversation():       # ignored (already restarting)
                events.emit("conversation_busy")
        else:                                    # session died (e.g. mic error)
            session._restarting = True           # debounce until online
            session._keep_muted = snapshot["muted"]
            events.emit("conversation_reset")
            events.emit("state", "idle")
            session_thread = threading.Thread(target=run_session, daemon=True)
            session_thread.start()
    elif cmd == "set_setting":
        key, value = args.get("key"), args.get("value")
        try:
            value = settings_runtime.set_setting(key, value)
        except ValueError as error:
            print(f"[settings] rejected {key}: {error}")
            return
        on_event("settings_changed", {"key": key, "value": value})
    elif cmd == "preview_voice":
        threading.Thread(target=tts.preview_voice, args=(args.get("voice"),), daemon=True).start()
    elif cmd == "quit":
        if session_thread and session_thread.is_alive():
            session.stop()                    # session saves, emits progress, then we exit
        else:
            exit_soon(0.2)                    # session already dead (mic error): just close
    elif cmd == "skip_shutdown":
        exit_soon(0.2)                        # unsaved items stay pending for next launch
    else:
        print(f"[ui] command not wired yet: {cmd} {args}")


# ---------- app ----------
def run_session():
    try:
        import comtypes
        comtypes.CoInitialize()          # Windows COM for volume control etc.
    except Exception:
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except Exception:
            pass
    try:
        while True:
            session.start()                      # blocking; saves memories in its finally
            if not session.take_restart():
                break
            events.emit("conversation_reset")    # UI clears transcript, error flag cleared
            events.emit("state", "idle")
    except Exception as error:
        on_event("error", {"message": str(error)})
    finally:
        session._restarting = False


@asynccontextmanager
async def lifespan(app):
    global loop, session, session_thread
    loop = asyncio.get_running_loop()
    events.subscribe(on_event)
    session = JarvisSession()
    session_thread = threading.Thread(target=run_session, daemon=True)
    session_thread.start()
    yield
    try:
        session.stop()
    except Exception:
        pass


app = FastAPI(lifespan=lifespan)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    await ws.send_text(json.dumps({"event": "state", "data": {"state": snapshot["state"]}}))
    await ws.send_text(json.dumps({"event": "muted", "data": {"on": snapshot["muted"]}}))
    if snapshot.get("quit"):
        await ws.send_text(json.dumps({"event": "shutdown_progress", "data": snapshot["quit"]}))
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            handle_command(msg.get("cmd"), msg.get("args") or {})
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)


@app.get("/api/settings")                      
def get_settings():
    return settings_store.load_all()

@app.get("/api/mics")
def api_mics():
    return ["Default microphone"] + settings_runtime.list_mics()

@app.get("/api/voices")
def api_voices():
    return [{"id": v, "label": tts.display_label(v),
             "group": tts.LANGUAGES.get(v[0], "other").capitalize()}
            for v in tts.ALL_VOICES]


def _when(ts):
    dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone()
    days = (datetime.now().astimezone().date() - dt.date()).days
    day = ("TODAY" if days == 0 else "YESTERDAY" if days == 1
           else dt.strftime("%a").upper() if days < 7 else dt.strftime("%d %b").upper())
    return f"{day} {dt:%H:%M}"


def _short(text, n=60):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


@app.get("/api/conversations")
def api_conversations(q: str = ""):
    q = q.strip().lower()
    hits = {m["conversation_id"] for m in database.search_messages(q, limit=500)} if q else None
    saved = database.count_facts_by_conversation()
    out = []
    for c in database.get_conversations(limit=200):
        if c["message_count"] == 0:
            continue
        summary = database.get_conversation_summary(c["id"])
        msgs = database.get_conversation_messages(c["id"], limit=1)
        title = (_short(summary["summary"]) if summary and summary["summary"]
                 else _short(msgs[0]["content"]) if msgs else c["title"])
        if q and c["id"] not in hits and q not in title.lower():
            continue
        n = saved.get(c["id"], 0)
        tags = f"{n} fact{'s' if n != 1 else ''} saved" if n else "no new facts"
        out.append({"id": c["id"], "when": _when(c["started_at"]), "title": title, "tags": tags})
    return out


@app.get("/api/conversations/{cid}")
def api_conversation(cid: int):
    c = database.get_conversation(cid)
    if not c:
        return None
    msgs = [{"role": "jarvis" if m["role"] == "assistant" else "user", "text": m["content"]}
            for m in database.get_conversation_messages(cid) if m["role"] != "system"]
    title = _short(msgs[0]["text"]) if msgs else c["title"]
    summary = database.get_conversation_summary(cid)
    if summary and summary["summary"]:
        title = _short(summary["summary"])
    return {"id": cid, "when": _when(c["started_at"]), "title": title, "messages": msgs}


@app.get("/api/facts")
def api_facts():
    out = []
    for f in database.get_facts_for_ui():
        v = f["value"]
        out.append({"id": f["id"], "key": f"{f['category']}.{f['key']}",
                    "value": ", ".join(v) if isinstance(v, list) else str(v),
                    "n": f["mention_count"], "conf": f["confidence"],
                    "status": "active" if f["status"] == "active" else "forgotten"})
    return out


@app.get("/api/log")
def api_log(limit: int = 100):
    return list(reversed(log_rows))[:limit]


@app.get("/api/system")
def api_system():
    import config
    g = lambda name, d="unknown": str(getattr(config, name, d))
    return {
        "items": [
            {"k": "Jarvis", "v": "Version 0.1"},
            {"k": "Language model", "v": g("MODEL_NAME"), "sub": "running through Ollama"},
            {"k": "Data", "v": str(database.DATABASE_FILE.name),
             "sub": f"{database.count_facts()['active']} active facts"},
            {"k": "Developer", "v": "Joy Nandy", "sub": "joynandy39@email.com"},
            {"k": "Website", "v": "https://joyn-portfolio.netlify.app/", "sub": "https://github.com/JoyNcoder"},
            {"k": "Built with", "v": "Python, Faster-Whisper, Kokoro, Ollama (Qwen3)", "sub": "Everything runs offline on your PC"},
            {"k": "Credits", "v": "Created by Joy, with help from Claude and ChatGPT", "sub": "Assistants used for code, design and debugging"},
        ],
        "log": "\n".join(log_lines) or "Nothing logged yet.",
    }

@app.get("/api/ready")
def api_ready():
    return {"ready": ready}
app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")   # keep this LAST

if __name__ == "__main__":
    print(f"Open http://127.0.0.1:{PORT}/")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")