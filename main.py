#!/usr/bin/env python3
"""
Real-Time Call Copilot
Single floating window: API key + context + device + start/stop + scrollable bullets.
Menu bar icon. Auto-hides when screensharing. macOS 12+ compatible.
"""

import asyncio
import base64
import json
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Optional

import pyaudio
import rumps
import websockets

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_DIR  = Path.home() / ".call-copilot"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE    = CONFIG_DIR / "copilot.log"
CONFIG_DIR.mkdir(exist_ok=True)

def log(msg: str):
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass

# ── Audio ─────────────────────────────────────────────────────────────────────
AUDIO_RATE     = 16000
AUDIO_CHANNELS = 1
AUDIO_FORMAT   = pyaudio.paInt16
CHUNK_SIZE     = 512
AUDIO_MIME     = "audio/pcm;rate=16000"

# ── Gemini ────────────────────────────────────────────────────────────────────
MODEL       = "gemini-2.0-flash-live-001"
WS_URI_TMPL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1alpha"
    ".GenerativeService.BidiGenerateContent?key={api_key}"
)

SYSTEM_PROMPT = """\
You are a real-time call copilot whispering answers into the user's ear.

CRITICAL RULES — violating ANY of these is a failure:

1. OUTPUT FORMAT: Only bullet points. Every line starts with •
2. BULLET LENGTH: Max 12 words per bullet. No exceptions.
3. BULLET COUNT: 1-4 bullets per response. Never more.
4. NO META-COMMENTARY: Never say "analyzing", "processing", "let me think".
   Give the ANSWER, not a description of what you're doing.
5. NO INTROS: No "Here's what I found" or "Based on the context". Just bullets.
6. NO FILLER: No "Great question!" or "That's interesting". Just answer.
7. SILENCE: If nobody is asking a question, output absolutely nothing.
8. ONLY RESPOND TO OTHER SPEAKERS: The user is wearing an earpiece. You hear
   TWO voices — the user and the other caller. Only respond when the OTHER
   person (not the user) asks a question or says something that needs a response.
   When the user speaks, stay silent — they don't need help with their own words.

GOOD examples:
  • Germany invaded Poland September 1, 1939
  • Treaty of Versailles created economic resentment
  • Britain and France declared war September 3

BAD examples (NEVER do this):
  • Analyzing the start of WWII
  • Let me break that down for you
  • That's a great question about history

{context_block}"""

# ── Colors ─────────────────────────────────────────────────────────────────────
BG          = "#0b0d1a"
BAR_BG      = "#13152a"
ACCENT      = "#4a8fff"
TEXT        = "#d0d8ff"
MUTED       = "#5a6080"
RED         = "#d04040"
GREEN       = "#38c060"
ENTRY_BG    = "#181c34"
ENTRY_FG    = "#b0bce0"
BORDER      = "#2a2e50"
BULLET_FG   = "#4a8fff"
BODY_FG     = "#e8ecff"
CARD_BG     = "#151830"


# ── Config helpers ─────────────────────────────────────────────────────────────
def load_config() -> dict:
    defaults = {"api_key": "", "device_index": "", "context": "", "win_x": "60", "win_y": "60"}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
            defaults.update(data)
        except Exception:
            pass
    return defaults


def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


# ── Audio device list ──────────────────────────────────────────────────────────
def list_input_devices() -> list:
    pa = pyaudio.PyAudio()
    devices = []
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info["maxInputChannels"] > 0:
            devices.append((i, info["name"]))
    pa.terminate()
    return devices


# ── Screenshare detection ──────────────────────────────────────────────────────
def is_screensharing() -> bool:
    """Detect macOS screenshare via multiple methods."""
    checks = [
        ["pgrep", "-x", "screencaptureui"],
        ["pgrep", "-x", "ScreenSharingAgent"],
    ]
    for cmd in checks:
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
            if out.strip():
                return True
        except subprocess.CalledProcessError:
            pass

    # Check CGSession for screen recording indicator
    try:
        out = subprocess.check_output(
            ["bash", "-c",
             "system_profiler SPDisplaysDataType 2>/dev/null | grep -i 'screen sharing'"],
            stderr=subprocess.DEVNULL
        )
        if out.strip():
            return True
    except subprocess.CalledProcessError:
        pass

    return False


