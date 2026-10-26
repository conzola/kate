import queue
import sys
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))
sys.path.append(str(ROOT / "Hey_Kate"))

from ASR.main import asr
from ASR.record import InterruptDetector, calibrate, get_speech_end_time
from LLM.automatization import scheduler_loop
from LLM.main import ask, reset_memory, split_continue
from TTS.main import stream_generate_audio, warmup
from listen_kate import CHUNK_BYTES, SAMPLE_RATE, feed, load_model

MODEL_CONFIG = ROOT / "Hey_Kate" / "hey_kate.json"
OUTPUT_WAV = "output_custom_voice.wav"
LOG_FILE = ROOT / "conversation_log.txt"

BARGE_IN = True              # let the user cut Kate off by simply talking over her
INTERRUPT_GAIN = 2.0         # mic level must beat ambient noise by this much to count
INTERRUPT_MIN_SECONDS = 0.35 # continuous speech needed to stop playback
INTERRUPT_IGNORE = 0.5       # deaf window when playback starts, so Kate cannot trigger herself
HEAD_PAD_SECONDS = 0.4       # Windows drops the head of a freshly opened output stream;
                             # pad it with silence so speech is never the part cut off

_SPEAK_LOCK = threading.Lock()  # the automation scheduler speaks too: never two voices at once

RESET_PHRASES = ("forget everything", "forget our conversation",
                 "clear your memory", "reset your memory")
STOP_PHRASES = {"stop", "stop talking", "be quiet", "shut up", "hush", "never mind", "that is enough"}
REPEAT_PHRASES = ("repeat that", "say that again", "say it again",
                  "come again", "what did you say", "pardon")
EXIT_PHRASES = {"goodbye", "bye", "goodbye kate", "bye kate",
                "good night", "good night kate", "shut down", "quit"}


def _normalize(text: str) -> str:
    """Lowercase an utterance and strip the punctuation ASR may add."""
    return text.strip().strip(".!?\"' ,;:").lower()


