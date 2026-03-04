#!/usr/bin/env bash
# install.sh — Real-Time Call Copilot installer for macOS
# Usage: curl -fsSL https://raw.githubusercontent.com/craihub/call-copilot/main/install.sh | bash

set -euo pipefail

REPO="craihub/call-copilot"
INSTALL_DIR="$HOME/.call-copilot"
BIN_LINK="/usr/local/bin/call-copilot"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[copilot]${NC} $*"; }
warn()  { echo -e "${YELLOW}[copilot]${NC} $*"; }
error() { echo -e "${RED}[copilot]${NC} $*" >&2; exit 1; }

# ── macOS guard ───────────────────────────────────────────────────────────────
[[ "$(uname)" == "Darwin" ]] || error "This installer is macOS-only."

# ── Homebrew ──────────────────────────────────────────────────────────────────
if ! command -v brew &>/dev/null; then
  warn "Homebrew not found — installing…"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

# ── Python 3.11+ ─────────────────────────────────────────────────────────────
PY=""
for cmd in python3.12 python3.11 python3; do
  if command -v "$cmd" &>/dev/null; then
    ver=$("$cmd" -c "import sys; print(sys.version_info[:2])")
    if [[ "$ver" > "(3, 10)" ]]; then PY="$cmd"; break; fi
  fi
done

if [[ -z "$PY" ]]; then
  info "Installing Python 3.12 via Homebrew…"
  brew install python@3.12
  PY="python3.12"
fi
info "Using Python: $($PY --version)"

# ── PortAudio (for PyAudio) ───────────────────────────────────────────────────
if ! brew list portaudio &>/dev/null; then
  info "Installing PortAudio…"
  brew install portaudio
fi

# ── Qt6 (for PyQt6) ──────────────────────────────────────────────────────────
# PyQt6 ships its own Qt binaries via pip — no Homebrew Qt needed.

# ── BlackHole (loopback driver) ───────────────────────────────────────────────
if ! brew list --cask blackhole-2ch &>/dev/null 2>&1; then
  info "Installing BlackHole 2ch (loopback audio driver)…"
  brew install --cask blackhole-2ch
  echo ""
  warn "╔══════════════════════════════════════════════════════════════╗"
  warn "║  ACTION REQUIRED — Set up Multi-Output Device in macOS:     ║"
  warn "║  1. Open Audio MIDI Setup (cmd+space → Audio MIDI Setup)    ║"
  warn "║  2. Click + → Create Multi-Output Device                    ║"
  warn "║  3. Tick: your speakers AND BlackHole 2ch                   ║"
  warn "║  4. Set this Multi-Output as System Output in Sound prefs   ║"
  warn "║  5. Set AUDIO_DEVICE_INDEX to BlackHole's input index       ║"
  warn "╚══════════════════════════════════════════════════════════════╝"
  echo ""
fi

# ── Clone / update repo ───────────────────────────────────────────────────────
if [[ -d "$INSTALL_DIR/.git" ]]; then
  info "Updating existing install at $INSTALL_DIR…"
  git -C "$INSTALL_DIR" pull --ff-only
else
  info "Cloning call-copilot to $INSTALL_DIR…"
  git clone "https://github.com/$REPO.git" "$INSTALL_DIR"
fi

# ── Virtual environment ───────────────────────────────────────────────────────
VENV="$INSTALL_DIR/.venv"
if [[ ! -d "$VENV" ]]; then
  info "Creating virtual environment…"
  "$PY" -m venv "$VENV"
fi

info "Installing Python dependencies…"
"$VENV/bin/pip" install --upgrade pip -q
"$VENV/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q

# ── Launcher script ───────────────────────────────────────────────────────────
LAUNCHER="$INSTALL_DIR/call-copilot"
cat > "$LAUNCHER" <<'LAUNCHER_EOF'
#!/usr/bin/env bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/.venv/bin/python" "$DIR/main.py" "$@"
LAUNCHER_EOF
chmod +x "$LAUNCHER"

# ── Symlink in PATH ───────────────────────────────────────────────────────────
if [[ -w "$(dirname "$BIN_LINK")" ]]; then
  ln -sf "$LAUNCHER" "$BIN_LINK"
  info "Symlinked → $BIN_LINK"
else
  warn "Could not write to /usr/local/bin — run: sudo ln -sf $LAUNCHER $BIN_LINK"
fi

# ── macOS .app wrapper (optional, via Platypus if available) ─────────────────
# Skipped — PyQt6 apps run fine from terminal on macOS.

echo ""
info "╔══════════════════════════════════════════════════════════════╗"
info "║  Call Copilot installed!                                     ║"
info "║                                                              ║"
info "║  Run:  call-copilot                                          ║"
info "║    or: GEMINI_API_KEY=AIza... call-copilot                   ║"
info "║                                                              ║"
info "║  Loopback audio:                                             ║"
info "║    export AUDIO_DEVICE_INDEX=<BlackHole index>               ║"
info "║    (see device list printed in the app's UI)                 ║"
info "╚══════════════════════════════════════════════════════════════╝"
