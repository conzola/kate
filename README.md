# Kate

*Work in progress...*

## General Information

This repository contains an AI assistant that runs on your ESP32 and computer.

- **LLM:** Uses [LM Studio](https://lmstudio.ai/) to run a local large language model (LLM).
- **TTS (Text-to-Speech):** Uses `Kokoro-82M` for speech synthesis.
- **ASR (Automatic Speech Recognition):** Uses `Qwen3-ASR-0.6B` for speech recognition.

### Hardware Requirements

- **Recommended VRAM:** 8 GB.
- **Larger LLMs:** More VRAM is recommended when running larger language models.

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

4. Create a `.env` file in the project directory based on the `example-dotenv` file.

   **Weather API:** The weather tool requires an API key. You can get one from [WeatherAPI.com](https://www.weatherapi.com/). This provider is recommended, but you can use any other weather provider you prefer.

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

## Using `server.bat`

Use the example `server.bat` file provided in `example-serverdotbat` as a starting point.

You can create a shortcut to `server.bat` (for example, on your desktop) to start the Kate server quickly and conveniently.
