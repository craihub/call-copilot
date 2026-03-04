# Call Copilot

Real-time call copilot powered by Gemini Live API. Streams audio to Gemini's multimodal WebSocket API and displays bullet-point suggestions in an always-on-top transparent overlay.

## Features

- **Zero STT latency** — raw 16-bit PCM @ 16kHz sent directly to Gemini Live API via WebSocket
- **Always-on-top overlay** — frameless transparent window, drag anywhere
- **Context box** — paste call context before starting (sent as `system_instruction`)
- **TEXT-only responses** — no audio output from AI, lowest possible latency
- **Built-in VAD** — Gemini's server-side voice activity detection triggers responses on natural pauses
- **Loopback support** — works with BlackHole or any system audio loopback device

## macOS Install (one-liner)

```bash
curl -fsSL https://raw.githubusercontent.com/craihub/call-copilot/main/install.sh | bash
```

This will:
1. Install Homebrew (if missing)
2. Install Python 3.12, PortAudio, BlackHole 2ch (loopback driver)
3. Clone the repo to `~/.call-copilot`
4. Create a venv and install Python deps
5. Add `call-copilot` to `/usr/local/bin`

## Manual Install

```bash
# Prerequisites (macOS)
brew install portaudio
brew install --cask blackhole-2ch   # loopback audio driver

git clone https://github.com/craihub/call-copilot.git
cd call-copilot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## Loopback Audio Setup (macOS)

To capture the far-end voice (the person you're talking to):

1. Open **Audio MIDI Setup** (Spotlight → "Audio MIDI Setup")
2. Click **+** → **Create Multi-Output Device**
3. Tick **your speakers** AND **BlackHole 2ch**
4. In **System Preferences → Sound**, set the Multi-Output Device as output
5. Note the index of BlackHole 2ch from the app's device list
6. `export AUDIO_DEVICE_INDEX=<that index>` before running

## Usage

```bash
# With API key from env
GEMINI_API_KEY=AIza... call-copilot

# Or enter it in the UI's API key field
call-copilot
```

**To use a specific audio device:**
```bash
AUDIO_DEVICE_INDEX=2 call-copilot
```

## Models

Default: `gemini-live-2.5-flash-preview`

Change the `MODEL` constant in `main.py` to use:
- `gemini-2.0-flash-live-001` (stable)
- `gemini-live-2.5-flash-preview` (latest, default)

## Requirements

- macOS 12+
- Python 3.11+
- Gemini API key ([get one](https://aistudio.google.com/app/apikey))
- BlackHole 2ch (for loopback) or any system audio device

## Architecture

```
[System Audio / Mic]
        │  PyAudio (raw PCM 16kHz)
        ▼
[AudioCapture thread]
        │  queue
        ▼
[GeminiWorker thread / asyncio loop]
        │  WebSocket (Gemini Live API)
        │  realtime_input.media_chunks (base64 PCM)
        ▼
[Gemini Live API]
        │  serverContent.modelTurn.parts[].text
        ▼
[Qt UI — Signals bridge]
        │
        ▼
[SuggestionBubble overlay]
```