# ── Gemini Live WebSocket client ───────────────────────────────────────────────
class GeminiLiveClient:
    def __init__(self, api_key: str, context: str, on_text, on_status, on_error):
        self.api_key   = api_key
        self.context   = context
        self.on_text   = on_text
        self.on_status = on_status
        self.on_error  = on_error
        self.audio_queue: queue.Queue = queue.Queue(maxsize=150)
        self.running   = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def build_setup(self) -> dict:
        ctx_block = ""
        if self.context.strip():
            ctx_block = f"CALL CONTEXT (use this to tailor answers):\n{self.context.strip()}"

        instruction = SYSTEM_PROMPT.format(context_block=ctx_block)

        return {
            "setup": {
                "model": f"models/{MODEL}",
                "generation_config": {
                    "response_modalities": ["AUDIO"],
                    "temperature": 0.2,
                },
                "system_instruction": {"parts": [{"text": instruction}]},
                "realtime_input_config": {
                    "automatic_activity_detection": {
                        "disabled": False,
                        "start_of_speech_sensitivity": "START_SENSITIVITY_LOW",
                        "end_of_speech_sensitivity":   "END_SENSITIVITY_HIGH",
                        "prefix_padding_ms":   20,
                        "silence_duration_ms": 500,
                    }
                },
            }
        }

    def start(self):
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._run_loop, daemon=True).start()

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._connect())

    async def _connect(self):
        uri = WS_URI_TMPL.format(api_key=self.api_key)
        self.on_status("Connecting…")
        log(f"[WS] Connecting to {uri[:80]}...")
        try:
            async with websockets.connect(
                uri,
                additional_headers={"Content-Type": "application/json"},
                max_size=10 * 1024 * 1024,
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                self.running = True
                setup_msg = self.build_setup()
                log(f"[WS] Sending setup: model={MODEL}")
                await ws.send(json.dumps(setup_msg))
                resp = json.loads(await ws.recv())
                log(f"[WS] Setup response: {json.dumps(resp)[:200]}")
                if "error" in resp:
                    self.on_error(str(resp["error"]))
                    return
                self.on_status("🟢 Connected — listening for other speaker")
                log("[WS] Connected successfully, entering send/recv loops")
                await asyncio.gather(self._send_loop(ws), self._recv_loop(ws))
        except websockets.exceptions.ConnectionClosedOK:
            self.on_status("Disconnected")
            log("[WS] Connection closed OK")
        except websockets.exceptions.ConnectionClosedError as e:
            self.on_error(f"Connection closed: {e.code} {e.reason}")
            log(f"[WS] Connection closed error: {e.code} {e.reason}")
        except Exception as e:
            self.on_error(f"Error: {e}")
            log(f"[WS] Exception: {e}")
        finally:
            self.running = False

    async def _send_loop(self, ws):
        loop = asyncio.get_event_loop()
        while self.running:
            try:
                chunk = await loop.run_in_executor(
                    None, lambda: self.audio_queue.get(timeout=0.05)
                )
                await ws.send(json.dumps({
                    "realtime_input": {
                        "media_chunks": [{
                            "mime_type": AUDIO_MIME,
                            "data": base64.b64encode(chunk).decode(),
                        }]
                    }
                }))
            except queue.Empty:
                continue
            except Exception as e:
                log(f"[SEND] Error: {e}")
                break

    async def _recv_loop(self, ws):
        async for raw in ws:
            if not self.running:
                break
            try:
                data = json.loads(raw)

                # Extract text from model responses (ignore audio bytes)
                server_content = data.get("serverContent", {})
                parts = server_content.get("modelTurn", {}).get("parts", [])
                for part in parts:
                    text = part.get("text", "").strip()
                    if text:
                        log(f"[RECV] Text: {text[:100]}")
                        self.on_text(text)
                    # Silently ignore inlineData (audio bytes)

            except json.JSONDecodeError:
                pass

    def push_audio(self, chunk: bytes):
        if self.running:
            try:
                self.audio_queue.put_nowait(chunk)
            except queue.Full:
                pass

    def stop(self):
        self.running = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)


