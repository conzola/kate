import ast
import base64
import calendar
import html as _html
import json
import math
import operator
import os
import random
import re
import secrets
import shutil
import socket
import statistics
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from xml.etree import ElementTree

import requests

try:
    import psutil  # optional, only used by pc_status
except ImportError:
    psutil = None


# =============================================================================
# CONFIGURATION
# Everything secret or machine-specific lives in the .env file three folders up
# from this file (.../.env). Real environment variables win over the file,
# so `WEATHER_API_KEY=xyz python server.py` still overrides it.
# No dependency needed: python-dotenv is used if it happens to be installed.
# =============================================================================
ENV_FILE = Path(os.environ.get(
    "ENV_FILE",
    Path(__file__).resolve().parent.parent / ".env",
))


def _load_env(path: Path = ENV_FILE) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return  # no .env is fine: defaults and the real environment are used

    # If python-dotenv is installed, let it handle the fancy cases.
    try:
        from dotenv import load_dotenv
    except ImportError:
        pass
    else:
        load_dotenv(path, override=False)
        return

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]                      # quoted: keep it verbatim
        else:
            value = re.sub(r"\s+#.*$", "", value)    # unquoted: strip "  # comment"
        os.environ.setdefault(key, value)


_load_env()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# ---- settings from .../.env ------------------------------------------------
WEATHER_API_KEY = _env("WEATHER_API_KEY")
WEATHER_BASE_URL = _env("WEATHER_BASE_URL", "https://api.weatherapi.com/v1").rstrip("/")
NEWS_BASE_URL = _env("NEWS_BASE_URL", "https://news.google.com/rss").rstrip("/")


def get_weather(location: str = "") -> str:
    """Get the current weather for a location as a short, speech-friendly summary."""
    location = (location or "").strip() or _default_location()
    if not location:
        return "Tell me which place you mean, or tell me your home city so I can remember it"
    if not WEATHER_API_KEY:
        return "I have no weather key set up, so I cannot check the weather"
    params = {
        "key": WEATHER_API_KEY,
        "q": location,
        "aqi": "no",
    }
    try:
        response = requests.get(f"{WEATHER_BASE_URL}/current.json", params=params, timeout=10)
    except requests.RequestException:
        # Do not put the exception text in the answer: it contains the request URL, key included.
        return "Sorry, I could not reach the weather service"
    if response.status_code != 200:
        try:
            message = response.json().get("error", {}).get("message", response.text)
        except ValueError:
            message = response.text
        return f"The weather service could not answer for {location}: {message}"
    try:
        data = response.json()
    except ValueError:
        return "Sorry, the weather service sent something I could not read"
    current = data.get("current", {})
    condition = current.get("condition", {}).get("text", "unknown conditions")
    # No place name and no trailing period: the result is inserted mid-sentence.
    return (
        f"{condition}, {round(current.get('temp_c', 0))} degrees Celsius, "
        f"feels like {round(current.get('feelslike_c', 0))} degrees Celsius, "
        f"humidity {current.get('humidity', '?')}%, "
        f"wind {round(current.get('wind_kph', 0))} kilometers per hour"
    )


def get_time() -> str:
    """Get the current local date and time."""
    now = time.localtime()
    # strftime's %-d and %-I are not portable (Windows), so strip the zeros by hand.
    return (
        f"{time.strftime('%A, %B', now)} {now.tm_mday}, {now.tm_year}, "
        f"{now.tm_hour % 12 or 12}:{now.tm_min:02d} {'AM' if now.tm_hour < 12 else 'PM'}"
    )


# ---- news: headlines, and more about one story ------------------------------
_NEWS_SECTIONS = {"world": "WORLD", "business": "BUSINESS", "technology": "TECHNOLOGY", "tech": "TECHNOLOGY",
                  "entertainment": "ENTERTAINMENT", "sports": "SPORTS", "sport": "SPORTS",
                  "science": "SCIENCE", "health": "HEALTH"}
_LOCAL_WORDS = {"local", "here", "home", "national", "nearby", "my area", "my city", "my country", "around me"}
_ORDINALS = {"first": 0, "1st": 0, "one": 0, "top": 0, "second": 1, "2nd": 1, "two": 1, "third": 2, "3rd": 2,
             "three": 2, "fourth": 3, "4th": 3, "four": 3, "fifth": 4, "5th": 4, "five": 4, "sixth": 5,
             "6th": 5, "six": 5, "seventh": 6, "7th": 6, "seven": 6, "eighth": 7, "8th": 7, "eight": 7,
             "ninth": 8, "9th": 8, "nine": 8, "tenth": 9, "10th": 9, "ten": 9}
_BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}
_NEWS_LOCK = threading.Lock()
_LAST_NEWS: list = []        # the stories of the last headline list, so "tell me more about the second" works
_NEWS_POS = 0                # the story talked about last ("the next one" moves from here)


def _clean_html(text: str) -> str:
    text = re.sub(r"<(script|style|noscript)\b.*?</\1>", " ", text or "", flags=re.S | re.I)
    return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", text)).split())


def _age_text(stamp: str) -> str:
    try:
        seconds = (datetime.now(timezone.utc) - parsedate_to_datetime(stamp)).total_seconds()
    except (TypeError, ValueError):
        return ""
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{round(seconds / 60)} minutes ago"
    if seconds < 86400:
        hours = round(seconds / 3600)
        return f"{hours} {'hour' if hours == 1 else 'hours'} ago"
    days = int(seconds // 86400)
    return "yesterday" if days == 1 else f"{days} days ago"


def _news_request(topic: str):
    """(url, params, search_query) for a topic: nothing, a section like technology, 'local', or free words."""
    params = {"hl": "en-US", "gl": "US", "ceid": "US:en"}
    low = (topic or "").strip().lower()
    if low in _LOCAL_WORDS:
        low = topic = (_home().get("name") or _env("DEFAULT_LOCATION").split(",")[0]).strip()
        if not low:
            return None, params, ""
        low = ""   # a search for the home city, not a section
    elif _NEWS_SECTIONS.get(low):
        return f"{NEWS_BASE_URL}/headlines/section/topic/{_NEWS_SECTIONS[low]}", params, ""
    if (topic or "").strip():
        return f"{NEWS_BASE_URL}/search", params, topic.strip()
    return NEWS_BASE_URL, params, ""


def _news_stories(root, limit: int) -> list:
    stories, seen = [], []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        # Google News titles end with " - Publisher"; keep the publisher apart, she cannot say it well in a list.
        headline, tail = title.rsplit(" - ", 1) if " - " in title else (title, "")
        source = (item.findtext("source") or tail or "").strip()
        words = {w for w in re.findall(r"[a-z0-9]+", headline.lower()) if len(w) > 3}
        if any(len(words & other) >= 3 and len(words & other) / min(len(words), len(other)) >= 0.7 for other in seen):
            continue        # the same story from another outlet
        seen.append(words)
        summary = _clean_html(item.findtext("description") or "")
        for noise in (headline, source, title):
            summary = summary.replace(noise, " ")
        stories.append({"headline": headline, "source": source, "link": (item.findtext("link") or "").strip(),
                        "age": _age_text(item.findtext("pubDate") or ""), "summary": " ".join(summary.split())})
        if len(stories) >= limit:
            break
    return stories


def _fetch_news(topic: str = "", limit: int = 5) -> tuple:
    """(stories, error_message). Searches ask for the last two days first, then for anything."""
    url, params, query = _news_request(topic)
    if url is None:
        return [], "Tell me your home city first, then I know what local means"
    variants = [dict(params, q=f"{query} when:2d"), dict(params, q=query)] if query else [params]
    stories = []
    for variant in variants:
        try:
            response = requests.get(url, params=variant, timeout=10)
        except requests.RequestException:
            return [], "Sorry, I could not reach the news service"
        if response.status_code != 200:
            return [], f"The news service could not answer, error {response.status_code}"
        try:
            stories = _news_stories(ElementTree.fromstring(response.content), limit)
        except ElementTree.ParseError:
            return [], "Sorry, the news service sent something I could not read"
        if len(stories) >= min(3, limit):
            break
    return stories, ""


def _remember_news(stories: list) -> None:
    global _NEWS_POS
    with _NEWS_LOCK:
        _LAST_NEWS[:] = stories
        _NEWS_POS = 0


def get_news(topic: str = "", count: str = "5") -> str:
    """Latest headlines (numbered, so a story can be picked afterwards with news_details)."""
    try:
        n = max(1, min(10, int(re.search(r"\d+", str(count or "5")).group())))
    except (AttributeError, ValueError):
        n = 5
    topic = (topic or "").strip()
    stories, error = _fetch_news(topic, n)
    if error:
        return error
    if not stories:
        return f"I could not find any news about {topic}" if topic else "I could not find any news"
    _remember_news(stories)
    about = f" about {topic}" if topic else ""
    listed = ", ".join(f"{i + 1}) {s['headline']}" for i, s in enumerate(stories))
    return f"The latest news{about}: {listed}"


def _google_target(link: str) -> str:
    """Older Google News links carry the real article address inside; newer ones do not (returns '')."""
    match = re.search(r"/articles/([A-Za-z0-9_-]+)", link or "")
    if not match:
        return ""
    token = match.group(1)
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("latin-1")
    except (ValueError, UnicodeError):
        return ""
    found = re.search(r"https?://[^\x00-\x20\"<>\\]+", raw)
    return found.group(0) if found else ""


_BOILERPLATE = re.compile(r"cookie|subscribe|newsletter|sign up|advertisement|all rights reserved|javascript|"
                          r"privacy policy|follow us|click here|read more|terms of use|log in", re.I)


def _article_text(link: str) -> str:
    """The readable text of an article (page description, then paragraphs), or '' when it cannot be opened."""
    url = _google_target(link) or link
    if not url.lower().startswith("http"):
        return ""
    try:
        response = requests.get(url, headers=_BROWSER, timeout=10, stream=True)
    except requests.RequestException:
        return ""
    try:
        if response.status_code != 200 or "html" not in response.headers.get("Content-Type", "").lower():
            return ""
        if "google." in urlparse(response.url).netloc:   # stuck on Google's redirect or consent page
            return ""
        raw = b""
        for chunk in response.iter_content(65536):
            raw += chunk
            if len(raw) > 900_000:
                break
        encoding = response.encoding or "utf-8"
    finally:
        response.close()
    page = raw.decode(encoding, errors="ignore")
    meta = ""
    for tag in re.findall(r"<meta\b[^>]*>", page, flags=re.I):
        if re.search(r"(?:property|name)\s*=\s*[\"'](?:og:description|description|twitter:description)[\"']", tag, re.I):
            found = re.search(r"content\s*=\s*([\"'])(.*?)\1", tag, re.I | re.S)
            if found and len(found.group(2)) > 40:
                meta = _clean_html(found.group(2))
                break
    paragraphs = []
    for paragraph in re.findall(r"<p\b[^>]*>(.*?)</p>", page, flags=re.S | re.I):
        text = _clean_html(paragraph)
        if len(text) >= 70 and not _BOILERPLATE.search(text) and text not in paragraphs:
            paragraphs.append(text)
    if meta and paragraphs and paragraphs[0][:40] == meta[:40]:
        paragraphs.pop(0)
    return " ".join(([meta] if meta else []) + paragraphs)[:6000]


def _first_sentences(text: str, count: int, limit: int) -> str:
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"“‘'])", text.strip())
    out = ""
    for sentence in sentences[:count]:
        if out and len(out) + len(sentence) > limit:
            break
        out += ("" if not out else " ") + sentence
    return out.strip()[:limit].rstrip()


def _pick_story(selector: str):
    """The remembered story a phrase points at: 'second', '3', 'the next one', 'the one about ...'; None if unknown."""
    global _NEWS_POS
    low = " ".join((selector or "").lower().replace("#", " ").split())
    with _NEWS_LOCK:
        stories, pos = list(_LAST_NEWS), _NEWS_POS
    if not stories:
        return None
    index = None
    words = re.findall(r"[a-z0-9]+", low)
    filler = {"the", "a", "an", "story", "article", "about", "news", "that", "this", "with", "tell", "me", "more",
              "on", "of", "number", "item", "one", "headline", "read", "please", "and", "to", "it", "is", "what"}
    keywords = {w for w in words if len(w) > 2 and w not in filler and w not in _ORDINALS and not w.isdigit()
                and w not in ("next", "previous", "back", "last", "first", "second", "third")}
    if keywords:        # "the moon one": words from the headline decide, not the word "one"
        best, score = None, 0
        for i, s in enumerate(stories):
            have = set(re.findall(r"[a-z0-9]+", f"{s['headline']} {s['source']}".lower()))
            hit = len(keywords & have)
            if hit > score:
                best, score = i, hit
        index = best
    elif not words or low in ("it", "that", "this", "that one", "this one", "the story", "story", "the same"):
        index = pos
    elif "next" in words:
        index = min(pos + 1, len(stories) - 1)
    elif "previous" in words or "back" in words:
        index = max(pos - 1, 0)
    elif "last" in words:
        index = len(stories) - 1
    else:
        for w in words:
            if w in _ORDINALS:
                index = _ORDINALS[w]
                break
            if w.isdigit() and 1 <= int(w) <= len(stories):
                index = int(w) - 1
                break
    if index is None or not 0 <= index < len(stories):
        return None
    with _NEWS_LOCK:
        _NEWS_POS = index
    return stories[index]


def news_details(story: str = "", depth: str = "short") -> str:
    """More about one story of the last headlines: who reports it, how old it is, what the article says."""
    chosen = _pick_story(story)
    if chosen is None:
        words = (story or "").strip()
        if not words or words.lower() in ("it", "that", "this"):
            return "Ask me for the news first, then tell me which story you want to hear more about"
        stories, error = _fetch_news(words, 1)       # not in the list: look for it
        if error:
            return error
        if not stories:
            return f"I could not find a story about {words}"
        _remember_news(stories)
        chosen = stories[0]
    detailed = str(depth or "").lower().startswith(("l", "d", "f", "m", "a"))   # long / detailed / full / more / all
    count, limit = (7, 1400) if detailed else (3, 600)
    text = _first_sentences(_article_text(chosen["link"]), count, limit)
    head = chosen["headline"].rstrip(".")
    source = f"This is from {chosen['source']}" if chosen["source"] else "This is"
    when = f", {chosen['age']}" if chosen["age"] else ""
    if text:
        return f"{head}. {source}{when}. {text}"
    if chosen["summary"]:
        return f"{head}. {source}{when}. {_first_sentences(chosen['summary'], count, limit)}"
    related, _ = _fetch_news(chosen["headline"], 8)      # the article is closed to me: what do the others report?
    others, seen = [], {chosen["source"]}
    for s in related or []:
        shared = {w for w in re.findall(r"[a-z0-9]+", chosen["headline"].lower()) if len(w) > 3} & \
                 set(re.findall(r"[a-z0-9]+", s["headline"].lower()))
        if s["source"] and s["source"] not in seen and len(shared) >= 2 and s["headline"].lower() != chosen["headline"].lower():
            seen.add(s["source"])
            others.append(f"{s['source']} reports {s['headline']}")
    if others:
        return (f"{head}. {source}{when}. I could not open the article itself, but "
                + "; ".join(others[: 4 if detailed else 2]))
    return f"{head}. {source}{when}. I could not open the article, that is all I know"


# =============================================================================
# TIME TOOLS: timers, reminders / alarms, stopwatch
# Timers are stored in timers.json (next to memory.json) so they survive a restart.
# timer_loop() runs in a thread (server.py starts it) and hands each finished
# timer's sentence to a callback, like automatization.scheduler_loop().
# =============================================================================
TIMERS_FILE = Path(os.environ.get(
    "TIMERS_FILE",
    Path(__file__).resolve().parent.parent / "timers.json",
))

POLL_SECONDS = 1.0          # how often timer_loop() looks for finished timers
MAX_SECONDS = 24 * 3600     # longest timer
STALE_SECONDS = 600         # a timer that ran out this long ago (server was off) is dropped silently

_TIMER_LOCK = threading.RLock()


# ---- numbers and durations -------------------------------------------------
_ONES = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}
_ONES_RE = "|".join(sorted(_ONES, key=len, reverse=True))
_TENS_RE = "|".join(sorted(_TENS, key=len, reverse=True))
# 10, 2.5, twenty five, ninety, a, an
_NUM = rf"(?:\d+(?:\.\d+)?|(?:(?:{_TENS_RE})[ -]?)?(?:{_ONES_RE})|(?:{_TENS_RE})|an?)"
_UNIT = r"(?:seconds?|secs?|minutes?|mins?|hours?|hrs?)"


def _to_number(token: str) -> float:
    token = token.strip().lower()
    try:
        return float(token)
    except ValueError:
        pass
    if token in ("a", "an"):
        return 1.0
    return float(sum(_ONES.get(w) or _TENS.get(w) or 0 for w in re.split(r"[ -]", token)))


def _unit_seconds(unit: str) -> int:
    return {"s": 1, "m": 60, "h": 3600}[unit.lower()[0]]


def parse_duration(text: str, bare_minutes: bool = True):
    """'1 hour 30 minutes', 'half an hour', 'an hour and a half', '90 seconds' -> seconds, or None."""
    low = " ".join(re.sub(r"[,]", " ", (text or "").lower()).split())
    low = re.sub(r"^(?:in|for|after|of)\s+", "", low)
    total, found = 0.0, False

    def with_half(match):
        nonlocal total, found
        total += (_to_number(match.group(1)) + 0.5) * _unit_seconds(match.group(2))
        found = True
        return " "

    low = re.sub(rf"\b({_NUM})\s*(hour|minute)s?\s+and\s+a\s+half\b", with_half, low)
    for pattern, seconds in ((r"\bhalf\s+an?\s+hour\b|\ban?\s+half\s+hour\b|\bhalf\s+hour\b", 1800),
                             (r"\b(?:a\s+)?quarter(?:\s+of\s+an?)?\s+hour\b", 900)):
        low, count = re.subn(pattern, " ", low)
        if count:
            total += seconds * count
            found = True
    for match in re.finditer(rf"\b({_NUM})\s*({_UNIT})\b", low):
        total += _to_number(match.group(1)) * _unit_seconds(match.group(2))
        found = True
    if not found and bare_minutes and re.fullmatch(r"\d+(?:\.\d+)?", low):
        total, found = float(low) * 60, True   # a bare "10" means ten minutes
    return int(round(total)) if found else None


def say_duration(seconds: float, exact: bool = False) -> str:
    """Speech-friendly: '1 hour and 30 minutes', '45 seconds'. Long spans drop the seconds."""
    seconds = int(round(seconds))
    if seconds <= 0:
        return "a moment"
    if seconds >= 600 and not exact:
        seconds = (seconds + 30) // 60 * 60
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    for value, word in ((hours, "hour"), (minutes, "minute"), (secs, "second")):
        if value:
            parts.append(f"{value} {word}{'' if value == 1 else 's'}")
    if len(parts) <= 2:
        return " and ".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


# ---- clock times (for alarms and reminders) --------------------------------
def parse_clock(text: str):
    """'6 PM', '7:30', 'at 18:00', 'noon' -> (hour, minute, ambiguous), or None.

    ambiguous is True for '7:30' (no AM/PM): the next 7:30, morning or evening, is used.
    """
    low = re.sub(r"\.(?=[a-z])", "", (text or "").lower())
    meridiem = None
    match = re.search(r"\b(\d{1,2})[:.](\d{2})\s*(am|pm)?\b", low)
    if match:
        hour, minute, meridiem = int(match.group(1)), int(match.group(2)), match.group(3)
    else:
        match = re.search(r"\b(\d{1,2})\s*(am|pm)\b", low)
        if match:
            hour, minute, meridiem = int(match.group(1)), 0, match.group(2)
        else:
            match = re.search(r"\b(\d{1,2})\s*o'?clock\b", low) or re.search(r"\bat\s+(\d{1,2})\b", low)
            if match:
                hour, minute = int(match.group(1)), 0
            elif re.search(r"\bnoon\b", low):
                hour, minute, meridiem = 12, 0, "pm"
            elif re.search(r"\bmidnight\b", low):
                hour, minute, meridiem = 12, 0, "am"
            else:
                return None
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    ambiguous = meridiem is None and 1 <= hour <= 11
    return hour, minute, ambiguous


def next_occurrence(hour: int, minute: int, ambiguous: bool = False,
                    now: float = None, min_days: int = 0) -> float:
    """Timestamp of the next time the clock shows hour:minute (tomorrow if min_days=1)."""
    now = time.time() if now is None else now
    today = time.localtime(now)
    hours = [hour, hour + 12] if ambiguous else [hour]
    best = None
    for day in range(min_days, min_days + 3):
        for h in hours:
            stamp = time.mktime((today.tm_year, today.tm_mon, today.tm_mday + day,
                                 h, minute, 0, -1, -1, -1))
            if stamp > now and (best is None or stamp < best):
                best = stamp
        if best is not None:
            break
    return best


def _clock_phrase(stamp: float, now: float = None) -> str:
    now = time.time() if now is None else now
    t = time.localtime(stamp)
    clock = f"{t.tm_hour % 12 or 12}" if t.tm_min == 0 else f"{t.tm_hour % 12 or 12}:{t.tm_min:02d}"
    clock += " AM" if t.tm_hour < 12 else " PM"
    same_day = time.localtime(now)[:3] == t[:3]
    return clock if same_day else f"{clock} tomorrow"


# ---- storage ---------------------------------------------------------------
def _timers_load(path=None) -> dict:
    target = Path(path) if path else TIMERS_FILE
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    timers = [t for t in data.get("timers", [])
              if isinstance(t, dict) and isinstance(t.get("due"), (int, float))]
    stopwatch = data.get("stopwatch")
    return {"timers": timers, "stopwatch": stopwatch if isinstance(stopwatch, (int, float)) else None}


def _timers_save(state: dict, path=None) -> None:
    target = Path(path) if path else TIMERS_FILE
    temp = target.with_name(target.name + ".tmp")
    try:
        # temp file first, so a crash cannot leave half a JSON file behind
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(target)
    except OSError as exc:
        print(f"Could not save timers: {exc}")


def _clean(text) -> str:
    return " ".join(str(text or "").replace('"', "").replace("'", "").split())


def _add(kind: str, seconds: int, label: str = "", message: str = "", due: float = None) -> None:
    now = time.time()
    timer = {
        "id": uuid.uuid4().hex[:8], "kind": kind, "label": label, "message": message,
        "created": now, "seconds": int(seconds), "due": due if due is not None else now + seconds,
    }
    with _TIMER_LOCK:
        state = _timers_load()
        for old in state["timers"]:  # the LLM sometimes writes the same tag twice
            if (now - old.get("created", 0) < 5 and old.get("kind") == kind
                    and old.get("label") == label and old.get("message") == message
                    and old.get("seconds") == timer["seconds"]):
                return
        state["timers"].append(timer)
        _timers_save(state)


# ---- descriptions ----------------------------------------------------------
def _adjective(seconds: float) -> str:
    """'10 minutes' -> '10 minute', for 'the 10 minute timer'."""
    return re.sub(r"(\d+) (hour|minute|second)s\b", r"\1 \2", say_duration(seconds))


def _name(timer: dict) -> str:
    if timer.get("kind") == "timer":
        return f"the {timer['label']} timer" if timer.get("label") else f"the {_adjective(timer.get('seconds', 0))} timer"
    message = timer.get("message", "")
    return f"the reminder to {message}" if message else "the alarm"


def _describe(timer: dict, now: float) -> str:
    left = say_duration(timer["due"] - now)
    if timer.get("kind") == "timer":
        return f"{_name(timer)} with {left} left"
    return f"{_name(timer)} in {left}"


def fire_message(timer: dict) -> str:
    if timer.get("kind") == "timer":
        label = timer.get("label")
        return f"Your {label} timer is done" if label else f"Your {_adjective(timer.get('seconds', 0))} timer is done"
    message = timer.get("message")
    return f"Reminder: {message}" if message else "This is your alarm"


# ---- the tools -------------------------------------------------------------
def set_timer(duration: str = "", label: str = "") -> str:
    """Start a countdown timer. Kate speaks up when it runs out."""
    seconds = parse_duration(duration)
    if seconds is None or seconds < 1:
        return "I did not catch how long the timer should run. Try something like 10 minutes"
    if seconds > MAX_SECONDS:
        return "I can only do timers up to 24 hours. For longer, use a reminder at a clock time"
    label = _clean(label)
    _add("timer", seconds, label=label)
    if label:
        return f"Done, the {label} timer is set for {say_duration(seconds)}"
    return f"Done, timer set for {say_duration(seconds)}"


def set_reminder(when: str = "", message: str = "") -> str:
    """Remind the user later, either after a delay ('in 20 minutes') or at a clock time ('at 6 PM')."""
    message = re.sub(r"^to\s+", "", _clean(message), flags=re.IGNORECASE)
    now = time.time()
    seconds = parse_duration(when, bare_minutes=False)
    if seconds is not None:
        if not 1 <= seconds <= MAX_SECONDS:
            return "I can remind you from one second up to 24 hours from now"
        _add("reminder", seconds, message=message)
        if message:
            return f"Done, I will remind you to {message} in {say_duration(seconds)}"
        return f"Done, alarm set for {say_duration(seconds)} from now"
    clock = parse_clock(when)
    if clock is None:
        return "I did not catch when. Try in 20 minutes, or at 6 PM"
    hour, minute, ambiguous = clock
    due = next_occurrence(hour, minute, ambiguous, now, 1 if "tomorrow" in (when or "").lower() else 0)
    _add("reminder", int(due - now), message=message, due=due)
    when_text = _clock_phrase(due, now)
    if message:
        return f"Done, I will remind you to {message} at {when_text}"
    return f"Done, alarm set for {when_text}"


def list_timers() -> str:
    """Say which timers and reminders are running."""
    now = time.time()
    with _TIMER_LOCK:
        timers = sorted(_timers_load()["timers"], key=lambda t: t["due"])
    if not timers:
        return "You have no timers or reminders running"
    parts = [_describe(t, now) for t in timers]
    if len(parts) == 1:
        return f"You have one running: {parts[0]}"
    joined = " and ".join(parts) if len(parts) == 2 else ", ".join(parts[:-1]) + ", and " + parts[-1]
    return f"You have {len(parts)} running: {joined}"


_CANCEL_STOP = {"the", "my", "a", "an", "timer", "timers", "reminder", "reminders", "alarm",
                "alarms", "please", "cancel", "stop", "delete", "remove", "one", "for", "that",
                "this", "it", "to", "of"}


def _stems(text: str) -> set:
    return {w.rstrip("s") for w in re.findall(r"[a-z0-9]+", (text or "").lower())}


def cancel_timer(name: str = "") -> str:
    """Cancel a timer or reminder by name, 'all', or the only one."""
    key = (name or "").strip().lower()
    now = time.time()
    with _TIMER_LOCK:
        state = _timers_load()
        timers = state["timers"]
        if not timers:
            return "You have no timers or reminders running"
        if re.search(r"\b(all|every|everything)\b", key):
            state["timers"] = []
            _timers_save(state)
            return "Done, I cancelled all of them"
        candidates = timers
        if re.search(r"\b(reminders?|alarms?)\b", key):
            candidates = [t for t in timers if t.get("kind") == "reminder"]
        elif re.search(r"\btimers?\b", key):
            candidates = [t for t in timers if t.get("kind") == "timer"]
        if not candidates:
            return "I could not find that one"
        words = {w.rstrip("s") for w in re.findall(r"[a-z0-9]+", key) if w not in _CANCEL_STOP}
        if words:
            scored = []
            for t in candidates:
                have = _stems(t.get("label", "") + " " + t.get("message", "") + " " + say_duration(t.get("seconds", 0)))
                if words & have:
                    scored.append((len(words & have), t))
            if not scored:
                return f"I could not find one called {key}. {list_timers()}"
            best = max(score for score, _ in scored)
            candidates = [t for score, t in scored if score == best]
        if len(candidates) > 1:
            names = ", ".join(_describe(t, now) for t in sorted(candidates, key=lambda t: t["due"]))
            return f"I have a few that could be it: {names}. Which one do you mean?"
        target = candidates[0]
        state["timers"] = [t for t in timers if t is not target]
        _timers_save(state)
        return f"Done, I cancelled {_name(target)}"


def stopwatch(action: str = "check") -> str:
    """Start, check, stop or reset a stopwatch."""
    action = (action or "check").strip().lower()
    now = time.time()
    with _TIMER_LOCK:
        state = _timers_load()
        started = state["stopwatch"]
        if re.search(r"\b(start|begin|go)\b", action):
            if started is not None:
                return f"The stopwatch is already running, at {say_duration(now - started, exact=True)}"
            state["stopwatch"] = now
            _timers_save(state)
            return "Stopwatch started"
        if re.search(r"\b(reset|clear)\b", action):
            state["stopwatch"] = None
            _timers_save(state)
            return "Stopwatch reset"
        if started is None:
            return "The stopwatch is not running. Say start the stopwatch to begin"
        elapsed = say_duration(now - started, exact=True)
        if re.search(r"\b(stop|end|finish)\b", action):
            state["stopwatch"] = None
            _timers_save(state)
            return f"Stopped at {elapsed}"
        return f"The stopwatch is at {elapsed}"


# ---- scheduler -------------------------------------------------------------
def fire_due(now: float = None, path=None) -> list:
    """Remove every finished timer and return [(sentence_to_speak, timer)]."""
    now = time.time() if now is None else now
    with _TIMER_LOCK:
        state = _timers_load(path)
        due = [t for t in state["timers"] if t["due"] <= now]
        if not due:
            return []
        state["timers"] = [t for t in state["timers"] if t["due"] > now]
        _timers_save(state, path)
    fired = []
    for timer in sorted(due, key=lambda t: t["due"]):
        if now - timer["due"] > STALE_SECONDS:
            print(f"Dropped a stale timer: {fire_message(timer)}")
            continue
        fired.append((fire_message(timer), timer))
    return fired


def timer_loop(stop_event=None, handler=None, interval: float = POLL_SECONDS, path=None) -> None:
    """Blocking loop: `handler(sentence, timer)` is called for every timer that runs out."""
    while stop_event is None or not stop_event.is_set():
        try:
            for message, timer in fire_due(path=path):
                if handler is not None:
                    handler(message, timer)
        except Exception as exc:  # one bad entry must not kill the loop
            print(f"Timer loop error: {exc}")
        if stop_event is None:
            time.sleep(interval)
        elif stop_event.wait(interval):
            break


# =============================================================================
# HARDWARE TOOLS: speaker volume
# The speaker is on the ESP32, so the server only keeps the wanted value. server.py
# sends it in an "X-Volume" header on every reply and the ESP32 applies it.
# Stored in device_state.json so it survives a restart.
# =============================================================================
DEVICE_STATE_FILE = Path(os.environ.get(
    "DEVICE_STATE_FILE",
    Path(__file__).resolve().parent.parent / "device_state.json",
))

