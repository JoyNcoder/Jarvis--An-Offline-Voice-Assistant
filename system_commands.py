"""
system_commands.py - turns spoken text into Windows system actions.

Public entry point:

    handle_system_command(text) -> str | None

    Returns the sentence Jarvis should speak if `text` was a system command,
    or None if it is not one (so the caller passes it on to Qwen).

How matching works (Batch 1 rewrite):
  * The text is normalised first: lower case, punctuation removed, wake word
    and politeness stripped ("Hey Jarvis, could you please ... for me").
  * Every command is a pattern that must match the WHOLE utterance, so
    "how long is my commute" can no longer trigger "mute".
  * Commands with a number also understand number words ("fifty five").
  * Anything that does not match goes to Qwen untouched.
  * A failing action is caught and answered with a short apology, so a
    system problem can never crash the voice loop.
  * Command-like phrases that did NOT match are appended to
    unmatched_commands.txt so you can see what to teach Jarvis next.

  * This part is the "UNDERSTAND THE ACTION" part of the system tool calling update.
"""

import re
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from system_tools import WindowsSystem, APP_ALIASES, SETTINGS_PAGES, spoken_duration

BASE_DIR = Path(__file__).resolve().parent
UNMATCHED_LOG = BASE_DIR / "unmatched_commands.txt"
ERROR_LOG = BASE_DIR / "system_errors.log"
LOG_MAX_BYTES = 200_000            # stop writing logs beyond this size


# ---------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------

_ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}

_LEADING = re.compile(
    r"^(?:(?:hey|hi|hello|ok|okay|yo|so|now|then|please|can you|could you|"
    r"would you|will you|i want you to|i need you to|go ahead and|just)\s+)+"
)
_TRAILING = re.compile(
    r"(?:\s+(?:please|now|for me|thanks|thank you|right now|again))+$"
)


