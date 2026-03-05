#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="$HOME/.call-copilot"
VENV="$INSTALL_DIR/venv"
LAUNCHER="$HOME/.local/bin/call-copilot"
REPO="https://github.com/craihub/call-copilot.git"

echo "→ Installing Call Copilot..."

# ── Deps ──────────────────────────────────────────────────────────────────────
echo "→ Installing dependencies..."
brew install python@3.11 portaudio 2>/dev/null || true

PYTHON="$(brew --prefix python@3.11)/bin/python3.11"
if [ ! -x "$PYTHON" ]; then
    echo "✗ python@3.11 not found at $PYTHON"
    exit 1
fi

# ── Clone or pull ─────────────────────────────────────────────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "→ Updating repo..."
    cd "$INSTALL_DIR" && git pull --ff-only 2>/dev/null || true
else
    echo "→ Cloning repo..."
    rm -rf "$INSTALL_DIR"
    git clone "$REPO" "$INSTALL_DIR"
fi

# ── Venv ──────────────────────────────────────────────────────────────────────
echo "→ Creating virtualenv..."
"$PYTHON" -m venv "$VENV" --clear
"$VENV/bin/pip" install --upgrade pip -q
"$VENV/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q

# ── Launcher ──────────────────────────────────────────────────────────────────
echo "→ Creating launcher..."
mkdir -p "$HOME/.local/bin"
cat > "$LAUNCHER" <<'SCRIPT'
#!/usr/bin/env bash
exec "$HOME/.call-copilot/venv/bin/python" "$HOME/.call-copilot/main.py" "$@"
SCRIPT
chmod +x "$LAUNCHER"

echo ""
echo "✓ Installed."
echo "  Run: call-copilot"
echo "  GEMINI_API_KEY can be entered in the app Settings panel."
