"""Reusable Jarvis runtime session, shared by CLI and future server UI."""
import time
_LAUNCH = time.perf_counter()
import json
import threading
import database
import jarvis_memory
import jarvis_summaries
import jarvis_retrieval
import jarvis_tools
import system_tools
import settings_runtime
import queue
from system_commands import handle_system_command
from memory_commands import handle_memory_command
import config, events, audio_in, stt, tts, voice_commands, playback, llm, turn

def _facts_signature():
    return tuple((f["id"], json.dumps(f["value"], sort_keys=True))
                 for f in database.get_active_facts())

class JarvisSession:
    def __init__(self):
        self._text_q = queue.Queue()
        self._stop_event = threading.Event()
        self._restart = False       # set by new_conversation(); server loop reads it
        self._restarting = False    # debounce: True until the new session is online
        self._keep_muted = False    # user's mute choice, re-applied after restart
        self._show_quit = False
        self._started = False
        self.conversation_id = None


    def _progress(self, step, pct, done=False):
        events.emit("shutdown_progress", {"step": step, "pct": pct, "done": done})

    def stop(self):
        """Quit requested (UI quit button, window close): end the loop, then save."""
        self._restart = False
        self._show_quit = True
        self._stop_event.set()
        audio_in.set_muted(True)        # makes the mic wait return immediately
        turn.interrupt_current()        # cut off any reply in progress

    def mute(self):
        self._keep_muted = True
        audio_in.set_muted(True)
        events.emit("muted", True)

    def unmute(self):
        self._keep_muted = False
        audio_in.set_muted(False)
        events.emit("muted", False)

    def interrupt(self):
        """UI stop button: silence the current reply."""
        return turn.interrupt_current()

    def new_conversation(self):
        """UI 'new conversation' button: save memories, then the server restarts start().
        Returns False if a restart is already in progress."""
        if self._restarting or self._show_quit:
            return False
        self._restarting = True
        self._restart = True
        self._keep_muted = audio_in.is_muted()   # remember the user's mute choice
        self._stop_event.set()
        audio_in.set_muted(True)                 # makes the mic wait return immediately
        turn.interrupt_current()                 # cut off any reply in progress
        events.emit("state", "thinking")
        return True

    def take_restart(self):
        """Server loop: True once if a restart was requested (and clears it)."""
        flag, self._restart = self._restart, False
        return flag

    def submit_text(self, text):
        """UI text box: queue a typed message and cut off any current reply."""
        text = (text or "").strip()
        if not text:
            return
        self._text_q.put(text)
        turn.interrupt_current()

    def start(self):
        settings_runtime.apply_all()
        self._stop_event.clear()
        self._show_quit = False
        self._started = True
        audio_in.set_muted(self._keep_muted)

        print(f"[Time] Loading libraries and models: {time.perf_counter() - _LAUNCH:.2f}s")
        step_start = time.perf_counter()

        if not audio_in.mic_available():
            print("\nNo microphone detected, so Jarvis can't listen.")
            print("Connect a microphone, check Windows Sound > Input and Privacy > Microphone, then start Jarvis again.")
            self._restarting = False
            events.emit("error", "No microphone detected")
            return

        vad = audio_in.SileroVAD()
        history = []

        conversation_id = None
        try:
            database.initialize_database()
            conversation_id = database.create_conversation()
            print("Local database ready.")
            events.emit("log", "Local database ready")
        except Exception as error:
            print("Database unavailable; Jarvis will run without saving history:", error)
        self.conversation_id = conversation_id

        # Short long-term profile summary in the system prompt. The previous
        # session's transcript is NOT reloaded.
        active_system_prompt = config.SYSTEM_PROMPT
        try:
            profile = database.get_user_profile()
            active_system_prompt = llm.build_system_prompt(profile)
            fact_count = active_system_prompt.count("\n- ")
            if fact_count:
                print(jarvis_retrieval.startup_report())
            else:
                print("Long-term profile empty or filtered; using base system prompt.")
        except Exception as error:
            print("Could not load long-term profile:", error)
            active_system_prompt = config.SYSTEM_PROMPT




        system_tools.report_missing()
        print(f"[Time] Database, profile and tools: {time.perf_counter() - step_start:.2f}s")

        warm_start = time.perf_counter()
        llm.warm_up()
        print(f"[Time] Model warm-up: {time.perf_counter() - warm_start:.2f}s")

        # Meaning search (Phase 6): loads the small embedding model and embeds new facts.
        # If it is not available Jarvis simply keeps using the word search.
        vector_start = time.perf_counter()
        print(jarvis_retrieval.vector_warm_up())
        print(f"[Time] Vector memory: {time.perf_counter() - vector_start:.2f}s")





        print("\n========== JARVIS IS ONLINE ==========")
        events.emit("state", "online")
        self._restarting = False
        print("Speak whenever you're ready.")
        print("Say 'Goodbye Jarvis' to shut down.")
        print("Offline tools ready: time and date, units, calculator, Windows system commands.")
        print(f"(Live history window: last {config.MAX_HISTORY_MESSAGES} messages.)")
        print("(Long-term memory will be processed only after this session ends.)")

        print(f"[Time] Ready {time.perf_counter() - _LAUNCH:.2f}s after launch.")

        pending_audio = None

        try:
            while not self._stop_event.is_set():

                typed = None
                if pending_audio is not None:
                    audio = pending_audio
                    pending_audio = None
                elif not self._text_q.empty():
                    audio = None
                    typed = self._text_q.get()
                else:
                    try:
                        audio = audio_in.record_until_silence(
                            vad, abort=lambda: not self._text_q.empty())
                    except Exception as error:
                        print(f"\nLost access to the microphone ({error}). Shutting down.")
                        events.emit("error", "Lost access to the microphone")
                        break

                if typed is None and audio.size == 0:
                    continue

                t0 = time.perf_counter()
                question = typed if typed is not None else stt.transcribe(audio)
                print(f"\n\033[92mYou said: {question}\033[0m")
                events.emit("user_text", question)
                print(f"Input ready: {time.perf_counter() - t0:.2f}s")

                if not question:
                    continue

                message_id = turn.save_database_message(conversation_id, "user", question)

                # 1. Goodbye
                if turn.is_goodbye(question):
                    turn.set_route(message_id, "ignored")                     
                    print("Shutting down. Goodbye!")
                    events.emit("log", "Goodbye command received")
                    events.emit("state", "stopping")
                    events.emit("route", "goodbye")
                    self._show_quit = True
                    break

                # 2. Windows system commands (also handles a pending "are you sure?").
                try:
                    sys_reply = handle_system_command(question)
                except Exception as error:      # a command bug must never stop Jarvis
                    print("System command error:", error)
                    sys_reply = None
                if sys_reply:
                    events.emit("route", "system")
                    turn.set_route(message_id, "system") 
                    pending_audio, heard = playback.say_interruptible(
                        sys_reply, vad, t0, label=f"(System command) {sys_reply}")
                    turn.save_database_message(conversation_id, "assistant", sys_reply,
                                               route="system")
                    continue

                # 2b. Memory commands: remember / forget / recall / correct.
                # These run instantly and are saved through database.upsert_fact().
                try:
                    facts_before = _facts_signature()
                    memory_reply = handle_memory_command(question, conversation_id, message_id)
                except Exception as error:      # a memory bug must never stop Jarvis
                    print("Memory command error:", error)
                    memory_reply = None
                if memory_reply:
                    events.emit("route", "memory")
                    turn.set_route(message_id, "memory")
                    print(f"(Memory command) {memory_reply}")
                    events.emit("log", "Memory command handled")
                    pending_audio, heard = playback.say_interruptible(memory_reply, vad, t0)
                    if heard:
                        turn.save_database_message(conversation_id, "assistant", heard,
                                                   route="memory")
                    # Facts may have changed: rebuild the prompt so a forgotten or
                    # corrected fact is gone from Qwen's view straight away.
                    try:
                        active_system_prompt = llm.build_system_prompt()
                    except Exception as error:
                        print("Could not refresh system prompt:", error)
                    if _facts_signature() != facts_before:
                        history.clear()            # facts changed: old turns may be stale
                    elif heard:
                        history.append({"role": "user", "content": question})
                        history.append({"role": "assistant", "content": heard})
                    continue

                # 3. Voice change
                reply = voice_commands.handle_voice_command(question)
                if reply:
                    turn.set_route(message_id, "voice")
                    if audio_in.is_muted():
                        events.emit("reply_chunk", reply)
                        events.emit("state", "idle")
                    else:
                        tts.say_now(reply, label=f"(Voice command) {reply}")
                    turn.save_database_message(conversation_id, "assistant", reply,
                                               route="voice")
                    continue

                # 4. Stop words and filler sounds
                if turn.is_stop_or_filler(question):
                    turn.set_route(message_id, "ignored")
                    events.emit("route", "stop")
                    print("(Stop command or filler sound: staying quiet.)")
                    continue

                # 4b. Live memory (silent): save clear statements like "I live in London"
                # right now, so retrieval can use them later in this same session.
                try:
                    saved = jarvis_memory.save_statement_now(message_id)
                    if saved:
                        print(f"(Live memory) {saved}")
                        events.emit("log", "Live memory updated")
                except Exception as error:      # must never stop Jarvis
                    print("Live memory error:", error)



                # 5. Offline tools (time, date, math, units), else Qwen with optional tools
                try:
                    route = jarvis_tools.route(question)
                except Exception as error:      # a tool bug must never stop Jarvis
                    print("Tool routing error:", error)
                    route = {"answer": None, "tools": [], "prompt": ""}
                events.emit("route", "tools" if route["answer"] else "llm")
                turn.set_route(message_id, "tool" if route["answer"] else "chat")

                # 5b. Memory for THIS turn (Phase 5): core facts plus whatever matches
                # the question. Direct answers (time, math) need no memory.
                turn_prompt = active_system_prompt
                if not route["answer"]:
                    try:
                        memory = jarvis_retrieval.retrieve(question)
                        turn_prompt = config.SYSTEM_PROMPT + (
                            "\n\n" + memory["text"] if memory["text"] else "")
                        print(memory["report"])
                    except Exception as error:      # a memory bug must never stop Jarvis
                        print("Memory retrieval error:", error)

                pending_audio = turn.run_turn(
                    question, history, t0, vad, route, conversation_id,
                    system_prompt=turn_prompt,
                )
                print(f"Total turn time: {time.perf_counter() - t0:.2f}s")
                events.emit("state", "idle")

        except KeyboardInterrupt:
            print("\nJarvis shutting down...")

        finally:
            show = self._show_quit
            if self._restart:
                events.emit("state", "thinking")
                while not self._text_q.empty():
                    try: self._text_q.get_nowait()
                    except queue.Empty: break
            if show:
                self._progress("saving", 8)
                time.sleep(0.4)                      # lets the first step be visible

            if conversation_id is not None:
                shutdown_start = time.perf_counter()
                if show:
                    self._progress("extracting", 30)
                memory_results = self._process_pending_memories()
                for line in memory_results: print(line)
                print(f"[Time] Memory processing: {time.perf_counter() - shutdown_start:.2f}s")

                if show:
                    self._progress("summarizing", 65)
                summary_start = time.perf_counter()
                summary_results = self._process_pending_summaries()
                for line in summary_results: print(line)
                print(f"[Time] Summaries: {time.perf_counter() - summary_start:.2f}s")

            if show:
                self._progress("done", 100, done=True)

        self.shutdown_results = {"memory": locals().get("memory_results", []), "summaries": locals().get("summary_results", [])}
        return self.shutdown_results

    def _process_pending_memories(self):
        report = []
        try:
            pending = database.get_unprocessed_user_messages(limit=1000)
            if not pending:
                return ["Memory: no pending messages to process."]
            results = jarvis_memory.process_pending_messages(limit=1000)
            changes = empty = skipped = errors = 0
            for result in results:
                status = result["status"]
                if status == "memories_saved":
                    for line in result.get("applied", []):
                        changes += 1; report.append(f"message #{result['message_id']}: {line}")
                elif status == "no_memories": empty += 1
                elif status == "skipped": skipped += 1
                else:
                    errors += 1; report.append(f"message #{result['message_id']} left pending ({result.get('error', 'unknown error')}).")
                for line in result.get("ignored", []): report.append(f"message #{result['message_id']}: ignored, {line}")
            report.append(f"Memory done: {changes} change(s) applied, {empty} with nothing durable, {skipped} skipped, {errors} error(s).")
        except Exception as error:
            report.append(f"Memory shutdown processing error: {error}")
        return report

    def _process_pending_summaries(self):
        report = []
        try:
            results = jarvis_summaries.process_pending_summaries()
            if not results: return ["Summaries: nothing to summarize."]
            saved = empty = short = errors = 0
            for result in results:
                status = result["status"]; cid = result["conversation_id"]
                if status == "saved":
                    saved += 1; report.append(f"conversation #{cid}: {result['summary']}")
                    report.append(f"topics: {', '.join(result['topics']) or '-'}")
                elif status == "empty": empty += 1
                elif status == "too_short": short += 1
                else:
                    errors += 1; report.append(f"conversation #{cid} left pending ({result.get('error', 'unknown error')}).")
            report.append(f"Summaries done: {saved} saved, {empty} with nothing to remember, {short} too short, {errors} error(s).")
        except Exception as error:
            report.append(f"Summary shutdown processing error: {error}")
        return report