DEFAULT_VOLUME = 40   # same as the ESP32 sketch
STEP = 10             # "louder" / "quieter"
SMALL_STEP = 5        # "a bit louder"
BIG_STEP = 25         # "a lot louder"
FLOOR = 5             # "quieter" never goes below this, so it cannot silence her by accident
LOWEST = 10           # what "minimum volume" means

_DEVICE_LOCK = threading.RLock()


def _clamp(value: int) -> int:
    return max(0, min(100, int(value)))


def _device_load() -> dict:
    try:
        data = json.loads(DEVICE_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    volume = data.get("volume")
    return {
        "volume": _clamp(volume) if isinstance(volume, (int, float)) else DEFAULT_VOLUME,
        "muted": bool(data.get("muted", False)),
    }


def _device_save() -> None:
    temp = DEVICE_STATE_FILE.with_name(DEVICE_STATE_FILE.name + ".tmp")
    try:
        temp.write_text(json.dumps(_device, indent=2), encoding="utf-8")
        temp.replace(DEVICE_STATE_FILE)
    except OSError as exc:
        print(f"Could not save device state: {exc}")


_device = _device_load()


def effective_volume() -> int:
    """What the speaker should use right now: 0 while muted."""
    with _DEVICE_LOCK:
        return 0 if _device["muted"] else _device["volume"]


def volume_header() -> str:
    """Value for the X-Volume HTTP header."""
    return str(effective_volume())


_UP_RE = re.compile(r"\b(up|louder|higher|increase|raise|boost|more)\b")
_DOWN_RE = re.compile(r"\b(down|quieter|lower|softer|decrease|reduce|less)\b")


def set_volume(level: str = "") -> str:
    """Change the speaker volume: a number 0-100, louder/quieter, max, min, mute or unmute."""
    low = " ".join(str(level or "").lower().split())
    with _DEVICE_LOCK:
        volume = _device["volume"]
        if re.search(r"\bun-?mute\b|\bsound on\b", low):
            _device["muted"] = False
            if volume == 0:
                _device["volume"] = volume = DEFAULT_VOLUME
            message = f"Okay, I am back at {volume} percent"
        elif re.search(r"\b(mute|silent|silence|off)\b", low):
            _device["muted"] = True
            message = "Okay, I am muted"
        else:
            number = re.search(r"(?<!\d)(\d{1,3})(?!\d)", low)
            up, down = _UP_RE.search(low), _DOWN_RE.search(low)
            direction = None
            if up and down:
                direction = "up" if up.start() < down.start() else "down"
            elif up:
                direction = "up"
            elif down:
                direction = "down"

            new = None
            if re.search(r"\b(max|maximum|full|loudest|highest)\b", low):
                new = 100
            elif re.search(r"\b(min|minimum|lowest|quietest|softest)\b", low):
                new = LOWEST
            elif re.search(r"\bhalf\b", low):
                new = 50
            elif re.search(r"\b(default|normal)\b", low):
                new = DEFAULT_VOLUME
            elif direction is None and number:
                new = _clamp(number.group(1))

            if new is not None:
                _device["muted"] = False
                _device["volume"] = new
                message = f"Okay, volume set to {new} percent"
            elif direction:
                _device["muted"] = False  # "louder" while muted brings her back first
                if number:
                    delta = max(1, int(number.group(1)))
                elif re.search(r"\b(bit|little|slightly|touch|tad)\b", low):
                    delta = SMALL_STEP
                elif re.search(r"\b(lot|much|way|far)\b", low):
                    delta = BIG_STEP
                else:
                    delta = STEP
                if direction == "up":
                    if volume >= 100:
                        message = "I am already at the maximum volume"
                    else:
                        _device["volume"] = _clamp(volume + delta)
                        message = f"Okay, volume up to {_device['volume']} percent"
                else:
                    if volume <= FLOOR:
                        message = "I am already at the lowest volume"
                    else:
                        _device["volume"] = max(FLOOR, _clamp(volume - delta))
                        message = f"Okay, volume down to {_device['volume']} percent"
            else:
                return "I did not catch that. Say louder, quieter, or a volume from 0 to 100"
        _device_save()
        return message


def get_volume() -> str:
    """Say the current volume."""
    with _DEVICE_LOCK:
        if _device["muted"]:
            return f"I am muted, the volume is stored at {_device['volume']} percent"
        return f"The volume is at {_device['volume']} percent"



# =============================================================================
# EXTRA TOOLS: math, units, money, forecast, dates, fun, notes, lists, pc status
# No new API keys. Free keyless services: Open-Meteo, Frankfurter, CoinGecko,
# Wikipedia, dictionaryapi.dev, MyMemory, Nager.Date, icanhazdadjoke, uselessfacts.
# Optional .env settings: DEFAULT_LOCATION=Prague, DEFAULT_COUNTRY=CZ,
#   DISABLED_TOOLS=fun_fact,pc_status (comma separated), USER_DATA_FILE=...
# Results are written to be spoken: short, no emoji, no trailing period.
# =============================================================================
UA = {"User-Agent": "KateVoiceAssistant/1.0 (personal project)"}
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _get(url: str, params: dict = None, headers: dict = None):
    """GET a JSON document. Returns None on any failure (the key or URL never leaks into speech)."""
    try:
        response = requests.get(url, params=params, headers={**UA, **(headers or {})}, timeout=10)
        if response.status_code != 200:
            return None
        return response.json()
    except (requests.RequestException, ValueError):
        return None


def _num(x) -> str:
    """A number as it should be spoken."""
    if isinstance(x, float):
        if math.isnan(x) or math.isinf(x):
            return "not a number"
        if x == int(x) and abs(x) < 1e15:
            x = int(x)
    if isinstance(x, int):
        if abs(x) < 10 ** 15:
            return f"{x:,}"
        text = f"{x:.6g}"
    elif abs(x) >= 1e15 or 0 < abs(x) < 1e-4:
        text = f"{x:.4g}"
    else:
        return f"{round(x, 6):,.6f}".rstrip("0").rstrip(".")
    return re.sub(r"e([+-]?)0*(\d+)", lambda m: f" times 10 to the {'minus ' if m.group(1) == '-' else ''}{m.group(2)}", text)


def _short(x: float) -> str:
    """Rounded for unit and money answers: 2 decimals normally, more for tiny values."""
    if x == 0:
        return "0"
    if abs(x) >= 1000:
        return _num(round(x))
    if abs(x) >= 1:
        return _num(round(x, 2))
    return _num(float(f"{x:.3g}"))


def _clock(hour: int, minute: int) -> str:
    return f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def _fmt_date(d: date, weekday: bool = True) -> str:
    base = f"{MONTHS[d.month - 1]} {d.day}"
    return f"{WEEKDAYS[d.weekday()]}, {base}" if weekday else base


def _join(items: list) -> str:
    items = [str(i) for i in items]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


# =============================================================================
# 1. calculate
# =============================================================================
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
        ast.Pow: operator.pow}
_UN = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {
    "sqrt": math.sqrt, "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil,
    "ln": math.log, "log": math.log10, "exp": math.exp,
    "factorial": lambda x: math.factorial(int(x)) if 0 <= x <= 170 and x == int(x) else (_ for _ in ()).throw(ValueError()),
    "sin": lambda x: math.sin(math.radians(x)),   # degrees: that is what people mean when they talk
    "cos": lambda x: math.cos(math.radians(x)),
    "tan": lambda x: math.tan(math.radians(x)),
}
_CONSTS = {"pi": math.pi, "e": math.e}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        a, b = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and a not in (0, 1, -1) and abs(b) * math.log10(abs(a)) > 300:
            raise OverflowError
        return _BIN[type(node.op)](a, b)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UN:
        return _UN[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Name) and node.id in _CONSTS:
        return _CONSTS[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
        return _FUNCS[node.func.id](*[_eval(a) for a in node.args])
    raise ValueError("unsupported")


def _average_expr(numbers: str) -> str:
    values = re.findall(r"\d+(?:\.\d+)?", numbers)
    return f"(({'+'.join(values)})/{len(values)})" if values else "0"


def _words_to_math(text: str) -> str:
    low = " " + text.lower().strip().rstrip("?=.") + " "
    low = re.sub(r"(?<=\d),(?=\d{3}\b)", "", low)                       # 1,000 -> 1000
    low = re.sub(r"\b(what is|what's|calculate|compute|equals?)\b", " ", low)
    low = re.sub(r"(\d+(?:\.\d+)?)\s*(?:%|percent)\s*of\b\s*", r"(\1/100)*", low)
    n = r"(\d+(?:\.\d+)?)"
    low = re.sub(rf"{n}\s*(?:%|percent)\s*(?:off|discount)(?: of| on| from)?\s*{n}", r"(\2*(1-\1/100))", low)        # 20% off 80
    low = re.sub(rf"{n}\s*(?:\+|plus)\s*{n}\s*(?:%|percent)", r"(\1*(1+\2/100))", low)                       # 100 plus 21%
    low = re.sub(rf"{n}\s*(?:-|minus)\s*{n}\s*(?:%|percent)", r"(\1*(1-\2/100))", low)                        # 100 minus 20%
    low = re.sub(rf"what (?:percent|percentage) is {n} of {n}", r"(\1/\2*100)", low)                            # what percent is 20 of 80
    low = re.sub(rf"{n} is what (?:percent|percentage) of {n}", r"(\1/\2*100)", low)
    low = re.sub(rf"(?:factorial of {n}|{n}\s*factorial)", lambda m: f"factorial({m.group(1) or m.group(2)})", low)
    low = re.sub(r"(?:average|mean) of ([\d.,\sand]+)", lambda m: _average_expr(m.group(1)), low)
    low = re.sub(r"square root of\s*(\d+(?:\.\d+)?)", r"sqrt(\1)", low)
    low = re.sub(r"(\d+(?:\.\d+)?)\s*squared", r"(\1)**2", low)
    low = re.sub(r"(\d+(?:\.\d+)?)\s*cubed", r"(\1)**3", low)
    for pattern, symbol in ((r"\bto the power of\b", "**"), (r"\bplus\b", "+"), (r"\bminus\b", "-"),
                            (r"\b(?:multiplied by|times)\b", "*"), (r"\b(?:divided by|over)\b", "/"),
                            (r"\b(?:modulo|mod)\b", "%"), (r"\^", "**"), ("×", "*"), ("÷", "/")):
        low = re.sub(pattern, f" {symbol} ", low)
    low = re.sub(r"(?<=[\d)])\s*x\s*(?=[\d(])", " * ", low)
    return low.strip()


def calculate(expression: str = "") -> str:
    """Evaluate a math expression safely."""
    text = _words_to_math(expression)
    if not text:
        return "Tell me what to calculate"
    try:
        return _num(_eval(ast.parse(text, mode="eval")))
    except ZeroDivisionError:
        return "You cannot divide by zero"
    except OverflowError:
        return "That number is far too big for me"
    except (ValueError, SyntaxError, TypeError):
        return "Sorry, I could not work that out"


# =============================================================================
# 2. convert_units
# =============================================================================
_UNITS = {}   # alias -> (category, factor to the base unit, singular, plural)


def _def(category: str, factor: float, singular: str, plural: str, *aliases: str) -> None:
    for name in {singular, plural, *aliases}:
        _UNITS[name] = (category, factor, singular, plural)


_def("length", 0.001, "millimeter", "millimeters", "mm")
_def("length", 0.01, "centimeter", "centimeters", "cm")
_def("length", 1, "meter", "meters", "m")
_def("length", 1000, "kilometer", "kilometers", "km")
_def("length", 0.0254, "inch", "inches", "in")
_def("length", 0.3048, "foot", "feet", "ft")
_def("length", 0.9144, "yard", "yards", "yd")
_def("length", 1609.344, "mile", "miles", "mi")
_def("length", 1852, "nautical mile", "nautical miles")
_def("mass", 1e-6, "milligram", "milligrams", "mg")
_def("mass", 0.001, "gram", "grams", "g")
_def("mass", 1, "kilogram", "kilograms", "kg", "kilo", "kilos")
_def("mass", 1000, "tonne", "tonnes", "ton", "tons", "metric ton")
_def("mass", 0.028349523125, "ounce", "ounces", "oz")
_def("mass", 0.45359237, "pound", "pounds", "lb", "lbs")
_def("mass", 6.35029318, "stone", "stone")
_def("volume", 0.001, "milliliter", "milliliters", "ml")
_def("volume", 1, "liter", "liters", "l")
_def("volume", 3.785411784, "gallon", "gallons", "us gallon", "us gallons")
_def("volume", 0.946352946, "quart", "quarts")
_def("volume", 0.473176473, "pint", "pints")
_def("volume", 0.2365882365, "cup", "cups")
_def("volume", 0.0295735296, "fluid ounce", "fluid ounces", "fl oz")
_def("volume", 0.01478676478, "tablespoon", "tablespoons", "tbsp")
_def("volume", 0.00492892159, "teaspoon", "teaspoons", "tsp")
_def("speed", 1, "meter per second", "meters per second", "m/s", "mps")
_def("speed", 1 / 3.6, "kilometer per hour", "kilometers per hour", "km/h", "kmh", "kph")
_def("speed", 0.44704, "mile per hour", "miles per hour", "mph")
_def("speed", 0.514444, "knot", "knots", "kn")
_def("area", 1, "square meter", "square meters", "sq m", "m2")
_def("area", 1e6, "square kilometer", "square kilometers", "sq km", "km2")
_def("area", 1e4, "hectare", "hectares", "ha")
_def("area", 4046.8564224, "acre", "acres")
_def("area", 0.09290304, "square foot", "square feet", "sq ft")
_def("area", 2589988.110336, "square mile", "square miles", "sq mi")
_def("time", 1, "second", "seconds", "sec", "s")
_def("time", 60, "minute", "minutes", "min")
_def("time", 3600, "hour", "hours", "hr", "h")
_def("time", 86400, "day", "days")
_def("time", 604800, "week", "weeks")
_def("time", 31536000, "year", "years")
_def("energy", 1, "joule", "joules", "j")
_def("energy", 1000, "kilojoule", "kilojoules", "kj")
_def("energy", 4184, "calorie", "calories", "kcal", "kilocalorie", "kilocalories", "food calorie")
_def("energy", 3.6e6, "kilowatt hour", "kilowatt hours", "kwh")
_def("pressure", 1, "pascal", "pascals", "pa")
_def("pressure", 1000, "kilopascal", "kilopascals", "kpa")
_def("pressure", 1e5, "bar", "bars")
_def("pressure", 100, "millibar", "millibars", "mbar")
_def("pressure", 6894.757, "psi", "pounds per square inch")
_def("pressure", 101325, "atmosphere", "atmospheres", "atm")
_def("pressure", 133.322, "millimeter of mercury", "millimeters of mercury", "mmhg")
_def("power", 1, "watt", "watts", "w")
_def("power", 1000, "kilowatt", "kilowatts", "kw")
_def("power", 745.7, "horsepower", "horsepowers", "hp")
_def("data", 1, "byte", "bytes")
_def("data", 1e3, "kilobyte", "kilobytes", "kb")
_def("data", 1e6, "megabyte", "megabytes", "mb")
_def("data", 1e9, "gigabyte", "gigabytes", "gb")
_def("data", 1e12, "terabyte", "terabytes", "tb")

_TEMPS = {"c": "Celsius", "celsius": "Celsius", "centigrade": "Celsius",
          "f": "Fahrenheit", "fahrenheit": "Fahrenheit",
          "k": "Kelvin", "kelvin": "Kelvin"}


def _unit_key(name: str) -> str:
    low = " ".join(str(name).lower().replace("°", " ").split())
    low = low.replace("metre", "meter").replace("litre", "liter").replace("squared", "")
    low = re.sub(r"\bdegrees?\b", " ", low)
    return " ".join(low.split())


def convert_units(value: str = "", from_unit: str = "", to_unit: str = "") -> str:
    """Convert between units of length, mass, volume, speed, area, time, data and temperature."""
    try:
        amount = float(str(value).replace(",", "").strip())
    except ValueError:
        return "Tell me the number to convert"
    a, b = _unit_key(from_unit), _unit_key(to_unit)
    if a in _TEMPS and b in _TEMPS:
        src, dst = _TEMPS[a], _TEMPS[b]
        celsius = {"Celsius": amount, "Fahrenheit": (amount - 32) * 5 / 9, "Kelvin": amount - 273.15}[src]
        result = {"Celsius": celsius, "Fahrenheit": celsius * 9 / 5 + 32, "Kelvin": celsius + 273.15}[dst]
        unit = lambda n: n if n == "Kelvin" else f"degrees {n}"
        return f"{_short(amount)} {unit(src)} is about {_short(result)} {unit(dst)}"
    ua, ub = _UNITS.get(a), _UNITS.get(b)
    if not ua or not ub:
        return f"Sorry, I do not know how to convert {from_unit} to {to_unit}"
    if ua[0] != ub[0]:
        return f"I cannot convert {ua[3]} to {ub[3]}, they measure different things"
    result = amount * ua[1] / ub[1]
    name = lambda unit, n: unit[2] if n == 1 else unit[3]
    return f"{_short(amount)} {name(ua, amount)} is about {_short(result)} {name(ub, round(result, 2))}"


# =============================================================================
# 3. convert_currency, 4. get_crypto_price
# =============================================================================
_CURRENCY_WORDS = {
    "dollar": "USD", "us dollar": "USD", "american dollar": "USD", "buck": "USD",
    "euro": "EUR", "pound": "GBP", "british pound": "GBP", "sterling": "GBP",
    "koruna": "CZK", "czech koruna": "CZK", "crown": "CZK", "czech crown": "CZK", "kc": "CZK",
    "yen": "JPY", "zloty": "PLN", "franc": "CHF", "swiss franc": "CHF", "forint": "HUF",
    "canadian dollar": "CAD", "australian dollar": "AUD", "yuan": "CNY", "renminbi": "CNY",
    "rupee": "INR", "swedish krona": "SEK", "krona": "SEK", "norwegian krone": "NOK",
    "danish krone": "DKK", "lira": "TRY", "real": "BRL", "won": "KRW", "mexican peso": "MXN",
    "new zealand dollar": "NZD", "rand": "ZAR", "shekel": "ILS", "leu": "RON", "lev": "BGN",
}
_CURRENCY_NAMES = {
    "USD": "US dollars", "EUR": "euros", "GBP": "British pounds", "CZK": "Czech korunas",
    "JPY": "Japanese yen", "PLN": "Polish zloty", "CHF": "Swiss francs", "HUF": "Hungarian forint",
    "CAD": "Canadian dollars", "AUD": "Australian dollars", "CNY": "Chinese yuan",
    "INR": "Indian rupees", "SEK": "Swedish kronor", "NOK": "Norwegian kroner",
    "DKK": "Danish kroner", "TRY": "Turkish lira", "BRL": "Brazilian reals", "KRW": "South Korean won",
}


def _currency_code(text: str):
    low = " ".join(str(text).lower().replace(".", "").split())
    if re.fullmatch(r"[a-z]{3}", low) and low.upper() in {*_CURRENCY_NAMES, *_CURRENCY_WORDS.values()}:
        return low.upper()
    for candidate in (low, low[:-1] if low.endswith("s") else low, low[:-2] if low.endswith("es") else low):
        if candidate in _CURRENCY_WORDS:
            return _CURRENCY_WORDS[candidate]
    if re.fullmatch(r"[a-z]{3}", low):
        return low.upper()
    return None


_RATE_CACHE = {}


def _rate(src: str, dst: str):
    """(rate, is_stale). Fresh rates are reused for an hour; an old one is the fallback when offline."""
    hit = _RATE_CACHE.get((src, dst))
    if hit and time.time() - hit[1] < 3600:
        return hit[0], False
    data = _get("https://api.frankfurter.dev/v1/latest", {"base": src, "symbols": dst})
    rate = ((data or {}).get("rates") or {}).get(dst)
    if rate:
        _RATE_CACHE[(src, dst)] = (rate, time.time())
        return rate, False
    return (hit[0], True) if hit else (None, False)


def convert_currency(amount: str = "1", from_currency: str = "", to_currency: str = "") -> str:
    """Convert money using the European Central Bank rates."""
    try:
        value = float(str(amount).replace(",", "").strip() or 1)
    except ValueError:
        return "Tell me the amount to convert"
    src, dst = _currency_code(from_currency), _currency_code(to_currency)
    if not src or not dst:
        return "I did not recognize one of those currencies"
    if src == dst:
        return f"That is the same currency, {_short(value)}"
    rate, stale = _rate(src, dst)
    if not rate:
        return "Sorry, I could not get the exchange rate for that"
    name = lambda code: _CURRENCY_NAMES.get(code, code)
    text = f"{_short(value)} {name(src)} is about {_short(value * rate)} {name(dst)}"
    return text + (", using my last known rate because I am offline" if stale else "")


_COINS = {"btc": "bitcoin", "eth": "ethereum", "ether": "ethereum", "sol": "solana", "doge": "dogecoin",
          "xrp": "ripple", "ada": "cardano", "ltc": "litecoin", "bnb": "binancecoin", "dot": "polkadot",
          "xmr": "monero", "usdt": "tether", "ton": "the-open-network", "toncoin": "the-open-network",
          "trx": "tron", "tron": "tron", "bitcoin cash": "bitcoin-cash", "binance coin": "binancecoin"}


def get_crypto_price(coin: str = "bitcoin", currency: str = "USD") -> str:
    """Current price of a cryptocurrency."""
    low = " ".join(str(coin or "bitcoin").lower().split())
    coin_id = _COINS.get(low, low.replace(" ", "-"))
    code = _currency_code(currency or "USD") or "USD"
    data = _get("https://api.coingecko.com/api/v3/simple/price",
                {"ids": coin_id, "vs_currencies": code.lower(), "include_24hr_change": "true"})
    entry = (data or {}).get(coin_id)
    if not entry or code.lower() not in entry:
        return f"Sorry, I could not find a price for {coin}"
    price = entry[code.lower()]
    change = entry.get(f"{code.lower()}_24h_change")
    text = f"{coin_id.replace('-', ' ').title()} is at {_short(price)} {_CURRENCY_NAMES.get(code, code)}"
    if isinstance(change, (int, float)):
        text += f", {'up' if change >= 0 else 'down'} {abs(change):.1f} percent in the last 24 hours"
    return text


# =============================================================================
# 5. define_word, 6. wiki_summary, 7. translate, 8. spell_word
# =============================================================================
def define_word(word: str = "") -> str:
    """Dictionary definition of an English word: the main meaning, a second one, and an example."""
    word = (word or "").strip().lower()
    if not word:
        return "Tell me which word"
    data = _get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{quote(word)}")
    try:
        meanings = data[0]["meanings"]
        first = meanings[0]
        text = f"{word}, a {first['partOfSpeech']}: {first['definitions'][0]['definition'].strip().rstrip('.')}"
        second = next((m for m in meanings[1:] if m["partOfSpeech"] != first["partOfSpeech"]), None)
        if second:
            text += f". It can also be a {second['partOfSpeech']}: {second['definitions'][0]['definition'].strip().rstrip('.')}"
        example = next((d["example"] for m in meanings for d in m["definitions"] if d.get("example")), "")
        if example:
            text += f". For example: {example.strip().rstrip('.')}"
        return text
    except (TypeError, KeyError, IndexError):
        return f"I could not find a definition for {word}"


def wiki_summary(topic: str = "", detail: str = "short") -> str:
    """Summary of a topic from Wikipedia: two sentences, or about six when more is wanted."""
    topic = (topic or "").strip()
    if not topic:
        return "Tell me what to look up"
    found = _get("https://en.wikipedia.org/w/api.php",
                 {"action": "opensearch", "search": topic, "limit": 1, "format": "json"})
    try:
        title = found[1][0]
    except (TypeError, IndexError):
        return f"I could not find anything about {topic}"
    long = str(detail or "").lower().startswith(("l", "d", "m", "f"))
    if long:   # the whole intro section, not just the summary
        extract = _get("https://en.wikipedia.org/w/api.php",
                       {"action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1,
                        "titles": title, "format": "json", "redirects": 1})
        try:
            page = next(iter(extract["query"]["pages"].values()))
            text = page.get("extract") or ""
        except (TypeError, KeyError, StopIteration):
            text = ""
    else:
        page = _get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(title.replace(' ', '_'))}")
        if page and page.get("type") == "disambiguation":
            return f"{title} could mean many things, be more specific"
        text = (page or {}).get("extract") or ""
    if not text:
        return f"I could not read the article about {topic}"
    text = re.sub(r"\s*\([^)]*\)", "", text)
    return _first_sentences(text, 6 if long else 2, 1100 if long else 450).rstrip(".")


_LANGUAGES = {"czech": "cs", "german": "de", "french": "fr", "spanish": "es", "italian": "it",
              "polish": "pl", "slovak": "sk", "russian": "ru", "portuguese": "pt", "dutch": "nl",
              "swedish": "sv", "japanese": "ja", "chinese": "zh-CN", "korean": "ko",
              "ukrainian": "uk", "hungarian": "hu", "turkish": "tr", "arabic": "ar", "greek": "el",
              "hindi": "hi", "norwegian": "no", "danish": "da", "finnish": "fi", "latin": "la",
              "english": "en"}


def translate(text: str = "", language: str = "") -> str:
    """Translate English text into another language."""
    text = (text or "").strip()[:500]
    low = (language or "").strip().lower()
    code = _LANGUAGES.get(low) or (low if re.fullmatch(r"[a-z]{2}", low) else None)
    if not text or not code:
        return "Tell me what to translate and into which language"
    data = _get("https://api.mymemory.translated.net/get", {"q": text, "langpair": f"en|{code}"})
    try:
        if int(data["responseStatus"]) != 200:
            raise ValueError
        result = data["responseData"]["translatedText"].strip()
    except (TypeError, KeyError, ValueError):
        return "Sorry, the translation service did not answer"
    return f"in {low.title() if low in _LANGUAGES else code} that is {result}"


def spell_word(word: str = "") -> str:
    """Spell a word letter by letter."""
    word = (word or "").strip()
    if not word:
        return "Tell me which word to spell"
    names = {"-": "dash", "'": "apostrophe", " ": "space"}
    letters = ", ".join(names.get(c, c.upper()) for c in word)
    return f"{word.lower()} is spelled {letters}"


# =============================================================================
# 9-13. Places: world_time, get_forecast, get_sun_times, get_air_quality
# =============================================================================
_GEO_CACHE = {}


def _geocode(query: str):
    query = (query or "").strip() or _default_location()
    if not query:
        return None
    key = query.lower()
    if key in _GEO_CACHE:
        return _GEO_CACHE[key]
    name, _, qualifier = query.partition(",")
    data = _get("https://geocoding-api.open-meteo.com/v1/search",
                {"name": name.strip(), "count": 5, "language": "en", "format": "json"})
    results = (data or {}).get("results") or []
    if not results:
        return None
    chosen = results[0]
    wanted = qualifier.strip().lower()
    if wanted:
        for r in results:
            if wanted in f"{r.get('country', '')} {r.get('admin1', '')} {r.get('country_code', '')}".lower():
                chosen = r
                break
    _GEO_CACHE[key] = chosen
    return chosen


def _no_place(location: str) -> str:
    return f"I could not find a place called {location}" if (location or "").strip() else "Tell me which place you mean"


def _world_time_one(location: str = "") -> str:
    """Current local date and time in another city."""
    place = _geocode(location)
    if not place:
        return _no_place(location)
    data = _get("https://api.open-meteo.com/v1/forecast",
                {"latitude": place["latitude"], "longitude": place["longitude"],
                 "current": "temperature_2m", "timezone": "auto"})
    if not data or "utc_offset_seconds" not in data:
        return "Sorry, I could not get the time there"
    local = datetime.now(timezone.utc) + timedelta(seconds=data["utc_offset_seconds"])
    text = f"{WEEKDAYS[local.weekday()]}, {MONTHS[local.month - 1]} {local.day}, {_clock(local.hour, local.minute)}"
    diff = (data["utc_offset_seconds"] - time.localtime().tm_gmtoff) / 3600
    if diff == 0:
        return text + ", the same time as here"
    unit = "hour" if abs(diff) == 1 else "hours"
    return text + f", {_num(abs(diff))} {unit} {'ahead of' if diff > 0 else 'behind'} you"


def world_time(location: str = "") -> str:
    """Local time in one or several places ('Tokyo and London')."""
    places = [p.strip() for p in re.split(r",|;|\band\b", location or "") if p.strip()][:4]
    if len(places) <= 1:
        return _world_time_one(places[0] if places else location)
    return "; ".join(f"{p.title()}: {_world_time_one(p)}" for p in places)


_WMO = {0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
        51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
        61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
        71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
        80: "light rain showers", 81: "rain showers", 82: "heavy rain showers",
        85: "snow showers", 86: "heavy snow showers", 95: "thunderstorm", 96: "thunderstorm with hail",
        99: "thunderstorm with hail"}


def _day_index(day: str, dates: list) -> object:
    """today=0 ... or 'week' for the overview; None if not understood."""
    low = (day or "today").lower().strip()
    if re.search(r"week|next days|coming days|few days", low):
        return "week"
    if "day after" in low:
        return 2
    if "tomorrow" in low:
        return 1
    if re.search(r"today|tonight|now|^$", low):
        return 0
    match = re.search(r"in (\d+) days?", low)
    if match:
        return int(match.group(1))
    for i, name in enumerate(WEEKDAYS):
        if name.lower() in low:
            today = date.fromisoformat(dates[0]).weekday()
            return (i - today) % 7
    return None


_WET_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
_SNOW_CODES = {71, 73, 75, 77, 85, 86}


def _advice(code: int, tmax: float, tmin: float, rain, uv=None) -> str:
    """What to do about the weather: umbrella, coat, water, sunscreen, ice."""
    snow = code in _SNOW_CODES
    tips = []
    if snow:
        tips.append("dress warmly because of the snow")
    elif code in _WET_CODES or (isinstance(rain, (int, float)) and rain >= 60):
        tips.append("take an umbrella")
    if tmax <= 5 and not snow:
        tips.append("wear a warm coat")
    elif tmax >= 28:
        tips.append("drink plenty of water")
    if isinstance(uv, (int, float)) and uv >= 6 and code not in _WET_CODES:
        tips.append("wear sunscreen")
    if tmin <= 0:
        tips.append("watch for ice in the morning")
    return _join(tips)


