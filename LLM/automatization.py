"""A mini n8n: workflows built from blocks, run on a schedule.

One automation is one workflow:

    trigger (when)  ->  block (a tool from tools.py)  ->  block  ->  output

The trigger is a plain-language time ("10:00, every day"), every block is a
tool from tools.py, and the result of the last block is what Kate says when
the workflow fires.  The LLM builds workflows by writing a tag, exactly like
the tool tags in tools.py:

    <new_automatization; time="10:00, every day"; actions="get_weather=\"Prague\""/>

Workflows are stored as JSON next to memory.json; scheduler_loop() fires
whatever is due and hands each result to a callback.
"""
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

try:
    from .tools import AUTOMATION_EXCLUDED, TOOLS, TOOL_FUNCTIONS, call_tool, example_value
except ImportError:  # run directly: python LLM/automatization.py
    from tools import AUTOMATION_EXCLUDED, TOOLS, TOOL_FUNCTIONS, call_tool, example_value

AUTOMATIONS_FILE = Path(os.environ.get(
    "AUTOMATIONS_FILE",
    Path(__file__).resolve().parent.parent / "automatizations.json",
))

POLL_SECONDS = 15.0  # how often the scheduler loop looks for due automations
PREVIOUS = "{{previous}}"  # argument placeholder for the previous block's result
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
BLOCK_SEPARATORS = (";", "->", "=>", "|", " then ")

# The scheduler thread and the chat thread both read and write the JSON file.
_LOCK = threading.RLock()

AUTOMATION_NAME_RE = re.compile(
    r"<\s*((?:new|create|add)_automatizations?"
    r"|(?:list|show)_automatizations?"
    r"|(?:remove|delete|cancel)_automatizations?)\b",
    re.IGNORECASE,
)


# ---- blocks ----------------------------------------------------------------
def blocks() -> list:
    """The block catalog: every tool that makes sense on a schedule is one block."""
    return [
        spec.get("function", {})
        for spec in TOOLS
        if spec.get("function", {}).get("name") not in AUTOMATION_EXCLUDED
    ]


def _block_params(name: str) -> dict:
    for spec in TOOLS:
        fn = spec.get("function", {})
        if fn.get("name") == name:
            return fn.get("parameters", {}).get("properties", {})
    return {}


# ---- storage ---------------------------------------------------------------
def _load(path=None) -> list:
    target = Path(path) if path else AUTOMATIONS_FILE
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("automatizations", []) if isinstance(data, dict) else data
    return [a for a in items if isinstance(a, dict)]