def log_line(who: str, text: str) -> None:
    """Append one line to the conversation log, so a chat can be reviewed later."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {who}: {text}\n")
    except OSError as exc:
        print(f"Could not write {LOG_FILE}: {exc}")


def _greeting() -> str:
    hour = time.localtime().tm_hour
    part = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    return f"{part}. Kate is ready."


def wait_for_wake_word(model_config=MODEL_CONFIG):
    mww, features = load_model(model_config)
    audio_q: "queue.Queue[bytes]" = queue.Queue()
    buf = b""

    def callback(indata, frames, time_info, status):
        if status:
            print(status, file=sys.stderr)
        audio_q.put(bytes(indata))

    print("Listening for 'Hey Kate'... (Ctrl+C to quit)")
    with sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                           blocksize=1280, callback=callback):
        while True:
            buf += audio_q.get()
            while len(buf) >= CHUNK_BYTES:
                chunk, buf = buf[:CHUNK_BYTES], buf[CHUNK_BYTES:]
                fired, certainty = feed(mww, features, chunk)
                if fired:
                    pct = f"{certainty:.1f}%" if certainty is not None else "?"
                    print(f">>> Hey Kate detected (certainty {pct})")
                    return


def speak_streaming(input_text, output_wav=OUTPUT_WAV, interrupt=None):
    """Play TTS audio as it is generated, chunk by chunk, and save the full wav.

    `interrupt` is an optional threading.Event; playback stops as soon as it is
    set (the user started talking over Kate). Returns the monotonic timestamp of
    the first chunk written to the output stream, or None if there was nothing
    to say.
    """
    chunks = []
    sr = None
    stream = None
    t_first_audio = None
    stop = interrupt.is_set if interrupt is not None else None
    with _SPEAK_LOCK:
        try:
            for chunk, chunk_sr in stream_generate_audio(input_text, stop=stop):
                if sr is None:
                    sr = chunk_sr
                    stream = sd.RawOutputStream(
                        samplerate=sr, channels=1, dtype="int16", blocksize=0
                    )
                    stream.start()
                    # a freshly opened stream loses its first buffer on Windows;
                    # give it silence to lose instead of the first words
                    pad = np.zeros(int(sr * HEAD_PAD_SECONDS), dtype=np.float32)
                    stream.write((pad * 32767.0).astype("int16").tobytes())
                if t_first_audio is None:
                    t_first_audio = time.monotonic()
                pcm = np.clip(np.asarray(chunk, dtype=np.float32), -1.0, 1.0)
                stream.write((pcm * 32767.0).astype("int16").tobytes())
                chunks.append(pcm)
        finally:
            if stream is not None:
                stream.stop()
                stream.close()
            if chunks and sr is not None:
                sf.write(output_wav, np.concatenate(chunks), sr)
    return t_first_audio


def main():
    calibrate()  # measure ambient noise once here; record.py reuses the cached threshold forever
    print("Warming up the voice...")
    warmup()  # first synthesis is slow (GPU kernel compilation); done here, not mid-conversation
    greeting = _greeting()
    speak_streaming(greeting)
    log_line("Kate", greeting)
    last_reply = None
    keep_going = False  # every conversation still starts with the wake word

    stop_automations = threading.Event()

    def _run_automation(result: str, automation: dict) -> None:
        """An automation fired: say its result out loud."""
        print(f"Automation ({automation.get('name', '?')}):", result)
        log_line("Kate", result)
        speak_streaming(result)

    scheduler = threading.Thread(
        target=scheduler_loop,
        args=(stop_automations, _run_automation),
        daemon=True,
    )
    scheduler.start()  # automations the user asked for fire on their own from now on

    try:
        while True:
            if keep_going:
                print("Continuing the conversation, listening...")
            else:
                wait_for_wake_word()
                print("Yes? I'm listening...")
            keep_going = False
            try:
                text = asr()
            except Exception as exc:
                print(f"ASR error: {exc}")
                continue
            t_stop = get_speech_end_time()  # registered as stopped talking during asr()
            t_asr_done = time.monotonic()
            print("You:", text)
            norm = _normalize(text)

            if not norm:
                print("Nothing heard, listening again.")
                keep_going = True
                continue
            if any(phrase in norm for phrase in RESET_PHRASES):
                reset_memory()
                print("Kate: Memory cleared.")
                speak_streaming("All right, I have forgotten our conversation.")
                continue
            if norm in STOP_PHRASES:
                print("Kate: (stopped)")
                continue
            if norm in EXIT_PHRASES:
                print("Kate: Goodbye.")
                log_line("Kate", "Goodbye.")
                speak_streaming("Goodbye.")
                break
            if any(phrase in norm for phrase in REPEAT_PHRASES):
                if last_reply:
                    print("Kate:", last_reply)
                    speak_streaming(last_reply)
                continue

            try:
                reply = ask(text)
            except Exception as exc:
                print(f"LLM error: {exc}")
                speak_streaming("Sorry, something went wrong on my side.")
                continue
            reply, keep_going = split_continue(reply)
            last_reply = reply
            t_llm_done = time.monotonic()
            print("Kate:", reply)
            log_line("You", text)
            log_line("Kate", reply)

            if BARGE_IN:
                with InterruptDetector(
                    gain=INTERRUPT_GAIN,
                    min_seconds=INTERRUPT_MIN_SECONDS,
                    ignore_seconds=INTERRUPT_IGNORE,
                ) as detector:
                    t_first_audio = speak_streaming(reply, interrupt=detector.event)
                interrupted = detector.interrupted
            else:
                t_first_audio = speak_streaming(reply)
                interrupted = False
            if interrupted:
                print("Interrupted - listening again.")
                keep_going = True
                continue

            if t_stop is None or t_first_audio is None:
                print("Latency: incomplete (no speech end or no audio output).")
            else:
                print(
                    f"Latency: ASR {t_asr_done - t_stop:.2f}s | "
                    f"LLM {t_llm_done - t_asr_done:.2f}s | "
                    f"first audio {t_first_audio - t_llm_done:.2f}s | "
                    f"total {t_first_audio - t_stop:.2f}s"
                )
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        stop_automations.set()


if __name__ == "__main__":
    main()