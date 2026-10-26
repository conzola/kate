import json
import re
import time
from pathlib import Path

import requests

try:
    from .tools import resolve_tool_tags, tool_prompt
    from .automatization import automation_prompt, resolve_automation_tags
except ImportError:  # run directly: python LLM/main.py
    from tools import resolve_tool_tags, tool_prompt
    from automatization import automation_prompt, resolve_automation_tags

LM_URL = "http://127.0.0.1:1234/v1/chat/completions"
MODEL = ""  # LM Studio uses whichever model is loaded; or put the exact model name here
DEFAULT_SYSTEM = """You are a voice assistant. Your replies are spoken aloud.

STYLE
- Short, friendly, plain answers. Talk like a normal person.
- Answer directly. Never open with filler like "Sure" or "Here is what I can do".
- No emoji, no asterisks, no brackets, no quotation marks.

THE /continue SIGNAL
/continue is a hidden control signal. It keeps the microphone open so the user can answer.

Use it only when your reply ENDS WITH A DIRECT QUESTION to the user, for example "What day?" or "Want the forecast too?". Then add a line break and write /continue on its own last line.

Never use it for thanks, goodbyes, greetings, statements, or phrases like "let me know if you need anything". Those need no answer. For them, end the reply normally and write nothing extra.
Never write continue without the slash. Never explain this signal.

Examples:
User: Thank you.
You: You're welcome.

User: Okay, bye.
You: Bye.

User: The capital of France.
You: Paris.

User: Remind me about the dentist.
You: What day should I remind you?
/continue"""

CONTINUE_MARKER = "/continue"
# The exact marker, anywhere in the text.
_MARKER_ANYWHERE_RE = re.compile(r"[ \t]*/continue\b[ \t]*", re.IGNORECASE)
# Sloppy variants the model produces at the very end: [continue], <continue/>,
# (continue), or the bare word alone on its own last line.
_SLOPPY_END_RE = re.compile(
    r"(?:"
    r"\s*[\[<(]\s*/?\s*continue\s*/?\s*[\]>)]\.?"   # [continue] <continue/> (continue)
    r"|(?<=[?!.])\s*/?\s*continue\s*\.?"            # "else? continue" or "else? /continue"
    r"|\n\s*/?\s*continue\s*\.?"                    # bare word on its own last line
    r")\s*$",
    re.IGNORECASE,
)
# Kept for any other code that imports it: matches the exact marker at the end.
_CONTINUE_RE = re.compile(r"\s*" + re.escape(CONTINUE_MARKER) + r"\s*$", re.IGNORECASE)

MEMORY_FILE = Path(__file__).resolve().parent.parent / "memory.json"
MAX_TURNS = 12  # remembered user+assistant messages, i.e. 6 exchanges
MEMORY_TIMEOUT = 600.0  # seconds of silence after which the old conversation is dropped

# Words that mean the user wants live data. If the model answers without writing a
# tag, the answer is made up, so ask() makes it try again.
LIVE_DATA_RE = re.compile(
    r"\b(news|headlines?|weather|temperature|forecast|raining|snowing|"
    r"what('?s| is) the time|what time|current time|what day|today'?s date|what('?s| is) the date)\b",
    re.IGNORECASE,
)
RETRY_NUDGE = (
    "(Reminder: you cannot know live news, weather or the time yourself. Do not answer from "
    "memory. If the user wants it now, write the tool tag. If they want it scheduled, write "
    "the automation tag. Do not apologize or mention this reminder.)"
)

_history: list = []
_last_active = None


def _load() -> tuple:
    """Return (turns, updated_timestamp) restored from disk, if any."""
    try:
        data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], None
    turns = [
        m for m in data.get("turns", [])
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)
    ]
    for m in turns:  # "shown" is optional and only an extra for assistant turns
        if not isinstance(m.get("shown"), str):
            m.pop("shown", None)
    updated = data.get("updated")
    return turns[-MAX_TURNS:], updated if isinstance(updated, (int, float)) else None


