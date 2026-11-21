"""Qwen streaming, profile prompt construction and model warm-up."""
import json
import re
import time
import requests
import config
import numpy as np
import os
LLM_DEBUG = os.environ.get("JARVIS_LLM_DEBUG", "") not in ("", "0")
from config import (SYSTEM_PROMPT, MODEL_NAME, OLLAMA_OPTIONS, OLLAMA_URL, MAX_TOOL_ROUNDS,
                    SENTENCE_END, _PROFILE_SKIP_KEYS, SPEECH_SPEED, SAMPLE_RATE)
from events import emit
from tts import kokoro, get_voice, voice_lang, clean_for_speech, synth
from stt import whisper
import database
import jarvis_tools

def _format_profile_value(value):
    """Turn a profile value into a short plain-language fragment."""
    if value is None:
        return None
    if isinstance(value, list):
        items = [str(v).strip() for v in value if v is not None and str(v).strip()]
        if not items:
            return None
        if len(items) == 1:
            return items[0]
        if len(items) == 2:
            return f"{items[0]} and {items[1]}"
        return ", ".join(items[:-1]) + f", and {items[-1]}"
    text = str(value).strip()
    return text or None
def format_profile_for_prompt(profile):
    """Short summary of durable user facts for the system prompt."""
    if not isinstance(profile, dict) or not profile:
        return ""

    lines = []
    preferred_order = ("personal", "hardware", "projects", "preferences", "interests")
    categories = [c for c in preferred_order if c in profile] + [
        c for c in profile if c not in preferred_order
    ]

    for category in categories:
        section = profile.get(category)
        if not isinstance(section, dict):
            continue
        for key, raw in section.items():
            if key in _PROFILE_SKIP_KEYS:
                continue
            formatted = _format_profile_value(raw)
            if not formatted:
                continue
            if len(formatted) > 120:
                formatted = formatted[:117].rstrip() + "..."
            lines.append(f"- {key.replace('_', ' ')}: {formatted}")

    if not lines:
        return ""

    lines = lines[:12]
    return (
        "What you know about the user from previous sessions "
        "(use only when relevant; do not recite this list):\n"
        + "\n".join(lines)
    )
def build_system_prompt(profile=None):
    """Base system prompt plus optional long-term profile summary."""
    if profile is None:
        try:
            profile = database.get_user_profile()
        except Exception:
            profile = {}
    summary = format_profile_for_prompt(profile)
    if not summary:
        return SYSTEM_PROMPT
    return SYSTEM_PROMPT + "\n\n" + summary
