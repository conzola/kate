"""Low-latency English speech recognition for the Kate pipeline.

The default model is Faster-Whisper's small.en running through CTranslate2. It
uses greedy decoding and no second VAD pass because record.py already detects
the end of speech. Set KATE_ASR_MODEL to another Faster-Whisper model if you
prefer more accuracy over latency.

Environment overrides (optional):
    KATE_ASR_DEVICE       "cuda:0" or "cpu" (default: CUDA when available)
    KATE_ASR_COMPUTE_TYPE "float16" or "int8" (default: float16 on CUDA, int8 on CPU)
    KATE_ASR_MODEL        Faster-Whisper model name (default: small.en)
"""
import io
import os
import threading
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import torch

# CTranslate2 loads CUDA/cuDNN DLLs independently of PyTorch on Windows. Keep
# PyTorch's DLL directory registered so its bundled CUDA 12/cuDNN 9 libraries
# are visible to Faster-Whisper.
_CUDA_DLL_HANDLES = []
if os.name == "nt" and torch.cuda.is_available():
    _torch_lib = Path(torch.__file__).resolve().parent / "lib"
    if _torch_lib.is_dir():
        _CUDA_DLL_HANDLES.append(os.add_dll_directory(str(_torch_lib)))

from faster_whisper import WhisperModel

from .record import record_wav

SAMPLE_RATE = 16000
MODEL_ID = os.getenv("KATE_ASR_MODEL", "small.en").strip() or "small.en"


def _runtime():
    requested = os.getenv("KATE_ASR_DEVICE", "").strip()
    if requested:
        device_spec = requested.lower()
    else:
        device_spec = "cuda:0" if torch.cuda.is_available() else "cpu"

    if device_spec.startswith("cuda"):
        device = "cuda"
        try:
            device_index = int(device_spec.split(":", 1)[1]) if ":" in device_spec else 0
        except ValueError:
            device_index = 0
        compute_type = os.getenv("KATE_ASR_COMPUTE_TYPE", "float16").strip() or "float16"
    else:
        device = "cpu"
        device_index = 0
        compute_type = os.getenv("KATE_ASR_COMPUTE_TYPE", "int8").strip() or "int8"
    return device, device_index, compute_type


DEVICE, DEVICE_INDEX, COMPUTE_TYPE = _runtime()
try:
    _whisper_model = WhisperModel(
        MODEL_ID,
        device=DEVICE,
        device_index=DEVICE_INDEX,
        compute_type=COMPUTE_TYPE,
        cpu_threads=max(1, min(os.cpu_count() or 4, 8)),
    )
except Exception as exc:
    if DEVICE != "cuda":
        raise
    print(f"Faster-Whisper CUDA init failed ({exc}); falling back to CPU int8.")
    DEVICE, DEVICE_INDEX, COMPUTE_TYPE = "cpu", 0, "int8"
    _whisper_model = WhisperModel(
        MODEL_ID,
        device=DEVICE,
        compute_type=COMPUTE_TYPE,
        cpu_threads=max(1, min(os.cpu_count() or 4, 8)),
    )
print(f"ASR ready: {MODEL_ID} on {DEVICE} ({COMPUTE_TYPE})")

_INFERENCE_LOCK = threading.Lock()


def wav_to_array(wav_bytes: bytes):
    """Convert in-memory .wav bytes to a (float32 array, sample_rate) tuple."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        sr = wf.getframerate()
        data = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    return data.astype(np.float32) / 32768.0, sr


class _ASRAdapter:
    """Keep the Qwen-style transcribe result shape used by both Kate entrypoints."""

    def transcribe(self, audio, language="English"):
        samples, sample_rate = audio
        samples = np.asarray(samples, dtype=np.float32).reshape(-1)
        if samples.size == 0:
            return [SimpleNamespace(text="")]
        if sample_rate != SAMPLE_RATE:
            raise ValueError(f"ASR expects {SAMPLE_RATE} Hz audio, got {sample_rate} Hz")

        lang = "en" if not language or language.lower().startswith("en") else language.lower()
        with _INFERENCE_LOCK:
            segments, _ = _whisper_model.transcribe(
                samples,
                language=lang,
                beam_size=5,
                temperature=0.0,
                condition_on_previous_text=False,
                without_timestamps=True,
                vad_filter=False,
            )
            text = "".join(segment.text for segment in segments).strip()
        return [SimpleNamespace(text=text)]


# ESP/on-pc/server.py imports `model` and calls .transcribe(), so keep the same
# small interface while replacing the heavyweight Transformers inference path.
model = _ASRAdapter()


def asr():
    audio = wav_to_array(record_wav())
    return model.transcribe(audio=audio, language="English")[0].text


def warmup():
    """Pay CTranslate2's first-inference setup cost during startup, not on turn one."""
    silence = np.zeros(SAMPLE_RATE // 2, dtype=np.float32)
    model.transcribe(audio=(silence, SAMPLE_RATE), language="English")
