# Call Copilot

Real-time AI call assistant that lives in your macOS menu bar.

Streams system audio to Gemini Live API and surfaces bullet-point answers in a floating panel — without covering your screen.

**Compatible with macOS 12+ (Monterey and later).**

---

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/craihub/call-copilot/main/install.sh | bash
```

---

## Setup

### 1. Audio loopback (one-time)

```bash
brew install blackhole-2ch
```

In **System Settings → Sound → Output**: create a Multi-Output Device that includes both BlackHole 2ch and your speakers.
Set it as your system output. BlackHole will mirror everything to Call Copilot.

### 2. API Key

Get a Gemini API key from [aistudio.google.com](https://aistudio.google.com).

After launching, click **🎤 → Settings…** and paste your key. It's saved to `~/.call-copilot/config`.

---

## Usage

```bash
call-copilot
```

A **🎤** icon appears in your menu bar.

| Menu item | Action |
|---|---|
| Set Context… | Paste call agenda or notes before starting |
| Start Session | Connects to Gemini, starts listening |
| End Session | Stops capture and disconnects |
| Settings… | API key + audio device picker |

When a session is active the icon turns **🔴** and a floating panel shows AI bullet points in real time.

---

## Requirements

- macOS 12+ (Monterey or later)
- Python 3.10+
- Homebrew (for PortAudio + BlackHole)
- Gemini API key with Live API access

---

## Model

Uses `gemini-2.5-flash-exp` via the Gemini Multimodal Live API (native audio streaming, no STT step).
