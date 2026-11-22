# Jarvis: An Offline Voice Assistant

Jarvis is a fully offline voice assistant for Windows. It listens through your microphone, transcribes speech locally, answers with a local language model, speaks back with a local text-to-speech engine, controls your PC, and keeps long-term memory in a local database. No cloud, no API keys. The interface is a web UI shown in a native window.

## Features

- **Voice chat:** speak naturally, and interrupt Jarvis mid-sentence by talking over him.
- **Local everything:** speech to text, language model, text to speech and memory all run on your machine.
- **PC control:** volume, brightness, opening and closing apps, folders and websites, window snapping, screenshots, dark mode, lock, sleep, and timed shutdown.
- **Quick answers:** time, date, math and unit conversions are answered instantly without calling the model.
- **Long-term memory:** remembers facts about you, summarizes past conversations, and recalls them when relevant ("remember...", "forget...", "what do you know about me").
- **Voices:** Kokoro preset voices, plus Pocket TTS for cloned voices.
- **Modern UI:** an animated orb with six states, a compact always-on-top mode, and a full window with chat, conversation history, a facts browser, an activity log and settings.

## Tech stack

Python 3.11, faster-whisper (speech to text), Silero VAD (voice activity), Ollama with Qwen3 (language model), nomic-embed-text (memory search), Kokoro and Pocket TTS (text to speech), SQLite with FTS5 (memory), FastAPI and WebSocket (server), pywebview (window), HTML/CSS/JS (UI).

## Requirements

- 64-bit Windows 10 or 11
- **Python 3.11** (other versions are not supported)
- [Ollama for Windows](https://ollama.com)
- A working microphone and speakers
- About 8 GB of free RAM for `qwen3:8b` (use `qwen3:4b-instruct` on a weaker machine)
- About 10 GB of free disk space for models
- An internet connection for the install and the first run. After that, Jarvis runs offline.

## Installation

### 1. Install Python and Ollama

1. Install **Python 3.11** from [python.org](https://www.python.org/downloads/). Tick **Add python.exe to PATH**.
2. Install **Ollama** from [ollama.com](https://ollama.com).

### 2. Pull the language models

This is the slow part, so start it first. If the connection drops, run the same command again and it resumes.

```powershell
ollama pull qwen3:8b
ollama pull nomic-embed-text
ollama pull qwen3:4b-instruct
```

`qwen3:4b-instruct` is optional. It is a lighter model for slower laptops.

### 3. Get the project

```powershell
git clone https://github.com/JoyNcoder/Jarvis--An-Offline-Voice-Assistant.git
cd Jarvis--An-Offline-Voice-Assistant
```

### 4. Create the environment and install packages

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

### 5. Download the voice model files

These files are too large or too specific to store in the repository. Download them and place them in the **project root folder** (next to `app.py`):

| File | Where to get it |
|---|---|
| `kokoro-v1.0.onnx` | Releases of the [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx) project |
| `voices-v1.0.bin` | Releases of the [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx) project |
| `silero_vad.onnx` | The [Silero VAD](https://github.com/snakers4/silero-vad) project |

### 6. First run (needs internet)

Whisper (`small`) and Pocket TTS download from Hugging Face the first time Jarvis starts, into `models\huggingface` inside the project. Jarvis normally blocks network access to Hugging Face, so allow it for this one run:

```powershell
$env:HF_HUB_OFFLINE = "0"
.\.venv\Scripts\python.exe app.py
```

Wait for the downloads to finish and the window to open. The first launch can take a while, and a splash screen shows "Loading models...".

### 7. Everyday use

After the first run, start Jarvis by double-clicking **`Jarvis.bat`**, or run:

```powershell
python app.py
```

From now on it works without internet.

## First start checklist

- **Microphone:** `config.py` has `MIC_DEVICE = 1`, a device number that differs between computers. If Jarvis hears nothing or shows a microphone error, set it to `None` (system default), or list your devices with:

  ```powershell
  .\.venv\Scripts\python.exe -m sounddevice
  ```

  You can also pick the microphone in Jarvis under **Settings, Preferences**.
- **Windows privacy:** allow microphone access under Settings, Privacy & security, Microphone.
- **Weak computer?** Switch the language model to `qwen3:4b-instruct` under **Settings, Preferences, Brain**.
- **Voice:** the default is the Kokoro voice `af_heart`. Pick another one in Settings, Preferences.

## Voice cloning (optional)

Jarvis can speak in a cloned voice through Pocket TTS. Create a voice file for your own voice and save it as `my_voice.safetensors` in the project folder, then select it in Settings. Only clone your own voice, or a voice you have permission to use.

## Troubleshooting

| Problem | What to check |
|---|---|
| The Jarvis window never opens | Open `jarvis_log.txt` in the project folder. Errors are written there. |
| `pip install` fails | Check that you are using Python 3.11 on 64-bit Windows. |
| Whisper or Pocket TTS "not found" | You skipped the first online run (step 6), or `models\huggingface` is incomplete. Run step 6 again with internet. |
| Jarvis cannot reach the language model | Make sure Ollama is running and that `ollama list` shows `qwen3:8b` and `nomic-embed-text`. |
| Jarvis hears nothing | See the microphone notes in the first start checklist. |
| Very slow replies | Use `qwen3:4b-instruct`, close other heavy programs, or use a computer with more RAM. |

## Privacy

Everything stays on your computer. Your memories and conversations are stored in a local database (`jarvis_brain.db`), which is created on first start and is never uploaded by this repository.

## License

This project is released under the MIT License (see `LICENSE`). The models and libraries Jarvis uses (Kokoro, Pocket TTS, Qwen, Silero VAD, faster-whisper and others) are downloaded separately and keep their own licenses. Check each one before redistributing.
