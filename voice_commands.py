"""Spoken voice-selection commands."""
import re
import tts
from events import emit

def handle_voice_command(text):
    """If `text` is a voice command, switch voice and return what to say. Otherwise None."""

    t = re.sub(r"[^a-z\s]", "", text.lower())
    t = re.sub(r"\b(hey|hi|ok|okay|please|jarvis|can you|could you|would you)\b", "", t)
    t = re.sub(r"\s+", " ", t).strip()

    new_voice = None

    if re.fullmatch(r"(change|switch)( your)? voice|next voice|"
                    r"(change|switch) to (a )?(different|another|new|other) voice", t):
        new_voice = tts.ALL_VOICES[(tts.ALL_VOICES.index(tts.get_voice()) + 1) % len(tts.ALL_VOICES)]

    elif re.fullmatch(r"(previous|last) voice|go back to (the )?(previous|last) voice", t):
        new_voice = tts.ALL_VOICES[(tts.ALL_VOICES.index(tts.get_voice()) - 1) % len(tts.ALL_VOICES)]
    elif re.fullmatch(r"(?:use|switch to|change to)(?: a| an)? "
                      r"(american|british|spanish|french|hindi|italian|japanese|portuguese|chinese)"
                      r"(?: voice)?", t):
        pool = tts.voices_in_language(re.search(r"american|british|spanish|french|hindi|italian|japanese|portuguese|chinese", t).group(0))
        cur = tts.get_voice()
        new_voice = pool[(pool.index(cur) + 1) % len(pool)] if cur in pool else pool[0]
    else:
        m = re.fullmatch(
            r"(?:(?:change|switch)(?: your)? voice to|use|switch to|change to|speak as)"
            r"(?: the)?(?: voice)?(?: of)? (\w+(?: \w+)?)(?: voice)?", t)
        if m:
            new_voice = tts.find_voice(m.group(1))

    if new_voice is None:
        return None

    tts.set_voice(new_voice)
    emit("log", f"Voice changed to {new_voice}")
    return f"Okay, I'm {tts.SPOKEN[tts.get_voice()].title()} now."