# ── Audio capture thread ───────────────────────────────────────────────────────
class AudioCapture(threading.Thread):
    def __init__(self, device_index: Optional[int], client: GeminiLiveClient):
        super().__init__(daemon=True)
        self.device_index = device_index
        self.client       = client
        self._stop_event  = threading.Event()

    def run(self):
        pa = pyaudio.PyAudio()
        try:
            log(f"[AUDIO] Opening device index={self.device_index}")
            stream = pa.open(
                format=AUDIO_FORMAT,
                channels=AUDIO_CHANNELS,
                rate=AUDIO_RATE,
                input=True,
                input_device_index=self.device_index,
                frames_per_buffer=CHUNK_SIZE,
            )
            log("[AUDIO] Capture started")
            while not self._stop_event.is_set():
                try:
                    data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
                    self.client.push_audio(data)
                except OSError:
                    break
            stream.stop_stream()
            stream.close()
            log("[AUDIO] Capture stopped")
        except Exception as e:
            log(f"[AUDIO] Error: {e}")
        finally:
            pa.terminate()

    def stop(self):
        self._stop_event.set()


# ── Main window (tkinter) ──────────────────────────────────────────────────────
class CopilotWindow:
    """Single floating window: config on top, bullets below."""

    def __init__(self, app_ref):
        self.app        = app_ref
        self._root: Optional[tk.Tk] = None
        self._ready     = threading.Event()
        self._ui_queue: queue.Queue = queue.Queue()
        self._drag_x    = 0
        self._drag_y    = 0
        self._client: Optional[GeminiLiveClient] = None
        self._audio: Optional[AudioCapture]       = None
        self._active    = False
        self._devices   = []
        self._cfg       = load_config()
        self._key_visible = False
        self._bullet_count = 0
        self._max_bullets  = 30

    def launch(self):
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait(timeout=5)

    # ── Build UI ──────────────────────────────────────────────────────────────
    def _run(self):
        self._root = tk.Tk()
        root = self._root

        root.title("Call Copilot")
        root.configure(bg=BG)
        root.overrideredirect(True)
        root.wm_attributes("-topmost", True)
        root.wm_attributes("-alpha", 0.95)
        root.resizable(False, False)

        # Restore position
        x = int(self._cfg.get("win_x", 60))
        y = int(self._cfg.get("win_y", 60))
        root.geometry(f"440x640+{x}+{y}")

        # ── Title bar ─────────────────────────────────────────────────────────
        bar = tk.Frame(root, bg=BAR_BG, height=32)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        bar.bind("<ButtonPress-1>",  self._drag_start)
        bar.bind("<B1-Motion>",      self._drag_motion)
        bar.bind("<ButtonRelease-1>", self._drag_end)

        tk.Label(bar, text="🎤  Call Copilot", bg=BAR_BG, fg=ACCENT,
                 font=("SF Pro Display", 12, "bold")).pack(side="left", padx=10, pady=4)

        close_btn = tk.Button(bar, text="✕", bg=BAR_BG, fg=MUTED, bd=0,
                              font=("SF Pro Display", 12), cursor="hand2",
                              activebackground=BAR_BG, activeforeground="#ff6060",
                              command=self._hide)
        close_btn.pack(side="right", padx=8)

        # Minimize button (collapse config)
        min_btn = tk.Button(bar, text="─", bg=BAR_BG, fg=MUTED, bd=0,
                            font=("SF Pro Display", 12), cursor="hand2",
                            activebackground=BAR_BG, activeforeground=ACCENT,
                            command=self._toggle_config)
        min_btn.pack(side="right", padx=2)

        # ── Config section (collapsible) ──────────────────────────────────────
        self._cfg_frame = tk.Frame(root, bg=BG)
        self._cfg_frame.pack(fill="x", padx=12, pady=(10, 0))
        self._cfg_visible = True

        # API Key row
        self._build_label(self._cfg_frame, "Gemini API Key")
        key_row = tk.Frame(self._cfg_frame, bg=BG)
        key_row.pack(fill="x", pady=(2, 6))

        self._api_var = tk.StringVar(value=self._cfg.get("api_key", ""))
        self._key_entry = tk.Entry(
            key_row, textvariable=self._api_var,
            show="●", bg=ENTRY_BG, fg=ENTRY_FG, insertbackground=ACCENT,
            relief="flat", font=("SF Pro Display", 11), bd=0,
            highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=ACCENT,
        )
        self._key_entry.pack(side="left", fill="x", expand=True, ipady=5, padx=(0, 6))
        self._key_entry.bind("<KeyRelease>", self._autosave)

        self._toggle_btn = tk.Button(
            key_row, text="Show", bg=ENTRY_BG, fg=MUTED, bd=0, cursor="hand2",
            font=("SF Pro Display", 10), activebackground=ENTRY_BG, activeforeground=ACCENT,
            command=self._toggle_key_visibility, padx=6,
        )
        self._toggle_btn.pack(side="right")

        # Context row
        self._build_label(self._cfg_frame, "Call Context  (optional — injected into prompt)")
        self._ctx_text = tk.Text(
            self._cfg_frame, height=4, bg=ENTRY_BG, fg=ENTRY_FG, insertbackground=ACCENT,
            relief="flat", font=("SF Pro Display", 11), bd=0, wrap="word",
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
        )
        self._ctx_text.insert("1.0", self._cfg.get("context", ""))
        self._ctx_text.pack(fill="x", pady=(2, 6), ipady=4)
        self._ctx_text.bind("<KeyRelease>", self._autosave)

        # Device row
        self._build_label(self._cfg_frame, "Audio Input Device")
        self._devices = list_input_devices()
        device_names  = [f"[{i}] {n}" for i, n in self._devices]

        self._device_var = tk.StringVar()
        saved_idx = self._cfg.get("device_index", "")
        if saved_idx != "" and self._devices:
            try:
                saved_idx_int = int(saved_idx)
                matches = [d for d in self._devices if d[0] == saved_idx_int]
                if matches:
                    self._device_var.set(f"[{matches[0][0]}] {matches[0][1]}")
            except (ValueError, IndexError):
                pass
        if not self._device_var.get() and device_names:
            self._device_var.set(device_names[0])

        device_menu = ttk.Combobox(
            self._cfg_frame, textvariable=self._device_var,
            values=device_names, state="readonly",
            font=("SF Pro Display", 11),
        )
        device_menu.pack(fill="x", pady=(2, 10))
        device_menu.bind("<<ComboboxSelected>>", self._autosave)

        # Style combobox
        style = ttk.Style()
        style.theme_use("default")
        style.configure("TCombobox",
                        fieldbackground=ENTRY_BG, background=ENTRY_BG,
                        foreground=ENTRY_FG, selectbackground=ENTRY_BG,
                        selectforeground=ENTRY_FG, arrowcolor=ACCENT,
                        borderwidth=0)

        # Start/Stop button
        self._start_btn = tk.Button(
            self._cfg_frame, text="🎤  Start Listening",
            bg=GREEN, fg="white", bd=0, cursor="hand2",
            font=("SF Pro Display", 13, "bold"), relief="flat",
            activebackground="#2aa050", activeforeground="white",
            command=self._toggle_session, pady=7,
        )
        self._start_btn.pack(fill="x", pady=(0, 10))

        # ── Divider ───────────────────────────────────────────────────────────
        self._divider = tk.Frame(root, bg=BORDER, height=1)
        self._divider.pack(fill="x", padx=0)

        # ── Suggestions feed ──────────────────────────────────────────────────
        feed_header = tk.Frame(root, bg=BG)
        feed_header.pack(fill="x", padx=12, pady=(6, 2))
        tk.Label(feed_header, text="💡 Suggestions", bg=BG, fg=MUTED,
                 font=("SF Pro Display", 11, "bold")).pack(side="left")
        tk.Button(feed_header, text="Clear", bg=BG, fg=MUTED, bd=0, cursor="hand2",
                  font=("SF Pro Display", 9), activebackground=BG, activeforeground=ACCENT,
                  command=self._clear_feed).pack(side="right")

        feed_frame = tk.Frame(root, bg=BG)
        feed_frame.pack(fill="both", expand=True, padx=12, pady=(0, 4))

        self._feed = tk.Text(
            feed_frame, bg=BG, fg=BODY_FG,
            font=("SF Pro Display", 16), wrap="word", bd=0,
            highlightthickness=0, state="disabled", cursor="arrow",
            spacing1=2, spacing3=6,
        )
        self._feed.tag_configure("bullet_dot", foreground=ACCENT,
                                 font=("SF Pro Display", 18, "bold"))
        self._feed.tag_configure("bullet_text", foreground=BODY_FG,
                                 font=("SF Pro Display", 16))
        self._feed.tag_configure("separator", foreground=BORDER,
                                 font=("SF Pro Display", 6))

        scroll = tk.Scrollbar(feed_frame, command=self._feed.yview, bg=BG,
                              troughcolor=BG, activebackground=ACCENT)
        self._feed.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self._feed.pack(side="left", fill="both", expand=True)

        # ── Status bar ────────────────────────────────────────────────────────
        self._status_var = tk.StringVar(value="Ready")
        tk.Label(root, textvariable=self._status_var, bg=BAR_BG, fg=MUTED,
                 font=("SF Pro Display", 10), anchor="w",
                 padx=10, pady=4).pack(fill="x", side="bottom")

        self._ready.set()
        root.after(100, self._poll_queue)
        root.after(4000, self._check_screenshare)
        root.after(2000, self._enforce_topmost)
        root.mainloop()

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _build_label(self, parent, text):
        tk.Label(parent, text=text, bg=BG, fg=MUTED,
                 font=("SF Pro Display", 10)).pack(anchor="w", pady=(0, 1))

    def _drag_start(self, e):
        self._drag_x = e.x_root - self._root.winfo_x()
        self._drag_y = e.y_root - self._root.winfo_y()

    def _drag_motion(self, e):
        self._root.geometry(f"+{e.x_root - self._drag_x}+{e.y_root - self._drag_y}")

    def _drag_end(self, e):
        self._cfg["win_x"] = str(self._root.winfo_x())
        self._cfg["win_y"] = str(self._root.winfo_y())
        save_config(self._cfg)

    def _hide(self):
        if self._root:
            self._root.withdraw()

    def show(self):
        self._ui_queue.put(("show", None))

    def _toggle_config(self):
        """Collapse/expand config section to maximize bullet area."""
        if self._cfg_visible:
            self._cfg_frame.pack_forget()
            self._cfg_visible = False
        else:
            # Re-insert config frame between title bar and divider
            self._cfg_frame.pack(fill="x", padx=12, pady=(10, 0),
                                 before=self._divider)
            self._cfg_visible = True

    def _toggle_key_visibility(self):
        self._key_visible = not self._key_visible
        self._key_entry.config(show="" if self._key_visible else "●")
        self._toggle_btn.config(text="Hide" if self._key_visible else "Show")

    def _autosave(self, _event=None):
        self._cfg["api_key"]      = self._api_var.get().strip()
        self._cfg["context"]      = self._ctx_text.get("1.0", "end-1c")
        device_str = self._device_var.get()
        if device_str.startswith("["):
            try:
                idx = int(device_str.split("]")[0][1:])
                self._cfg["device_index"] = str(idx)
            except (ValueError, IndexError):
                pass
        save_config(self._cfg)

    def _get_device_index(self) -> Optional[int]:
        device_str = self._device_var.get()
        if device_str.startswith("["):
            try:
                return int(device_str.split("]")[0][1:])
            except (ValueError, IndexError):
                pass
        return None

    def _enforce_topmost(self):
        """Periodically re-assert topmost to combat macOS de-focusing."""
        if self._root and self._root.winfo_viewable():
            self._root.wm_attributes("-topmost", True)
            self._root.lift()
        if self._root:
            self._root.after(3000, self._enforce_topmost)

    # ── Session control ───────────────────────────────────────────────────────
    def _toggle_session(self):
        if self._active:
            self._stop_session()
        else:
            self._start_session()

    def _start_session(self):
        api_key = self._api_var.get().strip()
        if not api_key:
            self._set_status("⚠️  Enter API key first")
            return
        context = self._ctx_text.get("1.0", "end-1c")
        device_idx = self._get_device_index()

        self._client = GeminiLiveClient(
            api_key=api_key,
            context=context,
            on_text=self._on_text,
            on_status=self._set_status,
            on_error=self._on_error,
        )
        self._client.start()
        self._audio = AudioCapture(device_idx, self._client)
        self._audio.start()
        self._active = True
        self._start_btn.config(text="⏹  Stop", bg=RED, activebackground="#b03030")
        self.app.set_menu_bar_icon(active=True)

        # Auto-collapse config when session starts to maximize bullet area
        if self._cfg_visible:
            self._toggle_config()

        self._autosave()
        log("[SESSION] Started")

    def _stop_session(self):
        if self._client:
            self._client.stop()
        if self._audio:
            self._audio.stop()
        self._client = None
        self._audio  = None
        self._active = False
        self._start_btn.config(text="🎤  Start Listening", bg=GREEN, activebackground="#2aa050")
        self._set_status("Stopped")
        self.app.set_menu_bar_icon(active=False)

        # Re-expand config when session stops
        if not self._cfg_visible:
            self._toggle_config()

        log("[SESSION] Stopped")

    # ── Feed ──────────────────────────────────────────────────────────────────
    def _on_text(self, text: str):
        self._ui_queue.put(("text", text))

    def _on_error(self, msg: str):
        self._ui_queue.put(("status", f"⚠️  {msg}"))
        log(f"[ERROR] {msg}")

    def _set_status(self, msg: str):
        self._ui_queue.put(("status", msg))

    def _append_bullet(self, text: str):
        """Process incoming text into clean bullet points."""
        self._feed.configure(state="normal")

        lines = text.splitlines()
        has_bullets = False

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Strip any bullet prefix variants and normalize
            clean = line
            for prefix in ("•", "- ", "* ", "– ", "· "):
                if clean.startswith(prefix):
                    clean = clean[len(prefix):].strip()
                    break

            # Skip meta-commentary that slips through
            skip_patterns = [
                "analyzing", "processing", "let me", "here's",
                "based on", "great question", "that's interesting",
                "i can help", "i understand", "certainly",
                "of course", "sure thing",
            ]
            if any(clean.lower().startswith(p) for p in skip_patterns):
                log(f"[FILTER] Skipped meta-commentary: {clean[:60]}")
                continue

            # Skip lines that are too long (paragraph filler)
            if len(clean) > 100:
                log(f"[FILTER] Skipped long line ({len(clean)} chars): {clean[:60]}...")
                continue

            if clean:
                self._feed.insert("end", "  •  ", "bullet_dot")
                self._feed.insert("end", clean + "\n", "bullet_text")
                self._bullet_count += 1
                has_bullets = True

        if has_bullets:
            # Add thin separator between response groups
            self._feed.insert("end", "\n", "separator")

        # Trim old bullets if too many
        if self._bullet_count > self._max_bullets:
            lines_to_delete = min(self._bullet_count - self._max_bullets + 5, 15)
            for _ in range(lines_to_delete):
                self._feed.delete("1.0", "2.0")
            self._bullet_count = max(self._bullet_count - lines_to_delete, 0)

        self._feed.see("end")
        self._feed.configure(state="disabled")

    def _clear_feed(self):
        self._feed.configure(state="normal")
        self._feed.delete("1.0", "end")
        self._feed.configure(state="disabled")
        self._bullet_count = 0

    # ── Queue poll (runs on tkinter thread) ───────────────────────────────────
    def _poll_queue(self):
        try:
            while True:
                cmd, arg = self._ui_queue.get_nowait()
                if cmd == "text":
                    self._append_bullet(arg)
                elif cmd == "status":
                    if self._status_var:
                        self._status_var.set(arg)
                elif cmd == "show":
                    self._root.deiconify()
                    self._root.lift()
                    self._root.wm_attributes("-topmost", True)
        except queue.Empty:
            pass
        if self._root:
            self._root.after(100, self._poll_queue)

    # ── Screenshare auto-hide ─────────────────────────────────────────────────
    def _check_screenshare(self):
        if not self._root:
            return
        try:
            if is_screensharing():
                self._root.withdraw()
            else:
                if not self._root.winfo_viewable():
                    self._root.deiconify()
                    self._root.lift()
                    self._root.wm_attributes("-topmost", True)
        except Exception:
            pass
        self._root.after(4000, self._check_screenshare)


# ── rumps menu bar app ─────────────────────────────────────────────────────────
class CallCopilotApp(rumps.App):
    def __init__(self):
        super().__init__("🎤", quit_button=None)
        self.menu = [
            rumps.MenuItem("Show / Hide", callback=self._toggle_window),
            None,
            rumps.MenuItem("Quit Call Copilot", callback=self._quit),
        ]
        self._window: Optional[CopilotWindow] = None
        self._active = False

    def set_menu_bar_icon(self, active: bool):
        """Blue mic when idle, red dot when listening."""
        self._active = active
        self.title = "🔴" if active else "🎤"

    @rumps.clicked("Show / Hide")
    def _toggle_window(self, _=None):
        if self._window:
            self._window.show()

    @rumps.clicked("Quit Call Copilot")
    def _quit(self, _=None):
        if self._window and self._window._active:
            self._window._stop_session()
        rumps.quit_application()

    def run_with_window(self):
        self._window = CopilotWindow(app_ref=self)
        self._window.launch()
        self.run()


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = CallCopilotApp()
    app.run_with_window()
