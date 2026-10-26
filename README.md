# Kate

*Work in progress...*

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

For the fastest ESP32 response, diagnostic audio recording is off by default. Set
`KATE_SAVE_RECORDINGS=1` to save each raw prompt, or `KATE_SAVE_DEBUG_WAV=1` to
save the normalized ASR input as `esp_last_recording.wav` when diagnosing audio.

## Using `server.bat`

Use the example `server.bat` file provided in `example-serverdotbat` as a starting point.

You can create a shortcut to `server.bat` (for example, on your desktop) to start the Kate server quickly and conveniently.
