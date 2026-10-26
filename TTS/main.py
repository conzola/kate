"""
Local Kokoro TTS: runs the model inside this Python process on your GPU.
No Unsloth Studio, no HTTP, no API key.

Install (in the same venv that already has PyTorch with CUDA):
    pip install kokoro soundfile

The first run downloads the Kokoro-82M weights and the voice (a few hundred MB,
cached afterwards in your Hugging Face cache folder).

Same functions as before: synthesize, warmup, call_generate_audio, split_text,
stream_generate_audio.  Importing this file loads the model once (takes a few
seconds), so import it at startup and call warmup() once.
"""
import io
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import soundfile as sf
import torch
from kokoro import KPipeline

VOICE = "af_heart"
SPEED = 1.0
LANG_CODE = "a"          # 'a' = American English, 'b' = British English
SAMPLE_RATE = 24000      # Kokoro's native output rate
OUTPUT_WAV = "output_custom_voice.wav"

# Local synthesis time grows with text length (no fixed server cost any more),
# so keep the first chunk short and let later chunks be a bit longer.
FIRST_CHUNK_WORDS = 10   # first chunk is cut at punctuation once it is longer than this
MERGE_WORDS = 30         # merge short sentences into one chunk up to this many words
MAX_WORDS = 30           # hard limit per chunk
PREFETCH_WORKERS = 1     # chunk N+1 is synthesized while chunk N plays
CLAUSE_BREAKS = ",;:"
FIRST_BREAKS = ",;:.!?"  # where the first chunk may be cut
_SENTENCE_RE = re.compile(r"[^.!?]*[.!?]+|[^.!?]+$")

# ---- load the model once, on the GPU if there is one -------------------------
_device = "cuda" if torch.cuda.is_available() else "cpu"
if _device == "cpu":
    print("WARNING: CUDA not available to PyTorch - Kokoro will run on the CPU (slower).")
try:
    _pipeline = KPipeline(lang_code=LANG_CODE, device=_device)
except TypeError:  # older kokoro versions have no `device` argument
    _pipeline = KPipeline(lang_code=LANG_CODE)
_lock = threading.Lock()  # the pipeline is not safe to call from two threads at once
_executor = ThreadPoolExecutor(max_workers=PREFETCH_WORKERS)


def _synth_array(text: str) -> np.ndarray:
    """Synthesize text -> mono float32 array at 24 kHz."""
    parts = []
    with _lock, torch.inference_mode():
        for _, _, audio in _pipeline(text, voice=VOICE, speed=SPEED):
            if audio is None:
                continue
            if hasattr(audio, "detach"):
                audio = audio.detach().cpu().numpy()
            parts.append(np.asarray(audio, dtype=np.float32))
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


def synthesize(input_text: str, response_format: str = "wav") -> bytes:
    """Kept for compatibility with the old Studio version: returns WAV bytes."""
    buf = io.BytesIO()
    sf.write(buf, _synth_array(input_text), SAMPLE_RATE, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def warmup() -> None:
    """Call once at startup: the first synthesis compiles GPU kernels and is slow."""
    _synth_array("Hello.")
    _synth_array("This is a short warm up sentence.")


def call_generate_audio(input_text: str, output_wav: str = OUTPUT_WAV) -> None:
    with open(output_wav, "wb") as f:
        f.write(synthesize(input_text))


def clean_text(text: str) -> str:
    """Drop markdown symbols that would be read out or waste synthesis time."""
    text = re.sub(r"[*_`#>]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def split_sentences(text: str) -> list:
    return [s for s in (m.strip() for m in _SENTENCE_RE.findall(text)) if s]


def split_long_sentence(sentence: str, max_words: int = MAX_WORDS, min_words: int = 3) -> list:
    words = sentence.split()
    pieces = []
    while len(words) > max_words:
        cut = max_words
        for i in range(max_words - 1, min_words - 1, -1):
            if words[i][-1] in CLAUSE_BREAKS:
                cut = i + 1
                break
        pieces.append(" ".join(words[:cut]))
        words = words[cut:]
    if words:
        pieces.append(" ".join(words))
    return pieces


def _merge_chunks(chunks: list, limit: int) -> list:
    merged = []
    for c in chunks:
        if merged and len(merged[-1].split()) + len(c.split()) <= limit:
            merged[-1] += " " + c
        else:
            merged.append(c)
    return merged


def _split_first_chunk(chunk: str) -> list:
    """Cut the first chunk at its earliest punctuation break (after >= 2 words,
    within the first FIRST_CHUNK_WORDS + 3 words). Never cuts mid-phrase, because each
    chunk is synthesized separately and would get its own, unnatural intonation."""
    words = chunk.split()
    if len(words) <= FIRST_CHUNK_WORDS:
        return [chunk]
    for i in range(1, min(len(words) - 1, FIRST_CHUNK_WORDS + 3)):
        if words[i][-1] in FIRST_BREAKS:
            return [" ".join(words[: i + 1]), " ".join(words[i + 1 :])]
    return [chunk]


def split_text(text: str, max_words: int = MAX_WORDS) -> list:
    chunks = []
    for sentence in split_sentences(clean_text(text)):
        chunks.extend(split_long_sentence(sentence, max_words))
    chunks = _merge_chunks(chunks, MERGE_WORDS)
    if chunks:
        chunks[0:1] = _split_first_chunk(chunks[0])
    return chunks


def _trim_and_fade(data: np.ndarray, sr: int) -> np.ndarray:
    """Cut leading/trailing silence (less dead air before and between chunks), then
    apply a 5 ms fade at both ends to avoid clicks."""
    loud = np.flatnonzero(np.abs(data) > 0.01)
    if loud.size:
        start = max(0, loud[0] - int(sr * 0.02))
        end = min(len(data), loud[-1] + 1 + int(sr * 0.10))
        data = data[start:end]
    n = int(sr * 0.005)
    if n and data.shape[0] >= 2 * n:
        data = data.copy()
        ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
        data[:n] *= ramp
        data[-n:] *= ramp[::-1]
    return data


def _synth_chunk(chunk_text: str):
    return _trim_and_fade(_synth_array(chunk_text), SAMPLE_RATE), SAMPLE_RATE


def stream_generate_audio(input_text: str, max_words: int = MAX_WORDS, stop=None):
    """Yield (audio, sample_rate) per chunk, in order.

    All chunks are queued up front, so while you play chunk N the next chunks are
    already being synthesized in the background. `audio` is mono float32 in [-1, 1].

    `stop` is an optional callable: as soon as it returns True the remaining
    futures are cancelled and generation ends early (barge-in).
    """
    futures = [_executor.submit(_synth_chunk, c) for c in split_text(input_text, max_words)]
    for i, f in enumerate(futures):
        if stop is not None and stop():
            for leftover in futures[i:]:
                leftover.cancel()
            break
        yield f.result()


if __name__ == "__main__":
    # quick test: python main.py "your text"
    import sys

    t0 = time.perf_counter()
    warmup()
    print(f"model on {_device}, warmup took {time.perf_counter() - t0:.2f}s")

    text = " ".join(sys.argv[1:]) or (
        "Sure, I can help with that. First I will check the settings, "
        "then I will let you know what I found."
    )
    for run in (1, 2):
        t0 = time.perf_counter()
        for i, (chunk, sr) in enumerate(stream_generate_audio(text)):
            print(f"run {run} chunk {i}: ready after {time.perf_counter() - t0:.2f}s "
                  f"({chunk.shape[0] / sr:.2f}s of audio)")