def _forecast_text(place: dict, day: str = "today") -> str:
    """Forecast sentence for one day or the week. Raises ValueError with a speakable message."""
    data = _get("https://api.open-meteo.com/v1/forecast",
                {"latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto",
                 "forecast_days": 7,
                 "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                          "precipitation_probability_max,wind_speed_10m_max,uv_index_max"})
    daily = (data or {}).get("daily")
    if not daily:
        raise ValueError("Sorry, I could not get the forecast")
    dates = daily["time"]
    index = _day_index(day, dates)
    if index is None or (index != "week" and index >= len(dates)):
        raise ValueError("I can only give the forecast for the next seven days")

    def one(i: int, label: bool) -> str:
        code = daily["weather_code"][i]
        hi, lo = round(daily["temperature_2m_max"][i]), round(daily["temperature_2m_min"][i])
        text = f"{_WMO.get(code, 'mixed weather')}, high of {hi} and low of {lo} degrees Celsius"
        rain = daily["precipitation_probability_max"][i]
        if isinstance(rain, (int, float)) and rain >= 20:
            text += f", {round(rain)} percent chance of precipitation"
        wind = daily["wind_speed_10m_max"][i]
        if not label:
            if isinstance(wind, (int, float)) and wind >= 30:
                text += f", windy with gusts up to {round(wind)} kilometers per hour"
            uv = (daily.get("uv_index_max") or [None] * 7)[i]
            tip = _advice(code, hi, lo, rain, uv)
            if tip:
                text += f", so {tip}"
        return (WEEKDAYS[date.fromisoformat(dates[i]).weekday()] + " " + text) if label else text

    if index == "week":
        return "; ".join(one(i, True) for i in range(1, 6))
    return one(index, False)


def get_forecast(location: str = "", day: str = "today") -> str:
    """Weather forecast for the next days, with practical advice."""
    place = _geocode(location)
    if not place:
        return _no_place(location)
    try:
        return _forecast_text(place, day)
    except ValueError as exc:
        return str(exc)


def _sun_times(place: dict):
    data = _get("https://api.open-meteo.com/v1/forecast",
                {"latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto",
                 "forecast_days": 1, "daily": "sunrise,sunset"})
    try:
        return (datetime.fromisoformat(data["daily"]["sunrise"][0]),
                datetime.fromisoformat(data["daily"]["sunset"][0]))
    except (TypeError, KeyError, IndexError, ValueError):
        raise ValueError("Sorry, I could not get the sun times")


def get_sun_times(location: str = "") -> str:
    """Sunrise, sunset and daylight length today."""
    place = _geocode(location)
    if not place:
        return _no_place(location)
    try:
        rise, sset = _sun_times(place)
    except ValueError as exc:
        return str(exc)
    span = int((sset - rise).total_seconds() // 60)
    return (f"sunrise at {_clock(rise.hour, rise.minute)} and sunset at {_clock(sset.hour, sset.minute)}, "
            f"that is {span // 60} hours {span % 60} minutes of daylight")


def _air_data(place: dict):
    """(aqi, label, fine_dust_or_None). Raises ValueError with a speakable message."""
    data = _get("https://air-quality-api.open-meteo.com/v1/air-quality",
                {"latitude": place["latitude"], "longitude": place["longitude"],
                 "current": "european_aqi,pm2_5,pm10"})
    current = (data or {}).get("current") or {}
    aqi = current.get("european_aqi")
    if aqi is None:
        raise ValueError("Sorry, I could not get the air quality there")
    for limit, label in ((20, "good"), (40, "fair"), (60, "moderate"), (80, "poor"), (100, "very poor")):
        if aqi < limit:
            break
    else:
        label = "extremely poor"
    return aqi, label, current.get("pm2_5")


def get_air_quality(location: str = "") -> str:
    """Current air quality (European index), fine dust and what to do about it."""
    place = _geocode(location)
    if not place:
        return _no_place(location)
    try:
        aqi, label, pm = _air_data(place)
    except ValueError as exc:
        return str(exc)
    text = f"air quality is {label}, index {round(aqi)}"
    if pm is not None:
        text += f", fine dust {round(pm)} micrograms per cubic meter"
    if label == "moderate":
        text += ", sensitive people may want to take it easy outside"
    elif label == "poor":
        text += ", better to limit outdoor exercise and keep windows closed"
    elif label in ("very poor", "extremely poor"):
        text += ", stay indoors if you can, especially with asthma or heart problems"
    return text


def rain_check(location: str = "", hours: str = "12") -> str:
    """Will it rain in the next hours? When it starts and stops."""
    place = _geocode(location)
    if not place:
        return _no_place(location)
    try:
        span = max(1, min(48, int(re.search(r"\d+", str(hours or "12")).group())))
    except (AttributeError, ValueError):
        span = 12
    data = _get("https://api.open-meteo.com/v1/forecast",
                {"latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto",
                 "forecast_days": 3, "hourly": "precipitation_probability,precipitation"})
    try:
        times = [datetime.fromisoformat(x) for x in data["hourly"]["time"]]
        probs = data["hourly"]["precipitation_probability"]
        mms = data["hourly"]["precipitation"]
        now = (datetime.now(timezone.utc) + timedelta(seconds=data["utc_offset_seconds"])).replace(
            tzinfo=None, minute=0, second=0, microsecond=0)
    except (TypeError, KeyError, ValueError):
        return "Sorry, I could not get the rain forecast"
    window = [(t_, p or 0, m or 0) for t_, p, m in zip(times, probs, mms) if now <= t_ < now + timedelta(hours=span)]
    if not window:
        return "Sorry, I could not get the rain forecast"
    wet = [i for i, (_, p, m) in enumerate(window) if p >= 50 or m >= 0.3]
    if not wet:
        top = max(p for _, p, _ in window)
        return f"no rain expected in the next {span} hours, the highest chance is {round(top)} percent"
    start = wet[0]
    end = start
    while end + 1 < len(window) and (end + 1) in wet:
        end += 1

    def when(stamp: datetime) -> str:
        day = "" if stamp.date() == now.date() else ("tomorrow " if stamp.date() == now.date() + timedelta(days=1) else WEEKDAYS[stamp.weekday()] + " ")
        return f"{day}{_clock(stamp.hour, stamp.minute)}"

    first, last = window[start][0], window[end][0] + timedelta(hours=1)
    total = sum(m for _, _, m in window[start:end + 1])
    text = ("precipitation is likely right now" if first <= now else f"precipitation is likely from about {when(first)}")
    text += f" until about {when(last)}" if end < len(window) - 1 else f" for the rest of the next {span} hours"
    if total >= 0.3:
        text += f", around {_num(round(total, 1))} millimeters"
    return text + ", take an umbrella"


# =============================================================================
# 14-15. Dates: days_until, next_holiday   16. moon_phase
# =============================================================================
def _easter(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * l) // 433
    month = (h + l - 7 * m + 90) // 25
    return date(year, month, (h + l - 7 * m + 33 * month + 19) % 32)


_FIXED_DAYS = {"christmas eve": (12, 24), "christmas": (12, 25), "new years eve": (12, 31),
               "new year": (1, 1), "new years": (1, 1), "halloween": (10, 31),
               "valentine": (2, 14), "valentines": (2, 14), "st patricks": (3, 17),
               "may day": (5, 1)}


def _parse_date(text: str, today: date):
    low = " ".join(re.sub(r"[']", "", text.lower()).replace(",", " ").split())
    low = re.sub(r"^(the|on|until|to)\s+", "", low)
    low = re.sub(r"\bday\b$", "", low).strip()
    if low in ("today", "now"):
        return today
    if low == "tomorrow":
        return today + timedelta(days=1)
    if low == "yesterday":
        return today - timedelta(days=1)

    def roll(month: int, day: int, year=None):
        if year:
            return date(year, month, day)
        d = date(today.year, month, day)
        return d if d >= today else date(today.year + 1, month, day)

    try:
        if low.startswith("easter"):
            d = _easter(today.year)
            return d if d >= today else _easter(today.year + 1)
        for name in sorted(_FIXED_DAYS, key=len, reverse=True):
            if low.startswith(name):
                return roll(*_FIXED_DAYS[name])
        m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", low)
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
        m = re.fullmatch(r"(\d{1,2})\.\s*(\d{1,2})\.?\s*(\d{4})?", low)        # 24.12. or 24.12.2026
        if m:
            return roll(int(m[2]), int(m[1]), int(m[3]) if m[3] else None)
        names = "|".join(n.lower() for n in MONTHS) + "|" + "|".join(n[:3].lower() for n in MONTHS)
        m = re.fullmatch(rf"(\d{{1,2}})(?:st|nd|rd|th)?(?: of)? ({names})[a-z]*(?: (\d{{4}}))?", low)
        if not m:
            m2 = re.fullmatch(rf"({names})[a-z]* (\d{{1,2}})(?:st|nd|rd|th)?(?: (\d{{4}}))?", low)
            if m2:
                m = (None, m2[2], m2[1], m2[3])
        if m:
            month = next(i + 1 for i, n in enumerate(MONTHS) if n.lower().startswith(m[2][:3]))
            return roll(month, int(m[1]), int(m[3]) if m[3] else None)
    except ValueError:
        return None
    return None


def days_until(target: str = "") -> str:
    """How many days until (or since) a date or holiday."""
    today = date.today()
    d = _parse_date(target or "", today)
    if d is None:
        return "Sorry, I did not understand that date. Try something like December 25 or Easter"
    n = (d - today).days
    when = _fmt_date(d)
    if n == 0:
        return f"That is today, {when}"
    if n == 1:
        return f"That is tomorrow, {when}"
    if n == -1:
        return f"That was yesterday, {when}"
    if n < 0:
        return f"That was {-n} days ago, on {when}"
    weeks = f", about {round(n / 7)} weeks" if n >= 21 else ""
    return f"That is in {n} days{weeks}, on {when}"


_COUNTRIES = {"czech republic": "CZ", "czechia": "CZ", "czech": "CZ", "slovakia": "SK", "poland": "PL",
              "germany": "DE", "austria": "AT", "united kingdom": "GB", "uk": "GB", "england": "GB",
              "britain": "GB", "united states": "US", "usa": "US", "america": "US", "us": "US",
              "france": "FR", "spain": "ES", "italy": "IT", "canada": "CA", "australia": "AU",
              "japan": "JP", "hungary": "HU", "netherlands": "NL", "switzerland": "CH", "sweden": "SE",
              "norway": "NO", "ireland": "IE", "portugal": "PT", "greece": "GR", "belgium": "BE"}


def next_holiday(country: str = "") -> str:
    """The next public holidays in a country."""
    low = (country or "").strip().lower() or _default_country().lower()
    code = _COUNTRIES.get(low) or (low.upper() if re.fullmatch(r"[a-z]{2}", low) else None)
    if not code:
        return f"I do not know the country {country}"
    data = _get(f"https://date.nager.at/api/v3/NextPublicHolidays/{code}")
    if not data:
        return "Sorry, I could not get the holidays"
    parts = []
    for h in data[:3]:
        d = date.fromisoformat(h["date"])
        parts.append(f"{h['name']} on {_fmt_date(d)}, in {(d - date.today()).days} days")
    return "the next public holidays are " + "; ".join(parts)


def moon_phase() -> str:
    """Current phase of the moon, computed offline."""
    synodic = 29.530588853
    ref = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
    age = ((datetime.now(timezone.utc) - ref).total_seconds() / 86400) % synodic
    names = ((1.85, "new moon"), (5.54, "waxing crescent"), (9.23, "first quarter"),
             (12.91, "waxing gibbous"), (16.61, "full moon"), (20.30, "waning gibbous"),
             (23.99, "last quarter"), (27.68, "waning crescent"))
    phase = next((n for limit, n in names if age < limit), "new moon")
    lit = round((1 - math.cos(2 * math.pi * age / synodic)) / 2 * 100)
    today = date.today()
    full = today + timedelta(days=round((synodic / 2 - age) % synodic))
    new = today + timedelta(days=round((synodic - age) % synodic))
    return (f"the moon is a {phase}, {lit} percent lit. The next full moon is around "
            f"{_fmt_date(full, False)} and the next new moon around {_fmt_date(new, False)}")


# =============================================================================
# 17-21. Fun: flip_coin, roll_dice, random_number, pick_random, tell_joke, fun_fact
# =============================================================================
def flip_coin() -> str:
    return f"It landed on {random.choice(['heads', 'tails'])}"


def roll_dice(sides: str = "6", count: str = "1") -> str:
    """Roll dice. Also understands '2d6' and 'd20'."""
    s = str(sides or "6").lower()
    m = re.search(r"(\d*)\s*d\s*(\d+)", s)
    if m:
        count, sides = m.group(1) or count, m.group(2)
    try:
        n = max(1, min(20, int(re.search(r"\d+", str(count or "1")).group())))
        faces = max(2, min(1000, int(re.search(r"\d+", str(sides or "6")).group())))
    except (AttributeError, ValueError):
        return "Tell me how many dice and how many sides"
    rolls = [random.randint(1, faces) for _ in range(n)]
    if n == 1:
        return f"You rolled a {rolls[0]}"
    return f"You rolled {_join(rolls)}, that makes {sum(rolls)}"


def random_number(low: str = "1", high: str = "10") -> str:
    try:
        a, b = int(float(str(low or 1))), int(float(str(high or 10)))
    except ValueError:
        return "Tell me the lowest and highest number"
    if a > b:
        a, b = b, a
    return f"Your number is {random.randint(a, b)}"


def pick_random(options: str = "") -> str:
    """Choose one of several options."""
    items = [o.strip() for o in re.split(r",|\bor\b", options or "") if o.strip()]
    if len(items) < 2:
        return "Give me at least two options to choose from"
    return f"I pick {random.choice(items)}"


_JOKES = ["I told my computer I needed a break, and now it will not stop sending me vacation ads",
          "Why do programmers prefer dark mode? Because light attracts bugs",
          "I would tell you a UDP joke, but you might not get it",
          "Why did the scarecrow win an award? He was outstanding in his field"]
_FACTS = ["Honey never spoils, jars found in ancient Egyptian tombs were still edible",
          "Octopuses have three hearts and blue blood",
          "A day on Venus is longer than a year on Venus",
          "Bananas are berries, but strawberries are not"]


_JOKE_CATEGORIES = (("Programming", ("program", "code", "coding", "computer", "tech", "developer", "software")),
                    ("Pun", ("pun", "wordplay")), ("Spooky", ("spooky", "halloween", "ghost", "scary")),
                    ("Christmas", ("christmas", "xmas", "santa")))


def tell_joke(topic: str = "") -> str:
    """A joke. Dad jokes by default; programming, pun, spooky and Christmas jokes on request."""
    low = (topic or "").lower()
    category = next((c for c, words in _JOKE_CATEGORIES if any(w in low for w in words)), "")
    if category or (low and "dad" not in low):
        data = _get(f"https://v2.jokeapi.dev/joke/{category or 'Misc'}",
                    {"safe-mode": "", "blacklistFlags": "nsfw,religious,political,racist,sexist,explicit"})
        if isinstance(data, dict) and not data.get("error"):
            if data.get("type") == "twopart":
                return f"{data.get('setup', '').strip()} {data.get('delivery', '').strip()}".strip()
            if data.get("joke"):
                return " ".join(str(data["joke"]).split())
    data = _get("https://icanhazdadjoke.com/", headers={"Accept": "application/json"})
    return (data or {}).get("joke") or random.choice(_JOKES)


def fun_fact() -> str:
    data = _get("https://uselessfacts.jsph.pl/api/v2/facts/random", {"language": "en"})
    return ((data or {}).get("text") or random.choice(_FACTS)).strip().rstrip(".")


# =============================================================================
# 22-29. Memory of the user: notes and lists (stored in user_data.json)
# =============================================================================
USER_DATA_FILE = Path(os.environ.get("USER_DATA_FILE", Path(__file__).resolve().parent.parent / "user_data.json"))
_DATA_LOCK = threading.RLock()


def _data_load() -> dict:
    try:
        data = json.loads(USER_DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    notes, lists = data.get("notes"), data.get("lists")
    # keep every other key too (the home location lives in here)
    return {**data, "notes": notes if isinstance(notes, list) else [],
            "lists": lists if isinstance(lists, dict) else {}}


def _home() -> dict:
    with _DATA_LOCK:
        home = _data_load().get("home")
    return home if isinstance(home, dict) else {}


def _default_location() -> str:
    """The home city the user told Kate, else DEFAULT_LOCATION from .env, else nothing."""
    return _home().get("query") or _env("DEFAULT_LOCATION")


def _default_country() -> str:
    return _home().get("country_code") or _env("DEFAULT_COUNTRY", "CZ")


def _data_save(data: dict) -> None:
    temp = USER_DATA_FILE.with_name(USER_DATA_FILE.name + ".tmp")
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(USER_DATA_FILE)
    except OSError as exc:
        print(f"Could not save user data: {exc}")


def add_note(text: str = "") -> str:
    text = (text or "").strip()
    if not text:
        return "Tell me what to write down"
    with _DATA_LOCK:
        data = _data_load()
        data["notes"].append({"text": text, "time": time.time()})
        _data_save(data)
        count = len(data["notes"])
        return f"Okay, I wrote that down. You have {count} {'note' if count == 1 else 'notes'}"


def _age(stamp) -> str:
    try:
        days = (date.today() - date.fromtimestamp(float(stamp))).days
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
    return "" if days <= 0 else ("yesterday" if days == 1 else f"{days} days ago")


def read_notes(query: str = "") -> str:
    """Read the latest notes, or search them for a word."""
    with _DATA_LOCK:
        notes = [n for n in _data_load()["notes"] if isinstance(n, dict) and n.get("text")]
    if not notes:
        return "You have no notes"
    numbered = list(enumerate(notes, start=1))
    words = [w for w in re.findall(r"\w+", (query or "").lower()) if len(w) > 2]
    if words:
        numbered = [(i, n) for i, n in numbered if any(w in n["text"].lower() for w in words)]
        if not numbered:
            return f"I found no note about {query.strip()}"
        shown, intro = numbered[-5:], f"Notes about {query.strip()}"
    else:
        shown, intro = numbered[-5:], "Your notes"
    listed = ", ".join(f"{i}) {n['text']}" + (f" ({_age(n.get('time'))})" if _age(n.get("time")) else "") for i, n in shown)
    more = f", and {len(numbered) - len(shown)} older ones" if len(numbered) > len(shown) else ""
    return f"{intro}: {listed}{more}"


def clear_notes(which: str = "all") -> str:
    """Delete all notes, the last one, or one by number."""
    low = (which or "all").strip().lower()
    with _DATA_LOCK:
        data = _data_load()
        notes = data["notes"]
        if not notes:
            return "You have no notes"
        number = re.search(r"\d+", low)
        if "last" in low:
            notes.pop()
            message = "Okay, I deleted the last note"
        elif number:
            i = int(number.group()) - 1
            if not 0 <= i < len(notes):
                return f"You only have {len(notes)} notes"
            notes.pop(i)
            message = f"Okay, I deleted note {i + 1}"
        else:
            data["notes"] = []
            message = "Okay, all notes are deleted"
        _data_save(data)
        return message


def _list_name(name: str) -> str:
    low = " ".join(str(name or "").lower().replace("to-do", "todo").replace("to do", "todo").split())
    low = re.sub(r"\b(my|the|a|our)\b", " ", low)
    low = re.sub(r"\blist\b", " ", low)
    return " ".join(low.split()) or "shopping"


def _items(text: str) -> list:
    return [p.strip() for p in re.split(r",|\band\b", text or "") if p.strip()]


def _same(a: str, b: str) -> bool:
    a, b = a.lower().strip(), b.lower().strip()
    return a == b or a.rstrip("s") == b.rstrip("s") or (len(b) > 2 and b in a)


def add_to_list(item: str = "", list_name: str = "shopping") -> str:
    """Add things to a named list."""
    name, items = _list_name(list_name), _items(item)
    if not items:
        return "Tell me what to add"
    with _DATA_LOCK:
        data = _data_load()
        current = data["lists"].setdefault(name, [])
        added = [i for i in items if not any(i.lower() == c.lower() for c in current)]
        current.extend(added)
        _data_save(data)
    if not added:
        return f"That is already on your {name} list"
    return f"Okay, I added {_join(added)} to your {name} list"


def read_list(list_name: str = "shopping") -> str:
    """Read one list, or give an overview of all lists when asked for 'all'."""
    name = _list_name(list_name)
    if name in ("all", "everything", "lists", "all lists"):
        with _DATA_LOCK:
            lists = {k: v for k, v in _data_load()["lists"].items() if v}
        if not lists:
            return "All your lists are empty"
        return "You have " + _join([f"{k} with {len(v)} {'item' if len(v) == 1 else 'items'}" for k, v in lists.items()])
    with _DATA_LOCK:
        current = _data_load()["lists"].get(name, [])
    if not current:
        return f"Your {name} list is empty"
    return f"Your {name} list has {len(current)} {'item' if len(current) == 1 else 'items'}: {_join(current)}"


def remove_from_list(item: str = "", list_name: str = "shopping") -> str:
    name, items = _list_name(list_name), _items(item)
    with _DATA_LOCK:
        data = _data_load()
        current = data["lists"].get(name, [])
        removed = []
        for wanted in items:
            for existing in list(current):
                if _same(existing, wanted):
                    current.remove(existing)
                    removed.append(existing)
                    break
        if not removed:
            return f"I did not find that on your {name} list"
        _data_save(data)
    return f"Okay, I removed {_join(removed)} from your {name} list"


def clear_list(list_name: str = "shopping") -> str:
    name = _list_name(list_name)
    with _DATA_LOCK:
        data = _data_load()
        if not data["lists"].get(name):
            return f"Your {name} list is already empty"
        data["lists"][name] = []
        _data_save(data)
    return f"Okay, your {name} list is empty now"


# =============================================================================
# 30. pc_status (the computer Kate runs on)
# =============================================================================
def pc_status() -> str:
    parts = []
    if psutil:
        parts.append(f"processor at {round(psutil.cpu_percent(interval=0.4))} percent")
        parts.append(f"memory {round(psutil.virtual_memory().percent)} percent used")
        battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
        if battery:
            parts.append(f"battery at {round(battery.percent)} percent{', charging' if battery.power_plugged else ''}")
    try:
        free = shutil.disk_usage(Path.home().anchor or "/").free / 1e9
        parts.append(f"{round(free)} gigabytes free on the disk")
    except OSError:
        pass
    if not parts:
        return "Sorry, I could not read the computer status"
    return "the computer has " + ", ".join(parts)





# =============================================================================
# NEW: set_home_location, daily_briefing, tip_split, date_math, distance_between
# =============================================================================
def set_home_location(location: str = "") -> str:
    """Remember the user's home city: forecast, weather, air quality... use it when no place is said."""
    if not (location or "").strip():
        return "Tell me which city you live in"
    place = _geocode(location)
    if not place:
        return _no_place(location)
    name, country = place.get("name", location), place.get("country", "")
    with _DATA_LOCK:
        data = _data_load()
        data["home"] = {"query": f"{name}, {country}" if country else name, "name": name,
                        "country": country, "country_code": place.get("country_code", "")}
        _data_save(data)
    return f"Okay, I will remember that you live in {name}{', ' + country if country else ''}"


def _money(x: float) -> str:
    return f"{x:,.2f}".replace(".00", "")


def tip_split(bill: str = "", tip_percent: str = "", people: str = "1") -> str:
    """Tip and per-person share of a restaurant bill."""
    try:
        total = float(re.search(r"\d+(?:\.\d+)?", str(bill).replace(",", "")).group())
    except (AttributeError, ValueError):
        return "Tell me the amount of the bill"
    try:
        pct = float(re.search(r"\d+(?:\.\d+)?", str(tip_percent)).group()) if str(tip_percent or "").strip() else float(_env("DEFAULT_TIP", "10"))
    except (AttributeError, ValueError):
        pct = 10.0
    try:
        n = max(1, int(re.search(r"\d+", str(people or "1")).group()))
    except (AttributeError, ValueError):
        n = 1
    tip = total * pct / 100
    grand = total + tip
    text = f"a {_num(pct)} percent tip on {_money(total)} is {_money(tip)}, so the total is {_money(grand)}"
    if n > 1:
        text += f", that is {_money(grand / n)} each for {n} people"
    return text


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    year, month = d.year + m // 12, m % 12 + 1
    last = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(d.day, last))


def date_math(start: str = "today", change: str = "", until: str = "") -> str:
    """Add or subtract time from a date, or measure the time between two dates (also ages)."""
    today = date.today()
    first = _parse_date(start or "today", today)
    if first is None:
        return "Sorry, I did not understand the date. Try something like October 5 or 2026-10-05"
    if (until or "").strip():
        second = _parse_date(until, today)
        if second is None:
            return "Sorry, I did not understand the second date"
        a, b = sorted((first, second))
        k = (b.year - a.year) * 12 + b.month - a.month
        if _add_months(a, k) > b:
            k -= 1
        days = (b - _add_months(a, k)).days
        years, months = divmod(k, 12)
        parts = [f"{x} {u}{'' if x == 1 else 's'}" for x, u in ((years, "year"), (months, "month"), (days, "day")) if x]
        total = (b - a).days
        return f"that is {total:,} days, or {_join(parts) if parts else '0 days'}" + (f", about {round(total / 7)} weeks" if 21 <= total < 365 else "")
    if (change or "").strip():
        low = change.lower()
        sign = -1 if re.search(r"\bago\b|\bbefore\b|\bback\b|\bminus\b|^\s*-", low) else 1
        found = re.findall(r"(\d+)\s*(day|week|month|year)s?", low)
        if not found:
            return "Tell me how much time to add, like 90 days or 3 weeks"
        result = first
        for amount, unit in found:
            n = int(amount) * sign
            result = (result + timedelta(days=n * {"day": 1, "week": 7}[unit])) if unit in ("day", "week") \
                else _add_months(result, n * (12 if unit == "year" else 1))
        return f"that is {_fmt_date(result)}, {result.year}"
    day_of_year = first.timetuple().tm_yday
    return f"{_fmt_date(first)}, {first.year} is day {day_of_year} of the year, in week {first.isocalendar()[1]}"


def _haversine_km(a: dict, b: dict) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a["latitude"], a["longitude"], b["latitude"], b["longitude"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def distance_between(from_place: str = "", to_place: str = "") -> str:
    """Straight-line distance between two places with a rough driving time. Empty from_place means home."""
    start, end = _geocode(from_place), _geocode(to_place) if (to_place or "").strip() else None
    if not start:
        return _no_place(from_place)
    if not end:
        return _no_place(to_place)
    km = _haversine_km(start, end)
    if km < 1:
        return "those two places are practically at the same spot"
    road = km * 1.2                                     # roads are never straight

    def span(minutes: float) -> str:
        minutes = int(round(minutes / 5) * 5)
        if minutes < 60:
            return f"{minutes} minutes"
        h, m = divmod(minutes, 60)
        return f"{h} {'hour' if h == 1 else 'hours'}" + (f" {m} minutes" if m else "")

    text = (f"{start.get('name')} to {end.get('name')} is about {_num(round(km))} kilometers, "
            f"{_num(round(km * 0.621371))} miles, in a straight line")
    if road <= 2500:
        text += f". By car it is very roughly {span(road / (50 if road < 60 else 80 if road < 150 else 95) * 60)}"
    if km > 600:
        text += f". Flying takes very roughly {span(km / 800 * 60 + 30)}"
    return text


def daily_briefing() -> str:
    """One spoken morning summary: date, weather, air, sunset, holidays, to-do list."""
    now = datetime.now()
    parts = [f"It is {_fmt_date(now.date())}, {_clock(now.hour, now.minute)}"]
    place = _geocode("")
    if place:
        for build in (
            lambda: f"Weather today: {_forecast_text(place, 'today')}",
            lambda: _air_warning(place),
            lambda: (lambda s: f"Sunset is at {_clock(s[1].hour, s[1].minute)}")(_sun_times(place)),
        ):
            try:
                text = build()
            except ValueError:
                continue
            if text:
                parts.append(text)
    else:
        parts.append("Tell me your home city and I will add the weather next time")
    data = _get(f"https://date.nager.at/api/v3/NextPublicHolidays/{_default_country().upper()}")
    for h in (data or [])[:1]:
        try:
            days = (date.fromisoformat(h["date"]) - now.date()).days
        except (KeyError, ValueError):
            break
        if days <= 7:
            parts.append(f"{h['name']} is " + ("today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"))
    with _DATA_LOCK:
        data = _data_load()
    soon = [b for b in data.get("birthdays", []) if isinstance(b, dict) and b.get("name")
            and (_next_birthday(b["month"], b["day"], now.date()) - now.date()).days <= 7]
    for b in sorted(soon, key=lambda b: _next_birthday(b["month"], b["day"], now.date()))[:2]:
        parts.append(_birthday_line(b, now.date()))
    today_events = sorted((e for e in _events(data) if e["date"] == now.date().isoformat()), key=lambda e: e.get("time") or "99")
    if today_events:
        parts.append("In your calendar today: " + _join([_event_line(e) for e in today_events]))
    todo = data["lists"].get("todo") or []
    if todo:
        parts.append(f"On your to do list: {_join(todo)}")
    if _env("BRIEFING_NEWS", "1") != "0":
        stories, _error = _fetch_news("", 3)
        if stories:
            _remember_news(stories)
            parts.append("Top headlines: " + "; ".join(s["headline"] for s in stories))
    return ". ".join(parts)


def _air_warning(place: dict) -> str:
    """A sentence only when the air is bad enough to matter."""
    aqi, label, _ = _air_data(place)
    return f"Air quality is {label}, better to limit outdoor exercise" if label in ("poor", "very poor", "extremely poor") else ""


# =============================================================================
# MORE TOOLS: animal_sound, recipe_search, get_stock_price, today_in_history, trivia,
# birthdays, expenses, sleep_calculator, magic_8ball, word_finder, inspire_me
# =============================================================================
_ANIMALS = {
    "dog": ("woof woof", "dogs can learn more than a hundred and fifty words"),
    "cat": ("meow", "cats sleep around fifteen hours a day"),
    "cow": ("moo, moooo", "cows have best friends and get stressed when they are separated"),
    "pig": ("oink oink", "pigs are about as smart as dogs"),
    "sheep": ("baaaa", "sheep can recognize faces for years"),
    "goat": ("maaaa", "goats have rectangular pupils"),
    "horse": ("neigh, neeigh", "horses can sleep standing up"),
    "donkey": ("hee haw, hee haw", "donkeys have an excellent memory"),
    "chicken": ("cluck cluck", "chickens can remember over a hundred faces"),
    "rooster": ("cock a doodle doo", "roosters crow because of an internal clock, not just at sunrise"),
    "duck": ("quack quack", "ducks have waterproof feathers thanks to an oil gland"),
    "goose": ("honk honk", "geese fly in a V to save energy"),
    "turkey": ("gobble gobble", "wild turkeys can run about 25 miles an hour"),
    "owl": ("hoo hoo", "owls can turn their heads about two hundred and seventy degrees"),
    "crow": ("caw caw", "crows can recognize human faces"),
    "frog": ("ribbit ribbit", "some frogs can freeze solid in winter and thaw back to life"),
    "snake": ("sssss", "snakes smell with their tongues"),
    "lion": ("roooaar", "a lion's roar can be heard from about eight kilometers away"),
    "tiger": ("grrrrr, roar", "every tiger has its own stripe pattern"),
    "bear": ("grrroar", "bears can smell food from kilometers away"),
    "wolf": ("awooooo", "wolves howl to keep their pack in touch"),
    "fox": ("yip yip, ring ding ding ding", "foxes use the earth's magnetic field to hunt"),
    "elephant": ("pffrrrooo, a big trumpet toot", "elephants can recognize themselves in a mirror"),
    "monkey": ("ooh ooh, aah aah", "some monkeys call each other by name"),
    "dolphin": ("click click, eee eee", "dolphins have signature whistles that work like names"),
    "whale": ("a slow, deep song", "humpback whale songs can go on for hours"),
    "seal": ("arf arf", "seals can sleep with half of the brain at a time"),
    "mouse": ("squeak squeak", "mice sing songs too high for humans to hear"),
    "bee": ("bzzzzz", "bees can recognize human faces"),
    "cricket": ("chirp chirp", "you can estimate the temperature by counting cricket chirps"),
    "bird": ("tweet tweet", "some birds can copy human speech"),
    "parrot": ("squawk, hello", "parrots can live for more than sixty years"),
    "seagull": ("kyow kyow", "seagulls can drink sea water"),
    "penguin": ("honk, bray", "penguins sometimes propose with a pebble"),
    "camel": ("grrrunt, bleat", "a camel's hump stores fat, not water"),
    "giraffe": ("hmmmm, a quiet hum", "giraffes hum to each other at night"),
    "hyena": ("hahaha hee hee", "a hyena's laugh tells the others how it is feeling"),
    "kangaroo": ("grunt, cluck", "kangaroos cannot walk backwards"),
    "cuckoo": ("cuckoo, cuckoo", "cuckoos lay their eggs in other birds' nests"),
    "peacock": ("a loud scream, ee-ow", "a peacock's tail can have over two hundred feathers"),
    "bat": ("eek, too high for you to hear", "bats are the only mammals that truly fly"),
    "hamster": ("squeak squeak", "hamsters can store food in their cheeks up to a fifth of their body weight"),
    "deer": ("a grunt, or a bellow", "deer antlers can grow an inch a day"),
    "dinosaur": ("ROOOOAR", "birds are living dinosaurs"),
}
_ANIMAL_ALIASES = {"doggy": "dog", "puppy": "dog", "kitty": "cat", "kitten": "cat", "cats": "cat", "dogs": "dog",
                   "piggy": "pig", "hen": "chicken", "chick": "chicken", "calf": "cow", "bull": "cow",
                   "lamb": "sheep", "ram": "sheep", "pony": "horse", "birdie": "bird", "mice": "mouse",
                   "wolves": "wolf", "geese": "goose", "bees": "bee", "lions": "lion", "bears": "bear",
                   "monkeys": "monkey", "elephants": "elephant", "whales": "whale", "dolphins": "dolphin",
                   "cock": "rooster", "t-rex": "dinosaur", "t rex": "dinosaur"}


def animal_sound(animal: str = "") -> str:
    """What an animal says, with a fun fact. A random animal when none is named."""
    low = re.sub(r"^(a|an|the)\s+", "", " ".join((animal or "").lower().replace("?", " ").split()))
    low = re.sub(r"^(what|how) (does|do|did) (a |an |the )?", "", low)
    low = re.sub(r"\s+(say|sound|make|go|goes)$", "", low).strip()
    key = _ANIMAL_ALIASES.get(low, low)
    if key not in _ANIMALS and key.endswith("s") and key[:-1] in _ANIMALS:
        key = key[:-1]
    if not key:
        key = random.choice(list(_ANIMALS))
        prefix = "Here is a random one. "
    else:
        prefix = ""
    if key not in _ANIMALS:
        return (f"I do not know what a {low} says. I can do " + _join(random.sample(sorted(_ANIMALS), 6))
                + " and many more")
    sound, fact = _ANIMALS[key]
    article = "an" if key[0] in "aeiou" else "a"
    return f"{prefix}{article.capitalize()} {key} says {sound}. Fun fact: {fact}"


# ---- recipes (TheMealDB, keyless) -------------------------------------------
def recipe_search(dish: str = "", part: str = "overview") -> str:
    """A recipe: overview, ingredients with amounts, or the steps. Random when no dish is named."""
    dish = (dish or "").strip()
    data = _get("https://www.themealdb.com/api/json/v1/1/" + ("search.php" if dish else "random.php"),
                {"s": dish} if dish else None)
    meals = (data or {}).get("meals") or []
    if not meals:
        return f"I could not find a recipe for {dish}" if dish else "Sorry, I could not get a recipe"
    meal = meals[0]
    ingredients = []
    for i in range(1, 21):
        name = (meal.get(f"strIngredient{i}") or "").strip()
        if name:
            ingredients.append((name, (meal.get(f"strMeasure{i}") or "").strip()))
    steps = [s.strip() for s in re.split(r"\r?\n+", meal.get("strInstructions") or "") if len(s.strip()) > 3]
    if len(steps) < 3:   # one big paragraph: split by sentence
        steps = re.split(r"(?<=[.!?])\s+", " ".join(steps))
    steps = [s for s in (re.sub(r"^(step\s*)?\d+[.:)]?\s*", "", s, flags=re.I).strip() for s in steps) if len(s) > 3]
    name, area, category = meal.get("strMeal", dish), meal.get("strArea", ""), meal.get("strCategory", "")
    low = (part or "overview").lower()
    if low.startswith("ingr"):
        return f"For {name} you need " + "; ".join(f"{m} {n}".strip() for n, m in ingredients)
    if low.startswith(("step", "method", "instr", "how", "direction")):
        listed = " ".join(f"Step {i + 1}: {s.rstrip('.')}." for i, s in enumerate(steps[:8]))
        return f"{name}. {listed}" + (f" There are {len(steps) - 8} more steps" if len(steps) > 8 else "")
    names = [n for n, _ in ingredients]
    shown = _join(names[:10]) + (f" and {len(names) - 10} more" if len(names) > 10 else "")
    kind = " ".join(x for x in (area, category.lower()) if x)
    article = "an" if kind[:1].lower() in "aeiou" and kind else "a"
    return (f"{name}, {article} {kind} dish. Ingredients: {shown}. It takes {len(steps)} steps, the first is: "
            f"{steps[0].rstrip('.') if steps else 'not listed'}")


# ---- stocks (Stooq, keyless, delayed) ---------------------------------------
_TICKERS = {"apple": "aapl", "tesla": "tsla", "microsoft": "msft", "google": "googl", "alphabet": "googl",
            "amazon": "amzn", "nvidia": "nvda", "meta": "meta", "facebook": "meta", "netflix": "nflx",
            "intel": "intc", "amd": "amd", "disney": "dis", "coca cola": "ko", "nike": "nke", "boeing": "ba",
            "walmart": "wmt", "visa": "v", "paypal": "pypl", "uber": "uber", "spotify": "spot",
            "sp500": "^spx", "s&p 500": "^spx", "s and p 500": "^spx", "nasdaq": "^ndq", "dow jones": "^dji",
            "dax": "^dax", "bitcoin etf": "ibit"}


def get_stock_price(company: str = "") -> str:
    """Latest price of a stock or index, and how it moved since the open (delayed data)."""
    low = " ".join((company or "").lower().replace(".", " ").split()).replace(" stock", "").replace(" shares", "")
    if not low:
        return "Tell me which company or ticker"
    ticker = _TICKERS.get(low) or (low.replace(" ", "") if re.fullmatch(r"[a-z]{1,6}(\.[a-z]{2})?|\^[a-z]{2,5}", low.replace(" ", "")) else "")
    if not ticker:
        return f"I do not know the ticker for {company}, try the stock symbol"
    if "." not in ticker and not ticker.startswith("^"):
        ticker += ".us"
    try:
        response = requests.get("https://stooq.com/q/l/", params={"s": ticker, "f": "sd2t2ohlcv", "h": "", "e": "csv"},
                                headers=UA, timeout=10)
        lines = response.text.strip().splitlines()
        row = dict(zip(lines[0].lower().split(","), lines[1].split(",")))
        close, opened = float(row["close"]), float(row["open"])
    except (requests.RequestException, IndexError, KeyError, ValueError):
        return f"Sorry, I could not get a price for {company}"
    move = (close - opened) / opened * 100 if opened else 0.0
    unit = " dollars" if ticker.endswith(".us") else " points" if ticker.startswith("^") else ""
    label = company.strip().title() if (low in _TICKERS or " " in low or len(low) > 5) else low.upper()
    return (f"{label} is at {_short(close)}{unit}, "
            f"{'up' if move >= 0 else 'down'} {abs(move):.1f} percent since the open. The data may be delayed")


# ---- this day in history (Wikipedia, keyless) --------------------------------
def today_in_history(day: str = "", count: str = "2") -> str:
    """Events that happened on this day (or another day) in history."""
    today = date.today()
    target = _parse_date(day, today) if (day or "").strip() else today
    if target is None:
        return "Sorry, I did not understand that date"
    try:
        n = max(1, min(4, int(re.search(r"\d+", str(count or "2")).group())))
    except (AttributeError, ValueError):
        n = 2
    data = _get(f"https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/events/{target.month:02d}/{target.day:02d}")
    events = [e for e in (data or {}).get("events", []) if e.get("text") and e.get("year")]
    if not events:
        return "Sorry, I could not look up that day in history"
    picked = sorted(random.sample(events, min(n, len(events))), key=lambda e: e["year"])
    when = "On this day" if target == today else f"On {MONTHS[target.month - 1]} {target.day}"
    return f"{when}, " + "; ".join(f"in {e['year']}, {e['text'].rstrip('.')}" for e in picked)


# ---- trivia ------------------------------------------------------------------
_TRIVIA_BANK = [
    ("What is the largest planet in our solar system?", "Jupiter", ["Saturn", "Neptune", "Earth"]),
    ("Which element has the chemical symbol Au?", "Gold", ["Silver", "Iron", "Copper"]),
    ("How many continents are there?", "Seven", ["Five", "Six", "Eight"]),
    ("What is the capital of Australia?", "Canberra", ["Sydney", "Melbourne", "Perth"]),
    ("Who painted the Mona Lisa?", "Leonardo da Vinci", ["Michelangelo", "Raphael", "Van Gogh"]),
    ("Which ocean is the largest?", "The Pacific", ["The Atlantic", "The Indian", "The Arctic"]),
    ("How many bones does an adult human have?", "206", ["180", "250", "300"]),
    ("In which country were the first modern Olympic Games held in 1896?", "Greece", ["France", "Italy", "England"]),
]
_TRIVIA = {}


def trivia(action: str = "new", guess: str = "") -> str:
    """A multiple-choice question; later check the answer, or reveal it."""
    act = (action or "new").lower()
    guess = (guess or "").strip()
    if guess or act.startswith(("check", "guess", "answer")) and guess:
        if not _TRIVIA:
            return "There is no question waiting, ask me for a trivia question first"
        low = re.sub(r"^(it'?s|its|is it|maybe|i think|option|letter|answer)\s+", "", guess.lower()).strip(" .?!")
        letter = re.fullmatch(r"([a-d])", low)
        right = low == _TRIVIA["answer"].lower() or (letter and letter.group(1).upper() == _TRIVIA["letter"]) \
            or (len(low) > 2 and (low in _TRIVIA["answer"].lower() or _TRIVIA["answer"].lower() in low))
        answer = _TRIVIA["answer"]
        _TRIVIA.clear()
        return f"Correct, it is {answer}" if right else f"Not quite, the answer is {answer}"
    if act.startswith(("reveal", "give", "show", "answer", "skip")):
        if not _TRIVIA:
            return "There is no question waiting"
        answer = _TRIVIA["answer"]
        _TRIVIA.clear()
        return f"The answer is {answer}"
    question = answer = None
    data = _get("https://opentdb.com/api.php", {"amount": 1, "type": "multiple", "encode": "url3986"})
    try:
        item = data["results"][0]
        question, answer = unquote(item["question"]), unquote(item["correct_answer"])
        wrong = [unquote(x) for x in item["incorrect_answers"]]
    except (TypeError, KeyError, IndexError):
        question, answer, wrong = random.choice(_TRIVIA_BANK)
    options = wrong + [answer]
    random.shuffle(options)
    letters = "ABCD"[: len(options)]
    _TRIVIA.update({"answer": answer, "letter": letters[options.index(answer)]})
    listed = "; ".join(f"{l}, {o}" for l, o in zip(letters, options))
    return f"{question} Is it {listed}?"


# ---- birthdays ---------------------------------------------------------------
def _next_birthday(month: int, day: int, today: date) -> date:
    def make(year):
        try:
            return date(year, month, day)
        except ValueError:
            return date(year, 2, 28)
    nxt = make(today.year)
    return nxt if nxt >= today else make(today.year + 1)


def _owner(name: str) -> str:
    return f"{name}'" if name.endswith("s") else f"{name}'s"


def add_birthday(name: str = "", birthday: str = "") -> str:
    """Remember a birthday (with the year to also know the age)."""
    name = (name or "").strip().title()
    if not name or not (birthday or "").strip():
        return "Tell me whose birthday it is and the date"
    parsed = _parse_date(birthday, date.today())
    if parsed is None:
        return "Sorry, I did not understand that date. Try May 12 or 1990-05-12"
    year = parsed.year if re.search(r"\b(19|20)\d{2}\b", birthday) else None
    with _DATA_LOCK:
        data = _data_load()
        people = [b for b in data.get("birthdays", []) if isinstance(b, dict) and b.get("name", "").lower() != name.lower()]
        people.append({"name": name, "month": parsed.month, "day": parsed.day, "year": year})
        data["birthdays"] = people
        _data_save(data)
    return f"Okay, I will remember that {_owner(name)} birthday is on {MONTHS[parsed.month - 1]} {parsed.day}"


def _birthday_line(b: dict, today: date) -> str:
    nxt = _next_birthday(b["month"], b["day"], today)
    days = (nxt - today).days
    when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days, on {_fmt_date(nxt)}"
    age = f", turning {nxt.year - b['year']}" if b.get("year") and nxt.year - b["year"] > 0 else ""
    return f"{_owner(b['name'])} birthday is {when}{age}"


def get_birthdays(who: str = "") -> str:
    """When a person's birthday is, or which birthdays come next."""
    with _DATA_LOCK:
        people = [b for b in _data_load().get("birthdays", []) if isinstance(b, dict) and b.get("name")]
    if not people:
        return "I do not know any birthdays yet. Tell me one, like Anna on May 12"
    today = date.today()
    who = (who or "").strip().lower()
    if who:
        found = [b for b in people if who in b["name"].lower() or b["name"].lower() in who]
        return "; ".join(_birthday_line(b, today) for b in found) if found else f"I do not know a birthday for {who.title()}"
    ordered = sorted(people, key=lambda b: _next_birthday(b["month"], b["day"], today))
    return "; ".join(_birthday_line(b, today) for b in ordered[:3])


def remove_birthday(name: str = "") -> str:
    name = (name or "").strip().lower()
    with _DATA_LOCK:
        data = _data_load()
        people = [b for b in data.get("birthdays", []) if isinstance(b, dict)]
        kept = [b for b in people if name not in b.get("name", "").lower()] if name else people
        if len(kept) == len(people) or not name:
            return f"I do not have a birthday for {name.title()}" if name else "Tell me whose birthday to forget"
        data["birthdays"] = kept
        _data_save(data)
    return f"Okay, I forgot the birthday of {name.title()}"


# ---- expenses ----------------------------------------------------------------
def _amount(text: str):
    found = re.search(r"\d[\d.,]*", str(text or ""))
    if not found:
        return None
    raw = found.group(0).rstrip(".,")
    raw = raw.replace(",", ".") if re.fullmatch(r"\d+,\d{1,2}", raw) else raw.replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def _spent(x: float) -> str:
    unit = _env("EXPENSE_CURRENCY")
    return f"{_money(x)}{' ' + unit if unit else ''}"


def log_expense(amount: str = "", category: str = "", note: str = "") -> str:
    """Write down money spent."""
    value = _amount(amount)
    if value is None or value <= 0:
        return "Tell me how much you spent"
    category = (category or "other").strip().lower() or "other"
    with _DATA_LOCK:
        data = _data_load()
        items = [e for e in data.get("expenses", []) if isinstance(e, dict)]
        items.append({"amount": value, "category": category, "note": (note or "").strip(), "time": time.time()})
        data["expenses"] = items
        _data_save(data)
    now = date.today()
    month = sum(e["amount"] for e in items if date.fromtimestamp(e["time"]).replace(day=1) == now.replace(day=1))
    return f"Okay, I logged {_spent(value)} for {category}. That is {_spent(month)} this month"


def _period_start(period: str, today: date):
    low = (period or "month").lower()
    if "today" in low:
        return today, "today"
    if "yesterday" in low:
        return today - timedelta(days=1), "yesterday"
    if "week" in low:
        return today - timedelta(days=today.weekday()), "this week"
    if "year" in low:
        return today.replace(month=1, day=1), "this year"
    if "all" in low or "ever" in low or "total" in low:
        return date.min, "in total"
    return today.replace(day=1), "this month"


def spending_summary(period: str = "month", category: str = "") -> str:
    """How much was spent today, this week, month or year, and on what."""
    today = date.today()
    start, label = _period_start(period, today)
    with _DATA_LOCK:
        items = [e for e in _data_load().get("expenses", []) if isinstance(e, dict)]
    chosen = [e for e in items if start <= date.fromtimestamp(e["time"]) <= (today if "yesterday" not in label else start)]
    wanted = (category or "").strip().lower()
    if wanted:
        chosen = [e for e in chosen if wanted in e.get("category", "")]
    if not chosen:
        return f"You have no expenses {label}" + (f" for {wanted}" if wanted else "")
    total = sum(e["amount"] for e in chosen)
    text = f"You spent {_spent(total)} {label}" + (f" on {wanted}" if wanted else "") + f", in {len(chosen)} {'purchase' if len(chosen) == 1 else 'purchases'}"
    if not wanted:
        by = {}
        for e in chosen:
            by[e["category"]] = by.get(e["category"], 0) + e["amount"]
        top = sorted(by.items(), key=lambda kv: kv[1], reverse=True)[:3]
        if len(top) > 1:
            text += ". Most of it went on " + _join([f"{c} {_spent(a)}" for c, a in top])
    return text


def undo_last_expense() -> str:
    with _DATA_LOCK:
        data = _data_load()
        items = [e for e in data.get("expenses", []) if isinstance(e, dict)]
        if not items:
            return "You have no expenses to undo"
        last = items.pop()
        data["expenses"] = items
        _data_save(data)
    return f"Okay, I removed the last expense, {_spent(last['amount'])} for {last['category']}"


# ---- sleep, 8 ball, words, quotes ---------------------------------------------
def _clock_time(text: str):
    low = (text or "").lower().strip()
    if low in ("now", "right now", "at once"):
        n = datetime.now()
        return n.hour, n.minute
    m = re.search(r"(\d{1,2})(?:[:.h](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", low)
    if not m:
        return None
    hour, minute, mer = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(".", "")
    if mer == "pm" and hour < 12:
        hour += 12
    elif mer == "am" and hour == 12:
        hour = 0
    return (hour, minute) if hour < 24 and minute < 60 else None


def sleep_calculator(wake_time: str = "", bedtime: str = "") -> str:
    """Best bedtimes for a wake-up time, or best wake-up times for a bedtime (90 minute sleep cycles, 15 to fall asleep)."""
    def fmt(minutes: int) -> str:
        minutes %= 1440
        return _clock(minutes // 60, minutes % 60)
    if (wake_time or "").strip():
        t = _clock_time(wake_time)
        if not t:
            return "I did not understand the wake-up time"
        base = t[0] * 60 + t[1]
        options = [(fmt(base - (c * 90 + 15)), c) for c in (6, 5, 4)]
        return ("to wake up at " + fmt(base) + " go to sleep at "
                + "; ".join(f"{when} for {c * 1.5:g} hours of sleep" for when, c in options))
    t = _clock_time(bedtime or "now")
    if not t:
        return "I did not understand the bedtime"
    base = t[0] * 60 + t[1]
    options = [(fmt(base + 15 + c * 90), c) for c in (4, 5, 6)]
    return ("if you fall asleep at " + fmt(base) + " the best times to wake up are "
            + "; ".join(f"{when} after {c * 1.5:g} hours" for when, c in options))


_EIGHT_BALL = ["It is certain", "Without a doubt", "Yes, definitely", "You may rely on it", "Most likely",
               "Outlook good", "Signs point to yes", "Yes", "Reply hazy, try again", "Ask again later",
               "Better not tell you now", "Cannot predict now", "Concentrate and ask again",
               "Do not count on it", "My reply is no", "My sources say no", "Outlook not so good", "Very doubtful"]


def magic_8ball(question: str = "") -> str:
    return random.choice(_EIGHT_BALL)


_WORD_KINDS = (("rhym", "rel_rhy", "rhymes with"), ("antonym", "rel_ant", "opposites of"), ("opposite", "rel_ant", "opposites of"),
               ("sounds like", "sl", "words that sound like"), ("sound", "sl", "words that sound like"),
               ("means like", "ml", "words that mean something like"), ("related", "ml", "words related to"),
               ("synonym", "rel_syn", "synonyms for"))


def word_finder(word: str = "", kind: str = "synonyms") -> str:
    """Synonyms, opposites, rhymes or similar-sounding words."""
    word = (word or "").strip().lower()
    if not word:
        return "Tell me which word"
    low = (kind or "synonyms").lower()
    param, label = next(((p, l) for key, p, l in _WORD_KINDS if key in low), ("rel_syn", "synonyms for"))
    data = _get("https://api.datamuse.com/words", {param: word, "max": 8})
    words = [d["word"] for d in (data or []) if isinstance(d, dict) and d.get("word")]
    if not words:
        return f"I could not find {label} {word}"
    return f"{label} {word}: {_join(words[:7])}"


_QUOTES = [("The only way to do great work is to love what you do", "Steve Jobs"),
           ("It always seems impossible until it is done", "Nelson Mandela"),
           ("Well done is better than well said", "Benjamin Franklin"),
           ("You miss one hundred percent of the shots you do not take", "Wayne Gretzky"),
           ("The best time to plant a tree was twenty years ago, the second best time is now", "Chinese proverb")]


def inspire_me() -> str:
    """A short quote."""
    data = _get("https://zenquotes.io/api/random")
    try:
        quote_, author = data[0]["q"].strip().rstrip("."), data[0]["a"].strip()
        if quote_ and author and "too many requests" not in quote_.lower():
            return f"{quote_}, {'says the' if 'proverb' in author.lower() else 'said'} {author}"
    except (TypeError, KeyError, IndexError, AttributeError):
        pass
    quote_, author = random.choice(_QUOTES)
    return f"{quote_}, {'says the' if 'proverb' in author.lower() else 'said'} {author}"


# =============================================================================
# 20 MORE TOOLS: live data and handy calculators
# Keyless services: USGS, wheretheiss.at, CountriesNow, ipify, GitHub,
# PyPI / npm, TheCocktailDB, Open Library, MusicBrainz, AniList, PokeAPI,
# catfact.ninja, Wikipedia and Open-Meteo.
# Results are written to be spoken: short, no emoji, no trailing period.
# =============================================================================
def _time_ago(stamp: float) -> str:
    """'12 minutes ago' for an epoch timestamp."""
    try:
        seconds = max(0.0, time.time() - float(stamp))
    except (TypeError, ValueError, OverflowError):
        return ""
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{round(seconds / 60)} minutes ago"
    if seconds < 86400:
        hours = round(seconds / 3600)
        return f"{hours} {'hour' if hours == 1 else 'hours'} ago"
    days = int(seconds // 86400)
    return "yesterday" if days == 1 else f"{days} days ago"


def _reverse_place(latitude, longitude) -> str:
    """A rough place name for coordinates, or '' when it cannot be looked up."""
    try:
        response = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"format": "jsonv2", "lat": latitude, "lon": longitude,
                    "zoom": 3, "addressdetails": 1},
            headers={**UA, "Accept-Language": "en"}, timeout=8,
        )
        if response.status_code != 200:
            return ""
        data = response.json()
        address = data.get("address") or {}
        for key in ("country", "state", "city", "town", "village", "county", "ocean", "sea"):
            if address.get(key):
                return address[key]
        return (data.get("display_name") or "").split(",")[0].strip()
    except (requests.RequestException, ValueError):
        return ""


def _post_json(url: str, payload: dict, headers: dict = None):
    """POST a JSON document. Returns None on any failure, like _get."""
    try:
        response = requests.post(url, json=payload, headers={**UA, **(headers or {})}, timeout=15)
        if response.status_code != 200:
            return None
        return response.json()
    except (requests.RequestException, ValueError):
        return None


# ---- live data --------------------------------------------------------------
def get_earthquakes(period: str = "day", count: str = "3") -> str:
    """Recent notable earthquakes from the USGS feed."""
    low = (period or "day").lower()
    window = "week" if "week" in low else "month" if "month" in low else "day"
    try:
        n = max(1, min(5, int(re.search(r"\d+", str(count or "3")).group())))
    except (AttributeError, ValueError):
        n = 3
    data = _get(f"https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_{window}.geojson")
    features = (data or {}).get("features") or []
    if not features:
        return f"I could not find any notable earthquakes in the last {window}"
    items = []
    for feature in features[:n]:
        props = feature.get("properties") or {}
        magnitude = props.get("mag")
        if not isinstance(magnitude, (int, float)):
            continue
        place = (props.get("place") or "an unknown place").strip()
        stamp = props.get("time")
        when = _time_ago(stamp / 1000) if isinstance(stamp, (int, float)) else ""
        items.append(f"magnitude {_num(round(magnitude, 1))} near {place}" + (f", {when}" if when else ""))
    if not items:
        return "Sorry, I could not read the earthquake report"
    return f"Notable earthquakes in the last {window}: " + "; ".join(items)


def iss_location() -> str:
    """Where the International Space Station is right now."""
    data = _get("https://api.wheretheiss.at/v1/satellites/25544")
    if not data:
        return "Sorry, I could not find the space station"
    try:
        latitude, longitude = float(data["latitude"]), float(data["longitude"])
    except (KeyError, TypeError, ValueError):
        return "Sorry, I could not read the space station position"
    text = (f"the International Space Station is at {abs(latitude):.1f} degrees "
            f"{'north' if latitude >= 0 else 'south'}, {abs(longitude):.1f} degrees "
            f"{'east' if longitude >= 0 else 'west'}")
    place = _reverse_place(latitude, longitude)
    if place:
        text += f", over {place}"
    speed = data.get("velocity")
    if isinstance(speed, (int, float)):
        text += f", moving at about {_num(round(speed))} kilometers per hour"
    return text


_COUNTRY_LIST = []


def _country_rows() -> list:
    """The CountriesNow country list, fetched once per process."""
    global _COUNTRY_LIST
    if not _COUNTRY_LIST:
        data = _get("https://countriesnow.space/api/v0.1/countries/info",
                    {"returns": "currency,capital"})
        _COUNTRY_LIST = (data or {}).get("data") or []
    return _COUNTRY_LIST


def country_info(country: str = "") -> str:
    """Capital, population and currency of a country."""
    name = (country or "").strip()
    if not name:
        return "Tell me which country"
    rows = _country_rows()
    low = name.lower()
    match = next((r for r in rows if str(r.get("name", "")).lower() == low), None)
    if match is None:
        match = next((r for r in rows if low in str(r.get("name", "")).lower()
                      or str(r.get("name", "")).lower() in low), None)
    if match is None:
        return f"I could not find a country called {name}"
    common = match.get("name") or name.title()
    capital = match.get("capital") or "no capital listed"
    currency = match.get("currency") or ""
    parts = [f"{common}, capital {capital}"]
    population = _post_json("https://countriesnow.space/api/v0.1/countries/population",
                            {"country": common})
    counts = ((population or {}).get("data") or {}).get("populationCounts") or []
    latest = max((c for c in counts if isinstance(c, dict)),
                 key=lambda c: c.get("year", 0), default=None)
    value = (latest or {}).get("value")
    if isinstance(value, (int, float)):
        parts.append(f"about {_num(round(value))} people")
    if currency:
        parts.append(f"currency {_CURRENCY_NAMES.get(currency, currency)}")
    return ", ".join(parts)


def public_ip() -> str:
    """The user's public IP address."""
    data = _get("https://api.ipify.org", {"format": "json"})
    address = (data or {}).get("ip")
    if not address:
        return "Sorry, I could not find your public IP address"
    return f"your public IP address is {address}"


def website_status(site: str = "") -> str:
    """Check whether a website answers, and how quickly."""
    raw = (site or "").strip()
    if not raw:
        return "Tell me which website to check"
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if not parsed.netloc:
        return f"I could not read the address {site}"
    started = time.time()
    try:
        response = requests.get(raw, headers=UA, timeout=10, allow_redirects=True, stream=True)
    except requests.RequestException:
        return f"{parsed.netloc} is not responding"
    elapsed = (time.time() - started) * 1000
    code = response.status_code
    response.close()
    if 200 <= code < 400:
        return f"{parsed.netloc} is up, HTTP {code}, answered in {round(elapsed)} milliseconds"
    if 400 <= code < 500:
        return f"{parsed.netloc} is reachable but returned HTTP {code}"
    return f"{parsed.netloc} has a server problem, HTTP {code}"


def github_repo(repo: str = "") -> str:
    """Stars, forks, issues and language of a GitHub repository."""
    raw = (repo or "").strip()
    if not raw:
        return "Tell me which repository, like python/cpython"
    match = re.search(r"github\.com/([^/\s]+)/([^/\s#?]+)", raw, re.I)
    if match:
        owner, name = match.group(1), match.group(2)
    elif "/" in raw:
        owner, name = raw.split("/", 1)
        name = name.split()[0]
    else:
        return "Tell me the repository as owner and name, like python/cpython"
    name = name.removesuffix(".git")
    data = _get(f"https://api.github.com/repos/{quote(owner)}/{quote(name)}",
                headers={"Accept": "application/vnd.github+json"})
    if not data:
        return f"I could not find the GitHub repository {owner}/{name}"
    full = data.get("full_name") or f"{owner}/{name}"
    parts = [f"{full} has {_num(data.get('stargazers_count', 0))} stars "
             f"and {_num(data.get('forks_count', 0))} forks"]
    issues = data.get("open_issues_count")
    if isinstance(issues, int):
        parts.append(f"{_num(issues)} open issues")
    parts.append(f"mainly {data.get('language') or 'no single language'}")
    description = (data.get("description") or "").strip().rstrip(".")
    if description:
        parts.append(description)
    pushed = data.get("pushed_at")
    if pushed:
        try:
            stamp = datetime.fromisoformat(pushed.replace("Z", "+00:00")).timestamp()
            parts.append(f"last push {_time_ago(stamp)}")
        except (ValueError, OverflowError):
            pass
    return ", ".join(parts)


def _pypi_info(name: str):
    data = _get(f"https://pypi.org/pypi/{quote(name)}/json")
    info = (data or {}).get("info") or {}
    version = info.get("version")
    if not version:
        return None
    text = f"the Python package {info.get('name') or name} is at version {version}"
    summary = (info.get("summary") or "").strip().rstrip(".")
    author = (info.get("author") or "").strip()
    if summary:
        text += f", {summary}"
    if author:
        text += f", by {author}"
    return text


def _npm_info(name: str, quiet: bool = False):
    data = _get(f"https://registry.npmjs.org/{quote(name, safe='@')}/latest")
    version = (data or {}).get("version")
    if not version:
        return None if quiet else f"I could not find the npm package {name}"
    text = f"the npm package {data.get('name') or name} is at version {version}"
    description = (data.get("description") or "").strip().rstrip(".")
    license_ = (data.get("license") or "").strip()
    if description:
        text += f", {description}"
    if license_:
        text += f", license {license_}"
    return text


def package_info(name: str = "", ecosystem: str = "pypi") -> str:
    """Latest version and summary of a Python or JavaScript package."""
    package = (name or "").strip()
    if not package:
        return "Tell me which package"
    low = (ecosystem or "pypi").lower()
    if low.startswith(("npm", "node", "javascript", "js")):
        return _npm_info(package)
    return _pypi_info(package) or _npm_info(package, quiet=True) \
        or f"I could not find a package called {package}"


def cocktail_recipe(drink: str = "", part: str = "overview") -> str:
    """A cocktail recipe: overview, ingredients, or the steps."""
    name = (drink or "").strip()
    data = _get("https://www.thecocktaildb.com/api/json/v1/1/" + ("search.php" if name else "random.php"),
                {"s": name} if name else None)
    drinks = (data or {}).get("drinks") or []
    if not drinks:
        return f"I could not find a cocktail called {name}" if name else "Sorry, I could not get a cocktail"
    item = drinks[0]
    ingredients = []
    for i in range(1, 16):
        ingredient = (item.get(f"strIngredient{i}") or "").strip()
        if ingredient:
            measure = (item.get(f"strMeasure{i}") or "").strip()
            ingredients.append(f"{measure} {ingredient}".strip())
    title = item.get("strDrink") or name or "this cocktail"
    instructions = (item.get("strInstructions") or "").strip()
    low = (part or "overview").lower()
    if low.startswith("ingr"):
        return f"for a {title} you need " + "; ".join(ingredients)
    if low.startswith(("step", "method", "instr", "how", "direction")):
        steps = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\r?\n+", instructions) if len(s.strip()) > 3]
        listed = " ".join(f"Step {i + 1}: {s.rstrip('.')}." for i, s in enumerate(steps[:6]))
        return f"{title}. {listed}" if listed else f"I have no instructions for a {title}"
    kind = " ".join(x for x in (item.get("strCategory"), item.get("strAlcoholic"), item.get("strGlass")) if x)
    first = _first_sentences(instructions, 2, 300)
    return f"{title}, {kind}. You need {_join(ingredients[:8])}." + (f" {first}" if first else "")


def book_search(query: str = "", detail: str = "short") -> str:
    """Find a book and say who wrote it and what it is about."""
    query = (query or "").strip()
    if not query:
        return "Tell me which book or author"
    data = _get("https://openlibrary.org/search.json",
                {"q": query, "limit": 1,
                 "fields": "title,author_name,first_publish_year,subject,edition_count"})
    docs = (data or {}).get("docs") or []
    if not docs:
        return f"I could not find a book matching {query}"
    book = docs[0]
    title = book.get("title") or query.title()
    authors = ", ".join((book.get("author_name") or [])[:3]) or "an unknown author"
    year = book.get("first_publish_year")
    text = f"{title} by {authors}"
    if year:
        text += f", first published in {year}"
    subjects = [s for s in (book.get("subject") or []) if s and len(s) < 30][:5]
    if subjects:
        text += f". It is about {_join(subjects)}"
    if str(detail or "").lower().startswith(("l", "d", "m", "f")):
        editions = book.get("edition_count")
        if editions:
            text += f". There are {_num(editions)} editions"
    return text


def music_artist(name: str = "") -> str:
    """Country, active years and style of a music artist or band."""
    artist = (name or "").strip()
    if not artist:
        return "Tell me which artist or band"
    data = _get("https://musicbrainz.org/ws/2/artist/",
                {"query": f'artist:"{artist}"', "fmt": "json", "limit": 1},
                headers={"Accept": "application/json"})
    artists = (data or {}).get("artists") or []
    if not artists:
        return f"I could not find an artist called {artist}"
    item = artists[0]
    display = item.get("name") or artist
    kind = (item.get("type") or "artist").lower()
    area = (item.get("area") or {}).get("name") or item.get("country") or ""
    life = item.get("life-span") or {}
    begin, end = life.get("begin"), life.get("end")
    text = f"{display} is a {kind}"
    if area:
        text += f" from {area}"
    if kind == "person":
        if begin and end:
            text += f", lived from {begin[:4]} to {end[:4]}"
        elif begin:
            text += f", born in {begin[:4]}"
    elif begin and end:
        text += f", active from {begin[:4]} to {end[:4]}"
    elif begin:
        text += f", active since {begin[:4]}"
    tags = [t.get("name") for t in (item.get("tags") or []) if t.get("name")][:3]
    if tags:
        text += f", known for {_join(tags)}"
    return text


_ANILIST_QUERY = (
    "query ($search: String) { Media(search: $search, type: ANIME) { "
    "title { english romaji } startDate { year } episodes averageScore "
    "description(asHtml: false) } }"
)


def anime_info(title: str = "") -> str:
    """Year, episode count, rating and synopsis of an anime."""
    title = (title or "").strip()
    if not title:
        return "Tell me which anime"
    data = _post_json("https://graphql.anilist.co",
                      {"query": _ANILIST_QUERY, "variables": {"search": title}})
    media = ((data or {}).get("data") or {}).get("Media")
    if not media:
        return f"I could not find an anime called {title}"
    titles = media.get("title") or {}
    name = titles.get("english") or titles.get("romaji") or title
    year = (media.get("startDate") or {}).get("year")
    text = name
    if year:
        text += f", from {year}"
    episodes = media.get("episodes")
    if isinstance(episodes, int):
        text += f", {_num(episodes)} episodes"
    score = media.get("averageScore")
    if isinstance(score, (int, float)):
        text += f", rated {score / 10:.1f} out of 10"
    synopsis = re.sub(r"\(Source:.*$", "", _clean_html(media.get("description") or ""),
                      flags=re.I | re.S).strip()
    if synopsis:
        text += f". {_first_sentences(synopsis, 2, 350)}"
    return text


def pokemon_info(name: str = "") -> str:
    """Type, size, abilities and a short Pokedex description of a Pokemon."""
    pokemon = re.sub(r"[^a-z0-9-]+", "-", (name or "").strip().lower()).strip("-")
    if not pokemon:
        return "Tell me which Pokemon"
    data = _get(f"https://pokeapi.co/api/v2/pokemon/{quote(pokemon)}")
    if not data:
        return f"I could not find a Pokemon called {name}"
    species = _get(f"https://pokeapi.co/api/v2/pokemon-species/{quote(pokemon)}") or {}
    display = (species.get("name") or data.get("name") or name).replace("-", " ").title()
    types = [t.get("type", {}).get("name") for t in (data.get("types") or []) if t.get("type")]
    abilities = [a.get("ability", {}).get("name", "").replace("-", " ")
                 for a in (data.get("abilities") or []) if a.get("ability")]
    parts = [f"{display} is a {' and '.join(types) if types else 'unknown'} type"]
    height = data.get("height")
    weight = data.get("weight")
    if isinstance(height, (int, float)):
        parts.append(f"{height / 10:g} meters tall")
    if isinstance(weight, (int, float)):
        parts.append(f"{weight / 10:g} kilograms")
    if abilities:
        parts.append(f"abilities {_join(abilities[:3])}")
    flavor = ""
    for entry in species.get("flavor_text_entries") or []:
        if (entry.get("language") or {}).get("name") == "en":
            flavor = _clean_html(entry.get("flavor_text") or "")
            break
    if flavor:
        parts.append(_first_sentences(flavor, 1, 200))
    return ", ".join(parts)


_NUMBER_FACTS = [
    "forty two is the answer to life, the universe and everything",
    "a million seconds is about eleven and a half days",
    "zero is the only number that cannot be represented in Roman numerals",
    "twelve is the smallest number with exactly six divisors",
    "one hundred and eleven million one hundred and eleven thousand one hundred and eleven is a perfect square",
]


def _local_number_fact(raw: str) -> str:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            return random.choice(_NUMBER_FACTS)
    if value == 0:
        return "zero is neither positive nor negative"
    properties = []
    if value < 0:
        properties.append("negative")
    properties.append("even" if value % 2 == 0 else "odd")
    small = 1 < value <= 10_000_000      # keep the checks quick for huge numbers
    if small and all(value % i for i in range(2, math.isqrt(value) + 1)):
        properties.append("prime")
    if value > 0:
        root = math.isqrt(value)
        if root * root == value:
            properties.append(f"a perfect square, {root} squared")
        if small:
            divisors = sum(1 for i in range(1, math.isqrt(value) + 1) if value % i == 0) * 2
            if root * root == value:
                divisors -= 1
            properties.append(f"has {divisors} divisors")
    text = f"{value} is " + _join(properties)
    special = {
        42: "it is the answer to life, the universe and everything",
        7: "there are seven days in a week",
        12: "there are twelve months in a year",
        13: "some people call it an unlucky number",
        100: "it is ten squared",
        365: "there are about 365 days in a year",
    }.get(value)
    return f"{text}. {special[:1].upper()}{special[1:]}" if special else text


def number_fact(number: str = "") -> str:
    """A fun fact about a number, or a random number fact."""
    raw = re.sub(r"[^0-9.-]", "", str(number or ""))
    return _local_number_fact(raw) if raw else random.choice(_NUMBER_FACTS)


_CAT_FACTS = [
    "cats have five toes on their front paws and four on their back paws",
    "a group of cats is called a clowder",
    "cats cannot taste sweetness",
    "cats spend about seventy percent of their lives asleep",
    "a cat's nose print is unique, like a fingerprint",
]


def cat_fact() -> str:
    """A random fact about cats."""
    data = _get("https://catfact.ninja/fact")
    fact = (data or {}).get("fact")
    return (fact or random.choice(_CAT_FACTS)).strip().rstrip(".")


def random_wikipedia() -> str:
    """A summary of a random Wikipedia article."""
    data = _get("https://en.wikipedia.org/api/rest_v1/page/random/summary")
    if not data or not data.get("extract"):
        return "Sorry, I could not find a random article"
    title = data.get("title") or "this article"
    text = _first_sentences(re.sub(r"\s*\([^)]*\)", "", data["extract"]), 2, 400)
    return f"{title}. {text}".strip()


_POLLEN_NAMES = {
    "alder_pollen": "alder", "birch_pollen": "birch", "grass_pollen": "grass",
    "mugwort_pollen": "mugwort", "olive_pollen": "olive", "ragweed_pollen": "ragweed",
}


def _pollen_values(place: dict) -> list:
    keys = ",".join(_POLLEN_NAMES)
    params = {"latitude": place["latitude"], "longitude": place["longitude"],
              "current": keys, "timezone": "auto"}
    current = (_get("https://air-quality-api.open-meteo.com/v1/air-quality", params) or {}).get("current") or {}
    values = [(label, current.get(key)) for key, label in _POLLEN_NAMES.items()
              if isinstance(current.get(key), (int, float))]
    if values:
        return values
    hourly = (_get("https://air-quality-api.open-meteo.com/v1/air-quality",
                   {"latitude": place["latitude"], "longitude": place["longitude"],
                    "hourly": keys, "forecast_days": 1, "timezone": "auto"}) or {}).get("hourly") or {}
    for key, label in _POLLEN_NAMES.items():
        numbers = [x for x in (hourly.get(key) or []) if isinstance(x, (int, float))]
        if numbers:
            values.append((label, numbers[0]))
    return values


def get_pollen(location: str = "") -> str:
    """Pollen levels for trees, grass and weeds."""
    place = _geocode(location)
    if not place:
        return _no_place(location)
    values = _pollen_values(place)
    if not values:
        return "Sorry, I could not get the pollen levels there"
    name = place.get("name") or "your area"
    high = max(values, key=lambda item: item[1])
    if high[1] < 1:
        return f"pollen levels are low in {name}"
    listed = ", ".join(f"{label} {round(value)}" for label, value in values if value >= 1)
    return f"pollen in {name}: {listed} grains per cubic meter, highest is {high[0]}"


# ---- handy calculators -------------------------------------------------------
def _weight_kg(text: str):
    low = str(text or "").lower()
    value = _amount(low)
    if value is None:
        return None
    if re.search(r"\b(lb|lbs|pound|pounds)\b", low):
        return value * 0.45359237
    if re.search(r"\b(st|stone|stones)\b", low):
        return value * 6.35029318
    return value


def _height_meters(text: str):
    low = str(text or "").lower()
    feet = re.search(r"(\d+(?:\.\d+)?)\s*(?:ft|feet|foot|')", low)
    inches = re.search(r"(\d+(?:\.\d+)?)\s*(?:in|inch|inches|\")", low)
    if feet:
        total = float(feet.group(1)) * 12 + (float(inches.group(1)) if inches else 0)
        return total * 0.0254
    value = _amount(low)
    if value is None:
        return None
    if re.search(r"\b(cm|centimeter|centimeters)\b", low):
        return value / 100
    if re.search(r"\b(in|inch|inches)\b|\"", low):
        return value * 0.0254
    return value / 100 if value > 3 else value


def bmi(weight: str = "", height: str = "", units: str = "metric") -> str:
    """Body mass index from weight and height, with the category."""
    low = (units or "").lower()
    imperial = any(word in low for word in ("imperial", "pound", "lb", "inch", "feet", "ft"))
    if not imperial:
        imperial = bool(re.search(r"\b(lbs?|pounds?|inches?|feet|ft)\b",
                                 f"{weight} {height}".lower()))
    if imperial:
        if not re.search(r"\b(lb|lbs|pound|pounds|kg|kilo|stone)\b", str(weight).lower()):
            weight = f"{weight} pounds"
        if not re.search(r"\b(cm|centimeter|meters?|m|ft|feet|foot|in|inch|inches)\b", str(height).lower()):
            height = f"{height} inches"
    kg, meters = _weight_kg(weight), _height_meters(height)
    if kg is None or meters is None or kg <= 0 or meters <= 0:
        return "Tell me your weight and height, for example 80 kilos and 180 centimeters"
    if not 0.5 <= meters <= 2.5:
        return "That height does not look right, tell me centimeters or meters"
    value = kg / (meters ** 2)
    if value < 18.5:
        label = "underweight"
    elif value < 25:
        label = "a healthy weight"
    elif value < 30:
        label = "overweight"
    else:
        label = "obese"
    return f"your body mass index is {_short(value)}, which is {label}"


def daily_calories(weight: str = "", height: str = "", age: str = "", sex: str = "female",
                   activity: str = "moderate", units: str = "metric") -> str:
    """Estimated daily calories from weight, height, age, sex and activity."""
    unit_low = (units or "").lower()
    imperial = any(word in unit_low for word in ("imperial", "pound", "lb", "inch", "feet", "ft"))
    if not imperial:
        imperial = bool(re.search(r"\b(lbs?|pounds?|inches?|feet|ft)\b",
                                 f"{weight} {height}".lower()))
    if imperial:
        if not re.search(r"\b(lb|lbs|pound|pounds|kg|kilo|stone)\b", str(weight).lower()):
            weight = f"{weight} pounds"
        if not re.search(r"\b(cm|centimeter|meters?|m|ft|feet|foot|in|inch|inches)\b", str(height).lower()):
            height = f"{height} inches"
    kg, meters = _weight_kg(weight), _height_meters(height)
    years = _amount(age)
    if kg is None or meters is None or years is None or kg <= 0 or meters <= 0 or years <= 0:
        return "Tell me your weight, height and age, for example 80 kilos, 180 centimeters and 30 years"
    if not 0.5 <= meters <= 2.5:
        return "That height does not look right, tell me centimeters or meters, or say imperial"
    male = str(sex or "").lower().startswith(("m", "man", "male", "boy"))
    bmr = 10 * kg + 6.25 * (meters * 100) - 5 * years + (5 if male else -161)
    low = (activity or "moderate").lower()
    factor, label = 1.55, "moderate"
    for key, value, name in (("very active", 1.9, "very active"), ("extra", 1.9, "very active"),
                             ("sedentary", 1.2, "sedentary"), ("light", 1.375, "light"),
                             ("moderate", 1.55, "moderate"), ("active", 1.725, "active")):
        if key in low:
            factor, label = value, name
            break
    assumed = "" if str(sex or "").strip() else ", assuming female"
    return (f"your basal metabolic rate is about {_num(round(bmr))} calories, and with "
            f"{label} activity you need about {_num(round(bmr * factor))} a day{assumed}")


def _distance_km(text: str):
    low = " ".join(str(text or "").lower().replace(",", "").split())
    if not low:
        return None
    if "half marathon" in low or "half-marathon" in low:
        return 21.0975
    if "marathon" in low:
        return 42.195
    if re.search(r"\b5\s*k\b|\b5k\b", low):
        return 5.0
    if re.search(r"\b10\s*k\b|\b10k\b", low):
        return 10.0
    value = _amount(low)
    if value is None:
        return None
    if re.search(r"\b(miles?|mi)\b", low):
        return value * 1.609344
    return value


def _time_seconds(text: str):
    low = " ".join(str(text or "").lower().split())
    if not low:
        return None
    match = re.fullmatch(r"(\d+):(\d{1,2})(?::(\d{2}))?", low)
    if match:
        if match.group(3) is not None:
            return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3))
        first, second = int(match.group(1)), int(match.group(2))
        return first * 3600 + second * 60 if first <= 6 else first * 60 + second
    seconds = parse_duration(low, bare_minutes=True)
    return int(seconds) if seconds else None


def pace_calculator(distance: str = "", time_text: str = "", unit: str = "km") -> str:
    """Running pace and speed from a distance and a time."""
    km = _distance_km(distance)
    seconds = _time_seconds(time_text)
    if not km or not seconds or km <= 0 or seconds <= 0:
        return "Tell me the distance and the time, for example 10 kilometers in 50 minutes"
    low = (unit or "km").lower()
    if "mile" in low or "mi" in low:
        pace_seconds = seconds / (km / 1.609344)
        pace_unit = "mile"
    else:
        pace_seconds = seconds / km
        pace_unit = "kilometer"
    speed = km / (seconds / 3600)
    return (f"{_num(round(km, 2))} kilometers in {say_duration(seconds, exact=True)} is "
            f"{say_duration(pace_seconds, exact=True)} per {pace_unit}, about "
            f"{_short(speed)} kilometers per hour")


def fuel_cost(distance: str = "", consumption: str = "", price: str = "",
              units: str = "metric") -> str:
    """Fuel needed and cost for a trip."""
    km = _distance_km(distance)
    use = _amount(consumption)
    cost = _amount(price)
    if km is None or use is None or cost is None or km <= 0 or use <= 0 or cost <= 0:
        return "Tell me the distance, the fuel use and the fuel price, for example 300 kilometers, 6.5 liters and 1.50"
    low = (units or "").lower()
    imperial = any(word in low for word in ("imperial", "gallon", "mpg", "mile"))
    if not imperial:
        imperial = bool(re.search(r"\b(mpg|gallons?|miles?)\b",
                                  f"{distance} {consumption} {price}".lower()))
    currency = _env("EXPENSE_CURRENCY")
    suffix = f" {currency}" if currency else ""
    if imperial:
        miles = km / 1.609344
        gallons = miles / use
        return (f"that trip is about {_num(round(miles))} miles, needs about "
                f"{_short(gallons)} gallons of fuel and costs about {_short(gallons * cost)}{suffix}")
    liters = km * use / 100
    return (f"that trip needs about {_short(liters)} liters of fuel and costs about "
            f"{_short(liters * cost)}{suffix}")


# =============================================================================
# MORE TOOLS (3): calendar, habits, time zones, money maths, sports, smart home,
# wake on lan, cooking timers, ingredients, nutrition, name days, workouts
# =============================================================================
def _when_parse(text: str, today: date = None):
    """('tomorrow at 3 PM', 'Friday 18:30', 'October 30 at 9') -> (date or None, (hour, minute) or None)."""
    today = today or date.today()
    low = " ".join((text or "").lower().replace(",", " ").split())
    at = None
    m = (re.search(r"\b(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", low) or re.search(r"\b(?:at\s+)?\d{1,2}:\d{2}\b", low)
         or re.search(r"\bat\s+\d{1,2}\b", low))
    if m:
        at = _clock_time(m.group(0).replace("at", "").strip())
        low = (low[:m.start()] + " " + low[m.end():]).strip()
    low = re.sub(r"\b(on|the|at)\b", " ", low)
    low = " ".join(low.split())
    if not low or low == "today":
        return today, at
    day = next((i for i, w in enumerate(WEEKDAYS) if w.lower() in low), None)
    if day is not None:
        gap = (day - today.weekday()) % 7
        now = datetime.now()
        if gap == 0 and at and (at[0], at[1]) <= (now.hour, now.minute):
            gap = 7
        return today + timedelta(days=gap + (7 if "next" in low and gap < 7 and gap > 0 else 0)), at
    m = re.search(r"in (\d+) (day|week)s?", low)
    if m:
        return today + timedelta(days=int(m.group(1)) * (7 if m.group(2) == "week" else 1)), at
    return _parse_date(low, today), at


def _events(data: dict) -> list:
    return [e for e in data.get("events", []) if isinstance(e, dict) and e.get("title") and e.get("date")]


def add_event(title: str = "", when: str = "") -> str:
    """Put an appointment in the calendar."""
    title = (title or "").strip()
    d, at = _when_parse(when)
    if not title:
        return "Tell me what the event is"
    if d is None:
        return "Sorry, I did not understand when. Try tomorrow at 3 PM or October 30 at 6 PM"
    if d < date.today():
        return "That date is already in the past"
    with _DATA_LOCK:
        data = _data_load()
        keep = [e for e in _events(data) if e["date"] >= (date.today() - timedelta(days=30)).isoformat()]
        keep.append({"title": title, "date": d.isoformat(), "time": f"{at[0]:02d}:{at[1]:02d}" if at else ""})
        data["events"] = keep
        _data_save(data)
    return f"Okay, {title} is in your calendar on {_fmt_date(d)}" + (f" at {_clock(*at)}" if at else "")


def _event_line(e: dict) -> str:
    h = e.get("time")
    return f"{_clock(int(h[:2]), int(h[3:]))} {e['title']}" if h else e["title"]


def get_events(day: str = "today") -> str:
    """What is in the calendar today, tomorrow, on a day, or in the coming week."""
    low = (day or "today").lower()
    today = date.today()
    with _DATA_LOCK:
        events = _events(_data_load())
    if re.search(r"week|coming|upcoming|next days|soon", low):
        days = [today + timedelta(days=i) for i in range(7)]
    else:
        d, _ = _when_parse(low)
        if d is None:
            return "Sorry, I did not understand which day"
        days = [d]
    parts = []
    for d in days:
        todays = sorted((e for e in events if e["date"] == d.isoformat()), key=lambda e: e.get("time") or "99")
        if todays:
            name = "Today" if d == today else "Tomorrow" if d == today + timedelta(days=1) else WEEKDAYS[d.weekday()]
            parts.append(f"{name}: " + _join([_event_line(e) for e in todays]))
    if not parts:
        return "You have nothing planned" + ("" if len(days) > 1 else " " + ("today" if days[0] == today else f"on {_fmt_date(days[0])}"))
    return "; ".join(parts)


def cancel_event(title: str = "") -> str:
    """Delete the next calendar event whose title matches."""
    words = [w for w in re.findall(r"\w+", (title or "").lower()) if len(w) > 2]
    with _DATA_LOCK:
        data = _data_load()
        events = sorted(_events(data), key=lambda e: (e["date"], e.get("time") or ""))
        hit = next((e for e in events if e["date"] >= date.today().isoformat() and words and any(w in e["title"].lower() for w in words)), None)
        if not hit:
            return f"I found no upcoming event called {title}" if title else "Tell me which event to cancel"
        data["events"] = [e for e in _events(data) if e is not hit and e != hit]
        _data_save(data)
    return f"Okay, I removed {hit['title']} on {_fmt_date(date.fromisoformat(hit['date']))}"


def log_habit(habit: str = "", amount: str = "1") -> str:
    """Count something the user did: glasses of water, workouts, pages read."""
    name = re.sub(r"^(a |an |the |some )?(glass(es)? of |cup(s)? of |bottle(s)? of )?", "", (habit or "").lower().strip()).strip()
    value = _amount(amount) or 1
    if not name:
        return "Tell me what to count"
    with _DATA_LOCK:
        data = _data_load()
        habits = data.get("habits") if isinstance(data.get("habits"), dict) else {}
        habits.setdefault(name, []).append({"d": date.today().isoformat(), "n": value})
        data["habits"] = habits
        _data_save(data)
        total = sum(x["n"] for x in habits[name] if x["d"] == date.today().isoformat())
    return f"Logged. That is {_num(total)} for {name} today"


def habit_stats(habit: str = "") -> str:
    """Today, this week and the streak for a counted habit."""
    with _DATA_LOCK:
        habits = _data_load().get("habits") or {}
    if not habits:
        return "You have not counted anything yet"
    low = (habit or "").lower().strip()
    names = [n for n in habits if low and (low in n or n in low)] or ([] if low else list(habits))
    if not names:
        return f"I have not counted {habit} yet"
    today, out = date.today(), []
    for n in names[:3]:
        by = {}
        for x in habits[n]:
            by[x["d"]] = by.get(x["d"], 0) + x["n"]
        week = sum(v for d, v in by.items() if today - timedelta(days=6) <= date.fromisoformat(d) <= today)
        streak, cursor = 0, today if today.isoformat() in by else today - timedelta(days=1)
        while cursor.isoformat() in by:
            streak, cursor = streak + 1, cursor - timedelta(days=1)
        out.append(f"{n}: {_num(by.get(today.isoformat(), 0))} today, {_num(week)} in the last seven days, "
                   f"{streak} {'day' if streak == 1 else 'days'} in a row")
    return "; ".join(out)


def _utc_offset(place: dict):
    data = _get("https://api.open-meteo.com/v1/forecast", {"latitude": place["latitude"], "longitude": place["longitude"],
                                                           "current": "temperature_2m", "timezone": "auto"})
    return (data or {}).get("utc_offset_seconds")


def convert_time(clock: str = "", from_place: str = "", to_place: str = "") -> str:
    """What a clock time in one place is in another place (empty from_place = here)."""
    at = _clock_time(clock)
    if not at:
        return "Tell me the time, like 3 PM"
    a = _geocode(from_place) if (from_place or "").strip() else None
    b = _geocode(to_place)
    if (from_place or "").strip() and not a:
        return _no_place(from_place)
    if not b:
        return _no_place(to_place)
    off_a = time.localtime().tm_gmtoff if a is None else _utc_offset(a)
    off_b = _utc_offset(b)
    if off_a is None or off_b is None:
        return "Sorry, I could not get the time zones"
    minutes = at[0] * 60 + at[1] + round((off_b - off_a) / 60)
    shift = "the same day" if 0 <= minutes < 1440 else "the next day" if minutes >= 1440 else "the day before"
    there = _clock((minutes % 1440) // 60, minutes % 60)
    return f"{_clock(*at)} {'here' if a is None else 'in ' + a['name']} is {there} in {b['name']}, {shift}"


def compare_deals(price_a: str = "", amount_a: str = "", price_b: str = "", amount_b: str = "") -> str:
    """Which of two offers is cheaper per unit (amounts may carry units like '500 g' and '1 kg')."""
    pa, pb = _amount(price_a), _amount(price_b)
    def qty(text):
        n = _amount(text)
        unit = _UNITS.get(_unit_key(re.sub(r"[\d.,\s]+", " ", str(text or "")).strip()))
        return n, unit
    (qa, ua), (qb, ub) = qty(amount_a), qty(amount_b)
    if None in (pa, pb, qa, qb) or 0 in (qa, qb):
        return "Tell me both prices and both amounts, like 40 for 500 grams and 70 for 1 kilogram"
    if ua and ub and ua[0] == ub[0]:
        ba, bb, label = qa * ua[1], qb * ub[1], ua[3]
        base_a, base_b = pa / ba, pb / bb
        scale = 1 / ua[1]            # price per one of the first unit
        per_a, per_b = base_a * ua[1], base_b * ua[1]
    else:
        per_a, per_b, label = pa / qa, pb / qb, "unit"
    for k in (1, 100, 1000):
        if min(per_a, per_b) * k >= 1:
            break
    names = f"per {k} {label}" if k > 1 and label != "unit" else f"per {label[:-1] if label.endswith('s') and k == 1 else label}"
    cheaper, dear = ("A", "B") if per_a <= per_b else ("B", "A")
    gap = abs(per_a - per_b) / max(per_a, per_b) * 100
    if gap < 0.5:
        return "Both offers cost the same per unit"
    return f"Offer {cheaper} is cheaper by {gap:.0f} percent: {_money(per_a * k)} against {_money(per_b * k)} {names}" if cheaper == "A" \
        else f"Offer {cheaper} is cheaper by {gap:.0f} percent: {_money(per_b * k)} against {_money(per_a * k)} {names}"


def _years(text: str):
    n = _amount(text)
    if n is None:
        return None
    return n / 12 if re.search(r"month", str(text).lower()) else n


def loan_payment(amount: str = "", rate: str = "", years: str = "") -> str:
    """Monthly payment, total cost and interest of a loan or mortgage."""
    p, r, y = _amount(amount), _amount(rate), _years(years)
    if not p or r is None or not y:
        return "Tell me the amount, the yearly interest rate and the number of years"
    n, m = round(y * 12), r / 1200
    pay = p / n if m == 0 else p * m / (1 - (1 + m) ** -n)
    return (f"the monthly payment is about {_money(pay)}, you pay {_money(pay * n)} in total, "
            f"of which {_money(pay * n - p)} is interest")


def compound_interest(amount: str = "", rate: str = "", years: str = "", monthly_deposit: str = "0") -> str:
    """What savings grow to with yearly interest and optional monthly deposits."""
    p, r, y, d = _amount(amount) or 0.0, _amount(rate), _years(years), _amount(monthly_deposit) or 0.0
    if r is None or not y or (p == 0 and d == 0):
        return "Tell me the starting amount, the yearly interest rate and the number of years"
    n, m = round(y * 12), r / 1200
    growth = (1 + m) ** n
    final = p * growth + (d * n if m == 0 else d * (growth - 1) / m)
    put_in = p + d * n
    return f"after {_num(y)} years you would have about {_money(final)}, of which {_money(final - put_in)} is interest on {_money(put_in)} put in"


def vat_calculator(amount: str = "", rate: str = "", mode: str = "add") -> str:
    """Add VAT to a net price or take it out of a gross price."""
    a = _amount(amount)
    r = _amount(rate) if (rate or "").strip() else float(_env("DEFAULT_VAT", "21"))
    if a is None:
        return "Tell me the amount"
    if re.search(r"remove|without|excl|net|out|gross|incl|with.?in|contain", (mode or "").lower()) and not re.search(r"^add|plus", (mode or "").lower()):
        net = a / (1 + r / 100)
        return f"{_money(a)} with {_num(r)} percent VAT is {_money(net)} without VAT, the VAT is {_money(a - net)}"
    return f"{_money(a)} plus {_num(r)} percent VAT is {_money(a * (1 + r / 100))}, the VAT is {_money(a * r / 100)}"


_LEAGUES = {"premier league": "soccer/eng.1", "english": "soccer/eng.1", "la liga": "soccer/esp.1", "spanish": "soccer/esp.1",
            "bundesliga": "soccer/ger.1", "german": "soccer/ger.1", "serie a": "soccer/ita.1", "italian": "soccer/ita.1",
            "ligue 1": "soccer/fra.1", "french": "soccer/fra.1", "champions league": "soccer/uefa.champions",
            "europa league": "soccer/uefa.europa", "czech": "soccer/cze.1", "mls": "soccer/usa.1", "nba": "basketball/nba",
            "wnba": "basketball/wnba", "nfl": "football/nfl", "nhl": "hockey/nhl", "mlb": "baseball/mlb"}


_SPORT_STATUS = {"ft": "full time", "ht": "half time", "aet": "after extra time", "final": "final", "f/ot": "final after overtime"}


def sports_scores(league: str = "", team: str = "") -> str:
    """Today's scores for a league, or just a team's game."""
    low = (league or "").lower().strip()
    path = next((v for k, v in _LEAGUES.items() if k in low or (low and low in k)), None)
    if not path:
        return "Tell me the league, like Premier League, NBA, NHL or Champions League"
    data = _get(f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard")
    games = []
    for ev in (data or {}).get("events", []):
        try:
            comp = ev["competitions"][0]
            teams = sorted(comp["competitors"], key=lambda c: c.get("homeAway") != "home")
            names = [c["team"]["displayName"] for c in teams]
            status = ev["status"]["type"]
            line = f"{names[0]} {teams[0].get('score', '')} against {names[1]} {teams[1].get('score', '')}".replace("  ", " ")
            games.append((names, line + (f", {_SPORT_STATUS.get(status.get('shortDetail', '').strip().lower(), status.get('shortDetail', '').lower())}" if status.get("shortDetail") else "")))
        except (KeyError, IndexError, TypeError):
            continue
    if (team or "").strip():
        games = [g for g in games if any(team.lower() in n.lower() for n in g[0])]
    if not games:
        return (f"I found no game for {team} today" if team else f"No {league} games today") if data is not None else "Sorry, I could not get the scores"
    return "; ".join(g[1] for g in games[:6])


def _ha(method: str, path: str, body: dict = None):
    base, token = _env("HA_URL").rstrip("/"), _env("HA_TOKEN")
    if not base or not token:
        return None, "The smart home is not set up. Add HA_URL and HA_TOKEN for Home Assistant to the dot env file"
    try:
        r = requests.request(method, base + path, json=body, timeout=8, headers={"Authorization": f"Bearer {token}"})
    except requests.RequestException:
        return None, "Sorry, I could not reach Home Assistant"
    if r.status_code == 401:
        return None, "Home Assistant refused my access token"
    return (r.json() if r.status_code < 300 and r.content else None), "" if r.status_code < 300 else f"Home Assistant answered error {r.status_code}"


_HA_DOMAINS = ("light", "switch", "fan", "cover", "lock", "climate", "media_player", "scene", "script", "input_boolean", "sensor", "binary_sensor", "vacuum")


def _ha_find(device: str, states: list, domains=_HA_DOMAINS):
    words = {w for w in re.findall(r"[a-z0-9]+", (device or "").lower()) if w not in ("the", "my", "light", "lights", "lamp") or len(re.findall(r"\w+", device)) == 1}
    best, score = None, 0
    for s in states:
        if s["entity_id"].split(".")[0] not in domains:
            continue
        have = set(re.findall(r"[a-z0-9]+", f"{s.get('attributes', {}).get('friendly_name', '')} {s['entity_id']}".lower()))
        hit = len(words & have)
        if words and hit > score or (hit == score and best and hit and len(have) < len(set(re.findall(r'[a-z0-9]+', best['entity_id'])))):
            best, score = s, hit
    return best if score else None


def smart_home(device: str = "", action: str = "toggle", value: str = "") -> str:
    """Control a Home Assistant device: lights, switches, blinds, locks, scenes, thermostat."""
    states, error = _ha("GET", "/api/states")
    if error:
        return error
    found = _ha_find(device, states or [], _HA_DOMAINS[:10])
    if not found:
        return f"I could not find a device called {device}"
    eid, domain = found["entity_id"], found["entity_id"].split(".")[0]
    name = found.get("attributes", {}).get("friendly_name", eid)
    act, body = (action or "toggle").lower(), {"entity_id": eid}
    number = _amount(value) if (value or "").strip() else _amount(act)
    if domain == "cover":
        service = "open_cover" if re.search(r"open|up|raise", act) else "close_cover" if re.search(r"close|down|lower", act) else "stop_cover"
    elif domain == "lock":
        service = "unlock" if "unlock" in act else "lock"
    elif domain in ("scene", "script"):
        service = "turn_on"
    elif domain == "climate" and number is not None:
        service, body["temperature"] = "set_temperature", number
    elif domain == "light" and number is not None and not re.search(r"\boff\b", act):
        service, body["brightness_pct"] = "turn_on", max(1, min(100, int(number)))
    else:
        service = "turn_off" if re.search(r"\boff\b|stop|close|disable", act) else "turn_on" if re.search(r"\bon\b|start|enable|activate|open", act) else "toggle"
    _, error = _ha("POST", f"/api/services/{domain}/{service}", body)
    if error:
        return error
    done = {"turn_on": "is on", "turn_off": "is off", "toggle": "is toggled", "open_cover": "is opening", "close_cover": "is closing",
            "stop_cover": "has stopped", "lock": "is locked", "unlock": "is unlocked", "set_temperature": f"is set to {_num(number or 0)} degrees"}
    return f"Okay, {name} " + (done.get(service, "is done") if "brightness_pct" not in body else f"is at {body['brightness_pct']} percent")


def home_status(device: str = "") -> str:
    """State of a device or sensor, or which lights are on."""
    states, error = _ha("GET", "/api/states")
    if error:
        return error
    if not (device or "").strip():
        on = [s["attributes"].get("friendly_name", s["entity_id"]) for s in states or []
              if s["entity_id"].startswith("light.") and s["state"] == "on"]
        return f"These lights are on: {_join(on[:8])}" if on else "All lights are off"
    found = _ha_find(device, states or [])
    if not found:
        return f"I could not find a device called {device}"
    attrs = found.get("attributes", {})
    unit = attrs.get("unit_of_measurement", "")
    return f"{attrs.get('friendly_name', found['entity_id'])} is {found['state']}{(' ' + unit) if unit else ''}"


def wake_on_lan(device: str = "") -> str:
    """Wake a computer on the network. Devices come from WOL_DEVICES=pc=AA:BB:CC:DD:EE:FF,nas=..."""
    import socket
    known = {}
    for pair in _env("WOL_DEVICES").split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            known[k.strip().lower()] = re.sub(r"[^0-9a-fA-F]", "", v)
    if not known:
        return "No devices are set up. Add WOL_DEVICES like pc=AA:BB:CC:DD:EE:FF to the dot env file"
    low = (device or "").lower().strip()
    key = next((k for k in known if low and (k in low or low in k)), next(iter(known)) if len(known) == 1 and not low else None)
    if not key or len(known[key]) != 12:
        return f"I do not know that device, I know {_join(list(known))}"
    packet = bytes.fromhex("FF" * 6 + known[key] * 16)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(packet, (_env("WOL_BROADCAST", "255.255.255.255"), 9))
    except OSError:
        return "Sorry, I could not send the wake up signal"
    return f"Okay, I sent the wake up signal to {key}"


_COOK = [(("egg", "soft"), 6, "soft boiled egg"), (("egg", "medium"), 8, "medium boiled egg"), (("egg",), 10, "hard boiled egg"),
         (("spaghetti",), 10, "spaghetti"), (("penne",), 11, "penne"), (("pasta",), 10, "pasta"), (("noodle",), 3, "noodles"),
         (("rice", "brown"), 40, "brown rice"), (("rice",), 15, "rice"), (("couscous",), 5, "couscous"), (("quinoa",), 15, "quinoa"),
         (("lentil",), 20, "lentils"), (("potato",), 20, "boiled potatoes"), (("dumpling",), 20, "dumplings"), (("broccoli",), 5, "broccoli"),
         (("oat",), 5, "oatmeal"), (("pizza",), 12, "frozen pizza"), (("tea", "green"), 3, "green tea"), (("tea", "herbal"), 7, "herbal tea"),
         (("tea", "fruit"), 7, "fruit tea"), (("tea",), 4, "tea"), (("coffee",), 4, "french press coffee"), (("steak",), 8, "steak")]


def cook_timer(food: str = "", doneness: str = "") -> str:
    """Start a timer with the usual cooking time for a food."""
    text = f"{food} {doneness}".lower()
    for words, minutes, label in _COOK:
        if all(w in text for w in words):
            return set_timer(f"{minutes} minutes", label)
    return "I do not know that one. I know eggs, pasta, rice, potatoes, dumplings, tea and a few more"


def recipe_by_ingredient(ingredient: str = "") -> str:
    """Dishes that can be cooked with an ingredient."""
    item = (ingredient or "").strip().lower().replace(" ", "_")
    if not item:
        return "Tell me which ingredient you have"
    meals = ((_get("https://www.themealdb.com/api/json/v1/1/filter.php", {"i": item}) or {}).get("meals")) or []
    if not meals:
        return f"I found no dishes with {ingredient}"
    picks = random.sample([m["strMeal"] for m in meals if m.get("strMeal")], min(5, len(meals)))
    return f"With {ingredient.strip().lower()} you could make {_join(picks)}. Ask for any of them and I will read the recipe"


def nutrition_info(food: str = "", amount: str = "") -> str:
    """Calories, protein, carbs and fat of a food (Open Food Facts), per 100 grams or a given weight."""
    food = (food or "").strip()
    if not food:
        return "Tell me which food"
    data = _get("https://world.openfoodfacts.org/cgi/search.pl", {"search_terms": food, "search_simple": 1, "action": "process",
                                                                   "json": 1, "page_size": 5, "fields": "product_name,nutriments"})
    for product in (data or {}).get("products", []):
        n = product.get("nutriments") or {}
        kcal = n.get("energy-kcal_100g")
        if kcal:
            grams = _amount(amount) if re.search(r"\bg\b|gram", str(amount).lower()) else (_amount(amount) * 1000 if re.search(r"kg|kilo", str(amount).lower()) and _amount(amount) else None)
            k = (grams or 100) / 100
            f = lambda key: _num(round((n.get(key) or 0) * k, 1))
            return (f"{product.get('product_name') or food} has about {_num(round(kcal * k))} calories, {f('proteins_100g')} grams of protein, "
                    f"{f('carbohydrates_100g')} grams of carbs and {f('fat_100g')} grams of fat per {_num(round(grams)) if grams else 100} grams")
    return f"I could not find nutrition facts for {food}"


def name_day(day: str = "") -> str:
    """Czech name day (svátek) for today, tomorrow or a date."""
    d = date.today() if not (day or "").strip() else _parse_date(day, date.today())
    if d is None:
        return "Sorry, I did not understand the date"
    data = _get(f"https://svatkyapi.cz/api/day/{d.isoformat()}")
    if not data or not data.get("name"):
        return "Sorry, I could not look up the name day"
    when = "Today" if d == date.today() else "Tomorrow" if d == date.today() + timedelta(days=1) else _fmt_date(d)
    return f"{when} the name day is {data['name']}"


_MOVES = {"legs": ["squats", "lunges", "glute bridges", "wall sit", "calf raises", "jump squats"],
          "arms": ["push ups", "tricep dips on a chair", "plank shoulder taps", "arm circles", "diamond push ups", "pike push ups"],
          "core": ["plank", "crunches", "bicycle crunches", "leg raises", "mountain climbers", "russian twists"],
          "cardio": ["jumping jacks", "high knees", "butt kicks", "burpees", "skaters", "fast feet"]}


def quick_workout(minutes: str = "10", focus: str = "") -> str:
    """A bodyweight workout of the asked length: 40 seconds work, 20 seconds rest."""
    n = max(5, min(30, int(_amount(minutes) or 10)))
    low = (focus or "").lower()
    key = next((k for k in _MOVES if k in low), None) or ("legs" if "leg" in low else None)
    pool = _MOVES[key] if key else [m for k in _MOVES for m in random.sample(_MOVES[k], 2)]
    random.shuffle(pool)
    moves = [pool[i % len(pool)] for i in range(n - 2)]
    return (f"{n} minute {key or 'full body'} workout. Start with one minute of marching on the spot. Then do each for 40 seconds with 20 seconds rest: "
            + ", ".join(moves) + ". Finish with one minute of stretching")



TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": (
                "Get the current weather for a place. Use for questions about "
                "weather, temperature, rain, wind, or humidity anywhere in the world."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City or place name, e.g. 'Prague' or 'Paris, France'. Leave empty for the home city.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": (
                "Get the current date and local time. Use when the user asks "
                "what time or date it is, or anything about today."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_news",
            "description": (
                "Get the latest news headlines, numbered, worldwide or about a topic. Use for questions about "
                "the news, current events, or what is happening. After the headlines the user can ask for "
                "more about one story: then use news_details."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": ("Topic or place for the news, e.g. 'technology'. Also 'Czech Republic', 'football', "
                                        "'business', 'science', 'health', or 'local' for the home city. "
                                        "Leave empty for the top world headlines."),
                    },
                    "count": {
                        "type": "string",
                        "description": "How many headlines, e.g. '3'. Up to 10. Leave empty for five.",
                    },
                },
                "required": [],
            },
        },
    },
]