def _save(items: list, path=None) -> None:
    target = Path(path) if path else AUTOMATIONS_FILE
    temp = target.with_name(target.name + ".tmp")
    try:
        # write to a temp file first so a crash cannot leave half a JSON file behind
        temp.write_text(
            json.dumps({"updated": time.time(), "automatizations": items}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(target)
    except OSError as exc:
        print(f"Could not save automations: {exc}")


# ---- text scanning ---------------------------------------------------------
def _split_outside_quotes(raw: str, separators) -> list:
    """Split on any separator that sits outside quotes and brackets."""
    parts, buf = [], []
    quote, escaped, depth, i = None, False, 0, 0
    while i < len(raw):
        ch = raw[i]
        if escaped:
            buf.append(ch)
            escaped = False
        elif ch == "\\":
            buf.append(ch)
            escaped = True
        elif quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch in "([{":
            depth += 1
            buf.append(ch)
        elif ch in ")]}":
            depth -= 1
            buf.append(ch)
        else:
            hit = next((s for s in separators if raw.startswith(s, i)), None)
            if hit is not None and depth == 0:
                parts.append("".join(buf))
                buf = []
                i += len(hit)
                continue
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        inner = text[1:-1]
        return inner.replace("\\" + text[0], text[0]).replace("\\\\", "\\")
    return text


def _parse_attributes(raw: str) -> dict:
    """Parse 'key="value"; key=value' from inside a tag."""
    parts = []
    for item in _split_outside_quotes(raw, (";", ",")):
        item = item.strip()
        if not item:
            continue
        match = re.match(r"^([A-Za-z_][\w-]*)\s*[=:]\s*(.*)$", item, re.DOTALL)
        if match:
            parts.append((match.group(1).lower(), match.group(2)))
        elif parts:  # an unquoted value cut in half by a comma: glue it back
            key, value = parts[-1]
            parts[-1] = (key, f"{value} {item}")
    return {key: _unquote(value) for key, value in parts}


def _first_attr(attrs: dict, keys) -> str:
    for key in keys:
        value = attrs.get(key)
        if value:
            return str(value).strip()
    return ""


# ---- triggers --------------------------------------------------------------
_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}


def _parse_time_of_day(text: str):
    """Return (hour, minute) mentioned in the text, or None."""
    # drop the dots in "a.m." but keep the ones in "10.30"
    low = re.sub(r"\.(?=[a-z])", "", text.lower())
    meridiem = None
    match = re.search(r"\b(\d{1,2})[:.](\d{2})\s*(am|pm)?\b", low)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        meridiem = match.group(3)
    else:
        for pattern, has_meridiem in ((r"\b(\d{1,2})\s*(am|pm)\b", True),
                                      (r"\b(\d{1,2})\s*o'?clock\b", False),
                                      (r"\bat\s+(\d{1,2})\b", False)):
            match = re.search(pattern, low)
            if match:
                hour = int(match.group(1))
                minute = 0
                if has_meridiem:
                    meridiem = match.group(2)
                break
        else:
            if re.search(r"\bnoon\b", low):
                hour, minute = 12, 0
            elif re.search(r"\bmidnight\b", low):
                hour, minute = 0, 0
            else:
                match = re.search(
                    r"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+(am|pm)\b",
                    low,
                )
                if not match:
                    return None
                hour, minute = _WORD_NUMBERS[match.group(1)], 0
                meridiem = match.group(2)
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    return hour, minute


def _parse_interval(text: str):
    """Return a repeat interval in minutes, or None."""
    low = text.lower()
    match = re.search(r"\bevery\s+(\d+)\s*(minutes?|mins?)\b", low)
    if match:
        return max(1, int(match.group(1)))
    match = re.search(r"\bevery\s+(\d+)\s*hours?\b", low)
    if match:
        return max(1, int(match.group(1))) * 60
    if re.search(r"\b(every\s+hour|hourly)\b", low):
        return 60
    if re.search(r"\bevery\s+half\s+an?\s+hour\b", low):
        return 30
    return None


_DAY_ALIASES = {}
for _index, _name in enumerate(WEEKDAYS):
    _DAY_ALIASES[_name.lower()] = _index
    _DAY_ALIASES[_name.lower()[:3]] = _index
_DAY_ALIASES.update({"tues": 1, "thur": 3, "thurs": 3})

_DAY_RE = re.compile(
    r"\b(mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|saturdays?|sundays?"
    r"|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\b"
)


def _parse_days(text: str) -> list:
    """Return the weekdays (0=Monday) the text mentions; all days by default."""
    low = text.lower()
    if re.search(r"\bweekdays?\b", low):
        return [0, 1, 2, 3, 4]
    if re.search(r"\bweekends?\b", low):
        return [5, 6]
    days = {_DAY_ALIASES[m] for m in _DAY_RE.findall(low) if m in _DAY_ALIASES}
    return sorted(days) if days else list(range(7))


def parse_trigger(text: str) -> tuple:
    """Turn '10:00, every day' into (trigger dict, error message)."""
    text = (text or "").strip()
    if not text:
        return None, "Tell me when the automation should run, for example every day at 10 AM."
    interval = _parse_interval(text)
    if interval:
        return {"interval_minutes": interval}, None
    moment = _parse_time_of_day(text)
    if moment is None:
        return None, (f"I did not understand the time {text}. Try something like "
                      "every day at 10 AM or every Monday at 9 AM.")
    hour, minute = moment
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None, f"The time {text} is not a real time of day."
    return {"hour": hour, "minute": minute, "days": _parse_days(text)}, None


# ---- blocks (actions) ------------------------------------------------------
def _parse_block_arguments(raw: str, single: str) -> dict:
    """Parse the argument part of a block call into a kwargs dict."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, dict):
            return {str(k): v for k, v in data.items()}
    kwargs = {}
    for item in _split_outside_quotes(raw, (",",)):
        item = item.strip()
        if not item:
            continue
        match = re.match(r"^([A-Za-z_]\w*)\s*=\s*(.*)$", item, re.DOTALL)
        if match:
            kwargs[match.group(1)] = _unquote(match.group(2))
        elif single:
            kwargs[single] = _unquote(item)
    return kwargs


def _parse_block(piece: str) -> tuple:
    """Parse one block call like 'get_weather="Prague"' into (name, kwargs)."""
    piece = piece.strip()
    if piece.startswith("<"):  # already written as a tool tag
        inner = piece[1:].strip()
        if inner.endswith("/>"):
            inner = inner[:-2].strip()
        parts = re.split(r"\s*[;,]\s*|\s+", inner, maxsplit=1)
        name = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
    else:
        match = re.match(r"^([A-Za-z_]\w*)\s*(.*)$", piece, re.DOTALL)
        if not match:
            return None, f"I could not read the block {piece}."
        name = match.group(1).lower()
        rest = re.sub(r"^\s*[=:;]\s*", "", match.group(2)).strip()
        if rest[:1] == "(" and rest[-1:] == ")":
            rest = rest[1:-1].strip()
        if rest and "=" not in rest and not rest.startswith("{"):
            rest = re.sub(r"^(?:for|in|at|of)\s+", "", rest, flags=re.IGNORECASE)
    if name in AUTOMATION_EXCLUDED:
        return None, f"The block {name} only works in a live conversation, not in an automation."
    if name not in TOOL_FUNCTIONS:
        available = ", ".join(sorted(set(TOOL_FUNCTIONS) - AUTOMATION_EXCLUDED))
        return None, f"I do not know a block called {name}. The blocks I have are: {available}."
    params = _block_params(name)
    single = next(iter(params)) if params else None  # a bare value fills the first parameter
    return name, _parse_block_arguments(rest, single)


def parse_actions(text: str) -> tuple:
    """Turn 'get_weather="Prague" -> get_time' into (block list, error message)."""
    text = (text or "").strip()
    if not text:
        return None, 'Tell me what the automation should do, for example get_weather="Prague".'
    pieces = [p.strip() for p in _split_outside_quotes(text, BLOCK_SEPARATORS) if p.strip()]
    merged = []
    for piece in pieces:
        if merged and piece[:1] in "\"'({":
            # 'get_weather; "Prague"': the quote is the previous block's argument
            merged[-1] = f"{merged[-1]} {piece}"
        else:
            merged.append(piece)
    actions = []
    for piece in merged:
        name, arguments = _parse_block(piece)
        if name is None:
            return None, arguments
        actions.append({"block": name, "arguments": arguments})
    if not actions:
        return None, 'Tell me what the automation should do, for example get_weather="Prague".'
    return actions, None


# ---- plain-English description ---------------------------------------------
_BLOCK_PHRASES = {
    "get_weather": {
        "with_arg": "tell you the weather for {arg}",
        "without_arg": "check the weather",
        "name": "{arg} weather",
        "name_no_arg": "the weather",
    },
    "get_time": {
        "with_arg": "tell you the time",
        "without_arg": "tell you the time",
        "name": "the time",
        "name_no_arg": "the time",
    },
}
_BLOCK_PHRASES["get_news"] = {
    "with_arg": "tell you the news about {arg}",
    "without_arg": "tell you the news",
    "name": "{arg} news",
    "name_no_arg": "the news",
}
_DEFAULT_PHRASE = {
    "with_arg": "run {block} with {arg}",
    "without_arg": "run {block}",
    "name": "{block} {arg}",
    "name_no_arg": "{block}",
}


def _phrase(block: str, plain: str, with_arg: str, label: str, label_arg: str) -> None:
    _BLOCK_PHRASES[block] = {"with_arg": with_arg, "without_arg": plain, "name": label_arg, "name_no_arg": label}


# block: what Kate says she will do (without / with an argument), then the short name (without / with)
_phrase("get_forecast", "tell you the forecast", "tell you the forecast for {arg}", "the forecast", "{arg} forecast")
_phrase("rain_check", "tell you if it will rain", "tell you if it will rain in {arg}", "the rain check", "{arg} rain check")
_phrase("get_sun_times", "tell you sunrise and sunset", "tell you sunrise and sunset for {arg}", "sun times", "{arg} sun times")
_phrase("get_air_quality", "tell you the air quality", "tell you the air quality in {arg}", "air quality", "{arg} air quality")
_phrase("daily_briefing", "give you your daily briefing", "give you your daily briefing", "the daily briefing", "the daily briefing")
_phrase("get_crypto_price", "tell you the bitcoin price", "tell you the {arg} price", "bitcoin price", "{arg} price")
_phrase("convert_currency", "check the exchange rate", "tell you what {arg} is", "exchange rate", "exchange rate")
_phrase("read_list", "read you your shopping list", "read you your {arg} list", "shopping list", "{arg} list")
_phrase("read_notes", "read you your notes", "read you your notes", "your notes", "your notes")
_phrase("tell_joke", "tell you a joke", "tell you a joke", "a joke", "a joke")
_phrase("fun_fact", "tell you a fun fact", "tell you a fun fact", "a fun fact", "a fun fact")
_phrase("moon_phase", "tell you the moon phase", "tell you the moon phase", "moon phase", "moon phase")
_phrase("next_holiday", "tell you the next holidays", "tell you the next holidays in {arg}", "holidays", "{arg} holidays")
_phrase("days_until", "count the days to a date", "tell you how many days until {arg}", "countdown", "{arg} countdown")
_phrase("world_time", "tell you the time somewhere", "tell you the time in {arg}", "world time", "{arg} time")
_phrase("pc_status", "check on the computer", "check on the computer", "computer status", "computer status")
_phrase("animal_sound", "tell you an animal sound", "tell you what a {arg} says", "an animal sound", "{arg} sound")
_phrase("get_stock_price", "tell you the stock price", "tell you the {arg} stock price", "stock price", "{arg} stock")
_phrase("today_in_history", "tell you what happened on this day in history", "tell you what happened in history on {arg}", "this day in history", "history of {arg}")
_phrase("get_birthdays", "tell you the upcoming birthdays", "tell you when {arg} birthday is", "birthdays", "{arg} birthday")
_phrase("spending_summary", "tell you how much you spent this month", "tell you how much you spent, {arg}", "spending summary", "spending summary")
_phrase("inspire_me", "tell you an inspiring quote", "tell you an inspiring quote", "a quote", "a quote")


def _arg_forecast(a: dict) -> str:
    day = str(a.get("day", "")).strip()
    return " ".join(x for x in (str(a.get("location", "")).strip(), "" if day.lower() in ("", "today") else day) if x)


_ARG_FORMATTERS = {
    "get_forecast": _arg_forecast,
    "convert_currency": lambda a: f"{a.get('amount') or 1} {a.get('from_currency', '')} to {a.get('to_currency', '')}",
    "get_crypto_price": lambda a: str(a.get("coin", "")),
    "rain_check": lambda a: str(a.get("location", "")),
}


def _action_text(action: dict) -> tuple:
    """Return (description, label) for one block call."""
    block = action.get("block", "")
    arguments = action.get("arguments") or {}
    fmt = _ARG_FORMATTERS.get(block)
    arg = (fmt(arguments) if fmt else " ".join(str(v) for v in arguments.values())).strip()
    phrase = _BLOCK_PHRASES.get(block, _DEFAULT_PHRASE)
    return (
        phrase["with_arg" if arg else "without_arg"].format(arg=arg, block=block),
        phrase["name" if arg else "name_no_arg"].format(arg=arg, block=block),
    )


def trigger_phrase(trigger: dict) -> str:
    """Human-readable schedule, e.g. 'every day at 10 AM'."""
    if not trigger:
        return "at some time"
    interval = trigger.get("interval_minutes")
    if interval:
        if interval % 60 == 0:
            hours = interval // 60
            return "every hour" if hours == 1 else f"every {hours} hours"
        return "every minute" if interval == 1 else f"every {interval} minutes"
    hour = int(trigger.get("hour", 0))
    minute = int(trigger.get("minute", 0))
    suffix = "AM" if hour < 12 else "PM"
    clock = f"{hour % 12 or 12}" if minute == 0 else f"{hour % 12 or 12}:{minute:02d}"
    moment = f"{clock} {suffix}"
    days = trigger.get("days") or list(range(7))
    if len(days) == 7:
        return f"every day at {moment}"
    if days == [0, 1, 2, 3, 4]:
        return f"on weekdays at {moment}"
    if days == [5, 6]:
        return f"every weekend at {moment}"
    names = [WEEKDAYS[d] for d in days]
    if len(names) == 1:
        return f"every {names[0]} at {moment}"
    plurals = [name + "s" for name in names]
    joined = " and ".join([", ".join(plurals[:-1]), plurals[-1]]) if len(plurals) > 2 else " and ".join(plurals)
    return f"on {joined} at {moment}"


def describe(automation: dict) -> str:
    """One sentence, e.g. 'tell you the weather for Prague every day at 10 AM'."""
    steps = " and then ".join(_action_text(a)[0] for a in automation.get("actions", []))
    return f"{steps} {trigger_phrase(automation.get('trigger', {}))}"


def _label(automation: dict) -> str:
    """Short name, e.g. 'Prague weather'."""
    first = (automation.get("actions") or [{}])[0]
    return _action_text(first)[1] or "automation"


# ---- public API ------------------------------------------------------------
def create(time_text: str, actions_text: str, name: str = "", path=None) -> tuple:
    """Create and store one automation. Returns (automation, error, is_new)."""
    trigger, error = parse_trigger(time_text)
    if error:
        return None, error, False
    actions, error = parse_actions(actions_text)
    if error:
        return None, error, False
    with _LOCK:
        items = _load(path)
        for existing in items:  # the LLM sometimes asks for the same thing twice
            if existing.get("trigger") == trigger and existing.get("actions") == actions:
                return existing, None, False
        automation = {
            "id": uuid.uuid4().hex[:8],
            "name": (name or "").strip() or _label({"actions": actions}),
            "created": time.time(),
            "trigger": trigger,
            "actions": actions,
            "enabled": True,
            "last_run": None,
        }
        items.append(automation)
        _save(items, path)
        return automation, None, True


# ---- matching, removing, listing -------------------------------------------
ALL_WORDS = {"all", "every", "everything", "each"}

# words that carry no signal about *which* automation the user means
STOPWORDS = {
    "a", "an", "the", "my", "me", "you", "your", "i", "it", "its", "this", "that",
    "these", "those", "one", "ones", "please", "can", "could", "would", "will",
    "want", "wanna", "get", "got", "give", "delete", "deleted", "deleting",
    "remove", "removal", "cancel", "canceled", "erase", "drop", "kill", "stop",
    "make", "made", "set", "setup", "thing", "things", "stuff", "something",
    "anything", "automation", "automations", "automatization", "automatizations",
    "automatisation", "automatisations", "workflow", "workflows", "schedule", "job",
    "task", "let", "lets", "maybe", "just", "about", "from", "with", "without",
    "and", "or", "but", "so", "then", "of", "for", "to", "into", "on", "off",
    "in", "at", "now", "there", "here", "every", "all", "each", "everything",
    "single", "like", "scrap", "scrape", "wipe", "trash", "clear", "rid", "meant",
    "mean", "actually", "really", "okay", "ok", "yes", "yeah", "hey", "kate", "them",
    "tell", "check", "run", "read", "day", "days", "also", "too", "any", "is", "are",
}


def _tokens(text: str) -> set:
    """Meaningful lowercase words of a name or description (numbers like 7 count)."""
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if (len(w) > 1 or w.isdigit()) and w not in STOPWORDS}


def _available(automation: dict) -> set:
    return _tokens(str(automation.get("name", ""))) | _tokens(describe(automation))


def _join_words(items: list) -> str:
    items = [str(i) for i in items]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


def _spoken(items: list, with_time: bool = False) -> list:
    """Names for speech. Two automations called 'the news' get their time added so they can be told apart."""
    labels = [_label(a) for a in items]
    twins = {name for name in labels if labels.count(name) > 1}
    return [f"{name} {trigger_phrase(a.get('trigger', {}))}" if with_time or name in twins else name
            for name, a in zip(labels, items)]


def _schedule_key(automation: dict) -> tuple:
    trigger = automation.get("trigger") or {}
    if trigger.get("interval_minutes"):
        return (1, trigger["interval_minutes"], 0, ())
    return (0, int(trigger.get("hour", 0)), int(trigger.get("minute", 0)), tuple(trigger.get("days") or ()))


def _list_text(items: list) -> str:
    """'You have 4 automations. Every day at 7 AM: the news and Prague news. Every day at 10 AM: ...'"""
    if not items:
        return "You have no automations yet."
    if len(items) == 1:
        return f"You have one automation: {_label(items[0])} {trigger_phrase(items[0].get('trigger', {}))}."
    groups = {}
    for automation in sorted(items, key=_schedule_key):
        groups.setdefault(trigger_phrase(automation.get("trigger", {})), []).append(automation)
    sentences = [f"{phrase[:1].upper()}{phrase[1:]}: {_join_words(_spoken(group))}" for phrase, group in groups.items()]
    return f"You have {len(items)} automations. " + ". ".join(sentences) + "."


def list_message(path=None) -> str:
    """Spoken summary of every automation, grouped by time."""
    return _list_text(_load(path))


def _score(automation: dict, key: str, wanted: set) -> float:
    if key and key == str(automation.get("id", "")):
        return 10.0
    name = str(automation.get("name", "")).lower().strip()
    score = 0.0
    if name and key:
        if key == name:
            score += 2
        elif len(key) > 2 and (name in key or key in name):
            score += 1
    if wanted:
        score += len(wanted & _available(automation)) / len(wanted)
    return score


def _pick(items: list, key: str, count: int = 1) -> tuple:
    """The automation(s) a description points at, or ([], question/explanation)."""
    wanted = _tokens(key)
    if not key or (not wanted and len(key) <= 2):
        if len(items) == 1:
            return list(items), ""
        return [], "Which automation should I remove? " + _list_text(items)
    scored = [(s, a) for a in items if (s := _score(a, key, wanted)) > 0]
    if not scored:
        return [], "I do not have an automation like that. " + _list_text(items)
    best = max(s for s, _ in scored)
    tied = [a for s, a in scored if s == best]
    if len(tied) <= max(count, 1):  # asked for the same name twice and two match: take both
        return tied, ""
    names = _join_words(_spoken(tied, with_time=True))
    return [], f"I have {len(tied)} that could be it: {names}. Which one do you mean?"


def _removed_text(removed: list, total_before: int) -> str:
    if not removed:
        return ""
    if len(removed) == 1:
        return f"Done, I removed {_spoken(removed)[0]}."
    if len(removed) == total_before:
        return f"Done, I removed all {len(removed)} automations."
    if len(removed) <= 3:
        return f"Done, I removed {_join_words(_spoken(removed))}."
    return f"Done, I removed {len(removed)} automations."


def remove_many(identifier: str, path=None, remove_all: bool = False, count: int = 1) -> tuple:
    """Delete automations by description. Returns (removed_list, message).

    'all' / 'every news' / remove_all=True removes every automation that matches the
    words left over (no words left = everything). Otherwise one automation is chosen;
    when several match equally well, nothing is deleted and the message is a question.
    """
    with _LOCK:
        items = _load(path)
        if not items:
            return [], "You have no automations to remove."
        key = " ".join((identifier or "").lower().split())
        words = set(re.findall(r"[a-z0-9]+", key))
        everything = remove_all or bool(words & ALL_WORDS)
        if everything:
            wanted = _tokens(key)
            matches = [a for a in items if wanted <= _available(a)]
            if not matches:
                return [], f"I do not have any {' '.join(sorted(wanted))} automations. " + _list_text(items)
        else:
            matches, message = _pick(items, key, count)
            if not matches:
                return [], message
        gone = {id(a) for a in matches}
        _save([a for a in items if id(a) not in gone], path)
        return matches, _removed_text(matches, len(items))


def remove(identifier: str, path=None) -> tuple:
    """Delete one automation whose name or id matches. Returns (removed, message)."""
    removed, message = remove_many(identifier, path=path)
    return (removed[0] if removed else None), message


def run_automation(automation: dict) -> str:
    """Run every block in order; the last block's result is the output."""
    result = ""
    for action in automation.get("actions", []):
        arguments = {}
        for key, value in (action.get("arguments") or {}).items():
            arguments[key] = value.replace(PREVIOUS, result) if isinstance(value, str) else value
        result = call_tool(action.get("block", ""), arguments)
    return (result or "").strip()


def is_due(automation: dict, now: float = None) -> bool:
    """True when the automation's schedule says it should run at `now`."""
    now = time.time() if now is None else now
    trigger = automation.get("trigger") or {}
    interval = trigger.get("interval_minutes")
    if interval:
        base = automation.get("last_run") or automation.get("created") or now
        return now - base >= interval * 60
    local = time.localtime(now)
    days = trigger.get("days") or list(range(7))
    if local.tm_wday not in days:
        return False
    scheduled = time.mktime((local.tm_year, local.tm_mon, local.tm_mday,
                             int(trigger.get("hour", 0)), int(trigger.get("minute", 0)),
                             0, -1, -1, -1))
    if now < scheduled:
        return False
    reference = automation.get("last_run") or automation.get("created")
    return True if reference is None else reference < scheduled


def run_due(path=None, now: float = None) -> list:
    """Run every automation whose time has come. Returns [(automation, result)]."""
    now = time.time() if now is None else now
    with _LOCK:
        due = [a for a in _load(path) if a.get("enabled", True) and is_due(a, now)]
    # The blocks run outside the lock: they hit the network and can take seconds.
    fired = [(automation, run_automation(automation)) for automation in due]
    if fired:
        with _LOCK:
            # reload so an automation created or removed while we were running is not lost
            items = _load(path)
            ran = {a.get("id") for a, _ in fired}
            for automation in items:
                if automation.get("id") in ran:
                    automation["last_run"] = now
            _save(items, path)
        for automation, _ in fired:
            automation["last_run"] = now
    return fired


def scheduler_loop(stop_event=None, handler=None, interval: float = POLL_SECONDS, path=None) -> None:
    """Blocking loop: fire due automations and report each result to the handler.

    `handler(result, automation)` is called from this thread for every
    automation that runs, so the caller can speak or log the output.
    """
    while stop_event is None or not stop_event.is_set():
        try:
            for automation, result in run_due(path=path):
                if result and handler is not None:
                    handler(result, automation)
        except Exception as exc:  # a broken automation must not kill the loop
            print(f"Automation scheduler error: {exc}")
        if stop_event is None:
            time.sleep(interval)
        elif stop_event.wait(interval):
            break


# ---- tags the LLM can write ------------------------------------------------
def _handle_new(attrs: dict, path=None) -> tuple:
    """Returns (status, text) with status new / exists / error."""
    time_text = _first_attr(attrs, ("time", "trigger", "schedule", "when", "at"))
    actions_text = _first_attr(attrs, ("actions", "action", "steps", "blocks", "do"))
    name = _first_attr(attrs, ("name", "title"))
    if not time_text or not actions_text:
        return "error", ("I need a time and the actions to set that up. Tell me when to run it "
                         "and what it should do, for example every day at 10 AM.")
    automation, error, is_new = create(time_text, actions_text, name, path=path)
    if error:
        return "error", error
    return ("new" if is_new else "exists"), describe(automation)


def _remove_target(attrs: dict) -> str:
    return _first_attr(attrs, ("name", "id", "automatization", "automation", "filter", "which")).lower()


def _wants_all(attrs: dict) -> bool:
    return _first_attr(attrs, ("all", "every", "everything")).lower() not in ("", "false", "no", "0")


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([.,!?;:])", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(" ,;:\n")


def _find_tags(text: str) -> list:
    """Return (start, end, command, content) for every automation tag in text.

    The tag body is scanned by hand, not matched by a regex, so quoted values
    may hold anything, including '>' and '/>', without backtracking blowups.
    Like tools.py, a closing '>' without the slash is accepted (the model often
    forgets it), but '->' and '=>' are block separators, not the end of the tag.
    """
    found, last = [], 0
    for match in AUTOMATION_NAME_RE.finditer(text):
        if match.start() < last:  # inside a tag that was already consumed
            continue
        i, quote, escaped, depth = match.end(), None, False, 0
        while i < len(text):
            ch = text[i]
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif quote:
                if ch == quote:
                    quote = None
            elif ch in "\"'":
                quote = ch
            elif ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif depth == 0 and ch == ">" and text[i - 1] not in "-=":
                stop = i - 1 if text[i - 1] == "/" else i
                found.append((match.start(), i + 1, match.group(1), text[match.end():stop]))
                last = i + 1
                break
            i += 1
    return found


def resolve_automation_tags(text: str, path=None) -> tuple:
    """Replace automation tags with their results.

    Returns (resolved_text, number_of_changes), mirroring resolve_tool_tags.
    Several tags of one kind are answered with ONE sentence, placed where the first one stood:
    three removals read "Done, I removed A, B and C." instead of three separate lines.
    """
    tags = _find_tags(text)
    if not tags:
        return text, 0  # leave ordinary replies untouched
    with _LOCK:
        total_before = len(_load(path))
    parsed, wanted_twice = [], {}
    for start, end, command, content in tags:
        attrs = _parse_attributes(content)
        low = command.lower()
        kind = "new" if low.startswith(("new", "create", "add")) else "list" if low.startswith(("list", "show")) else "remove"
        target = _remove_target(attrs) if kind == "remove" else ""
        if kind == "remove":
            wanted_twice[target] = wanted_twice.get(target, 0) + 1
        parsed.append((start, end, kind, attrs, target))

    created, removed, problems, seen, list_text = [], [], [], set(), ""
    for start, end, kind, attrs, target in parsed:  # run them in the order written
        if kind == "new":
            created.append(_handle_new(attrs, path=path))
        elif kind == "list" and not list_text:
            list_text = list_message(path=path)
        elif kind == "remove" and (target, _wants_all(attrs)) not in seen:
            seen.add((target, _wants_all(attrs)))
            gone, message = remove_many(target, path=path, remove_all=_wants_all(attrs), count=wanted_twice[target])
            if gone:
                removed.extend(g for g in gone if g not in removed)
            elif message not in problems:
                problems.append(message)

    sentences = {"list": list_text}
    new = [d for s, d in created if s == "new"]
    known = [d for s, d in created if s == "exists"]
    bad = list(dict.fromkeys(d for s, d in created if s == "error"))
    sentences["new"] = " ".join(filter(None, [
        f"All set, I will {_join_words(new)}." if new else "",
        (f"You already have that one: {known[0]}." if len(known) == 1 else f"You already have those: {_join_words(known)}.") if known else "",
        *bad,
    ]))
    sentences["remove"] = " ".join(filter(None, [_removed_text(removed, total_before), *problems]))

    pieces, last, done = [], 0, set()
    for start, end, kind, _attrs, _target in parsed:
        pieces.append(text[last:start])
        if kind not in done:  # the combined sentence goes where the first tag of its kind stood
            done.add(kind)
            pieces.append(f" {sentences[kind]} ")
        last = end
    pieces.append(text[last:])
    return _tidy("".join(pieces)), len(parsed)


def automation_prompt() -> str:
    """Describe the automation tag syntax for the system prompt."""
    lines = [
        "You can schedule automations, little workflows that run later on their own, "
        "by writing one tag; the tag is replaced by the real confirmation before the "
        "user sees your message. Use them only when the user asks for something "
        "repeated or scheduled, like every day at ten.",
        "",
        'Create: <new_automatization; time="10:00, every day"; actions="get_weather=\\"Prague\\""/>',
        '- time: when it runs, for example "10:00, every day", "every Monday at 9 AM", '
        '"on weekdays at 8:30 AM", "every 30 minutes", "every 2 hours".',
        "- actions: one or more blocks, separated by ; or ->. A block is a block name "
        'with its argument, for example get_weather="Prague" or get_time. Write '
        "{{previous}} to feed the result of one block into the next one.",
        '- name="..." is optional.',
        "List: <list_automatizations/>",
        'Remove: <remove_automatization; name="the news at 7 AM"/>',
        "- Describe the automation in your own words and add its time when several are alike. "
        "Write ONE tag per automation and never the same tag twice.",
        '- To remove several at once: name="all" removes every automation, name="all news" removes '
        'every news one, name="all Prague" every Prague one. When the user says all, every or '
        "everything, write that tag right away: do not list first, do not ask, do not repeat yourself.",
        "- If the user names one thing and several match equally well, the tag asks them "
        "which one by itself; you never need to ask.",
        "",
        "Available blocks (each block is one of your tools):",
    ]
    for fn in blocks():
        name = fn.get("name", "")
        props = fn.get("parameters", {}).get("properties", {})
        needed = [k for k in fn.get("parameters", {}).get("required", []) if k in props] or list(props)[:1]
        if not props:
            call = f'actions="{name}"'
        elif len(props) == 1:
            call = f'actions="{name}=\\\"{example_value(props[needed[0]])}\\\""'
        elif len(needed) == 1:
            call = f'actions="{name}=\\\"{example_value(props[needed[0]])}\\\""'
        else:
            pairs = ", ".join(f'{k}=\\"{example_value(props[k])}\\"' for k in needed)
            call = f"actions='{name}={pairs}'"
        lines.append(f"- {name}: {fn.get('description', '')} Write it as {call}.")
    lines += [
        "",
        'If a value needs quotes inside actions, either escape them '
        '(actions="get_weather=\\"Prague\\"") or wrap the whole value in single quotes '
        "(actions='get_weather=\"Prague\"').",
        "Write the tag exactly where the confirmation belongs and let the confirmation "
        "come from the tag; do not describe the automation yourself and never guess what "
        "it did later.",
    ]
    return "\n".join(lines)


# ---- fast path: "delete all my news automations" without asking the LLM ------
_VERB_WITH_NOUN = re.compile(r"\b(delete|remove|scrap|scrape|erase|wipe|trash|cancel|clear|stop|kill|get rid of)\b")
_VERB_IN_CONTEXT = re.compile(r"\b(delete|remove|scrap|scrape|erase|wipe|trash|get rid of)\b")
_ALL_WORD = re.compile(r"\b(all|every|each|everything)\b")
_NOUN = re.compile(r"\b(automati[sz]ations?|automations?|workflows?|schedules?|scheduled)\b")
_ALL_ONES = re.compile(r"\b(every|all|each)\b( single)? ?(one|ones|of them)\b|\ball of them\b|\beverything\b")
_PURE_ALL = re.compile(r"\b(all|every|each)\b( single)?( of)?( my| the| your)?( like)?( automati[sz]ations?|automations?|workflows?|ones?|them|schedules?)\b")


def quick_command(text: str, path=None, last_reply: str = "") -> str:
    """Handle 'delete every news automation' directly, so it never depends on the LLM writing the right tag.

    Returns the spoken reply, or None when the sentence is not clearly a bulk removal.
    `last_reply` is what Kate said last: 'delete every single one' only counts when that was about automations.
    """
    low = " ".join(re.sub(r"[^a-z0-9' ]", " ", (text or "").lower()).split())
    if not low or not _ALL_WORD.search(low):
        return None
    about_them = bool(_NOUN.search(low))
    previous = (last_reply or "").lower()
    in_context = "automation" in previous or "every day at" in previous or "on weekdays at" in previous
    if about_them:
        if not _VERB_WITH_NOUN.search(low):
            return None
    elif not (in_context and _VERB_IN_CONTEXT.search(low) and _ALL_ONES.search(low)):
        return None
    items = _load(path)
    if not items:
        return "You have no automations to remove."
    vocabulary = set().union(*(_available(a) for a in items))
    wanted = _tokens(low)
    known = wanted & vocabulary
    if wanted and not known and not _PURE_ALL.search(low):
        return None  # it names something I do not recognize: let the LLM sort it out
    return remove_many(" ".join(sorted(known)), path=path, remove_all=True)[1]


if __name__ == "__main__":
    import tempfile

    demo = Path(tempfile.gettempdir()) / "kate_automatizations_demo.json"
    if demo.exists():
        demo.unlink()

    print("create:", resolve_automation_tags(
        '<new_automatization; time="10:00, every day"; '
        'actions=\'get_weather="Prague" -> get_time\'/>', path=demo
    )[0])
    print("trigger phrase:", trigger_phrase(_load(demo)[0]["trigger"]))
    print("list:", list_message(demo))
    print("resolve list:", resolve_automation_tags("<list_automatizations/>", path=demo)[0])

    today = time.localtime()
    items = _load(demo)
    yesterday = time.mktime(today[:3] + (0, 0, 1, 0, 0, -1)) - 86400
    for automation in items:  # pretend it was set up yesterday
        automation["created"] = yesterday
    _save(items, demo)

    ten_today = time.mktime(today[:3] + (10, 0, 5, 0, 0, -1))
    ten_tomorrow = ten_today + 86400
    print("fired at 10:00:", [(a["name"], r) for a, r in run_due(path=demo, now=ten_today)])
    print("quiet on the next poll:", run_due(path=demo, now=ten_today))
    print("fired at 10:00 next day:", [(a["name"], r) for a, r in run_due(path=demo, now=ten_tomorrow)])
    print("remove:", resolve_automation_tags(
        '<remove_automatization; name="Prague weather"/>', path=demo
    )[0])
    print("list:", list_message(demo))