def _save() -> None:
    temp = MEMORY_FILE.with_name(MEMORY_FILE.name + ".tmp")
    try:
        # write to a temp file first so a crash cannot leave half a JSON file behind
        temp.write_text(
            json.dumps({"updated": _last_active, "turns": _history}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(MEMORY_FILE)
    except OSError:
        pass


def reset_memory() -> None:
    """Forget the whole conversation, now and on disk."""
    global _last_active
    _history.clear()
    _last_active = None
    try:
        MEMORY_FILE.unlink()
    except OSError:
        pass


_history, _last_active = _load()


def strip_think(text: str) -> str:
    # remove complete blocks
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # if only a closing tag is left (opening tag was added by the chat template), drop everything before it
    if "</think>" in text:
        text = text.split("</think>")[-1]
    # a reply cut off in the middle of its thinking has an opening tag and nothing after it
    if "<think>" in text:
        text = text.split("<think>")[0]
    return text.strip()


def split_continue(text: str) -> tuple:
    """Split the /continue control signal off a reply.

    Returns (clean_reply, wants_continue). wants_continue is True when the
    model wrote the marker in any tolerated form, or when the reply ends in a
    question mark (the user is obviously expected to answer).
    """
    wants = False
    if _MARKER_ANYWHERE_RE.search(text):
        wants = True
        text = _MARKER_ANYWHERE_RE.sub(" ", text)
    if _SLOPPY_END_RE.search(text):
        wants = True
        text = _SLOPPY_END_RE.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    if text.endswith("?"):
        wants = True
    return text, wants


def _post(messages: list, temperature: float) -> dict:
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
        "reasoning_effort": "none"
    }
    r = requests.post(LM_URL, json=payload, timeout=120)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]


def _build_messages(system: str, nudge: str = "") -> list:
    """System prompt plus history. Only role and content go to the model.

    The history holds the model's raw replies, tags included, so it keeps seeing
    itself use tools. What the user was actually shown is added to the latest
    user message, so follow-up questions about the results still work.
    """
    messages = [{"role": "system", "content": f"{system}\n\n{tool_prompt()}\n\n{automation_prompt()}"}]
    last = len(_history) - 1
    for index, turn in enumerate(_history):
        content = turn["content"]
        if index == last and turn["role"] == "user":
            previous = _history[index - 1] if index else {}
            shown = previous.get("shown")
            if shown and shown != previous.get("content"):
                content += f"\n\n(Your previous reply reached the user as: {shown})"
            if nudge:
                content += f"\n\n{nudge}"
        messages.append({"role": turn["role"], "content": content})
    return messages


def _respond(messages: list, temperature: float) -> tuple:
    """One model call. Returns (raw_text, resolved_text, number_of_tags_resolved)."""
    raw = strip_think(_post(messages, temperature).get("content") or "")
    # Automations first, tools second. Tool results (news headlines, for example) come
    # from the internet and are inserted last, so a headline can never smuggle in a
    # tag that gets executed, and a tool tag inside an automation is not run early.
    text, automation_changes = resolve_automation_tags(raw)
    text, tool_calls = resolve_tool_tags(text)
    return raw, text, automation_changes + tool_calls


def ask(input: str, system: str = DEFAULT_SYSTEM, temperature: float = 0.7) -> str:
    """Ask the LLM with the recent conversation as context, then resolve tool tags."""
    global _last_active
    now = time.time()
    if _last_active is not None and now - _last_active > MEMORY_TIMEOUT:
        reset_memory()
    _history.append({"role": "user", "content": input})
    try:
        raw, text, handled = _respond(_build_messages(system), temperature)
        if not handled and LIVE_DATA_RE.search(input):
            # asked for live data but no tag: the reply is invented, so try once more
            raw, text, handled = _respond(_build_messages(system, RETRY_NUDGE), temperature)
    except Exception:
        if _history and _history[-1].get("role") == "user":
            _history.pop()
        raise

    raw_clean, wants_continue = split_continue(raw)
    text_clean, text_wants = split_continue(text)
    wants_continue = wants_continue or text_wants
    # History keeps the marker so the model sees itself using it consistently.
    entry = {"role": "assistant", "content": raw_clean + ("\n/continue" if wants_continue else "")}
    if handled:
        entry["shown"] = text_clean
    _history.append(entry)
    del _history[:-MAX_TURNS]
    _last_active = time.time()
    _save()
    # One clean marker on the last line, which is what split_continue expects downstream.
    return text_clean + ("\n/continue" if wants_continue else "")


if __name__ == "__main__":
    # quick test: python main.py "your prompt"
    import sys
    print(ask(" ".join(sys.argv[1:]) or "Say hello"))