def _tool(name: str, description: str, properties: dict = None, required: list = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties or {}, "required": required or []},
        },
    }


def _text(description: str) -> dict:
    return {"type": "string", "description": description}


TOOLS.extend([
    # ---- time tools (timers.py) ----
    _tool(
        "set_timer",
        "Start a countdown timer; Kate speaks up when it runs out. Use for 'set a timer for ...', "
        "'timer for 10 minutes', 'tell me when 5 minutes are over'.",
        {
            "duration": _text("How long, e.g. '10 minutes'. Also fine: '1 hour 30 minutes', '90 seconds', 'half an hour'."),
            "label": _text("Optional name of the timer, e.g. 'pasta'. Leave empty if the user gave none."),
        },
        ["duration"],
    ),
    _tool(
        "set_reminder",
        "Remind the user of something later, or set an alarm, after a delay or at a clock time. "
        "Use for 'remind me in 20 minutes to ...', 'wake me at 7', 'remind me at 6 PM'. "
        "If the user gives no time at all, ask them when instead of using this.",
        {
            "when": _text("A delay or a clock time, e.g. 'in 20 minutes'. Also: 'at 6 PM', 'at 7:30 AM', 'tomorrow at 8'."),
            "message": _text("What to remind about, as a short phrase, e.g. 'call mom'. Leave empty for a plain alarm."),
        },
        ["when"],
    ),
    _tool(
        "list_timers",
        "Say which timers, reminders and alarms are running and how long is left. "
        "Use for 'how much time is left', 'what timers do I have'.",
    ),
    _tool(
        "cancel_timer",
        "Cancel a timer, reminder or alarm. Use for 'cancel the timer', 'stop the pasta timer', 'delete my reminder'.",
        {"name": _text("Which one, e.g. 'pasta'. Use 'all' for every one. Leave empty when there is only one.")},
    ),
    _tool(
        "stopwatch",
        "Start, check, stop or reset a stopwatch. Use for 'start the stopwatch', 'how long has it been', 'stop the stopwatch'.",
        {"action": _text("One of 'start', 'check', 'stop' or 'reset', e.g. 'start'.")},
        ["action"],
    ),
    # ---- hardware tools (hardware.py) ----
    _tool(
        "set_volume",
        "Change how loud Kate speaks. Use for 'louder', 'quieter', 'turn it up', 'turn it down', "
        "'set the volume to 60', 'mute', 'unmute'.",
        {"level": _text("A number from 0 to 100, or a change, e.g. 'louder'. Also: 'quieter', 'a bit louder', 'up 20', 'max', 'mute', 'unmute'.")},
        ["level"],
    ),
    _tool(
        "get_volume",
        "Tell the current speaker volume. Use for 'what is the volume'.",
    ),
])