def qwen_worker(question, history, route, text_q, interrupt_event, response_holder,
                response_lock, outcome, system_prompt=None):

    start = time.perf_counter()
    first_token = True
    if system_prompt is None:
        system_prompt = SYSTEM_PROMPT

    def say(sentence):
        sentence = re.sub(r"[*_#`~]", "", sentence)
        sentence = clean_for_speech(sentence)
        if sentence and not interrupt_event.is_set():
            text_q.put(sentence)
            emit("reply_chunk", sentence)

    try:
        # The rules in jarvis_tools already have the answer (time, date, units,
        # math): speak it straight away. No Qwen call is needed.
        if route["answer"]:
            say(route["answer"])
            print(f"Direct answer: {time.perf_counter() - start:.2f}s")
            emit("log", "Direct answer generated")
            return

        hint = getattr(config, "REPLY_LENGTH_HINT", "").strip()
        asked = question + (f"\n\n({hint})" if hint else "")
        messages = (
            [{"role": "system", "content": system_prompt + route["prompt"]}]
            + history
            + [{"role": "user", "content": asked}]
        )

        if LLM_DEBUG:
            approx = sum(len(str(m.get("content", ""))) for m in messages) // 4
            print(f"[llm debug] sending {len(messages)} messages, about {approx} tokens "
                  f"(num_ctx {OLLAMA_OPTIONS.get('num_ctx')})")
            for m in messages:
                print(f"   {m['role']}: {str(m.get('content', ''))[:250]!r}")

        # Each round is one request to Qwen. If Qwen calls a tool, we run it and
        # ask again with the result; the final spoken answer is always streamed.
        for tool_round in range(MAX_TOOL_ROUNDS):
            buffer = ""
            assistant_text = ""
            truncated = False
            tool_calls = []

            request = {
                "model": MODEL_NAME,
                "messages": messages,
                "think": False,
                "stream": True,
                "keep_alive": -1,
                "options": OLLAMA_OPTIONS,
            }
            # Offer tools on every round except the last, so Qwen must answer in words.
            if route["tools"] and tool_round < MAX_TOOL_ROUNDS - 1:
                request["tools"] = route["tools"]

            with requests.post(
                OLLAMA_URL,
                json=request,
                stream=True,
                timeout=(10, 120),
            ) as response:
                with response_lock:
                    response_holder["response"] = response

                response.raise_for_status()

                for line in response.iter_lines():
                    if interrupt_event.is_set():
                        break
                    if not line:
                        continue

                    data = json.loads(line)

                    if LLM_DEBUG and data.get("done"):
                        print(f"[llm debug] Ollama prompt tokens: {data.get('prompt_eval_count')}")

                    if data.get("done_reason") == "length":
                        truncated = True

                    message = data.get("message", {})
                    tool_calls.extend(message.get("tool_calls") or [])

                    piece = message.get("content", "")
                    if not piece:
                        continue

                    if first_token:
                        print(f"Qwen first token: {time.perf_counter() - start:.2f}s")
                        emit("log", "Qwen first token received")
                        first_token = False

                    assistant_text += piece
                    buffer += piece
                    *finished, buffer = SENTENCE_END.split(buffer)
                    for sentence in finished:
                        say(sentence)
                        if interrupt_event.is_set():
                            break

            with response_lock:
                response_holder["response"] = None

            if interrupt_event.is_set():
                break

            if tool_calls:
                messages.append({"role": "assistant", "content": assistant_text,
                                 "tool_calls": tool_calls})
                for call in tool_calls:
                    result = jarvis_tools.execute_tool_call(call)
                    name = call.get("function", {}).get("name", "")
                    messages.append({"role": "tool", "tool_name": name, "content": result})
                continue                    # ask Qwen again, now with the tool results

            leftover = buffer.strip()
            if leftover and (not truncated or leftover[-1] in ".!?"):
                say(leftover)
            break
        else:
            say("Sorry, I got stuck on that one.")

        if interrupt_event.is_set():
            print("Qwen generation cancelled (voice interruption).")
            emit("state", "interrupted")
        else:
            print(f"Qwen total: {time.perf_counter() - start:.2f}s")
            emit("log", "Qwen response complete")

    except Exception as error:
        if interrupt_event.is_set():
            print("Qwen stream closed after voice interruption.")
        else:
            print("\nQwen error:", error)
            emit("log", f"Qwen error: {error}")
            outcome["failed"] = True
            text_q.put("Sorry, I could not reach my language model.")

    finally:
        with response_lock:
            response_holder["response"] = None
        text_q.put(None)


def warm_up():
    """Load every model once so the first real question is not slow."""

    synth("Ready.")

    segments, _ = whisper.transcribe(
        np.zeros(SAMPLE_RATE, dtype=np.float32), beam_size=1, language="en"
    )
    list(segments)      # transcribe() is lazy, so it must be consumed to actually run

    try:
        requests.post(
            OLLAMA_URL,
            json={
                "model": MODEL_NAME,
                "messages": [{"role": "user", "content": "hi"}],
                "think": False,
                "stream": False,
                "keep_alive": -1,
                "options": {**OLLAMA_OPTIONS, "num_predict": 1},
            },
            timeout=120,
        )
    except Exception as error:
        print("Could not warm up Qwen (is Ollama running?):", error)
