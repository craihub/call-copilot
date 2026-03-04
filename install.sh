#!/usr/bin/env bash
# Call Copilot — installer for macOS 12+
set -e

INSTALL_DIR="$HOME/.call-copilot"
BIN_DIR="$HOME/.local/bin"
REPO="https://raw.githubusercontent.com/craihub/call-copilot/main"

echo "→ Installing Call Copilot..."

# Ensure bin dir exists and is on PATH
mkdir -p "$BIN_DIR"
if ! echo "$PATH" | grep -q "$BIN_DIR"; then
  echo "export PATH=\"\$HOME/.local/bin:\$PATH\"" >> "$HOME/.zshrc"
  export PATH="$BIN_DIR:$PATH"
fi

# Create install dir
mkdir -p "$INSTALL_DIR"

# Download main.py and requirements.txt
curl -fsSL "$REPO/main.py"           -o "$INSTALL_DIR/main.py"
curl -fsSL "$REPO/requirements.txt"  -o "$INSTALL_DIR/requirements.txt"

# Set up Python venv
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

# Install PortAudio if not present (required by pyaudio)
if ! brew list portaudio &>/dev/null 2>&1; then
  echo "→ Installing PortAudio via Homebrew..."
  brew install portaudio
fi

# Write launcher script
cat > "$BIN_DIR/call-copilot" << 'EOF'
#!/usr/bin/env bash
exec "$HOME/.call-copilot/venv/bin/python" "$HOME/.call-copilot/main.py" "$@"
EOF
chmod +x "$BIN_DIR/call-copilot"

echo ""
echo "✓ Installed. Usage:"
echo ""
echo "  1. brew install blackhole-2ch       (one-time: audio loopback)"
echo "  2. Set GEMINI_API_KEY in Settings… after first launch"
echo "  3. call-copilot"
echo ""
echo "  The 🎤 icon will appear in your menu bar."
echo ""