_EXTRA_SPECS = [
    _tool("calculate", "Do math: arithmetic, percentages, powers, roots. Use for 'what is 15 percent of 80', "
          "'12 times 7', 'square root of 144', 'split 86 by 4'. Never calculate in your head.",
          {"expression": _text("The math, e.g. '15% of 80'. Also: '12 * 7', 'sqrt(144)', '2 to the power of 10'.")},
          ["expression"]),
    _tool("convert_units", "Convert between units of length, weight, volume, speed, area, time, data size and "
          "temperature. Use for 'how many miles is 10 kilometers', '350 Fahrenheit in Celsius', 'cups in a liter'.",
          {"value": _text("The number, e.g. '10'."),
           "from_unit": _text("Unit to convert from, e.g. 'kilometers'. Also: 'pounds', 'fahrenheit', 'mph'."),
           "to_unit": _text("Unit to convert to, e.g. 'miles'.")},
          ["value", "from_unit", "to_unit"]),
    _tool("convert_currency", "Convert money at today's exchange rate. Use for 'how many euros is 100 dollars', "
          "'what is 50 pounds in crowns'.",
          {"amount": _text("The amount, e.g. '100'."),
           "from_currency": _text("Currency to convert from, e.g. 'dollars'. Also: 'EUR', 'pounds', 'crowns'."),
           "to_currency": _text("Currency to convert to, e.g. 'euros'.")},
          ["amount", "from_currency", "to_currency"]),
    _tool("get_crypto_price", "Get the current price of a cryptocurrency. Use for 'bitcoin price', 'how much is ethereum'.",
          {"coin": _text("Coin name or symbol, e.g. 'bitcoin'. Also: 'ETH', 'solana'."),
           "currency": _text("Price currency, e.g. 'USD'. Also: 'EUR', 'CZK'. Leave empty for dollars.")},
          ["coin"]),
    _tool("define_word", "Give the dictionary definition of an English word. Use for 'what does ... mean', 'define ...'.",
          {"word": _text("The word, e.g. 'ephemeral'.")}, ["word"]),
    _tool("wiki_summary", "Short encyclopedia summary of a person, place, thing or idea. Use for 'who is ...', "
          "'what is ...', 'tell me about ...' when it is a factual topic.",
          {"topic": _text("What to look up, e.g. 'Marie Curie'."),
           "detail": _text("How much, e.g. 'short'. Use 'long' when the user wants to hear more.")}, ["topic"]),
    _tool("translate", "Translate English into another language. Use for 'how do you say ... in German', 'translate ... to Czech'.",
          {"text": _text("The English text, e.g. 'where is the train station'."),
           "language": _text("Target language, e.g. 'German'. Also: 'Czech', 'French', 'Japanese'.")},
          ["text", "language"]),
    _tool("spell_word", "Spell a word letter by letter. Use for 'how do you spell ...', 'spell ...'.",
          {"word": _text("The word, e.g. 'necessary'.")}, ["word"]),
    _tool("world_time", "Current date and time in another city or country. Use for 'what time is it in Tokyo'.",
          {"location": _text("City or place, e.g. 'Tokyo' or 'New York'.")}, ["location"]),
    _tool("get_forecast", "Weather FORECAST for later today, tomorrow, a weekday or the coming week (get_weather is only "
          "for right now). Use for 'will it rain tomorrow', 'forecast for Friday', 'weather this week'.",
          {"location": _text("City or place, e.g. 'Prague'. Leave empty for the home location."),
           "day": _text("Which day, e.g. 'tomorrow'. Also: 'today', 'Friday', 'in 3 days', 'week'.")},
          ["location"]),
    _tool("get_sun_times", "Sunrise, sunset and daylight length today. Use for 'when is sunset', 'when does the sun rise'.",
          {"location": _text("City or place, e.g. 'Prague'. Leave empty for the home location.")}),
    _tool("get_air_quality", "Current air quality and fine dust in a place. Use for 'how is the air', 'is there smog', 'air quality'.",
          {"location": _text("City or place, e.g. 'Ostrava'. Leave empty for the home location.")}),
    _tool("days_until", "Count the days until a date or holiday, and say which weekday it is. Use for "
          "'how many days until Christmas', 'what day is June 5', 'days till Easter'.",
          {"target": _text("A date or holiday, e.g. 'December 25'. Also: 'Easter', 'Halloween', '2027-01-01', '24.12.'.")},
          ["target"]),
    _tool("next_holiday", "The next public holidays of a country. Use for 'when is the next holiday', 'next day off'.",
          {"country": _text("Country, e.g. 'Czech Republic'. Leave empty for the home country.")}),
    _tool("moon_phase", "Current phase of the moon and the next full and new moon. Use for 'is there a full moon', 'moon phase'."),
    _tool("flip_coin", "Flip a coin. Use for 'flip a coin', 'heads or tails'."),
    _tool("roll_dice", "Roll one or more dice. Use for 'roll a die', 'roll two dice', 'roll a d20'.",
          {"sides": _text("Number of sides, e.g. '6'. Also '20' or '2d6'."),
           "count": _text("How many dice, e.g. '1'.")}),
    _tool("random_number", "Pick a random number in a range. Use for 'pick a number between 1 and 100'.",
          {"low": _text("Lowest number, e.g. '1'."), "high": _text("Highest number, e.g. '100'.")},
          ["low", "high"]),
    _tool("pick_random", "Choose one of several options for the user. Use for 'help me decide', 'pizza or pasta, you pick'.",
          {"options": _text("The options separated by commas, e.g. 'pizza, pasta, sushi'.")}, ["options"]),
    _tool("tell_joke", "Tell a joke. Use for 'tell me a joke', 'make me laugh', 'tell me a programming joke'.",
          {"topic": _text("Kind of joke, e.g. 'programming'. Also 'pun', 'spooky', 'christmas'. Leave empty for a dad joke.")}),
    _tool("fun_fact", "Tell a random fun fact. Use for 'tell me something interesting', 'fun fact'."),
    _tool("add_note", "Write down a note for later. Use for 'make a note that ...', 'remember that ...', 'write down ...'.",
          {"text": _text("The note, e.g. 'the wifi password is in the drawer'.")}, ["text"]),
    _tool("read_notes", "Read the saved notes, or search them. Use for 'what are my notes', 'what did I ask you to remember', "
          "'what did I note about the dentist', 'where did I write the wifi password'.",
          {"query": _text("Word to search for, e.g. 'wifi'. Leave empty to read the latest notes.")}),
    _tool("clear_notes", "Delete notes. Use for 'delete my notes', 'delete the last note', 'delete note 2'.",
          {"which": _text("What to delete, e.g. 'all'. Also: 'last' or a number like '2'.")}),
    _tool("add_to_list", "Add things to the shopping list, to-do list or any named list. Use for 'add milk to my shopping list', "
          "'put call the dentist on my to-do list'.",
          {"item": _text("What to add, e.g. 'milk, eggs'."),
           "list_name": _text("Which list, e.g. 'shopping'. Also: 'todo' or any name. Leave empty for shopping.")},
          ["item"]),
    _tool("read_list", "Read a list out loud. Use for 'what is on my shopping list', 'read my to-do list', 'what lists do I have'.",
          {"list_name": _text("Which list, e.g. 'shopping'. Also: 'todo', or 'all' for an overview. Leave empty for shopping.")}),
    _tool("remove_from_list", "Remove things from a list. Use for 'take milk off the list', 'I bought the eggs', 'done with the dentist'.",
          {"item": _text("What to remove, e.g. 'milk'."),
           "list_name": _text("Which list, e.g. 'shopping'. Also: 'todo'. Leave empty for shopping.")},
          ["item"]),
    _tool("clear_list", "Empty a whole list. Use for 'clear my shopping list', 'start the list over'.",
          {"list_name": _text("Which list, e.g. 'shopping'. Also: 'todo'. Leave empty for shopping.")}),
    _tool("pc_status", "Status of the computer Kate runs on: processor, memory, disk, battery. Use for 'how is the computer doing'."),
    _tool("set_home_location", "Remember which city the user lives in, so weather, forecast, air quality, sun times and "
          "holidays work without naming a place. Use for 'I live in Ostrava', 'my home is Brno', 'set my location to Prague'.",
          {"location": _text("City, e.g. 'Ostrava'. Also: 'Paris, France'.")}, ["location"]),
    _tool("daily_briefing", "A spoken morning summary: date, weather, air quality warning, sunset, upcoming holiday and the to-do list. "
          "Use for 'good morning', 'brief me', 'catch me up', 'what does my day look like'."),
    _tool("rain_check", "Will it rain in the next hours, and when does it start and stop. Use for 'do I need an umbrella', "
          "'will it rain this afternoon', 'is it going to rain soon'.",
          {"location": _text("City or place, e.g. 'Prague'. Leave empty for the home city."),
           "hours": _text("How many hours ahead, e.g. '12'. Up to 48.")}),
    _tool("tip_split", "Work out the tip and split a restaurant bill between people. Use for 'what is a tip on 850', "
          "'split 1200 between 4 people with a 10 percent tip'.",
          {"bill": _text("The bill amount, e.g. '850'."),
           "tip_percent": _text("Tip in percent, e.g. '10'. Leave empty for the usual tip."),
           "people": _text("How many people share it, e.g. '4'. Leave empty for one.")}, ["bill"]),
    _tool("date_math", "Date calculations: what date is it in N days or weeks, what was it N days ago, how many days between "
          "two dates, how old someone is, what weekday a date is. Use for 'what date is in 90 days', 'how old is someone "
          "born in 1990', 'how long since March 3', 'what day of the week is July 4 2027'.",
          {"start": _text("Starting date, e.g. 'today'. Also: 'March 3 2026', '1990-05-12', 'tomorrow'."),
           "change": _text("Time to add or subtract, e.g. '90 days'. Also: '3 weeks', '2 months ago', '1 year'. Leave empty if not needed."),
           "until": _text("Second date to measure the time between, e.g. 'today'. Leave empty if not needed.")}),
    _tool("distance_between", "Distance between two places with a rough driving time. Use for 'how far is Vienna from Prague', "
          "'how far is it to Berlin' (from home).",
          {"from_place": _text("Starting place, e.g. 'Prague'. Leave empty for the home city."),
           "to_place": _text("Destination, e.g. 'Vienna'.")}, ["to_place"]),
    _tool("news_details", "More about ONE story from the headlines just read: who reports it, how old it is and what the "
          "article says. Use for 'tell me more about the second one', 'what is that story about', 'more on the "
          "Ukraine one', 'read me the third article'. Pass the story number or a few words from its headline.",
          {"story": _text("Which story, e.g. 'second'. Also '3', 'the next one', or words like 'Ukraine'."),
           "depth": _text("'short' normally. Use 'long' when the user wants all the details.")}),
    _tool("animal_sound", "What sound an animal makes, with a fun fact. Use for 'what does a cow say', "
          "'make a cat sound', 'do an owl', 'give me a random animal sound'.",
          {"animal": _text("The animal, e.g. 'cow'. Leave empty for a random animal.")}),
    _tool("recipe_search", "Find a recipe: ingredients and steps. Use for 'how do I make carbonara', 'a recipe for pancakes', "
          "'what should I cook' (random).",
          {"dish": _text("The dish, e.g. 'lasagne'. Leave empty for a random recipe."),
           "part": _text("What to say: 'overview' normally, 'ingredients' for the full shopping list, 'steps' for the method.")}),
    _tool("get_stock_price", "Latest price of a stock or index and how it moved today. Use for 'how is Tesla stock', "
          "'apple share price', 'how is the S and P 500'.",
          {"company": _text("Company or ticker, e.g. 'Apple'. Also 'TSLA', 'Nasdaq', 'S and P 500'.")}, ["company"]),
    _tool("today_in_history", "Things that happened on this day in history. Use for 'what happened today in history', "
          "'on this day', 'what happened on July 4'.",
          {"day": _text("A date, e.g. 'July 4'. Leave empty for today."),
           "count": _text("How many events, e.g. '2'. Up to 4.")}),
    _tool("trivia", "Play trivia: ask a quiz question, then check the answer. Use for 'ask me a trivia question', 'quiz me', "
          "and when the user answers a pending question, or gives up.",
          {"action": _text("'new' for a question. 'reveal' when the user gives up."),
           "guess": _text("The user's answer to the pending question, e.g. 'B' or 'Jupiter'. Leave empty for a new question.")}),
    _tool("add_birthday", "Remember someone's birthday. Use for 'Anna's birthday is May 12', 'remember my dad was born on 3 March 1965'.",
          {"name": _text("The person, e.g. 'Anna'."),
           "birthday": _text("The date, e.g. 'May 12'. Add the year, like '1990-05-12', to also know their age.")},
          ["name", "birthday"]),
    _tool("get_birthdays", "Say when a birthday is, or which birthdays are coming up. Use for 'when is Anna's birthday', "
          "'whose birthday is next', 'any birthdays coming up'.",
          {"who": _text("The person, e.g. 'Anna'. Leave empty for the next birthdays.")}),
    _tool("remove_birthday", "Forget a saved birthday. Use for 'forget Anna's birthday'.",
          {"name": _text("The person, e.g. 'Anna'.")}, ["name"]),
    _tool("log_expense", "Write down money the user spent. Use for 'I spent 250 on lunch', 'log 40 for coffee', 'add an expense of 1200 for groceries'.",
          {"amount": _text("The amount, e.g. '250'."),
           "category": _text("What kind, e.g. 'food'. Also 'transport', 'groceries', 'fun'."),
           "note": _text("Optional detail, e.g. 'lunch with Tom'.")}, ["amount"]),
    _tool("spending_summary", "How much the user spent and on what. Use for 'how much did I spend this month', "
          "'what did I spend on food', 'spending today'.",
          {"period": _text("'month' normally. Also 'today', 'yesterday', 'week', 'year', 'all'."),
           "category": _text("Optional category to look at, e.g. 'food'.")}),
    _tool("undo_last_expense", "Delete the last logged expense. Use for 'undo that expense', 'that was a mistake'."),
    _tool("sleep_calculator", "Best bedtime for a wake-up time, or best wake-up time for a bedtime, based on 90 minute sleep "
          "cycles. Use for 'when should I go to bed to wake at 7', 'if I sleep now when should I wake up'.",
          {"wake_time": _text("When the user wants to wake up, e.g. '7 AM'. Leave empty if they give a bedtime."),
           "bedtime": _text("When the user goes to sleep, e.g. '11 PM' or 'now'. Leave empty if they give a wake-up time.")}),
    _tool("magic_8ball", "Answer a yes or no question like a magic 8 ball, for fun. Use for 'ask the magic 8 ball', 'should I order pizza, 8 ball'.",
          {"question": _text("The question, e.g. 'will I win tonight'.")}),
    _tool("word_finder", "Synonyms, opposites, rhymes or similar-sounding words. Use for 'another word for happy', "
          "'what rhymes with orange', 'opposite of brave'.",
          {"word": _text("The word, e.g. 'happy'."),
           "kind": _text("'synonyms' normally. Also 'opposites', 'rhymes', 'sounds like', 'related'.")}, ["word"]),
    _tool("inspire_me", "A short inspiring quote. Use for 'motivate me', 'give me a quote', 'inspire me'."),
]

