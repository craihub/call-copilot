#!/usr/bin/env bash
set -e

REPO="https://github.com/craihub/call-copilot.git"
INSTALL_DIR="$HOME/.call-copilot"
VENV_DIR="$INSTALL_DIR/.venv"
BIN_DIR="$HOME/.local/bin"

echo "==> Installing Call Copilot…"

# Dependencies
if ! command -v brew &>/dev/null; then
  echo "Homebrew not found. Install from https://brew.sh then re-run."
  exit 1
fi

echo "==> Installing portaudio (required for PyAudio)…"
brew install portaudio 2>/dev/null || true

echo "==> Cloning repo…"
if [ -d "$INSTALL_DIR/.git" ]; then
  git -C "$INSTALL_DIR" pull --ff-only
else
  git clone "$REPO" "$INSTALL_DIR"
fi

echo "==> Creating virtualenv…"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q

echo "==> Creating launcher…"
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/call-copilot" <<EOF
#!/usr/bin/env bash
cd "$INSTALL_DIR"
export GEMINI_API_KEY="\${GEMINI_API_KEY:-}"
"$VENV_DIR/bin/python" main.py "\$@"
EOF
chmod +x "$BIN_DIR/call-copilot"

# Ensure ~/.local/bin is in PATH
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  echo ""
  echo "  Add to your shell config (~/.zshrc or ~/.bash_profile):"
  echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

echo ""
echo "==> Done! Run with:"
echo "    GEMINI_API_KEY=your_key call-copilot"
echo "    # or set GEMINI_API_KEY in your environment and just run: call-copilot"
