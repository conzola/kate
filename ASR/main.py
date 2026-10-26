"""Qwen3-ASR transcription for the Kate pipeline.

The model prefers a CUDA GPU with FlashAttention 2, but falls back to plain
PyTorch SDPA and then to the CPU, so the pipeline also starts on a machine
without an NVIDIA GPU or a flash-attn wheel.

Environment overrides (optional):
    KATE_ASR_DEVICE   e.g. "cuda:0", "cpu"      (default: cuda:0 if available)
    KATE_ASR_ATTN     e.g. "flash_attention_2", "sdpa", "eager"
"""
import importlib.util
import io
import os
import wave

import numpy as np
import torch
from qwen_asr import Qwen3ASRModel

from .record import record_wav

MODEL_ID = "Qwen/Qwen3-ASR-0.6B"


def _flash_attention_available() -> bool:
    """True only if flash-attn is installed *and* importable on this machine."""
    if importlib.util.find_spec("flash_attn") is None:
        return False
    try:
        import flash_attn  # noqa: F401
    except Exception:  # missing CUDA libs, wrong torch build, ...
        return False
    return True


def _runtime():
    """Choose device, dtype and attention implementation for this machine."""
    device = os.getenv("KATE_ASR_DEVICE", "").strip()
    if not device:
        device = "cuda:0" if torch.cuda.is_available() else "cpu"

    on_gpu = device.startswith("cuda")
    attention = os.getenv("KATE_ASR_ATTN", "").strip()
    if not attention:
        attention = "flash_attention_2" if on_gpu and _flash_attention_available() else "sdpa"
    dtype = torch.bfloat16 if on_gpu else torch.float32
    return device, dtype, attention


DEVICE, DTYPE, ATTENTION = _runtime()
if DEVICE == "cpu":
    print("WARNING: CUDA not available to PyTorch - ASR will run on the CPU (slower).")

model = Qwen3ASRModel.from_pretrained(
    MODEL_ID,
    dtype=DTYPE,
    device_map=DEVICE,
    attn_implementation=ATTENTION,
    max_inference_batch_size=-1,  # Batch size limit for inference. -1 means unlimited. Smaller values can help avoid OOM.
    max_new_tokens=256,  # Maximum number of tokens to generate. Set a larger value for long audio input.
)


def wav_to_array(wav_bytes: bytes):
    """Convert in-memory .wav bytes to a (float32 array, sample_rate) tuple."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        sr = wf.getframerate()
        data = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    return data.astype(np.float32) / 32768.0, sr


def asr():
    audio = wav_to_array(record_wav())  # records until you stop talking, nothing saved to disk

    results = model.transcribe(
        audio=audio,  # (numpy array, sample_rate) tuple
        language="English",  # set "English" to force the language
    )

    return results[0].text