_MORE_SPECS = [
    _tool("get_earthquakes", "Recent notable earthquakes, magnitude 4.5 and up. Use for 'any earthquakes today', "
          "'latest earthquakes', 'biggest earthquake this week'.",
          {"period": _text("'day' normally, e.g. 'day'. Also 'week' or 'month'."),
           "count": _text("How many, e.g. '3'. Up to 5.")}),
    _tool("iss_location", "Where the International Space Station is right now. Use for 'where is the ISS', "
          "'where is the space station', 'what is the ISS flying over'."),
    _tool("country_info", "Facts about a country: capital, population and currency. Use for "
          "'tell me about Japan', 'what is the capital of Brazil', 'population of Canada'.",
          {"country": _text("Country name, e.g. 'Japan'.")}, ["country"]),
    _tool("public_ip", "The user's public IP address. Use for 'what is my IP', 'what is my public IP address'."),
    _tool("website_status", "Check whether a website is up and how fast it answers. Use for 'is github down', "
          "'is example.com working', 'check my website'.",
          {"site": _text("Website address, e.g. 'example.com'.")}, ["site"]),
    _tool("github_repo", "Stats about a GitHub repository: stars, forks, issues and language. Use for "
          "'how many stars does python have on github', 'tell me about the opencode repo'.",
          {"repo": _text("Repository as owner and name, e.g. 'python/cpython'. A full GitHub URL also works.")},
          ["repo"]),
    _tool("package_info", "Latest version and summary of a Python or JavaScript package. Use for "
          "'what is the latest version of requests', 'npm version of react'.",
          {"name": _text("Package name, e.g. 'requests'. Also 'react'."),
           "ecosystem": _text("'pypi' for Python, 'npm' for JavaScript, e.g. 'pypi'. Leave empty to try both.")},
          ["name"]),
    _tool("cocktail_recipe", "A cocktail recipe: ingredients and how to make it. Use for 'how do I make a margarita', "
          "'recipe for mojito', 'give me a random cocktail'.",
          {"drink": _text("The cocktail, e.g. 'margarita'. Leave empty for a random one."),
           "part": _text("'overview' normally, e.g. 'overview'. Also 'ingredients' or 'steps'.")}),
    _tool("book_search", "Find a book and say who wrote it and what it is about. Use for 'who wrote Dune', "
          "'tell me about the book 1984', 'find a book by Isaac Asimov'.",
          {"query": _text("Book title or author, e.g. 'Dune'."),
           "detail": _text("'short' normally, e.g. 'short'. Use 'long' for more detail.")}, ["query"]),
    _tool("music_artist", "Facts about a music artist or band: country, active years and style. Use for "
          "'who is Nirvana', 'tell me about Dua Lipa', 'what kind of music does Radiohead make'.",
          {"name": _text("Artist or band, e.g. 'Nirvana'.")}, ["name"]),
    _tool("anime_info", "Find an anime and say its year, episode count, rating and what it is about. Use for "
          "'tell me about Attack on Titan', 'what is One Piece about'.",
          {"title": _text("Anime title, e.g. 'Attack on Titan'.")}, ["title"]),
    _tool("pokemon_info", "Pokedex facts about a Pokemon: type, size, abilities and a short description. Use for "
          "'tell me about Pikachu', 'what type is Charizard'.",
          {"name": _text("Pokemon name, e.g. 'Pikachu'.")}, ["name"]),
    _tool("number_fact", "A fun fact about a number, or a random number fact. Use for 'tell me a fact about 42', "
          "'random number fact'.",
          {"number": _text("The number, e.g. '42'. Leave empty for a random fact.")}),
    _tool("cat_fact", "A random fact about cats. Use for 'tell me a cat fact', 'something about cats'."),
    _tool("random_wikipedia", "A random Wikipedia article summary. Use for 'tell me something random', "
          "'random Wikipedia article', 'surprise me with a fact'."),
    _tool("get_pollen", "Pollen levels for trees, grass and weeds in a place. Use for 'how is the pollen today', "
          "'is it bad for allergies', 'pollen count'.",
          {"location": _text("City or place, e.g. 'Prague'. Leave empty for the home location.")}),
    _tool("bmi", "Calculate body mass index from weight and height, with the category. Use for 'what is my BMI', "
          "'BMI for 80 kilos and 180 centimeters'.",
          {"weight": _text("Weight, e.g. '80'. Also '176 pounds'."),
           "height": _text("Height, e.g. '180'. Also '5 feet 10 inches' or '1.8'."),
           "units": _text("'metric' normally, e.g. 'metric'. Use 'imperial' for pounds and inches.")},
          ["weight", "height"]),
    _tool("daily_calories", "Estimate daily calories from weight, height, age, sex and activity. Use for "
          "'how many calories should I eat', 'my daily calorie needs'.",
          {"weight": _text("Weight, e.g. '80'."),
           "height": _text("Height, e.g. '180'."),
           "age": _text("Age in years, e.g. '30'."),
           "sex": _text("'female' or 'male', e.g. 'female'."),
           "activity": _text("'sedentary', 'light', 'moderate', 'active' or 'very active', e.g. 'moderate'."),
           "units": _text("'metric' normally, e.g. 'metric'. Use 'imperial' for pounds and inches.")},
          ["weight", "height", "age"]),
    _tool("pace_calculator", "Running pace and speed from a distance and a time. Use for 'what pace is a 4 hour marathon', "
          "'pace for 10 kilometers in 50 minutes', 'how fast is a 5k in 25 minutes'.",
          {"distance": _text("Distance, e.g. '10 kilometers'. Also '5k', 'marathon', '26.2 miles'."),
           "time_text": _text("Time, e.g. '50 minutes'. Also '4 hours', '3:30:00'."),
           "unit": _text("'km' for pace per kilometer, e.g. 'km'. Use 'mile' for pace per mile.")},
          ["distance", "time_text"]),
    _tool("fuel_cost", "Fuel needed and cost for a trip. Use for 'how much fuel for 300 kilometers', "
          "'trip cost for 200 miles at 30 mpg'.",
          {"distance": _text("Distance, e.g. '300 kilometers'. Also '200 miles'."),
           "consumption": _text("Fuel use, e.g. '6.5' liters per 100 kilometers. Also '30 mpg'."),
           "price": _text("Fuel price per liter or gallon, e.g. '1.50'."),
           "units": _text("'metric' normally, e.g. 'metric'. Use 'imperial' for miles, gallons and mpg.")},
          ["distance", "consumption", "price"]),
]

