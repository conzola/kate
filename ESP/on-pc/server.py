r"""
ESP32 bridge for the Kate pipeline (ASR -> LLM -> TTS).

The file lives two folders below the project root, next to ASR/, LLM/ and TTS/:

    <project root>/
        ASR/   LLM/   TTS/   Hey_Kate/
        ESP/on-pc/server.py        <- this file

Install (inside your existing venv):   pip install -r requirements.txt
Run:                                   python ESP\on-pc\server.py   (or from the ESP\on-pc folder: uvicorn server:app --host 0.0.0.0 --port 8000)
Windows will ask to allow Python through the firewall the first time: allow it
(private networks), or the ESP32 cannot connect.

What the ESP32 does
  POST /talk      raw 16 kHz 16-bit mono PCM of what you said (chunked upload).
                  Reply: streamed WAV (24 kHz, Kokoro's native rate) that the ESP plays
                  while it is still being generated, plus the header
                  "X-Continue: 1" when Kate asked a question and wants you to answer
                  without saying the wake word again.
                  204 with no audio = nothing to say.
  GET  /announce?wait=300
                  the ESP opens this and the server HOLDS it open. When an automation or timer
                  fires (reminders, alarms, scheduled news...) the server answers at once with the
                  audio, so nothing is polled. If nothing happens within `wait` seconds it answers
                  204 (a heartbeat: "the server is alive") and the ESP opens a new one.

Every reply (also the empty 204 ones) carries "X-Volume: 0..100", the speaker volume
the user asked for ("louder", "set the volume to 30"...). The ESP applies it before playing.
"""
import asyncio
import os
import queue
import re
import struct
import sys
import threading
import time
import wave
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import soundfile as sf
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

ROOT = Path(__file__).resolve().parents[2]  # .../kate, where ASR/, LLM/ and TTS/ live
sys.path.append(str(ROOT))

from ASR.main import model as asr_model            # the Qwen3-ASR model, loaded once
from LLM.automatization import quick_command, scheduler_loop   # quick_command: 'delete all my news automations'
from LLM.main import ask, reset_memory, split_continue
from LLM.tools import effective_volume, timer_loop   # speaker volume + timers, reminders, alarms
from TTS.main import SAMPLE_RATE as TTS_RATE, stream_generate_audio, warmup

ESP_RATE = 16000                 # what the ESP32 sends
MIN_AUDIO_SECONDS = 0.3          # shorter than this is treated as "nothing heard"
NORMALIZE = True                 # the analog mic is quiet: scale it up before ASR
TARGET_PEAK = 0.7
MAX_GAIN = 20.0
HEAD_PAD_SECONDS = 0.15          # silence before the first word so the amp never clips it
CHIME_REPEATS = 3                # how many times the timer chime rings before Kate speaks (0 = no chime)
DEBUG_WAV = ROOT / "esp_last_recording.wav"   # the last prompt AFTER normalizing (what the ASR heard)
RECORDINGS_DIR = ROOT / "esp_recordings"      # every prompt, exactly as the ESP sent it
SAVE_RECORDINGS = os.getenv("KATE_SAVE_RECORDINGS", "0").strip().lower() in {"1", "true", "yes"}
SAVE_DEBUG_WAV = os.getenv("KATE_SAVE_DEBUG_WAV", "0").strip().lower() in {"1", "true", "yes"}
if SAVE_RECORDINGS:
    RECORDINGS_DIR.mkdir(exist_ok=True)
LOG_FILE = ROOT / "conversation_log.txt"

# same phrases as main.py
RESET_PHRASES = ("forget everything", "forget our conversation",
                 "clear your memory", "reset your memory")
STOP_PHRASES = {"stop", "stop talking", "be quiet", "shut up", "hush", "never mind", "that is enough"}
REPEAT_PHRASES = ("repeat that", "say that again", "say it again",
                  "come again", "what did you say", "pardon")
EXIT_PHRASES = {"goodbye", "bye", "goodbye kate", "bye kate",
                "good night", "good night kate", "shut down", "quit"}

_PIPE_LOCK = threading.Lock()        # ASR and LLM run one request at a time
_announcements: "queue.Queue[tuple]" = queue.Queue()   # (text, ring_the_chime_first)
_stop_automations = threading.Event()
_last_reply = None
_last_prompt = None                  # Path of the newest saved prompt


