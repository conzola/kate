"""Local "Hey Kate" wake word for the desktop (Final/main.py) path.

The ESP32 sketch runs the same microWakeWord model with TensorFlow Lite Micro.
On the PC we use pymicro-wakeword, which wraps the same .tflite file with the
TensorFlow Lite C library and the identical audio frontend.

If pymicro-wakeword is missing:
    pip install pymicro-wakeword
"""
from pathlib import Path

try:
    from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures
except ImportError as exc:  # clearer than a bare ModuleNotFoundError at startup
    raise ImportError(
        "Wake word support needs pymicro-wakeword. Install it with: pip install pymicro-wakeword"
    ) from exc

SAMPLE_RATE = 16000
CHUNK_MS = 80
CHUNK_BYTES = SAMPLE_RATE * CHUNK_MS // 1000 * 2  # 16-bit mono


def load_model(model_config):
    """Load the model and its feature frontend from the JSON next to the .tflite file."""
    config = Path(model_config)
    return MicroWakeWord.from_config(config), MicroWakeWordFeatures()


def feed(model, features, chunk: bytes):
    """Feed one block of 16 kHz 16-bit mono audio.

    Returns (fired, certainty_percent). `features` buffers partial blocks, so
    any block size that is a multiple of 10 ms works.
    """
    certainty = None
    for frame in features.process_streaming(chunk):
        probability = model.process_streaming_prob(frame)
        if probability is None:
            continue
        certainty = probability * 100.0
        if probability > model.probability_cutoff:
            return True, certainty
    return False, certainty
