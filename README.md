# Call Copilot

Real-time call assistant powered by Gemini Live API. Streams system audio → WebSocket → instant bullet-point suggestions in an always-on-top overlay.

## Quick Install (macOS)

```bash
curl -fsSL https://raw.githubusercontent.com/craihub/call-copilot/main/install.sh | bash
```

## Manual Setup

```bash
git clone https://github.com/craihub/call-copilot.git
cd call-copilot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
GEMINI_API_KEY=your_key_here python main.py
```

## macOS Audio Loopback

To capture system audio (the remote caller's voice), install [BlackHole](https://github.com/ExistentialAudio/BlackHole):

```bash
brew install blackhole-2ch
```

Then in **System Settings → Sound → Input**, select **BlackHole 2ch** as your input device. In the app, enter the BlackHole device index (shown in the device list at the bottom of the overlay).

For a multi-output device (hear AND capture): Open **Audio MIDI Setup**, create a Multi-Output Device combining your speakers + BlackHole.

## Usage

1. Launch the app
2. Paste your Gemini API key (or set `GEMINI_API_KEY` env var)
3. Enter call context (e.g. "Interview for Senior Dev role")
4. Select audio input device index (blank = default mic)
5. Click **▶ Start Listening**
6. The overlay stays on top — bullet suggestions appear as the AI hears questions

## Requirements

- macOS 12+
- Python 3.10+
- Gemini API key with Live API access
- PortAudio (`brew install portaudio`)

## Model

Uses `gemini-2.5-flash-exp` via Multimodal Live API (WebSockets) — native audio understanding, no STT step.

**Latency optimizations:**
- 512-sample chunks (~32ms) streamed directly as PCM
- Server-side VAD with 500ms silence trigger
- TEXT-only response modality (no audio synthesis)
- Temperature 0.2 for fast, focused responses