def reply_headers(keep_going: bool = False) -> dict:
    return {"X-Continue": "1" if keep_going else "0", "X-Volume": str(effective_volume())}


def _normalize(text: str) -> str:
    return text.strip().strip(".!?\"' ,;:").lower()


def log_line(who: str, text: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {who}: {text}\n")
    except OSError as exc:
        print(f"Could not write {LOG_FILE}: {exc}")


def wav_header(rate: int) -> bytes:
    """44-byte header for a stream of unknown length (the ESP reads the rate and skips it)."""
    big = 0x7FFFFFFF
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", big, b"WAVE", b"fmt ", 16,
                       1, 1, rate, rate * 2, 2, 16, b"data", big)


def save_prompt(pcm: bytes):
    """Keep every audio prompt as a WAV (raw, as received) so you can listen to it or download it."""
    global _last_prompt
    pcm = pcm[: len(pcm) // 2 * 2]
    if not pcm:
        return None
    path = RECORDINGS_DIR / time.strftime("prompt_%Y%m%d_%H%M%S.wav")
    try:
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(ESP_RATE)
            w.writeframes(pcm)
    except OSError as exc:
        print(f"Could not save prompt: {exc}")
        return None
    _last_prompt = path
    return path


def transcribe(pcm: bytes) -> str:
    if SAVE_RECORDINGS:
        save_prompt(pcm)
    audio = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
    if audio.size < ESP_RATE * MIN_AUDIO_SECONDS:
        return ""
    peak = float(np.max(np.abs(audio)))
    if NORMALIZE and 0 < peak < TARGET_PEAK:
        audio = audio * min(TARGET_PEAK / peak, MAX_GAIN)
    if SAVE_DEBUG_WAV:
        try:
            sf.write(str(DEBUG_WAV), audio, ESP_RATE)
        except Exception:
            pass
    results = asr_model.transcribe(audio=(audio, ESP_RATE), language="English")
    return (results[0].text or "").strip()


def decide(text: str):
    """Same command handling as main.py. Returns (reply_text_or_None, keep_listening)."""
    global _last_reply
    norm = _normalize(text)
    if not norm:
        return None, False
    if any(p in norm for p in RESET_PHRASES):
        reset_memory()
        return "All right, I have forgotten our conversation.", False
    if norm in STOP_PHRASES:
        return None, False
    if norm in EXIT_PHRASES:
        log_line("Kate", "Goodbye.")
        return "Goodbye.", False
    if any(p in norm for p in REPEAT_PHRASES):
        return _last_reply, False
    # "delete every news automation" is handled directly: it never depends on the LLM writing the right tag
    quick = quick_command(text, last_reply=_last_reply or "")
    if quick:
        _last_reply = quick
        log_line("You", text)
        log_line("Kate", quick)
        return quick, quick.rstrip().endswith("?")
    try:
        reply = ask(text)
    except Exception as exc:
        print(f"LLM error: {exc}")
        return "Sorry, something went wrong on my side.", False
    reply, keep_going = split_continue(reply)
    _last_reply = reply
    log_line("You", text)
    log_line("Kate", reply)
    return reply, keep_going


def process_turn(pcm: bytes):
    if len(pcm) < 2:                 # the ESP32 renewing its idle, pre-opened connection
        return None, False
    with _PIPE_LOCK:
        t0 = time.monotonic()
        text = transcribe(pcm)
        t1 = time.monotonic()
        print(f"You: {text!r}  ({len(pcm) / 2 / ESP_RATE:.1f}s of audio, ASR {t1 - t0:.2f}s)")
        reply, keep_going = decide(text)
        print(f"Kate: {reply!r}  (LLM {time.monotonic() - t1:.2f}s, continue={keep_going})")
    return reply, keep_going


_chime_cache = None


def chime_pcm() -> bytes:
    """A soft two-tone 'ding-dong' repeated CHIME_REPEATS times, as 16-bit PCM at the TTS rate."""
    global _chime_cache
    if _chime_cache is None:
        def tone(freq: float, seconds: float) -> np.ndarray:
            t = np.arange(int(TTS_RATE * seconds)) / TTS_RATE
            envelope = np.minimum(1.0, t * 200) * np.exp(-t * 5)          # quick attack, ringing decay
            wave_ = np.sin(2 * np.pi * freq * t) + 0.3 * np.sin(2 * np.pi * 2 * freq * t)
            return 0.45 * wave_ * envelope
        one = np.concatenate([tone(880, 0.35), tone(1175, 0.55), np.zeros(int(TTS_RATE * 0.3))])
        samples = np.clip(np.tile(one, max(CHIME_REPEATS, 0)), -1.0, 1.0)
        _chime_cache = (samples * 32767.0).astype("<i2").tobytes()
    return _chime_cache


def audio_stream(text: str, chime: bool = False):
    """WAV header, a little silence, an optional chime, then each TTS chunk as soon as it is synthesized."""
    yield wav_header(TTS_RATE)
    yield b"\x00\x00" * int(TTS_RATE * HEAD_PAD_SECONDS)
    if chime and CHIME_REPEATS > 0:
        yield chime_pcm()
    for chunk, _sr in stream_generate_audio(text):
        pcm = np.clip(np.asarray(chunk, dtype=np.float32), -1.0, 1.0)
        yield (pcm * 32767.0).astype("<i2").tobytes()


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Warming up the voice...")
    await run_in_threadpool(warmup)

    def on_automation(result: str, automation: dict) -> None:
        print(f"Automation ({automation.get('name', '?')}): {result}")
        log_line("Kate", result)
        _announcements.put((result, False))   # the ESP picks it up on its next poll

    def on_timer(result: str, timer: dict) -> None:
        print(f"Timer ({timer.get('kind', '?')}): {result}")
        log_line("Kate", result)
        _announcements.put((result, True))    # timers, reminders and alarms ring first

    threading.Thread(target=scheduler_loop, args=(_stop_automations, on_automation),
                     daemon=True).start()
    threading.Thread(target=timer_loop, args=(_stop_automations, on_timer),
                     daemon=True).start()
    print("Ready. Waiting for the ESP32.")
    yield
    _stop_automations.set()


app = FastAPI(lifespan=lifespan)


@app.post("/talk")
async def talk(request: Request):
    pcm = bytearray()
    async for chunk in request.stream():
        pcm.extend(chunk)
    reply, keep_going = await run_in_threadpool(process_turn, bytes(pcm))
    headers = reply_headers(keep_going)    # built after the LLM ran, so a new volume is already in it
    if not reply:
        return Response(status_code=204, headers=headers)
    return StreamingResponse(audio_stream(reply), media_type="audio/wav", headers=headers)


@app.get("/announce")
async def announce(request: Request, wait: float = 0):
    """Long poll: hold the request until something has to be said, or `wait` seconds pass (max 10 min)."""
    deadline = time.monotonic() + max(0.0, min(wait, 600.0))
    while True:
        try:
            text, chime = _announcements.get_nowait()
            break
        except queue.Empty:
            pass
        # a dead connection must never take an announcement: the ESP would not hear it
        if time.monotonic() >= deadline or await request.is_disconnected():
            return Response(status_code=204, headers=reply_headers())
        await asyncio.sleep(0.25)
    return StreamingResponse(audio_stream(text, chime), media_type="audio/wav",
                             headers=reply_headers())


_NAME_RE = re.compile(r"^prompt_\d{8}_\d{6}\.wav$")


@app.get("/last.wav")
def last_prompt():
    """Download the newest audio prompt: open http://<pc-ip>:8000/last.wav in a browser."""
    if _last_prompt is None or not _last_prompt.exists():
        return Response("No prompt recorded yet.", status_code=404)
    return FileResponse(_last_prompt, media_type="audio/wav", filename=_last_prompt.name)


@app.get("/recordings")
def list_prompts():
    """A page with every saved prompt, newest first. Click one to play or download it."""
    files = sorted(RECORDINGS_DIR.glob("prompt_*.wav"), reverse=True)
    rows = "".join(
        f'<li><a href="/recordings/{f.name}">{f.name}</a> ({f.stat().st_size // 1024} KB)'
        f' <audio controls preload="none" src="/recordings/{f.name}"></audio></li>'
        for f in files
    ) or "<li>Nothing recorded yet.</li>"
    return HTMLResponse(f"<h2>ESP32 audio prompts</h2><ul>{rows}</ul>")


@app.get("/recordings/{name}")
def get_prompt(name: str):
    if not _NAME_RE.match(name):
        return Response("Bad name.", status_code=400)
    path = RECORDINGS_DIR / name
    if not path.exists():
        return Response("Not found.", status_code=404)
    return FileResponse(path, media_type="audio/wav", filename=name)


if __name__ == "__main__":
    # lets you start it with plain:  python ESP\on-pc\server.py
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
