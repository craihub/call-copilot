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
CONFIG_DIR.mkdir(exist_ok=True)

# ── Audio ─────────────────────────────────────────────────────────────────────
AUDIO_RATE     = 16000
AUDIO_CHANNELS = 1
AUDIO_FORMAT   = pyaudio.paInt16
CHUNK_SIZE     = 512
AUDIO_MIME     = "audio/pcm;rate=16000"

# ── Gemini ────────────────────────────────────────────────────────────────────
MODEL       = "gemini-2.5-flash-exp"
WS_URI_TMPL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta"
    ".GenerativeService.BidiGenerateContent?key={api_key}"
)

SYSTEM_PROMPT = (
    "You are a silent real-time call copilot. "
    "When you hear a question directed at the user, respond with 2-3 bullet points only. "
    "Rules: each bullet max 15 words, start with •, one per line, no intro text, no filler. "
    "If no question is asked, output nothing. "
    "Use the call context provided to tailor your answers."
)

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
BODY_FG     = "#d0d8ff"


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
    """Detect if any screencapture/screenshare process is running."""
    try:
        out = subprocess.check_output(
            ["pgrep", "-x", "-l", "screencaptureuiagent"], stderr=subprocess.DEVNULL
        )
        return bool(out.strip())
    except subprocess.CalledProcessError:
        pass
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "screensharing"], stderr=subprocess.DEVNULL
        )
        return bool(out.strip())
    except subprocess.CalledProcessError:
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
        instruction = SYSTEM_PROMPT
        if self.context.strip():
            instruction += f"\n\nCall context: {self.context.strip()}"
        return {
            "setup": {
                "model": f"models/{MODEL}",
                "generation_config": {
                    "response_modalities": ["TEXT"],
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
        try:
            async with websockets.connect(
                uri,
                additional_headers={"Content-Type": "application/json"},
                max_size=10 * 1024 * 1024,
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                self.running = True
                await ws.send(json.dumps(self.build_setup()))
                resp = json.loads(await ws.recv())
                if "error" in resp:
                    self.on_error(str(resp["error"]))
                    return
                self.on_status("🟢 Listening…")
                await asyncio.gather(self._send_loop(ws), self._recv_loop(ws))
        except websockets.exceptions.ConnectionClosedOK:
            self.on_status("Disconnected")
        except Exception as e:
            self.on_error(f"Error: {e}")
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
            except Exception:
                break

    async def _recv_loop(self, ws):
        async for raw in ws:
            if not self.running:
                break
            try:
                data = json.loads(raw)
                parts = (
                    data.get("serverContent", {})
                        .get("modelTurn", {})
                        .get("parts", [])
                )
                for part in parts:
                    text = part.get("text", "").strip()
                    if text:
                        self.on_text(text)
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
            stream = pa.open(
                format=AUDIO_FORMAT,
                channels=AUDIO_CHANNELS,
                rate=AUDIO_RATE,
                input=True,
                input_device_index=self.device_index,
                frames_per_buffer=CHUNK_SIZE,
            )
            while not self._stop_event.is_set():
                try:
                    data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
                    self.client.push_audio(data)
                except OSError:
                    break
            stream.stop_stream()
            stream.close()
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
        root.geometry(f"420x560+{x}+{y}")

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

        # ── Config section ────────────────────────────────────────────────────
        cfg_frame = tk.Frame(root, bg=BG)
        cfg_frame.pack(fill="x", padx=12, pady=(10, 0))

        # API Key row
        self._build_label(cfg_frame, "Gemini API Key")
        key_row = tk.Frame(cfg_frame, bg=BG)
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
        self._build_label(cfg_frame, "Call Context  (optional — injected as system prompt)")
        self._ctx_text = tk.Text(
            cfg_frame, height=5, bg=ENTRY_BG, fg=ENTRY_FG, insertbackground=ACCENT,
            relief="flat", font=("SF Pro Display", 11), bd=0, wrap="word",
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
        )
        self._ctx_text.insert("1.0", self._cfg.get("context", ""))
        self._ctx_text.pack(fill="x", pady=(2, 6), ipady=4)
        self._ctx_text.bind("<KeyRelease>", self._autosave)

        # Device row
        self._build_label(cfg_frame, "Audio Input Device")
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
            cfg_frame, textvariable=self._device_var,
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
            cfg_frame, text="🎤  Start Listening",
            bg=GREEN, fg="white", bd=0, cursor="hand2",
            font=("SF Pro Display", 13, "bold"), relief="flat",
            activebackground="#2aa050", activeforeground="white",
            command=self._toggle_session, pady=7,
        )
        self._start_btn.pack(fill="x", pady=(0, 10))

        # ── Divider ───────────────────────────────────────────────────────────
        tk.Frame(root, bg=BORDER, height=1).pack(fill="x", padx=0)

        # ── Suggestions feed ──────────────────────────────────────────────────
        feed_header = tk.Frame(root, bg=BG)
        feed_header.pack(fill="x", padx=12, pady=(6, 2))
        tk.Label(feed_header, text="Suggestions", bg=BG, fg=MUTED,
                 font=("SF Pro Display", 10, "bold")).pack(side="left")
        tk.Button(feed_header, text="Clear", bg=BG, fg=MUTED, bd=0, cursor="hand2",
                  font=("SF Pro Display", 9), activebackground=BG, activeforeground=ACCENT,
                  command=self._clear_feed).pack(side="right")

        feed_frame = tk.Frame(root, bg=BG)
        feed_frame.pack(fill="both", expand=True, padx=12, pady=(0, 4))

        self._feed = tk.Text(
            feed_frame, bg=BG, fg=BODY_FG,
            font=("SF Pro Display", 12), wrap="word", bd=0,
            highlightthickness=0, state="disabled", cursor="arrow",
        )
        self._feed.tag_configure("bullet", foreground=BULLET_FG, font=("SF Pro Display", 12, "bold"))
        self._feed.tag_configure("body",   foreground=BODY_FG,   font=("SF Pro Display", 12))

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
        self._autosave()

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

    # ── Feed ──────────────────────────────────────────────────────────────────
    def _on_text(self, text: str):
        self._ui_queue.put(("text", text))

    def _on_error(self, msg: str):
        self._ui_queue.put(("status", f"⚠️  {msg}"))

    def _set_status(self, msg: str):
        self._ui_queue.put(("status", msg))

    def _append_text(self, text: str):
        self._feed.configure(state="normal")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("•"):
                self._feed.insert("end", "• ", "bullet")
                self._feed.insert("end", line[1:].strip() + "\n", "body")
            else:
                self._feed.insert("end", line + "\n", "body")
        self._feed.see("end")
        self._feed.configure(state="disabled")

    def _clear_feed(self):
        self._feed.configure(state="normal")
        self._feed.delete("1.0", "end")
        self._feed.configure(state="disabled")

    # ── Queue poll (runs on tkinter thread) ───────────────────────────────────
    def _poll_queue(self):
        try:
            while True:
                cmd, arg = self._ui_queue.get_nowait()
                if cmd == "text":
                    self._append_text(arg)
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
                self._root.deiconify()
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

    def set_menu_bar_icon(self, active: bool):
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
