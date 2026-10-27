# Kate

*Work in progress...*

## General Information

This repository contains an AI assistant that runs on your ESP32 and computer.

- **LLM:** Uses [LM Studio](https://lmstudio.ai/) to run a local large language model (LLM).
- **TTS (Text-to-Speech):** Uses `Kokoro-82M` for speech synthesis.
- **ASR (Automatic Speech Recognition):** Uses Faster-Whisper `small.en` with CTranslate2 GPU inference and beam search for better recognition accuracy.

### Performance and Latency

On an NVIDIA RTX 4060, `./Final/main.py` currently achieves approximately **1 second of latency**, with the best run getting **0.65 seconds of latency**. When running on the ESP32, latency is around **0.8 seconds**, with lows reaching **0.65 seconds**.

The time heavily depends on the LLM running. The model running with the fastest run was qwen3-1.7b.

We’re actively working to optimize performance and reduce latency to the following targets:

- **PC (RTX 4060):** Approximately 1 second.
- **ESP32:** Approximately 0.8 seconds.

These are target latency figures, and optimization is still ongoing.

### Hardware Requirements

- **Recommended VRAM:** 8 GB.
- **Larger LLMs:** More VRAM is recommended when running larger language models.

### Goals:

- **120/500 tools** (counted from the `TOOLS` registry in `LLM/tools.py`)
- **more coming soon**

## Requirements

- Python 3.12
- [LM Studio](https://lmstudio.ai/) or any other AI provider

## Quick Start

1. Create a virtual environment:

   ```bash
   python -m venv kate-env
   ```

2. Activate the virtual environment:

   **Windows:**
   ```powershell
   .\kate-env\Scripts\activate.ps1
   ```

   **Linux / macOS:**
   ```bash
   source kate-env/bin/activate
   ```

3. Install the required dependencies:

   ```bash
   pip install -r requirements.txt
   ```

On a Windows PC with an NVIDIA GPU, install the matching CUDA build of PyTorch
after the requirements so ASR and TTS can use the GPU. For CUDA 12.8, run this
inside the activated `kate-env`:

```powershell
python -m pip install --upgrade torch==2.11.0+cu128 --index-url https://download.pytorch.org/whl/cu128
```

PyTorch's [official installer](https://pytorch.org/get-started/locally/) lists
the available CUDA builds. Kate automatically falls back to CPU when CUDA is
unavailable.

English ASR defaults to Faster-Whisper `small.en` with GPU float16 or CPU int8,
beam search, and startup warmup. Set `KATE_ASR_MODEL` to a larger Faster-Whisper
model if you prefer more accuracy, or `base.en` if you need lower latency.

4. Create a `.env` file in the project directory based on the `example-dotenv` file.

   **Weather API:** The weather tool requires an API key. You can get one from [WeatherAPI.com](https://www.weatherapi.com/). This provider is recommended, but you can use any other weather provider you prefer.

Kate defaults to LM Studio model ID `qwen/qwen3-1.7b` for lower response
latency. Load it in LM Studio before starting Kate. To choose another loaded
model, set `KATE_LLM_MODEL` to that model's ID in `.env`.

## Running on PC Only

Run the following command to start Kate on your PC:

```bash
python ./Final/main.py
```

## Running on the ESP32

1. Flash the `.ino` file onto your ESP32.
2. Start the server on your PC:

   ```bash
   python ./ESP/on-pc/server.py
   ```

Every turn prints a latency line in the server console and in the ESP32 Serial
Monitor, for example:
`Latency: ASR 0.38s | LLM complete 0.28s | first audio after ASR 0.24s | total 0.61s`.

For the fastest ESP32 response, diagnostic audio recording is off by default. Set
`KATE_SAVE_RECORDINGS=1` to save each raw prompt, or `KATE_SAVE_DEBUG_WAV=1` to
save the normalized ASR input as `esp_last_recording.wav` when diagnosing audio.

## Using `server.bat`

Use the example `server.bat` file provided in `example-serverdotbat` as a starting point.

You can create a shortcut to `server.bat` (for example, on your desktop) to start the Kate server quickly and conveniently.
