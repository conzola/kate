import collections
import io
import math
import queue
import threading
import time
import wave

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000  # 16 kHz mono is what most ASR models expect
CHUNK = 0.05         # seconds of audio handled per block
NOISE_FLOOR_SECONDS = 0.5   # ambient calibration time before listening
SILENCE_LIMIT = 0.7  # seconds of trailing silence that ends the utterance
START_TIMEOUT = 10.0 # give up if nobody talks within this window
MAX_DURATION = 15    # hard cap on a single recording (seconds)
PREROLL = 0.3        # audio kept before speech onset so words aren't clipped
ABSOLUTE_FLOOR = 0.005  # rms under this is always treated as silence
NOISE_MULTIPLIER = 3.0  # speech threshold = ambient noise rms * this

_threshold = None  # cached after the first calibration so we never calibrate twice
speech_end_time = None  # monotonic time when the last recording detected the talker stopped


def _rms(chunk: np.ndarray) -> float:
    """Loudness of a block of audio - the basis of the speech/silence decision."""
    return float(np.sqrt(np.mean(np.square(chunk, dtype=np.float64))))


def calibrate(sample_rate: int = SAMPLE_RATE) -> float:
    """Measure the room's ambient noise once and set the speech threshold safely above it."""
    global _threshold
    if _threshold is not None:
        return _threshold

    print("Calibrating ambient noise, please stay quiet...")
    with sd.InputStream(samplerate=sample_rate, channels=1, dtype="int16") as stream:
        noise = stream.read(int(NOISE_FLOOR_SECONDS * sample_rate))[0]
    noise_rms = _rms(noise)
    _threshold = max(ABSOLUTE_FLOOR, noise_rms * NOISE_MULTIPLIER)
    print(f"Ambient rms {noise_rms:.5f} -> speech threshold {_threshold:.5f}")
    return _threshold


def record_wav(duration: float = MAX_DURATION, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Record until the user stops talking and return a complete .wav file as bytes (nothing is written to disk)."""
    global speech_end_time
    threshold = calibrate(sample_rate)
    block_size = int(CHUNK * sample_rate)
    pre_speech = collections.deque(maxlen=max(1, int(PREROLL / CHUNK)))
    silence_blocks = max(1, int(SILENCE_LIMIT / CHUNK))

    chunks = queue.Queue()

    def callback(indata, frame_count, time_info, status):
        if status:
            print(f"Audio stream status: {status}")
        chunks.put(indata.copy())

    blocks = []
    started_at = None
    stopped_at = None
    trailing_silence = 0
    listen_start = time.monotonic()

    print("Listening (speak now, stop when you're done)...")
    with sd.InputStream(
        samplerate=sample_rate, channels=1, dtype="int16", blocksize=block_size, callback=callback
    ):
        while True:
            try:
                chunk = chunks.get(timeout=CHUNK * 2)
            except queue.Empty:
                if started_at is None:
                    continue  # still waiting for the first sound
                break  # input stream stalled

            if started_at is None:
                pre_speech.append(chunk)
                if _rms(chunk) >= threshold:
                    started_at = time.monotonic()
                    blocks.extend(pre_speech)
                    trailing_silence = 0
                    print("Speech detected, listening until you stop...")
                elif time.monotonic() - listen_start > START_TIMEOUT:
                    print("No speech detected.")
                    break
            else:
                blocks.append(chunk)
                if _rms(chunk) < threshold:
                    trailing_silence += 1
                    if trailing_silence >= silence_blocks:
                        stopped_at = time.monotonic()
                        print("Silence detected, finishing recording.")
                        break
                else:
                    trailing_silence = 0

            if started_at is not None and time.monotonic() - started_at >= duration:
                print("Maximum duration reached.")
                break

    speech_end_time = stopped_at if stopped_at is not None else time.monotonic()

    print("Recording complete.")
    audio = np.concatenate(blocks) if blocks else np.zeros((0, 1), dtype=np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16 = 2 bytes
        wf.setframerate(sample_rate)
        wf.writeframes(audio.tobytes())

    return buf.getvalue()


def get_speech_end_time():
    """Monotonic timestamp of when the user was last registered as having stopped talking."""
    return speech_end_time


class InterruptDetector:
    """Watches the mic while Kate talks and fires when the user starts talking.

    Used as a context manager: it opens an input stream on __enter__ and closes
    it on __exit__, so it can never be left running. `event` is a threading.Event
    that gets set once continuous speech is detected - pass it to the playback
    loop to cut Kate's answer short.

    Kate's own voice coming out of the speakers also reaches the mic, so the mic
    level must clearly beat the ambient threshold (gain), the speech must last
    min_seconds, and the first ignore_seconds are deaf so a click when playback
    starts cannot trigger it.
    """

    def __init__(self, min_seconds: float = 0.35, gain: float = 2.0, ignore_seconds: float = 0.5):
        self.event = threading.Event()
        self.min_seconds = min_seconds
        self.ignore_seconds = ignore_seconds
        self._threshold = max(ABSOLUTE_FLOOR, calibrate() * gain)
        self._min_blocks = max(1, math.ceil(min_seconds / CHUNK))
        self._stream = None
        self._loud_blocks = 0
        self._opened_at = None
        self.tripped_at = None

    @property
    def interrupted(self) -> bool:
        return self.event.is_set()

    def __enter__(self):
        self._opened_at = time.monotonic()

        def callback(indata, frames, time_info, status):
            if status:
                print(f"Audio stream status: {status}")
            if self.event.is_set():
                return
            if time.monotonic() - self._opened_at < self.ignore_seconds:
                return
            if _rms(indata) >= self._threshold:
                self._loud_blocks += 1
                if self._loud_blocks >= self._min_blocks:
                    self.tripped_at = time.monotonic()
                    self.event.set()
            else:
                self._loud_blocks = 0

        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="int16",
            blocksize=int(CHUNK * SAMPLE_RATE), callback=callback,
        )
        self._stream.start()
        return self

    def __exit__(self, *exc):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        return False


if __name__ == "__main__":
    wav_bytes = record_wav()
    print(f"Recorded {len(wav_bytes)} bytes of WAV data")