def normalize(text):
    """Lower-case, strip punctuation, the wake word and polite filler."""
    t = text.lower().replace("’", "'").replace("'", "")
    t = t.replace("%", " percent ")
    t = re.sub(r"[^a-z0-9\s]", " ", t)          # punctuation and hyphens -> space
    t = re.sub(r"\bjarvis\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = _LEADING.sub("", t)
    t = _TRAILING.sub("", t)
    return t.strip()


def _number_at(tokens, i):
    """Read one number starting at tokens[i]. Returns (value, next_index)."""
    token = tokens[i]
    if token == "hundred":
        return 100, i + 1
    if token in _TENS:
        value = _TENS[token]
        if i + 1 < len(tokens) and tokens[i + 1] in _ONES and 1 <= _ONES[tokens[i + 1]] <= 9:
            return value + _ONES[tokens[i + 1]], i + 2
        return value, i + 1
    value = _ONES[token]
    if value <= 9 and i + 1 < len(tokens) and tokens[i + 1] == "hundred":
        return value * 100, i + 2
    return value, i + 1


def words_to_numbers(text):
    """'volume to fifty five percent' -> 'volume to 55 percent'."""
    tokens = text.split()
    out, i = [], 0
    while i < len(tokens):
        token = tokens[i]
        if token == "a" and i + 1 < len(tokens) and tokens[i + 1] == "hundred":
            out.append("100")
            i += 2
        elif token in _ONES or token in _TENS or token == "hundred":
            value, i = _number_at(tokens, i)
            out.append(str(value))
        else:
            out.append(token)
            i += 1
    return " ".join(out)


# ---------------------------------------------------------------
# Command table
# ---------------------------------------------------------------

@dataclass
class Command:
    name: str
    patterns: list
    handler: Callable
    numeric: bool = False                      # match against text with number words converted
    validate: Optional[Callable] = None        # extra check on the match
    confirm: Optional[Callable] = None         # returns a yes/no question to ask first


COMMANDS = []


def command(name, patterns, handler, numeric=False, validate=None, confirm=None):
    COMMANDS.append(Command(
        name=name,
        patterns=[re.compile(p) for p in patterns],
        handler=handler,
        numeric=numeric,
        validate=validate,
        confirm=confirm,
    ))


# Small building blocks for the patterns below.
THE = r"(?:the |my |your )?"
VOL = r"(?:volume|sound|audio)"
BRI = r"(?:(?:screen|display) )?brightness"
LITTLE = r"(?:a bit |a little |a lot |slightly |much |way )?"
PC = r"(?:pc|computer|laptop|machine|windows)"
SET_VERB = r"(?:(?:set|change|make|put|turn|adjust) )?"

# --- Volume ---------------------------------------------------
command("volume_set", [
    SET_VERB + THE + VOL + r" (?:to |at |on )?(?P<n>\d{1,3})(?: percent)?",
    THE + VOL + r" (?:(?:set|put|turn|change|adjust) )?(?:to |at |on )?(?P<n>\d{1,3})(?: percent)?",
], lambda m: WindowsSystem.set_volume(int(m["n"])),
    numeric=True)

command("volume_up", [
    r"(?:turn |bring |crank |bump |pump )?" + THE + VOL + r" (?:up|higher|louder)",
    r"(?:turn up|raise|increase|bump up|pump up|crank up|boost) " + THE + VOL,
    r"(?:make (?:it|" + THE + VOL + r") )?" + LITTLE + r"louder",
], lambda m: WindowsSystem.change_volume(+10))

command("volume_down", [
    r"(?:turn |bring |keep )?" + THE + VOL + r" (?:down|lower|softer|quieter)",
    r"(?:turn down|lower|decrease|reduce) " + THE + VOL,
    r"(?:make (?:it|" + THE + VOL + r") )?" + LITTLE + r"(?:quieter|softer)",
], lambda m: WindowsSystem.change_volume(-10))

command("mute", [
    r"mute(?: it| " + THE + r"(?:" + VOL + r"|pc|computer|speakers|system))?",
    r"(?:turn|switch) off " + THE + r"(?:sound|audio|volume)",
    r"(?:turn|switch) " + THE + r"(?:sound|audio|volume) off",
], lambda m: WindowsSystem.mute(True))

command("unmute", [
    r"(?:unmute|un mute)(?: it| " + THE + r"(?:" + VOL + r"|pc|computer|speakers|system))?",
    r"(?:turn|switch) (?:on|back on) " + THE + r"(?:sound|audio|volume)",
    r"(?:turn|switch) " + THE + r"(?:sound|audio|volume) (?:back )?on",
], lambda m: WindowsSystem.mute(False))

# --- Brightness -----------------------------------------------
command("brightness_set", [
    SET_VERB + THE + BRI + r" (?:to |at )?(?P<n>\d{1,3})(?: percent)?",
    THE + BRI + r" (?:(?:set|put|turn|change|adjust) )?(?:to |at )?(?P<n>\d{1,3})(?: percent)?",
], lambda m: WindowsSystem.set_brightness(int(m["n"])),
    numeric=True)

command("brightness_up", [
    r"(?:turn |bring )?" + THE + BRI + r" (?:up|higher)",
    r"(?:turn up|raise|increase|boost) " + THE + BRI,
    r"(?:make (?:it|" + THE + r"(?:screen|display)) )?" + LITTLE + r"brighter",
    r"brighten(?: " + THE + r"(?:screen|display))?",
], lambda m: WindowsSystem.change_brightness(+10))

command("brightness_down", [
    r"(?:turn |bring )?" + THE + BRI + r" (?:down|lower)",
    r"(?:turn down|lower|decrease|reduce|dim) " + THE + BRI,
    r"(?:make (?:it|" + THE + r"(?:screen|display)) )?" + LITTLE + r"(?:dimmer|darker)",
    r"dim(?: " + THE + r"(?:screen|display|monitor))?",
], lambda m: WindowsSystem.change_brightness(-10))

# --- System stats ---------------------------------------------
_STAT = r"(?:usage|stats?|status|resources|load|performance)"
_ASK = r"(?:(?:what is|whats|what are|show|show me|tell me|give me|check|read|how is|hows) )?"

command("stats_cpu", [
    _ASK + THE + r"cpu(?: " + _STAT + r")?",
], lambda m: WindowsSystem.get_system_stats("cpu"))

command("stats_ram", [
    _ASK + THE + r"ram(?: " + _STAT + r")?",
    _ASK + THE + r"memory " + _STAT,
    r"how much (?:ram|memory)"
    r"(?: (?:am i using|is being used|is in use|are we using|do i use|is used))?",
], lambda m: WindowsSystem.get_system_stats("ram"))

command("stats", [
    _ASK + THE + r"(?:gpu|system|pc|computer|hardware|resource)s? " + _STAT,
    r"(?:full |complete |system |pc |computer )+(?:report|stats|status|check)",
    r"how is " + THE + r"(?:pc|computer|laptop|system) doing",
    r"(?:check|show|tell me|give me) (?:everything|all stats|all system stats)",
], lambda m: WindowsSystem.get_system_stats("all"))

# --- Lock and power -------------------------------------------
command("lock", [
    r"lock(?: " + THE + r"(?:pc|computer|windows|workstation|screen|laptop))?",
    r"lock up",
], lambda m: WindowsSystem.lock())

# --- Power, with spoken confirmation and timers (Batch 4) ----------
_SHUT = r"(?:shut ?down|turn off|power off|switch off)"
_REST = r"(?:restart|reboot)"
_WAIT = (r"(?:(?P<half>half(?: an?)?)|(?P<n>\d+|an?)) ?"
         r"(?P<unit>seconds?|secs?|minutes?|mins?|hours?|hrs?)")


def _wait_seconds(m):
    amount = 0.5 if m["half"] else (1 if m["n"] in ("a", "an") else int(m["n"]))
    return int(amount * {"s": 1, "m": 60, "h": 3600}[m["unit"][0]])


def _sane_wait(m):
    return 10 <= _wait_seconds(m) <= 86400          # 10 seconds to 24 hours


command("abort_shutdown", [
    r"(?:cancel|stop|abort) " + THE + r"(?:shutdown|shut down|restart|reboot|timer|countdown)",
    r"abort",
], lambda m: WindowsSystem.cancel_shutdown())

command("shutdown_timer", [
    _SHUT + r"(?: " + THE + PC + r")? (?:in|after) " + _WAIT,
], lambda m: WindowsSystem.shutdown(_wait_seconds(m)),
    numeric=True, validate=_sane_wait,
    confirm=lambda m: f"Shut down in {spoken_duration(_wait_seconds(m))}. Are you sure? Say yes or no.")

command("restart_timer", [
    _REST + r"(?: " + THE + PC + r")? (?:in|after) " + _WAIT,
], lambda m: WindowsSystem.restart(_wait_seconds(m)),
    numeric=True, validate=_sane_wait,
    confirm=lambda m: f"Restart in {spoken_duration(_wait_seconds(m))}. Are you sure? Say yes or no.")

command("shutdown", [
    _SHUT + r" " + THE + PC,
], lambda m: WindowsSystem.shutdown(30),
    confirm=lambda m: "Are you sure you want to shut down the computer? Say yes or no.")

command("restart", [
    _REST + r" " + THE + PC,
], lambda m: WindowsSystem.restart(30),
    confirm=lambda m: "Are you sure you want to restart the computer? Say yes or no.")

command("sleep", [                                   # replaces the sleep command from my last message
    r"put " + THE + PC + r" (?:to sleep|in sleep mode)",
    r"go to sleep",
    r"sleep(?: mode)?",
    r"(?:sleep|suspend) " + THE + PC,
], lambda m: WindowsSystem.sleep(),
    confirm=lambda m: "Are you sure you want to put the computer to sleep? Say yes or no.")

# --- Folders and drives ---------------------------------------
command("open_folder", [
    r"(?:open|show me|show) " + THE
        + r"(?P<folder>downloads?|documents?|desktop|pictures?|photos?|music|videos?|home|temp|appdata|screenshots?|program files)(?: folder)?",
], lambda m: WindowsSystem.open_folder(m["folder"]))

command("open_drive", [
    r"(?:open|show me|show) " + THE
    + r"(?:(?P<d1>[a-z]) ?drive|drive (?P<d2>[a-z]))(?: folder)?",
], lambda m: WindowsSystem.open_folder(f"{m['d1'] or m['d2']} drive"))

# --- Settings pages (Batch 4) ----------------------------------------
command("open_settings", [
    r"(?:(?:open|show me|show|go to|take me to) )?" + THE + r"(?P<page>[a-z ]+?) settings(?: page| menu)?",
], lambda m: WindowsSystem.open_settings(m["page"]),
    validate=lambda m: m["page"].strip() in SETTINGS_PAGES)

# --- Apps -----------------------------------------------------
_APP_BAD_FIRST = {"a", "an", "some", "any", "up", "it", "that", "these", "those",
                  "another", "to", "and", "or"}
_APP_BAD_WORDS = {"and", "or", "then", "with", "about", "for", "to", "in", "on", "at",
                  "is", "are", "was", "can", "will", "please", "me", "you", "minded",
                  "door", "window", "session", "conversation", "up"}


def _plausible_app(m):
    """Reject 'open a jar', 'open ai is a company', 'open up'... (those go to Qwen)."""
    words = m["app"].split()
    if not 1 <= len(words) <= 3:
        return False
    if words[0] in _APP_BAD_FIRST:
        return False
    return not any(word in _APP_BAD_WORDS for word in words)


# --- Websites -----------------------------------------------------------
_SITE_WORDS = sorted(set(WindowsSystem.SITES) | set(WindowsSystem.SITE_ALIASES),
                     key=len, reverse=True)
_SITE = r"(?P<site>" + "|".join(re.escape(s) for s in _SITE_WORDS) + r")"
_ENGINE = r"(?P<engine>google maps|google|youtube|wikipedia|maps)"

command("open_website", [
    r"(?:open|go to|launch|take me to|visit|show me) " + THE + _SITE
    + r"(?: ?\.com| dot com| website| site| web)?",
], lambda m: WindowsSystem.open_website(m["site"]))

_ENGINE_END = r"(?:on|in|using|with) (?:the )?" + _ENGINE
_SEARCH_VERB = r"(?:search|look up|lookup|google)"


def _plausible_search(m):
    """Leave 'search my memory...' style phrases to the memory commands / Qwen."""
    query = m["query"].strip()
    return bool(query) and not re.match(
        r"(?:(?:my|your|the) )?(?:memory|memories|facts?|conversations?)\b", query)


command("search_web", [
    # "search youtube for lofi", "search google maps pizza"
    r"search " + _ENGINE + r"(?: for)? (?P<query>.+)",
    # "search lofi on youtube", "google indiana jones game on google"
    _SEARCH_VERB + r"(?: for| up)? (?P<query>.+?) " + _ENGINE_END,
    # "search indiana jones game on the web / internet"
    _SEARCH_VERB + r"(?: for| up)? (?P<query>.+?) (?:on|in|using) the (?:web|internet)",
    # "search indiana jones game", "look up indiana jones", "google indiana jones",
    # "search the web for ...", "search online for ..."
    _SEARCH_VERB + r"(?: the web| the internet| online)?(?: for| up)? (?P<query>.+)",
], lambda m: WindowsSystem.search_web(m.groupdict().get("engine") or "google", m["query"]),
    validate=_plausible_search)



command("open_app", [
    r"(?:open|launch) " + THE + r"(?P<app>.+?)(?: app| application| program)?",
], lambda m: WindowsSystem.open_app(m["app"]), validate=_plausible_app)

_BARE_SKIP = {"word", "edge", "files",}     # too common as single words


def _bare_app_pattern():
    names = sorted((n for n in APP_ALIASES if n not in _BARE_SKIP), key=len, reverse=True)
    return r"(?P<app>" + "|".join(re.escape(n) for n in names) + r")"


command("open_app_bare", [_bare_app_pattern()],
        lambda m: WindowsSystem.open_app(m["app"]))


# --- Close apps and rescan (Batch 2) ---------------------------------
command("close_app", [
    r"(?:close|quit|exit|kill) " + THE + r"(?P<app>.+?)(?: app| application| program)?",
], lambda m: WindowsSystem.close_app(m["app"]), validate=_plausible_app)

command("rescan_apps", [
    r"(?:rescan|refresh|reload|update) " + THE + r"(?:installed )?(?:apps|applications|programs|app list)",
    r"scan (?:for )?(?:new )?(?:apps|applications|programs)",
], lambda m: WindowsSystem.rescan_apps())


# --- Screenshot, windows (Batch 4) -----------------------------------
command("screenshot", [
    r"(?:(?:take|grab|capture|make) (?:a |the )?)?(?:screenshot|screen shot|screen capture)"
    r"(?: of (?:the |my )?(?:screen|desktop))?",
    r"(?:take|grab) (?:a )?picture of (?:the |my )?screen",
], lambda m: WindowsSystem.screenshot())

command("minimize_all", [
    r"(?:minimi[sz]e|hide) (?:all|everything|every window|all apps)(?: windows)?",
], lambda m: WindowsSystem.minimize_all())

command("restore_windows", [
    r"(?:restore|bring back|unminimi[sz]e) (?:all |the |my )?windows",
], lambda m: WindowsSystem.restore_windows())

command("switch_window", [
    r"(?:switch|change|swap|go to)(?: the)?(?: to)?(?: next| previous| last| other)? (?:window|windows|app)",
    r"(?:next|previous|last|other) (?:window|app)",
    r"alt tab",
], lambda m: WindowsSystem.switch_window())



# --- Batch 5: help, window controls, theme, window screenshot ---------
_TOPICS = (r"(?:pc|computer|laptop|windows|window|apps|folders|volume|brightness|"
           r"power|screen|screenshots|stats)")
_PREP = r"(?:on|with|in|inside|for|about|to)(?: of)?"
_WHAT = r"(?:what can you do|what are your commands|what commands do you (?:have|know))"
_HELP = r"(?:help|(?:list|show|tell me)(?: all)?(?: your)? (?:pc |system )?commands)"

# The topic is REQUIRED, so a bare "what can you do" or "help" falls through to Qwen.
command("pc_help", [
    _WHAT + r" " + _PREP + r" " + THE + r"(?P<topic>" + _TOPICS + r")",
    _HELP + r" " + _PREP + r" " + THE + r"(?P<topic>" + _TOPICS + r")",
    r"(?:list|show|tell me)(?: all)?(?: your)? (?:pc|system) commands",
], lambda m: WindowsSystem.pc_help(m.groupdict().get("topic")))

CAPABILITIES_TEXT = (
    "I can chat and answer questions from what I know, tell the time and date, "
    "do math and unit conversions, remember things about you and our past talks, "
    "change my voice, and control your PC: apps, websites, volume, brightness, "
    "windows, screenshots and power. I can open web searches in your browser, but I "
    "can't read the results, set reminders, or play music. "
    "Say what can you do on my PC for the PC commands, sir."
)

command("capabilities", [
    r"what (?:tasks |things |else |all )?can you (?:do|help(?: me)?(?: with)?)",
    r"what are your (?:abilities|capabilities|features|skills|functions)",
    r"what do you do",
    r"how can you help(?: me)?",
    r"(?:list|tell me|show me) your (?:abilities|capabilities|features|skills)",
], lambda m: CAPABILITIES_TEXT)

_WIN = r"(?:the |this |my |current |active |focused )*window"

command("window_minimize", [r"minimi[sz]e(?: " + _WIN + r")?"],
        lambda m: WindowsSystem.window_action("minimize"))
command("window_maximize", [r"maximi[sz]e(?: " + _WIN + r")?"],
        lambda m: WindowsSystem.window_action("maximize"))
command("window_restore", [r"restore " + _WIN],
        lambda m: WindowsSystem.window_action("restore"))
command("window_close", [r"close " + _WIN],
        lambda m: WindowsSystem.window_action("close"))

_LEFT = r"(?:left|lift|lef)"
_RIGHT = r"(?:right|write|rite)"
_SIDE = r"(?P<side>" + _LEFT + "|" + _RIGHT + r")"

command("window_snap", [
    r"snap(?: " + _WIN + r")? (?:to )?(?:the )?" + _SIDE + r"(?: side)?",
    r"(?:move|put) " + _WIN + r" (?:to )?(?:the )?" + _SIDE + r"(?: side)?",
], lambda m: WindowsSystem.snap_window(
    "right" if m["side"] in ("right", "write", "rite") else "left"))

command("theme", [
    r"(?:switch|change|go|set|turn|use|enable)(?: it)?(?: on| to)? " + THE
        + r"(?P<mode>dark|light)(?: mode| theme)?",
    r"(?P<mode>dark|light) (?:mode|theme)(?: on)?",
    r"(?:toggle|flip|switch|change) " + THE + r"(?:(?:dark|light|windows|system) )?(?:mode|theme)",
], lambda m: WindowsSystem.set_theme(m.groupdict().get("mode")))

command("screenshot_window", [
    r"(?:(?:take|grab|capture|make) (?:a |the )?)?(?:screenshot|screen shot|screen capture)"
    r"(?: of)?(?: this| the current| the active| the focused| current| active| focused| the) window",
    r"(?:(?:take|grab) (?:a )?)?window (?:screenshot|screen shot)",
], lambda m: WindowsSystem.screenshot_window())


# ---------------------------------------------------------------
# Parsing and dispatch
# ---------------------------------------------------------------

def parse_system_command(text):
    """Return (Command, match) for the first command that fits, else None."""
    normal = normalize(text)
    if not normal:
        return None
    normal_numbers = words_to_numbers(normal)
    for cmd in COMMANDS:
        subject = normal_numbers if cmd.numeric else normal
        for pattern in cmd.patterns:
            match = pattern.fullmatch(subject)
            if match and (cmd.validate is None or cmd.validate(match)):
                return cmd, match
    return None


def parse_intent(text):
    """(name, arguments) without running anything. Used by the tests."""
    found = parse_system_command(text)
    if found is None:
        return None
    cmd, match = found
    args = {key: value for key, value in match.groupdict().items() if value is not None}
    return cmd.name, args


# Words that make an unmatched phrase look like an attempted PC command.
_COMMAND_VERBS = {
    "open", "launch", "close", "set", "turn", "change", "switch", "mute", "unmute",
    "lock", "shut", "shutdown", "restart", "reboot", "volume", "brightness",
    "increase", "decrease", "raise", "lower", "dim", "brighten", "minimize",
    "maximize", "snap", "abort", "screenshot", "take", "play", "pause", "skip", "next", "sleep", "resume",
}


def _append_log(path, line):
    """Append one line to a log file. Logging must never break Jarvis."""
    try:
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            return
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass


def _log_unmatched(text):
    normal = normalize(text)
    if normal and normal.split()[0] in _COMMAND_VERBS:
        _append_log(UNMATCHED_LOG, f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{text.strip()}")


def _log_error(context, text):
    print(f"[system_commands] {context} failed for: {text!r}")
    _append_log(ERROR_LOG,
                f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{context}\t{text.strip()}\n"
                + traceback.format_exc())


CONFIRM_TIMEOUT = 25            # seconds you have to answer yes or no
_YES = re.compile(r"(?:yes|yeah|yep|yup|sure|ok|okay|confirm|confirmed|proceed|affirmative|"
                  r"absolutely|do it|go ahead|yes do it|yes go ahead|(?:yes )?(?:i am|im) sure)")
_NO = re.compile(r"(?:no|nope|nah|negative|cancel(?: it)?|dont(?: do it)?|abort|stop|"
                 r"never ?mind|wait|hold on|no (?:dont|cancel(?: it)?))")
_pending = None                 # (Command, match, time asked) while waiting for yes or no


def _run(cmd, match, text):
    try:
        reply = cmd.handler(match)
    except Exception:
        _log_error(cmd.name, text)
        return "Sorry, that command failed."
    return reply or "Done."


def _answer_pending(text):
    """If Jarvis just asked 'are you sure?', handle the answer. None = not an answer."""
    global _pending
    if _pending is None:
        return None
    cmd, match, asked_at = _pending
    _pending = None                         # one answer only; anything else cancels
    normal = normalize(text)
    if _NO.fullmatch(normal):
        return "Okay, cancelled."
    if _YES.fullmatch(normal):
        if time.time() - asked_at > CONFIRM_TIMEOUT:
            return "That took too long, so I cancelled it. Ask me again."
        return _run(cmd, match, text)
    return None                             # a different request: drop the old one, handle this normally


def handle_system_command(text):
    """Return the spoken reply for a system command, or None to pass to Qwen."""
    global _pending

    answered = _answer_pending(text)
    if answered is not None:
        return answered

    try:
        found = parse_system_command(text)
    except Exception:
        _log_error("parsing", text)
        return None

    if found is None:
        _log_unmatched(text)
        return None

    cmd, match = found
    if cmd.confirm is not None:
        try:
            question = cmd.confirm(match)
        except Exception:
            _log_error(cmd.name + " confirm", text)
            return "Sorry, that command failed."
        _pending = (cmd, match, time.time())
        return question

    return _run(cmd, match, text)