_EXTRA_SPECS.extend(_MORE_SPECS)


_MORE_SPECS2 = [
    _tool("add_event", "Put an appointment or event in the calendar. Use for 'add dentist tomorrow at 3 PM', 'remind me about the meeting on Friday at 9', 'put a party on October 30'.",
          {"title": _text("What it is, e.g. 'dentist'."), "when": _text("When, e.g. 'tomorrow at 3 PM'. Also 'Friday 18:30', 'October 30 at 6 PM', 'in 2 weeks'.")}, ["title", "when"]),
    _tool("get_events", "Read the calendar. Use for 'what do I have today', 'what is on tomorrow', 'what is my week like', 'am I free on Friday'.",
          {"day": _text("Which day, e.g. 'today'. Also 'tomorrow', 'Friday', 'October 30', or 'week'.")}),
    _tool("cancel_event", "Delete an upcoming calendar event. Use for 'cancel the dentist', 'remove the meeting from my calendar'.",
          {"title": _text("Words from the event, e.g. 'dentist'.")}, ["title"]),
    _tool("log_habit", "Count something the user did, for tracking. Use for 'I drank a glass of water', 'log a workout', 'I read 20 pages'.",
          {"habit": _text("What was done, e.g. 'water'. Also 'workout', 'pages'."), "amount": _text("How many, e.g. '1'. Use '20' for 20 pages.")}, ["habit"]),
    _tool("habit_stats", "Totals and streak of counted habits. Use for 'how much water did I drink', 'how many workouts this week', 'what is my streak'.",
          {"habit": _text("Which habit, e.g. 'water'. Leave empty for all of them.")}),
    _tool("convert_time", "Convert a clock time between two places. Use for 'when it is 3 PM in Prague what time is it in Tokyo', 'my call is at 9 in New York, what is that for me'.",
          {"clock": _text("The time, e.g. '3 PM'."), "from_place": _text("Where that time is, e.g. 'Prague'. Leave empty for here."), "to_place": _text("Where to convert to, e.g. 'Tokyo'.")}, ["clock", "to_place"]),
    _tool("compare_deals", "Which of two offers is cheaper per unit. Use for 'is 500 grams for 40 or 1 kilo for 70 cheaper', 'compare two shampoos'.",
          {"price_a": _text("Price of the first offer, e.g. '40'."), "amount_a": _text("Amount of the first, with unit, e.g. '500 g'."),
           "price_b": _text("Price of the second offer, e.g. '70'."), "amount_b": _text("Amount of the second, with unit, e.g. '1 kg'.")}, ["price_a", "amount_a", "price_b", "amount_b"]),
    _tool("loan_payment", "Monthly payment of a loan or mortgage. Use for 'what is the payment on a 3 million loan at 5 percent for 20 years'.",
          {"amount": _text("Loan amount, e.g. '3000000'."), "rate": _text("Yearly interest in percent, e.g. '5'."), "years": _text("Length, e.g. '20'. Also '36 months'.")}, ["amount", "rate", "years"]),
    _tool("compound_interest", "What savings grow to. Use for 'what is 10000 worth in 10 years at 4 percent', 'if I save 200 a month for 5 years'.",
          {"amount": _text("Starting amount, e.g. '10000'. Use '0' if none."), "rate": _text("Yearly interest in percent, e.g. '4'."), "years": _text("How long, e.g. '10'."),
           "monthly_deposit": _text("Saved every month, e.g. '200'. Leave empty if none.")}, ["amount", "rate", "years"]),
    _tool("vat_calculator", "Add VAT to a price or take it out. Use for 'what is 1000 with VAT', 'how much VAT is in 1210'.",
          {"amount": _text("The amount, e.g. '1000'."), "rate": _text("VAT percent, e.g. '21'. Leave empty for the usual rate."),
           "mode": _text("'add' for a price without VAT, 'remove' for a price that already includes VAT.")}, ["amount"]),
    _tool("sports_scores", "Today's scores for a league or one team. Use for 'how did Arsenal do', 'NBA scores', 'Champions League results', 'is there a hockey game'.",
          {"league": _text("The league, e.g. 'Premier League'. Also 'NBA', 'NHL', 'NFL', 'La Liga', 'Bundesliga', 'Champions League'."), "team": _text("Optional team, e.g. 'Arsenal'.")}, ["league"]),
    _tool("smart_home", "Control the smart home (Home Assistant): lights, switches, blinds, locks, scenes, thermostat. Use for 'turn off the living room light', 'dim the bedroom lamp to 30 percent', 'close the blinds', 'lock the door'.",
          {"device": _text("The device, e.g. 'living room light'."), "action": _text("'on', 'off', 'toggle', 'open', 'close', 'lock', 'unlock'. For a level use 'set'."),
           "value": _text("A number when needed, e.g. '30' for brightness percent or '21' for degrees.")}, ["device", "action"]),
    _tool("home_status", "State of a smart home device or sensor, or which lights are on. Use for 'is the garage door closed', 'how warm is the bedroom', 'which lights are on'.",
          {"device": _text("The device or sensor, e.g. 'bedroom temperature'. Leave empty to list lights that are on.")}),
    _tool("wake_on_lan", "Wake up a computer on the home network. Use for 'turn on my PC', 'wake the NAS'.",
          {"device": _text("The device name, e.g. 'pc'.")}),
    _tool("cook_timer", "Start a timer with the usual cooking time of a food. Use for 'boil an egg', 'timer for the pasta', 'brew green tea'.",
          {"food": _text("The food, e.g. 'egg'. Also 'pasta', 'rice', 'potatoes', 'dumplings', 'tea'."), "doneness": _text("Optional, e.g. 'soft', 'hard', 'brown', 'green'.")}, ["food"]),
    _tool("recipe_by_ingredient", "Dishes you can make with an ingredient. Use for 'what can I cook with chicken', 'ideas for potatoes'.",
          {"ingredient": _text("The ingredient, e.g. 'chicken'.")}, ["ingredient"]),
    _tool("nutrition_info", "Calories, protein, carbs and fat of a food. Use for 'how many calories in a banana', 'nutrition of 200 grams of rice'.",
          {"food": _text("The food, e.g. 'banana'."), "amount": _text("Optional weight, e.g. '200 g'.")}, ["food"]),
    _tool("name_day", "Czech name day for a day. Use for 'who has a name day today', 'name day tomorrow'.",
          {"day": _text("Which day, e.g. 'tomorrow'. Leave empty for today.")}),
    _tool("quick_workout", "A short bodyweight workout. Use for 'give me a 10 minute workout', 'a quick ab workout', 'I want to exercise'.",
          {"minutes": _text("Length in minutes, e.g. '10'. 5 to 30."), "focus": _text("'legs', 'arms', 'core' or 'cardio'. Leave empty for full body.")}),
]
_EXTRA_SPECS.extend(_MORE_SPECS2)

# ---------------------------------------------------------------------------
# 20 more tools: sky & network, calculators, health, kitchen, converters, fun
# ---------------------------------------------------------------------------

def _floats(text) -> list:
    """All numbers in a text. Thousands separators ('1,200') are removed first."""
    cleaned = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", str(text or ""))
    return [float(m) for m in re.findall(r"-?\d+(?:\.\d+)?", cleaned)]


def _first_float(text, default=None):
    found = _floats(text)
    return found[0] if found else default


def _cash(x: float) -> str:
    """An amount with the user's currency label, e.g. '1,200 CZK'."""
    return f"{_money(x)} {_env('EXPENSE_CURRENCY')}".strip()


def _and_join(items: list) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


_COMPASS = ["north", "north-northeast", "northeast", "east-northeast", "east", "east-southeast",
            "southeast", "south-southeast", "south", "south-southwest", "southwest", "west-southwest",
            "west", "west-northwest", "northwest", "north-northwest"]


def uv_index(location: str = "") -> str:
    """Current and peak UV index with sunscreen advice (Open-Meteo, no key)."""
    place = _geocode(location)
    if not place:
        return _no_place(location)
    data = _get("https://api.open-meteo.com/v1/forecast",
                {"latitude": place["latitude"], "longitude": place["longitude"],
                 "current": "uv_index", "daily": "uv_index_max", "timezone": "auto"})
    now = ((data or {}).get("current") or {}).get("uv_index")
    peaks = ((data or {}).get("daily") or {}).get("uv_index_max") or [None]
    peak = peaks[0]
    if now is None and peak is None:
        return "Sorry, I could not get the UV index"

    def level(v: float) -> str:
        return ("low" if v < 3 else "moderate" if v < 6 else "high" if v < 8
                else "very high" if v < 11 else "extreme")

    name = place.get("name") or "there"
    parts = []
    if now is not None:
        parts.append(f"In {name} the UV index is {_short(now)} right now, which is {level(now)}")
    if peak is not None:
        parts.append(f"today's peak is {_short(peak)}, {level(peak)}")
    text = ", and ".join(parts) if len(parts) == 2 else parts[0]
    if peak is not None and peak >= 3:
        text += ". Sunscreen is a good idea"
    return text


def wind_info(location: str = "") -> str:
    """Current wind speed, gusts and direction (Open-Meteo, no key)."""
    place = _geocode(location)
    if not place:
        return _no_place(location)
    data = _get("https://api.open-meteo.com/v1/forecast",
                {"latitude": place["latitude"], "longitude": place["longitude"],
                 "current": "wind_speed_10m,wind_gusts_10m,wind_direction_10m", "timezone": "auto"})
    cur = (data or {}).get("current") or {}
    speed = cur.get("wind_speed_10m")
    if speed is None:
        return "Sorry, I could not get the wind"
    gust = cur.get("wind_gusts_10m")
    degrees = cur.get("wind_direction_10m")
    feel = ("calm" if speed < 5 else "a light breeze" if speed < 20 else "a moderate wind" if speed < 40
            else "a strong wind" if speed < 60 else "a storm-force wind")
    text = f"In {place.get('name') or 'there'} it is {feel}, {_short(speed)} kilometers per hour"
    if degrees is not None:
        text += f" from the {_COMPASS[int((degrees % 360) / 22.5 + 0.5) % 16]}"
    if gust is not None and gust - speed >= 10:
        text += f", with gusts up to {_short(gust)}"
    return text


def password_generator(length: str = "", kind: str = "") -> str:
    """A random password or PIN, read out character by character (uses `secrets`)."""
    kind = (kind or "").strip().lower()
    wanted = _first_float(length)
    if "pin" in kind or "digit" in kind or "number" in kind:
        size = max(4, min(int(wanted or 6), 12))
        chars = [secrets.choice("0123456789") for _ in range(size)]
        label = "PIN"
    else:
        size = max(8, min(int(wanted or 14), 40))
        lower, upper, digits = "abcdefghijkmnopqrstuvwxyz", "ABCDEFGHJKLMNPQRSTUVWXYZ", "23456789"
        pool = lower + upper + digits
        while True:
            chars = [secrets.choice(pool) for _ in range(size)]
            if (any(c in lower for c in chars) and any(c in upper for c in chars)
                    and any(c in digits for c in chars)):
                break
        label = "password"
    spoken = ", ".join(f"capital {c}" if c.isupper() else c for c in chars)
    return f"Here is a {size} character {label}: {spoken}"


_ROMAN_PAIRS = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
                (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]


def _to_roman(n: int) -> str:
    out = []
    for value, symbol in _ROMAN_PAIRS:
        while n >= value:
            out.append(symbol)
            n -= value
    return "".join(out)


def _from_roman(s: str) -> int:
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    for i, ch in enumerate(s):
        v = values[ch]
        total += -v if i + 1 < len(s) and values[s[i + 1]] > v else v
    return total


def roman_numerals(value: str = "") -> str:
    """Convert a number to Roman numerals or Roman numerals to a number."""
    s = re.sub(r"[\s,]", "", str(value or "")).upper()
    if re.fullmatch(r"\d+", s):
        n = int(s)
        if not 1 <= n <= 3999:
            return "Roman numerals only cover 1 to 3999"
        return f"{n} in Roman numerals is {' '.join(_to_roman(n))}"
    if re.fullmatch(r"[IVXLCDM]+", s):
        n = _from_roman(s)
        if _to_roman(n) != s:
            return f"{' '.join(s)} is not a valid Roman numeral"
        return f"{' '.join(s)} is {n}"
    return "Tell me a number or a Roman numeral"


_NW_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
            "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
            "eighteen", "nineteen"]
_NW_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_NW_SCALES = ["", "thousand", "million", "billion", "trillion"]


def _under_thousand_words(n: int) -> str:
    parts = []
    if n >= 100:
        parts.append(f"{_NW_ONES[n // 100]} hundred")
        n %= 100
    if n:
        if n < 20:
            parts.append(_NW_ONES[n])
        else:
            parts.append(_NW_TENS[n // 10] + (f"-{_NW_ONES[n % 10]}" if n % 10 else ""))
    return " ".join(parts)


def _int_words(n: int) -> str:
    if n == 0:
        return "zero"
    chunks, i = [], 0
    while n:
        n, rest = divmod(n, 1000)
        if rest:
            chunks.append(" ".join(x for x in (_under_thousand_words(rest), _NW_SCALES[i]) if x))
        i += 1
    return " ".join(reversed(chunks))


def number_to_words(number: str = "") -> str:
    """Spell a number in words, e.g. for writing a cheque."""
    s = re.sub(r"[\s,]", "", str(number or ""))
    m = re.fullmatch(r"(-?)(\d+)(?:\.(\d+))?", s)
    if not m:
        return "Tell me a number to spell out"
    if len(m.group(2)) > 15:
        return "That number is too big for me to spell"
    text = ("minus " if m.group(1) else "") + _int_words(int(m.group(2)))
    if m.group(3):
        text += " point " + " ".join(_NW_ONES[int(d)] for d in m.group(3))
    return f"{s} is {text}"


_BASE_NAMES = {"binary": 2, "bin": 2, "octal": 8, "oct": 8, "decimal": 10, "dec": 10,
               "hex": 16, "hexadecimal": 16}
_BASE_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def _base_of(name: str):
    s = str(name or "").strip().lower()
    if not s:
        return None
    if s in _BASE_NAMES:
        return _BASE_NAMES[s]
    m = re.search(r"\d+", s)
    return int(m.group()) if m and 2 <= int(m.group()) <= 36 else None


def _to_base(n: int, base: int) -> str:
    if n == 0:
        return "0"
    sign, n, out = ("-" if n < 0 else ""), abs(n), []
    while n:
        n, r = divmod(n, base)
        out.append(_BASE_DIGITS[r])
    return sign + "".join(reversed(out))


def _base_name(base: int) -> str:
    return {2: "binary", 8: "octal", 10: "decimal", 16: "hexadecimal"}.get(base, f"base {base}")


def base_converter(number: str = "", from_base: str = "", to_base: str = "") -> str:
    """Convert between binary, octal, decimal, hexadecimal and any base up to 36."""
    s = re.sub(r"[\s_]", "", str(number or "").lower())
    if not s or len(s) > 64:
        return "Tell me the number to convert"
    src = _base_of(from_base)
    for prefix, b in (("0x", 16), ("0b", 2), ("0o", 8)):
        body = s.lstrip("-")
        if body.startswith(prefix):
            src = src or b
            s = ("-" if s.startswith("-") else "") + body[len(prefix):]
    src = src or (16 if re.search(r"[a-f]", s) else 10)
    try:
        value = int(s, src)
    except ValueError:
        return f"{number} is not a valid {_base_name(src)} number"
    dst = _base_of(to_base)
    targets = [dst] if dst else ([2, 8, 16] if src == 10 else [10])

    def shown(n: int, base: int) -> str:
        text = _to_base(n, base)
        return text if base == 10 else " ".join(text)

    results = [f"{shown(value, b)} in {_base_name(b)}" for b in targets if b != src] or [f"{value} in {_base_name(src)}"]
    return f"{number} in {_base_name(src)} is " + _and_join(results)


_SI_WORDS = (("milli", 1e-3), ("micro", 1e-6), ("mega", 1e6), ("meg", 1e6), ("kilo", 1e3))


def _si_value(text):
    """'20 mA' -> 0.02, '2.2k' -> 2200, '5 volts' -> 5."""
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)\s*([a-zA-Zµ]*)", str(text or ""))
    if not m:
        return None
    value, unit = float(m.group(1)), m.group(2)
    low = unit.lower()
    for word, factor in _SI_WORDS:
        if low.startswith(word):
            return value * factor
    if unit[:1] in ("µ", "u"):
        return value * 1e-6
    if unit[:1] == "m" and len(unit) <= 2:
        return value * 1e-3
    if unit[:1] == "M":
        return value * 1e6
    if unit[:1] == "k" or unit[:1] == "K":
        return value * 1e3
    return value


def _eng(value: float, unit: str) -> str:
    for factor, prefix in ((1e6, "mega"), (1e3, "kilo"), (1, ""), (1e-3, "milli")):
        if abs(value) >= factor:
            return f"{_short(value / factor)} {prefix}{unit}"
    return f"{_short(value / 1e-6)} micro{unit}"


def ohms_law(voltage: str = "", current: str = "", resistance: str = "", power: str = "") -> str:
    """Give any two of voltage, current, resistance, power and get the other two."""
    given = {k: _si_value(v) for k, v in
             (("v", voltage), ("i", current), ("r", resistance), ("p", power)) if str(v or "").strip()}
    given = {k: v for k, v in given.items() if v is not None}
    if len(given) < 2:
        return "Give me any two of voltage, current, resistance or power"
    v, i, r, p = (given.get(k) for k in ("v", "i", "r", "p"))
    try:
        if v is not None and i is not None:
            r, p = v / i, v * i
        elif v is not None and r is not None:
            i, p = v / r, v * v / r
        elif i is not None and r is not None:
            v, p = i * r, i * i * r
        elif v is not None and p is not None:
            i, r = p / v, v * v / p
        elif i is not None and p is not None:
            v, r = p / i, p / (i * i)
        else:
            v, i = math.sqrt(p * r), math.sqrt(p / r)
    except (ZeroDivisionError, ValueError):
        return "Those values do not work together"
    return (f"That gives {_eng(v, 'volts')}, {_eng(i, 'amps')}, {_eng(r, 'ohms')} "
            f"and {_eng(p, 'watts')}")


def paint_calculator(room: str = "", coats: str = "2", ceiling: str = "") -> str:
    """Liters of paint for a room, from length, width and optional wall height."""
    nums = _floats(room)
    if len(nums) < 2:
        return "Tell me the room size, like 4 by 5 meters"
    feet = bool(re.search(r"\b(ft|feet|foot)\b", str(room).lower()))
    k = 0.3048 if feet else 1.0
    length, width = nums[0] * k, nums[1] * k
    height = nums[2] * k if len(nums) >= 3 else 2.5
    n_coats = max(1, min(int(_first_float(coats, 2)), 4))
    walls = 2 * (length + width) * height
    area = walls + (length * width if re.search(r"yes|ceil|true|with", str(ceiling).lower()) else 0)
    liters = math.ceil(area * n_coats / 10 * 2) / 2
    ceil_note = " including the ceiling" if area > walls else ""
    return (f"About {_short(area)} square meters{ceil_note}. For {n_coats} coats you need roughly "
            f"{_short(liters)} liters of paint, at 10 square meters per liter. Subtract doors and windows if you like")


def _md_y(text):
    """(month, day, year or None) from 'tomorrow', '2026-12-24', '24.12.2026', 'March 14', 'Dec 24th 2026'."""
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", str(text or "").strip().lower())
    today = date.today()
    if s in ("", "today", "now"):
        return today.month, today.day, today.year
    if s in ("tomorrow", "yesterday"):
        d = today + timedelta(days=1 if s == "tomorrow" else -1)
        return d.month, d.day, d.year
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return int(m.group(2)), int(m.group(3)), int(m.group(1))
    m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", s)
    if m:
        return int(m.group(2)), int(m.group(1)), int(m.group(3))
    names = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    month = next((i + 1 for i, n in enumerate(names) if re.search(rf"\b{n}[a-z]*", s)), None)
    nums = [int(x) for x in re.findall(r"\d+", s)]
    day = next((n for n in nums if 1 <= n <= 31), None)
    year = next((n for n in nums if n >= 1000), None)
    return (month, day, year) if month and day else None


def calendar_facts(date_text: str = "") -> str:
    """Weekday, week number, day of the year and leap-year status for a date."""
    parsed = _md_y(date_text)
    if not parsed:
        return "I could not understand that date"
    month, day, year = parsed
    try:
        d = date(year or date.today().year, month, day)
    except ValueError:
        return "That date does not exist"
    total = 366 if calendar.isleap(d.year) else 365
    yday = d.timetuple().tm_yday
    left = total - yday
    text = (f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}, {d.year} is day {yday} of {total} "
            f"and falls in week {d.isocalendar()[1]}. {d.year} is {'' if total == 366 else 'not '}a leap year")
    if left == 0:
        return text + ". It is the last day of the year"
    return text + f". {left} day{'' if left == 1 else 's'} left in the year"


_MORSE = {"a": ".-", "b": "-...", "c": "-.-.", "d": "-..", "e": ".", "f": "..-.", "g": "--.", "h": "....",
          "i": "..", "j": ".---", "k": "-.-", "l": ".-..", "m": "--", "n": "-.", "o": "---", "p": ".--.",
          "q": "--.-", "r": ".-.", "s": "...", "t": "-", "u": "..-", "v": "...-", "w": ".--", "x": "-..-",
          "y": "-.--", "z": "--..", "1": ".----", "2": "..---", "3": "...--", "4": "....-", "5": ".....",
          "6": "-....", "7": "--...", "8": "---..", "9": "----.", "0": "-----"}
_MORSE_BACK = {v: k for k, v in _MORSE.items()}


def morse_code(text: str = "") -> str:
    """Encode text to Morse (spoken as dots and dashes) or decode dots and dashes."""
    raw = str(text or "").strip().lower()
    if not raw:
        return "Tell me what to translate into Morse code"
    symbols = re.sub(r"\bdot\b", ".", re.sub(r"\bdash\b", "-", raw.replace("slash", "/")))
    if re.fullmatch(r"[.\-/\s,|]+", symbols):
        words = re.split(r"\s*[/|]\s*", symbols.strip())
        decoded = ["".join(_MORSE_BACK.get(code, "?") for code in re.split(r"[\s,]+", w.strip()) if code)
                   for w in words]
        return "That says " + " ".join(decoded).strip().upper()
    if len(raw) > 40:
        return "That is too long for me to say in Morse code. Keep it under 40 characters"
    spoken = []
    for word in raw.split():
        letters = [_MORSE[c] for c in word if c in _MORSE]
        if letters:
            spoken.append(", ".join(c.replace(".", "dot ").replace("-", "dash ").strip() for c in letters))
    return f"{raw} in Morse code is " + "; new word; ".join(spoken) if spoken else "I could not find any letters to translate"


def heart_rate_zones(age: str = "", resting_heart_rate: str = "") -> str:
    """Estimated max heart rate and training zones (Tanaka formula, Karvonen with resting rate)."""
    years = _first_float(age)
    if years is None or not 5 <= years <= 100:
        return "Tell me your age, like 35"
    peak = 208 - 0.7 * years
    rest = _first_float(resting_heart_rate)
    if rest is not None and not 30 <= rest < peak:
        rest = None

    def bpm(fraction: float) -> int:
        return round(rest + fraction * (peak - rest)) if rest is not None else round(peak * fraction)

    how = " using your resting rate" if rest is not None else ""
    return (f"Your estimated maximum heart rate is {round(peak)} beats per minute{how}. "
            f"Easy zone: {bpm(.6)} to {bpm(.7)}. Aerobic zone: {bpm(.7)} to {bpm(.8)}. "
            f"Hard zone: {bpm(.8)} to {bpm(.9)}. Above {bpm(.9)} is peak effort")


