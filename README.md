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

4. Create a `.env` file in the project directory and configure your environment variables.

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
