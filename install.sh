#!/usr/bin/env bash
set -e

REPO="https://github.com/craihub/call-copilot.git"
INSTALL_DIR="$HOME/.call-copilot"
BIN_DIR="$HOME/.local/bin"

echo "→ Installing Call Copilot..."

if ! command -v brew &>/dev/null; then
  echo "✗ Homebrew not found. Install from https://brew.sh then re-run."
  exit 1
fi

echo "→ Installing python@3.11 + tk support + portaudio..."
brew install python@3.11 portaudio 2>/dev/null || true

# python-tk@3.11 has a broken pip step on some systems — install separately, ignore errors
brew install python-tk@3.11 2>/dev/null || true

PYTHON="$(brew --prefix)/bin/python3.11"
if [ ! -x "$PYTHON" ]; then
  echo "✗ python3.11 not found at $PYTHON"
  exit 1
fi

echo "→ Using Python: $PYTHON"
"$PYTHON" --version

echo "→ Cloning repo..."
if [ -d "$INSTALL_DIR/.git" ]; then
  git -C "$INSTALL_DIR" pull --ff-only
else
  git clone "$REPO" "$INSTALL_DIR"
fi

echo "→ Creating virtualenv with python@3.11..."
VENV_DIR="$INSTALL_DIR/.venv"
"$PYTHON" -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q

echo "→ Creating launcher..."
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/call-copilot" << 'LAUNCHEREOF'
#!/usr/bin/env bash
cd "$HOME/.call-copilot"
exec "$HOME/.call-copilot/.venv/bin/python" main.py "$@"
LAUNCHEREOF
chmod +x "$BIN_DIR/call-copilot"

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  echo ""
  echo "  ⚠ Add to PATH (one-time):"
  echo "    echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc && source ~/.zshrc"
fi

echo ""
echo "✓ Installed."
echo "  call-copilot"
echo "  GEMINI_API_KEY can be set in the app Settings panel."