def water_intake(weight: str = "", exercise_minutes: str = "") -> str:
    """Rough daily water need from body weight and exercise."""
    value = _first_float(weight)
    if value is None:
        return "Tell me your weight, like 70 kilos"
    kg = value * 0.4536 if re.search(r"lb|pound|stone", str(weight).lower()) else value
    if not 20 <= kg <= 300:
        return "That weight does not look right to me"
    minutes = max(0.0, min(_first_float(exercise_minutes, 0.0), 600.0))
    liters = kg * 0.033 + minutes / 60 * 0.5
    text = f"You need about {_short(round(liters, 1))} liters of water a day, around {round(liters / 0.25)} glasses"
    if minutes:
        text += f", counting {_short(minutes)} minutes of exercise"
    return text + ". More if it is hot"


def electricity_cost(power: str = "", hours_per_day: str = "", price_per_kwh: str = "") -> str:
    """Running cost of a device from its power draw, daily hours and the electricity price."""
    watts = _si_value(power)
    hours = _first_float(hours_per_day)
    price = _first_float(price_per_kwh, _first_float(_env("ELECTRICITY_PRICE")))
    if watts is None or watts <= 0:
        return "Tell me the power of the device, like 100 watts"
    if hours is None or not 0 < hours <= 24:
        return "Tell me how many hours a day it runs"
    if price is None:
        return "Tell me the electricity price per kilowatt hour"
    daily_kwh = watts / 1000 * hours
    daily = daily_kwh * price
    return (f"It uses {_short(daily_kwh)} kilowatt hours a day, which costs {_cash(daily)} a day, "
            f"about {_cash(daily * 30)} a month and {_cash(daily * 365)} a year")


def discount_calculator(price: str = "", discount: str = "", extra_discount: str = "") -> str:
    """Sale price after one or two percentage discounts."""
    amount, first = _first_float(price), _first_float(discount)
    second = _first_float(extra_discount, 0.0)
    if amount is None or first is None:
        return "Tell me the price and the discount percent"
    if not (0 <= first <= 100 and 0 <= second <= 100):
        return "A discount has to be between 0 and 100 percent"
    final = amount * (1 - first / 100) * (1 - second / 100)
    both = f"{_short(first)} and then {_short(second)} percent" if second else f"{_short(first)} percent"
    return f"With {both} off, it costs {_cash(final)}. You save {_cash(amount - final)}"


def statistics_summary(numbers: str = "") -> str:
    """Count, total, average, median, extremes and spread of a list of numbers."""
    values = _floats(numbers)
    if not values:
        return "Tell me some numbers"
    if len(values) > 200:
        return "That is too many numbers for me to read out, keep it under 200"
    text = (f"{len(values)} number{'s' if len(values) != 1 else ''}. Total {_short(sum(values))}, "
            f"average {_short(statistics.fmean(values))}, median {_short(statistics.median(values))}, "
            f"lowest {_short(min(values))}, highest {_short(max(values))}")
    if len(values) >= 2:
        text += f", standard deviation {_short(statistics.stdev(values))}"
    return text


_ZODIAC_STARTS = [(1, 20, "Aquarius"), (2, 19, "Pisces"), (3, 21, "Aries"), (4, 20, "Taurus"),
                  (5, 21, "Gemini"), (6, 21, "Cancer"), (7, 23, "Leo"), (8, 23, "Virgo"),
                  (9, 23, "Libra"), (10, 23, "Scorpio"), (11, 22, "Sagittarius"), (12, 22, "Capricorn")]
_CHINESE_ZODIAC = ["Rat", "Ox", "Tiger", "Rabbit", "Dragon", "Snake", "Horse", "Goat", "Monkey",
                   "Rooster", "Dog", "Pig"]


def zodiac_sign(birthday: str = "") -> str:
    """Western star sign for a birth date, plus the Chinese animal when a year is given."""
    parsed = _md_y(birthday) if str(birthday or "").strip() else None
    if not parsed:
        return "Tell me a birthday, like March 14 1990"
    month, day, year = parsed
    try:
        date(2000, month, day)
    except ValueError:
        return "That date does not exist"
    sign = "Capricorn"
    for m, d, name in _ZODIAC_STARTS:
        if (month, day) >= (m, d):
            sign = name
    text = f"Someone born on {date(2000, month, day).strftime('%B')} {day} is a {sign}"
    if year:
        text += f". In the Chinese zodiac {year} is the year of the {_CHINESE_ZODIAC[(year - 4) % 12]}"
        if month <= 2:
            text += ", unless the birthday is before Lunar New Year"
    return text


def ping_host(host: str = "", port: str = "") -> str:
    """Check whether a host answers, and how fast, by timing a TCP connection."""
    m = re.match(r"^\s*(?:\w+://)?([a-z0-9._-]+)(?::(\d+))?", str(host or "").lower())
    if not m:
        return "Tell me which host to check"
    name = m.group(1)
    explicit = _first_float(port) or (int(m.group(2)) if m.group(2) else None)
    ports = [int(explicit)] if explicit and 1 <= explicit <= 65535 else [443, 80]
    for p in ports:
        start = time.perf_counter()
        try:
            with socket.create_connection((name, p), timeout=3):
                ms = (time.perf_counter() - start) * 1000
            return f"{name} answered in {round(ms)} milliseconds on port {p}"
        except socket.gaierror:
            return f"I could not find a host called {name}"
        except OSError:
            continue
    return f"{name} did not answer on port {_and_join([str(p) for p in ports]).replace(' and ', ' or ')}"


_GRAMS_PER_CUP = {
    "powdered sugar": 120, "icing sugar": 120, "brown sugar": 213, "sugar": 200, "flour": 120,
    "butter": 227, "honey": 340, "milk": 245, "water": 237, "rice": 185, "oats": 90, "cocoa": 85,
    "olive oil": 216, "oil": 218, "salt": 288, "yogurt": 245, "cream": 238, "peanut butter": 258,
    "chocolate chips": 170, "parmesan": 100,
}
_COOK_UNIT_PATTERNS = ((r"tbsp|tbs|tablespoons?", "tbsp"), (r"tsp|teaspoons?", "tsp"), (r"cups?", "cup"),
                       (r"kg|kilograms?|kilos?", "kg"), (r"g|grams?", "g"), (r"oz|ounces?", "oz"),
                       (r"lbs?|pounds?", "lb"), (r"ml|milliliters?|millilitres?", "ml"))
_COOK_LABELS = {"cup": "cups", "tbsp": "tablespoons", "tsp": "teaspoons", "g": "grams", "kg": "kilograms",
                "oz": "ounces", "lb": "pounds", "ml": "milliliters"}


def _cook_unit(word: str):
    w = str(word or "").strip().lower().rstrip(".")
    return next((unit for pattern, unit in _COOK_UNIT_PATTERNS if re.fullmatch(pattern, w)), None)


def _qty(text: str):
    """(number, rest) from '2 cups', '1/2 cup', '1 1/2 cups'."""
    s = str(text or "").lower().strip()
    m = re.match(r"(\d+)\s+(\d+)\s*/\s*(\d+)\s*(.*)", s)
    if m and int(m.group(3)):
        return int(m.group(1)) + int(m.group(2)) / int(m.group(3)), m.group(4).strip()
    m = re.match(r"(\d+)\s*/\s*(\d+)\s*(.*)", s)
    if m and int(m.group(2)):
        return int(m.group(1)) / int(m.group(2)), m.group(3).strip()
    m = re.match(r"(\d+(?:\.\d+)?)\s*(.*)", s)
    return (float(m.group(1)), m.group(2).strip()) if m else None


def cooking_measure(amount: str = "", ingredient: str = "", to: str = "") -> str:
    """Convert baking measures between cups, spoons, grams and ounces for common ingredients."""
    parsed = _qty(amount)
    if not parsed:
        return "Tell me the amount, like 2 cups"
    value, rest = parsed
    words = rest.split()
    src = _cook_unit(words[0]) if words else None
    if not src:
        return "Tell me the unit, like cups, tablespoons or grams"
    item = re.sub(r"\bof\b", " ", (ingredient or " ".join(words[1:])).lower()).strip()
    key = next((k for k in sorted(_GRAMS_PER_CUP, key=len, reverse=True) if k in item), None)
    if not key:
        return "I know flour, sugar, butter, honey, milk, water, rice, oats, cocoa, oil, salt and a few more"
    cup = _GRAMS_PER_CUP[key]
    per_unit = {"cup": cup, "tbsp": cup / 16, "tsp": cup / 48, "ml": cup / 236.588,
                "g": 1, "kg": 1000, "oz": 28.3495, "lb": 453.592}
    dst = _cook_unit(to) or ("g" if src in ("cup", "tbsp", "tsp", "ml") else "cup")
    result = value * per_unit[src] / per_unit[dst]
    def label(unit: str, n: float) -> str:
        word = _COOK_LABELS[unit]
        return word[:-1] if abs(n - 1) < 1e-9 else word

    return f"{_short(value)} {label(src, value)} of {key} is about {_short(result)} {label(dst, result)}"


def _data_parse(text: str):
    """(amount in base units, is_bits, unit text) from '4 GB', '100 Mbps', '50 megabytes per second'."""
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*([a-z/ ]*)", str(text or "").lower())
    if not m:
        return None
    unit = m.group(2).strip()
    factor = 1.0
    for key, f in (("tera", 1e12), ("giga", 1e9), ("mega", 1e6), ("kilo", 1e3),
                   ("t", 1e12), ("g", 1e9), ("m", 1e6), ("k", 1e3)):
        if unit.startswith(key):
            factor = f
            break
    return float(m.group(1)) * factor, ("bit" in unit or "bps" in unit), unit


def _spoken_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 1:
        return "less than a second"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [f"{n} {name}{'' if n == 1 else 's'}"
             for n, name in ((days, "day"), (hours, "hour"), (minutes, "minute"), (secs, "second")) if n]
    return _and_join(parts[:2])


def download_time(size: str = "", speed: str = "") -> str:
    """How long a download takes from file size and connection speed."""
    file = _data_parse(size)
    if not file or not file[2]:
        return "Tell me the file size, like 4 gigabytes"
    link = _data_parse(speed)
    if not link:
        return "Tell me the connection speed, like 100 megabits per second"
    if not link[2]:  # a bare number is read as megabits per second, the usual way ISPs quote it
        link = (link[0] * 1e6, True, "")
    file_bytes = file[0] / 8 if file[1] else file[0]
    link_bytes = link[0] / 8 if link[1] else link[0]
    if link_bytes <= 0:
        return "That speed does not look right"
    return f"It would take about {_spoken_duration(file_bytes / link_bytes)} at full speed"


def hourly_wage(amount: str = "", period: str = "hour", hours_per_week: str = "40") -> str:
    """Convert pay between hourly, weekly, monthly and yearly."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(k|thousand|m|million)?\b",
                  re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", str(amount or "").lower()))
    if not m:
        return "Tell me the amount, like 30 an hour or 60 thousand a year"
    value = float(m.group(1)) * {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6}.get(m.group(2), 1)
    p = str(period or "hour").lower()
    p = ("hour" if "hour" in p else "week" if "week" in p else "month" if "month" in p
         else "year" if re.search(r"year|annual|salary", p) else "hour")
    hours = _first_float(hours_per_week, 40.0)
    if not 0 < hours <= 100:
        return "Hours per week should be between 1 and 100"
    weekly = {"hour": value * hours, "week": value, "month": value * 12 / 52, "year": value / 52}[p]
    figures = {"hour": weekly / hours, "week": weekly, "month": weekly * 52 / 12, "year": weekly * 52}
    per = {"hour": "an hour", "week": "a week", "month": "a month", "year": "a year"}
    others = [f"{_cash(figures[k])} {per[k]}" for k in ("hour", "week", "month", "year") if k != p]
    return f"{_cash(value)} {per[p]} works out to {_and_join(others)}, at {_short(hours)} hours a week"


_MORE_SPECS3 = [
    _tool("uv_index", "UV index now and today's peak, with sunscreen advice. Use for 'what is the UV index', 'do I need sunscreen', 'how strong is the sun'.",
          {"location": _text("City, e.g. 'Prague'. Leave empty for home.")}),
    _tool("wind_info", "Current wind speed, gusts and direction. Use for 'how windy is it', 'what is the wind like in Brno', 'is it stormy'.",
          {"location": _text("City, e.g. 'Prague'. Leave empty for home.")}),
    _tool("password_generator", "Generate a random password or PIN and read it out. Use for 'make me a password', 'give me a 6 digit PIN'.",
          {"length": _text("How many characters, e.g. '14'."), "kind": _text("'password' normally, e.g. 'password'. Use 'pin' for digits only.")}),
    _tool("roman_numerals", "Convert a number to Roman numerals or back. Use for 'what is 2026 in Roman numerals', 'what is XIV'.",
          {"value": _text("A number like '2026' or Roman numerals like 'XIV'.")}, ["value"]),
    _tool("number_to_words", "Spell a number in words. Use for 'write 1250 in words', 'how do you spell the number 4096'.",
          {"number": _text("The number, e.g. '1250'. Decimals like '3.14' work too.")}, ["number"]),
    _tool("base_converter", "Convert numbers between binary, octal, decimal and hexadecimal. Use for 'what is 255 in binary', 'convert ff from hex to decimal'.",
          {"number": _text("The number, e.g. '255'. Also 'ff', '1010', '0x1F'."), "from_base": _text("Its base, e.g. 'decimal'. Also 'binary', 'octal', 'hex'. Leave empty for decimal."),
           "to_base": _text("Target base, e.g. 'binary'. Leave empty to show the usual ones.")}, ["number"]),
    _tool("ohms_law", "Electronics: give any two of voltage, current, resistance and power, get the rest. Use for 'what resistor for 5 volts and 20 milliamps', 'current through 220 ohms at 3.3 volts'.",
          {"voltage": _text("e.g. '5 V' or '3.3'."), "current": _text("e.g. '20 mA'."), "resistance": _text("e.g. '220 ohm' or '4.7k'."), "power": _text("e.g. '0.5 W'.")}),
    _tool("paint_calculator", "How much paint a room needs. Use for 'how much paint for a 4 by 5 meter room', 'paint for my bedroom'.",
          {"room": _text("Length, width and optional wall height, e.g. '4 by 5 meters, 2.5 high'. Say 'feet' for feet."),
           "coats": _text("Number of coats, e.g. '2'."), "ceiling": _text("'yes' to include the ceiling. Leave empty otherwise.")}, ["room"]),
    _tool("calendar_facts", "Weekday, week number, day of the year and leap year for a date. Use for 'what week is it', 'what day of the year is it', 'is 2028 a leap year', 'what weekday is December 24'.",
          {"date_text": _text("The date, e.g. 'December 24'. Also 'tomorrow', '2028-02-29'. Leave empty for today.")}),
    _tool("morse_code", "Translate text into Morse code or Morse back into text. Use for 'say SOS in Morse code', 'what is dot dash dot in Morse'.",
          {"text": _text("The words to encode, e.g. 'SOS'. Or dots and dashes to decode, e.g. '... --- ...'.")}, ["text"]),
    _tool("heart_rate_zones", "Heart rate training zones from age. Use for 'what is my max heart rate', 'heart rate zones for a 40 year old'.",
          {"age": _text("Age in years, e.g. '35'."), "resting_heart_rate": _text("Optional resting pulse, e.g. '60'.")}, ["age"]),
    _tool("water_intake", "How much water to drink a day. Use for 'how much water should I drink', 'daily water for 70 kilos'.",
          {"weight": _text("Body weight, e.g. '70 kg'. Also '150 pounds'."), "exercise_minutes": _text("Optional minutes of exercise per day, e.g. '45'.")}, ["weight"]),
    _tool("electricity_cost", "Running cost of an appliance. Use for 'what does a 2000 watt heater cost to run', 'cost of leaving a 100 watt PC on all day'.",
          {"power": _text("Device power, e.g. '100 W'. Also '2 kW'."), "hours_per_day": _text("Hours it runs per day, e.g. '8'."),
           "price_per_kwh": _text("Electricity price per kilowatt hour, e.g. '5'. Leave empty for the saved price.")}, ["power", "hours_per_day"]),
    _tool("discount_calculator", "Price after a percentage discount, or two stacked discounts. Use for 'what is 1200 with 30 percent off', 'a jacket for 90 at 20 percent off and another 10'.",
          {"price": _text("Original price, e.g. '1200'."), "discount": _text("Discount percent, e.g. '30'."), "extra_discount": _text("Optional second discount percent, e.g. '10'.")}, ["price", "discount"]),
    _tool("statistics_summary", "Average, median, total, lowest, highest and spread of a list of numbers. Use for 'average of 4, 8 and 15', 'what is the median of these numbers'.",
          {"numbers": _text("The numbers, e.g. '4, 8, 15, 16, 23'.")}, ["numbers"]),
    _tool("zodiac_sign", "Star sign and Chinese zodiac animal for a birthday. Use for 'what sign is March 14', 'what is my zodiac if I was born in 1990'.",
          {"birthday": _text("The birthday, e.g. 'March 14 1990'. Without a year only the star sign is given.")}, ["birthday"]),
    _tool("ping_host", "Check whether a computer, router or server answers and how fast. Use for 'is my NAS up', 'ping google.com', 'can you reach 192.168.1.1'.",
          {"host": _text("Host name or IP, e.g. 'google.com' or '192.168.1.1'."), "port": _text("Optional port, e.g. '22'. Leave empty to try the web ports.")}, ["host"]),
    _tool("cooking_measure", "Convert baking amounts between cups, spoons, grams and ounces for an ingredient. Use for 'how many grams is a cup of flour', '200 grams of sugar in cups'.",
          {"amount": _text("Amount with unit, e.g. '2 cups'. Also '200 g', '3 tbsp'."), "ingredient": _text("The ingredient, e.g. 'flour'. Also 'sugar', 'butter', 'honey'."),
           "to": _text("Optional target unit, e.g. 'grams'. Also 'cups', 'ounces'.")}, ["amount", "ingredient"]),
    _tool("download_time", "How long a download takes. Use for 'how long to download 50 gigabytes on 100 megabit', 'how fast will a 700 MB file arrive'.",
          {"size": _text("File size, e.g. '4 GB'."), "speed": _text("Connection speed, e.g. '100 Mbps'. Also '12 MB/s'.")}, ["size", "speed"]),
    _tool("hourly_wage", "Convert pay between hourly, weekly, monthly and yearly. Use for 'what is 30 an hour per year', 'what is 60 thousand a year per hour'.",
          {"amount": _text("The pay, e.g. '30'. Also '60k'."), "period": _text("What the amount covers: 'hour', 'week', 'month' or 'year'."),
           "hours_per_week": _text("Working hours per week, e.g. '40'.")}, ["amount", "period"]),
]
_EXTRA_SPECS.extend(_MORE_SPECS3)


_DISABLED = {n.strip().lower() for n in _env("DISABLED_TOOLS").split(",") if n.strip()}
TOOLS.extend(s for s in _EXTRA_SPECS if s["function"]["name"] not in _DISABLED)

TOOL_FUNCTIONS = {
    "get_weather": get_weather,
    "get_time": get_time,
    "get_news": get_news,
    "set_timer": set_timer,
    "set_reminder": set_reminder,
    "list_timers": list_timers,
    "cancel_timer": cancel_timer,
    "stopwatch": stopwatch,
    "set_volume": set_volume,
    "get_volume": get_volume,
}

_EXTRA_FUNCS = {
    "calculate": calculate, "convert_units": convert_units, "convert_currency": convert_currency,
    "get_crypto_price": get_crypto_price, "define_word": define_word, "wiki_summary": wiki_summary,
    "translate": translate, "spell_word": spell_word, "world_time": world_time,
    "get_forecast": get_forecast, "get_sun_times": get_sun_times, "get_air_quality": get_air_quality,
    "days_until": days_until, "next_holiday": next_holiday, "moon_phase": moon_phase,
    "flip_coin": flip_coin, "roll_dice": roll_dice, "random_number": random_number,
    "pick_random": pick_random, "tell_joke": tell_joke, "fun_fact": fun_fact,
    "add_note": add_note, "read_notes": read_notes, "clear_notes": clear_notes,
    "add_to_list": add_to_list, "read_list": read_list, "remove_from_list": remove_from_list,
    "clear_list": clear_list, "pc_status": pc_status,
    "set_home_location": set_home_location, "daily_briefing": daily_briefing, "rain_check": rain_check,
    "tip_split": tip_split, "date_math": date_math, "distance_between": distance_between,
    "news_details": news_details, "animal_sound": animal_sound, "recipe_search": recipe_search,
    "get_stock_price": get_stock_price, "today_in_history": today_in_history, "trivia": trivia,
    "add_birthday": add_birthday, "get_birthdays": get_birthdays, "remove_birthday": remove_birthday,
    "log_expense": log_expense, "spending_summary": spending_summary, "undo_last_expense": undo_last_expense,
    "sleep_calculator": sleep_calculator, "magic_8ball": magic_8ball, "word_finder": word_finder,
    "inspire_me": inspire_me,
}

_EXTRA_FUNCS.update({
    "get_earthquakes": get_earthquakes, "iss_location": iss_location, "country_info": country_info,
    "public_ip": public_ip, "website_status": website_status, "github_repo": github_repo,
    "package_info": package_info, "cocktail_recipe": cocktail_recipe, "book_search": book_search,
    "music_artist": music_artist, "anime_info": anime_info, "pokemon_info": pokemon_info,
    "number_fact": number_fact, "cat_fact": cat_fact, "random_wikipedia": random_wikipedia,
    "get_pollen": get_pollen, "bmi": bmi, "daily_calories": daily_calories,
    "pace_calculator": pace_calculator, "fuel_cost": fuel_cost,
})

_EXTRA_FUNCS.update({
    "add_event": add_event, "get_events": get_events, "cancel_event": cancel_event, "log_habit": log_habit,
    "habit_stats": habit_stats, "convert_time": convert_time, "compare_deals": compare_deals, "loan_payment": loan_payment,
    "compound_interest": compound_interest, "vat_calculator": vat_calculator, "sports_scores": sports_scores,
    "smart_home": smart_home, "home_status": home_status, "wake_on_lan": wake_on_lan, "cook_timer": cook_timer,
    "recipe_by_ingredient": recipe_by_ingredient, "nutrition_info": nutrition_info, "name_day": name_day,
    "quick_workout": quick_workout,
})
_EXTRA_FUNCS.update({
    "uv_index": uv_index, "wind_info": wind_info, "password_generator": password_generator,
    "roman_numerals": roman_numerals, "number_to_words": number_to_words, "base_converter": base_converter,
    "ohms_law": ohms_law, "paint_calculator": paint_calculator, "calendar_facts": calendar_facts,
    "morse_code": morse_code, "heart_rate_zones": heart_rate_zones, "water_intake": water_intake,
    "electricity_cost": electricity_cost, "discount_calculator": discount_calculator,
    "statistics_summary": statistics_summary, "zodiac_sign": zodiac_sign, "ping_host": ping_host,
    "cooking_measure": cooking_measure, "download_time": download_time, "hourly_wage": hourly_wage,
})
TOOL_FUNCTIONS.update({n: f for n, f in _EXTRA_FUNCS.items() if n not in _DISABLED})

# Tools that make no sense inside a scheduled automation (they are for live conversation).
# set_volume stays available: "every night at 10 PM turn the volume down" is a fine automation.
AUTOMATION_EXCLUDED = {"set_timer", "set_reminder", "list_timers", "cancel_timer", "stopwatch", "get_volume"}
# Meaningless or harmful inside a scheduled automation (they need live input or change the user's data).
# Kept: forecast, sun times, air quality, crypto, currency, holidays, days_until, moon, joke, fun fact,
# read_list, read_notes, world_time, pc_status.
_EXTRA_AUTOMATION_EXCLUDED = {
    "calculate", "convert_units", "define_word", "wiki_summary", "translate", "spell_word",
    "flip_coin", "roll_dice", "random_number", "pick_random",
    "add_note", "clear_notes", "add_to_list", "remove_from_list", "clear_list",
    "set_home_location", "tip_split", "date_math", "distance_between",
    "news_details", "trivia", "add_birthday", "remove_birthday", "log_expense", "undo_last_expense",
    "sleep_calculator", "magic_8ball", "word_finder", "recipe_search",
    "bmi", "daily_calories", "pace_calculator", "fuel_cost",
}
_EXTRA_AUTOMATION_EXCLUDED |= {"add_event", "cancel_event", "log_habit", "convert_time", "compare_deals", "loan_payment",
                               "compound_interest", "vat_calculator", "cook_timer", "recipe_by_ingredient", "nutrition_info"}
# Calculators and converters need live input; uv_index, wind_info, ping_host and calendar_facts stay schedulable.
_EXTRA_AUTOMATION_EXCLUDED |= {"password_generator", "roman_numerals", "number_to_words", "base_converter",
                               "ohms_law", "paint_calculator", "morse_code", "heart_rate_zones", "water_intake",
                               "electricity_cost", "discount_calculator", "statistics_summary", "zodiac_sign",
                               "cooking_measure", "download_time", "hourly_wage"}
AUTOMATION_EXCLUDED |= _EXTRA_AUTOMATION_EXCLUDED


TOOL_TAG_RE = re.compile(
    r"<\s*(" + "|".join(re.escape(n) for n in sorted(TOOL_FUNCTIONS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def _parse_value(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    text = text.strip("\\")  # tolerate escaped quotes: \"Prague\"
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _split_args(raw: str) -> list:
    parts, buf, depth, quote = [], [], 0, None
    for ch in raw:
        if quote:
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
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _parse_args(raw: str):
    """Parse the inside of a tool tag into (args, kwargs).

    Accepts <tool; "Prague"/>, <tool; location="Prague"/>,
    <tool; {"location": "Prague"/> and <tool/> / <tool; />
    """
    raw = (raw or "").strip()
    if not raw:
        return [], {}
    if raw[0] in "{[":		# noqa: W191
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return [raw], {}
        if isinstance(parsed, dict):
            return [], parsed
        if isinstance(parsed, list):
            return parsed, {}
        return [parsed], {}
    args, kwargs = [], {}
    for item in _split_args(raw):
        if "=" in item and not item.startswith(("\"", "'")):
            key, _, value = item.partition("=")
            kwargs[key.strip()] = _parse_value(value)
        else:
            args.append(_parse_value(item))
    return args, kwargs


def _find_tool_tags(text: str) -> list:
    """Return (start, end, name, raw_arguments) for every tool tag in text.

    The model very often forgets the slash of the closing "/>" and writes the
    tag as <get_news; "topic">; that half-written tag used to be left in the
    answer and read out loud, character by character. Both closings are
    accepted now, and the body is scanned by hand so a quoted value may hold
    '>' without cutting the tag short. A tag that is never closed at all is
    not a tag and is left alone.
    """
    found, last = [], 0
    for match in TOOL_TAG_RE.finditer(text):
        if match.start() < last:  # inside a tag that was already consumed
            continue
        name = match.group(1).lower()
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
            elif depth == 0 and ch == ">":
                stop = i - 1 if i > match.end() and text[i - 1] == "/" else i
                found.append((match.start(), i + 1, name, text[match.end():stop]))
                last = i + 1
                break
            i += 1
    return found


def resolve_tool_tags(text: str) -> tuple:
    """Replace every <tool; args/> tag in text with the tool's result.

    Returns (resolved_text, number_of_tool_calls). Tags that name an unknown
    tool, and tags that are never closed, are left untouched.
    """
    calls = 0
    pieces, last = [], 0
    for start, end, name, raw in _find_tool_tags(text):
        function = TOOL_FUNCTIONS.get(name)
        if function is None:
            continue
        raw = re.sub(r"^[;,]\s*", "", raw.strip())  # the separator after the name
        args, kwargs = _parse_args(raw)
        calls += 1
        try:
            result = str(function(*args, **kwargs))
        except Exception:
            result = f"Sorry, I could not get that ({name.replace('_', ' ')} failed)"
        pieces.append(text[last:start])
        pieces.append(result)
        last = end
    pieces.append(text[last:])
    return "".join(pieces), calls


def example_value(prop: dict) -> str:
    """First e.g. 'sample' found in a parameter description, for prompt examples."""
    match = re.search(r"e\.g\.\s+'([^']+)'", prop.get("description", ""))
    return match.group(1) if match else "value"


def tool_prompt() -> str:
    """Describe the tag syntax for the system prompt."""
    lines = [
        "You can call tools by writing a tag inside your answer; the tag is "
        "replaced by the real tool result before the user sees your message.",
        "Tag format: <tool_name; argument=\"value\"/>  Always start with <, then the tool name, "
        "then a semicolon, and end with />.",
        "Examples:",
        'User: Weather in Prague? You: Right now in Prague it is <get_weather; "Prague"/>',
        'User: Who is Ada Lovelace? You: <wiki_summary; topic="Ada Lovelace"/>',
        'User: What is Meta Quest? You: <wiki_summary; topic="Meta Quest"/>',
        "These tags are the ONLY way to call a tool. Never write <tool_call>, <function=...>, "
        "<parameter=...> or [tool(...)]: those are not understood and would be read aloud.",
        "You can call tools by writing a tag inside your answer; the tag is ",
        "replaced by the real tool result before the user sees your message.",
        'Example: Right now in Prague it is <get_weather; "Prague"/>',
        "Write your whole answer as plain text with the tag exactly where the "
        "result belongs. Never guess tool data and never write a tool's result "
        "yourself. Whenever the user asks for the weather, the time or the news, "
        "write a fresh tag, even if you did it earlier in this conversation: "
        "results from earlier messages are out of date, and without a tag you "
        "cannot know the answer. Timers, reminders, alarms, the stopwatch and the "
        "volume work only through tags too: you cannot set or change them any other "
        "way, so never say one is done unless you wrote its tag. When the user asks "
        "for a timer or reminder but gives no time, ask when instead of guessing. "
        "Do not label your answer (no 'Sure:', no "
        "'Confirmation:') and never describe or repeat the tag in your own words.",
        "",
        "Available tools:",
    ]
    for spec in TOOLS:
        fn = spec.get("function", {})
        name = fn.get("name", "")
        props = fn.get("parameters", {}).get("properties", {})
        if not props:
            lines.append(f"- {name}: {fn.get('description', '')} Call it as <{name}/>.")
            continue
        if len(props) == 1:
            key = next(iter(props))
            call = f'<{name}; "{example_value(props[key])}"/>'
        else:
            joined = ", ".join(f'{key}="{example_value(props[key])}"' for key in props)
            call = f"<{name}; {joined}/>"
        lines.append(f"- {name}: {fn.get('description', '')} Call it as {call}.")
    return "\n".join(lines)


def call_tool(name: str, arguments) -> str:
    """Run one tool call by name. Arguments may be a dict or a JSON string.

    Always returns a string so it can be sent straight back to the LLM.
    """
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return f"Error: could not parse arguments for {name}: {arguments!r}"
    function = TOOL_FUNCTIONS.get(name)
    if function is None:
        return f"Error: no tool named '{name}' is available."
    try:
        return str(function(**(arguments or {})))
    except Exception as exc:
        return f"Error: {name} failed ({exc})."


if __name__ == "__main__":
    print(get_time())
    print(get_weather("Prague"))
    print(resolve_tool_tags('Right now in Prague it is <get_weather; "Prague"/>')[0])