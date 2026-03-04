#!/usr/bin/env python3
"""
Real-Time Call Copilot
Menu bar app (rumps) + floating tkinter suggestions panel.
Streams system audio (BlackHole loopback) to Gemini Live API.
Compatible with macOS 12+.
"""

import asyncio
import base64
import json
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from typing import Optional

import pyaudio
import rumps
import websockets

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_DIR  = Path.home() / ".call-copilot"
CONFIG_FILE = CONFIG_DIR / "config"
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
    "Act as a silent call copilot. Use the provided context to inform your answers. "
    "When you hear a question directed at the user, immediately provide 2-3 short, "
    "high-impact bullet points. Maximum 15 words per bullet. "
    "No conversational filler. If no question is asked, stay silent. "
    "Format: each bullet on its own line starting with •"
)


# ── Config helpers ─────────────────────────────────────────────────────────────
def load_config() -> dict:
    cfg = {"api_key": "", "device_index": ""}
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text().splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()
    return cfg


def save_config(cfg: dict):
    CONFIG_FILE.write_text("\n".join(f"{k}={v}" for k, v in cfg.items()))


# ── Audio device list ──────────────────────────────────────────────────────────
def list_input_devices() -> list[tuple[int, str]]:
    pa = pyaudio.PyAudio()
    devices = []
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info["maxInputChannels"] > 0:
            devices.append((i, info["name"]))
    pa.terminate()
    return devices


# ── Gemini Live WebSocket client ───────────────────────────────────────────────
class GeminiLiveClient:
    def __init__(self, api_key: str, context: str, on_text, on_status, on_error):
        self.api_key    = api_key
        self.context    = context
        self.on_text    = on_text
        self.on_status  = on_status
        self.on_error   = on_error
        self.audio_queue: queue.Queue = queue.Queue(maxsize=100)
        self.running    = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def build_setup_message(self) -> dict:
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
                        "prefix_padding_ms":    20,
                        "silence_duration_ms": 500,
                    }
                },
            }
        }

    def start(self):
        self._loop = asyncio.new_event_loop()
        t = threading.Thread(target=self._run_loop, daemon=True)
        t.start()

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
                self.on_status("Connected — listening…")
                await ws.send(json.dumps(self.build_setup_message()))
                resp = await ws.recv()
                data = json.loads(resp)
                if "error" in data:
                    self.on_error(str(data["error"]))
                    return
                await asyncio.gather(
                    self._send_loop(ws),
                    self._recv_loop(ws),
                )
        except websockets.exceptions.ConnectionClosedOK:
            self.on_status("Disconnected")
        except Exception as e:
            self.on_error(f"WS error: {e}")
        finally:
            self.running = False

    async def _send_loop(self, ws):
        loop = asyncio.get_event_loop()
        while self.running:
            try:
                chunk = await loop.run_in_executor(
                    None, lambda: self.audio_queue.get(timeout=0.05)
                )
                msg = {
                    "realtime_input": {
                        "media_chunks": [{
                            "mime_type": AUDIO_MIME,
                            "data": base64.b64encode(chunk).decode(),
                        }]
                    }
                }
                await ws.send(json.dumps(msg))
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


# ── Floating suggestions panel (tkinter) ───────────────────────────────────────
class SuggestionsPanel:
    """Always-on-top semi-transparent floating window for AI bullets."""

    def __init__(self):
        self._root: Optional[tk.Tk] = None
        self._text: Optional[tk.Text] = None
        self._status_var: Optional[tk.StringVar] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._queue: queue.Queue = queue.Queue()

    def show(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=3)

    def _run(self):
        self._root = tk.Tk()
        self._root.title("Call Copilot")
        self._root.geometry("420x320+40+40")
        self._root.configure(bg="#0a0a14")
        self._root.wm_attributes("-topmost", True)
        self._root.wm_attributes("-alpha", 0.92)
        self._root.overrideredirect(True)   # frameless

        # Drag support
        self._root.bind("<ButtonPress-1>",   self._drag_start)
        self._root.bind("<B1-Motion>",       self._drag_motion)
        self._drag_x = self._drag_y = 0

        # Title bar row
        bar = tk.Frame(self._root, bg="#16162a", height=28)
        bar.pack(fill="x")
        bar.bind("<ButtonPress-1>", self._drag_start)
        bar.bind("<B1-Motion>",     self._drag_motion)

        tk.Label(bar, text="🎤 Call Copilot", bg="#16162a",
                 fg="#a0a0ff", font=("SF Pro Display", 11, "bold")).pack(side="left", padx=8)

        close_btn = tk.Button(bar, text="✕", bg="#16162a", fg="#666",
                              bd=0, font=("SF Pro Display", 11),
                              activebackground="#16162a", activeforeground="#ff6b6b",
                              command=self.hide)
        close_btn.pack(side="right", padx=6)

        # Status label
        self._status_var = tk.StringVar(value="Connecting…")
        tk.Label(self._root, textvariable=self._status_var,
                 bg="#0a0a14", fg="#4a9eff",
                 font=("SF Pro Display", 10)).pack(anchor="w", padx=10, pady=(4, 0))

        # Suggestions text area
        frame = tk.Frame(self._root, bg="#0a0a14")
        frame.pack(fill="both", expand=True, padx=10, pady=6)

        self._text = tk.Text(
            frame,
            bg="#0a0a14", fg="#e0e0ff",
            font=("SF Pro Display", 12),
            wrap="word", bd=0, highlightthickness=0,
            state="disabled", cursor="arrow",
        )
        self._text.pack(fill="both", expand=True)

        # Tag for bullet styling
        self._text.tag_configure("bullet", foreground="#4a9eff", font=("SF Pro Display", 12, "bold"))
        self._text.tag_configure("body",   foreground="#e0e0ff", font=("SF Pro Display", 12))

        self._ready.set()
        self._root.after(100, self._poll_queue)
        self._root.mainloop()

    def _poll_queue(self):
        """Pull pending UI updates from queue — runs on tkinter thread."""
        try:
            while True:
                cmd, arg = self._queue.get_nowait()
                if cmd == "text":
                    self._append_text(arg)
                elif cmd == "status":
                    if self._status_var:
                        self._status_var.set(arg)
                elif cmd == "clear":
                    self._clear()
                elif cmd == "hide":
                    self._root.withdraw()
                elif cmd == "show":
                    self._root.deiconify()
        except queue.Empty:
            pass
        if self._root:
            self._root.after(100, self._poll_queue)

    def _append_text(self, text: str):
        if not self._text:
            return
        self._text.configure(state="normal")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("•"):
                self._text.insert("end", "• ", "bullet")
                self._text.insert("end", line[1:].strip() + "\n", "body")
            else:
                self._text.insert("end", line + "\n", "body")
        self._text.see("end")
        self._text.configure(state="disabled")

    def _clear(self):
        if self._text:
            self._text.configure(state="normal")
            self._text.delete("1.0", "end")
            self._text.configure(state="disabled")

    def hide(self):
        self._queue.put(("hide", None))

    def show_panel(self):
        self._queue.put(("show", None))

    def set_status(self, msg: str):
        self._queue.put(("status", msg))

    def add_text(self, text: str):
        self._queue.put(("text", text))

    def clear(self):
        self._queue.put(("clear", None))

    def _drag_start(self, event):
        self._drag_x = event.x_root - self._root.winfo_x()
        self._drag_y = event.y_root - self._root.winfo_y()

    def _drag_motion(self, event):
        x = event.x_root - self._drag_x
        y = event.y_root - self._drag_y
        self._root.geometry(f"+{x}+{y}")


# ── Settings dialog (tkinter) ─────────────────────────────────────────────────
def open_settings_dialog(cfg: dict, devices: list[tuple[int, str]], on_save):
    """Blocking settings window — run in a thread."""
    root = tk.Tk()
    root.title("Call Copilot — Settings")
    root.configure(bg="#0a0a14")
    root.resizable(False, False)
    root.geometry("400x300")
    root.wm_attributes("-topmost", True)

    pad = {"padx": 16, "pady": 6}

    tk.Label(root, text="Gemini API Key", bg="#0a0a14", fg="#a0a0ff",
             font=("SF Pro Display", 11)).pack(anchor="w", **pad)
    api_var = tk.StringVar(value=cfg.get("api_key", ""))
    api_entry = tk.Entry(root, textvariable=api_var, show="•", width=42,
                         bg="#16162a", fg="#e0e0ff", insertbackground="#e0e0ff",
                         font=("SF Pro Display", 11), bd=0, highlightthickness=1,
                         highlightcolor="#4a9eff")
    api_entry.pack(anchor="w", padx=16)

    tk.Label(root, text="Audio Input Device", bg="#0a0a14", fg="#a0a0ff",
             font=("SF Pro Display", 11)).pack(anchor="w", **pad)

    device_names = [f"{i}: {name}" for i, name in devices]
    device_var = tk.StringVar()
    saved_idx = cfg.get("device_index", "")
    if saved_idx:
        match = next((n for n in device_names if n.startswith(f"{saved_idx}:")), None)
        if match:
            device_var.set(match)
    if not device_var.get() and device_names:
        device_var.set(device_names[0])

    device_menu = tk.OptionMenu(root, device_var, *device_names)
    device_menu.configure(bg="#16162a", fg="#e0e0ff", activebackground="#4a9eff",
                          font=("SF Pro Display", 11), bd=0, highlightthickness=0)
    device_menu["menu"].configure(bg="#16162a", fg="#e0e0ff")
    device_menu.pack(anchor="w", padx=16)

    def save():
        selected = device_var.get()
        idx = selected.split(":")[0].strip() if selected else ""
        new_cfg = {"api_key": api_var.get().strip(), "device_index": idx}
        save_config(new_cfg)
        on_save(new_cfg)
        root.destroy()

    tk.Button(root, text="Save & Close", command=save,
              bg="#4a9eff", fg="#0a0a14", font=("SF Pro Display", 11, "bold"),
              bd=0, padx=16, pady=6, cursor="hand2").pack(pady=20)

    root.mainloop()


# ── Menu bar app ───────────────────────────────────────────────────────────────
class CallCopilotApp(rumps.App):
    def __init__(self):
        super().__init__("🎤", quit_button=None)

        self.cfg     = load_config()
        self.devices = list_input_devices()
        self.panel   = SuggestionsPanel()
        self.panel.show()

        self.gemini: Optional[GeminiLiveClient] = None
        self.capture: Optional[AudioCapture]    = None
        self._session_active = False

        # Context field shown in menu
        self._context_item = rumps.MenuItem("Context: (paste before session)", callback=None)

        self.menu = [
            rumps.MenuItem("Start Session",   callback=self.start_session),
            rumps.MenuItem("End Session",     callback=self.end_session),
            rumps.separator,
            rumps.MenuItem("Set Context…",    callback=self.set_context),
            rumps.MenuItem("Settings…",       callback=self.open_settings),
            rumps.separator,
            rumps.MenuItem("Quit",            callback=rumps.quit_application),
        ]
        self._context = ""

    # ── Context ───────────────────────────────────────────────────────────────
    @rumps.clicked("Set Context…")
    def set_context(self, _):
        resp = rumps.Window(
            title="Set Call Context",
            message="Paste agenda, notes, or context for this call:",
            default_text=self._context,
            dimensions=(380, 120),
        ).run()
        if resp.clicked:
            self._context = resp.text.strip()

    # ── Settings ──────────────────────────────────────────────────────────────
    @rumps.clicked("Settings…")
    def open_settings(self, _):
        def on_save(new_cfg):
            self.cfg = new_cfg
        t = threading.Thread(
            target=open_settings_dialog,
            args=(self.cfg, self.devices, on_save),
            daemon=True,
        )
        t.start()

    # ── Session management ────────────────────────────────────────────────────
    @rumps.clicked("Start Session")
    def start_session(self, _):
        if self._session_active:
            rumps.alert("Session already running.")
            return

        api_key = self.cfg.get("api_key", "").strip()
        if not api_key:
            rumps.alert("No API key set. Open Settings… first.")
            return

        device_idx_str = self.cfg.get("device_index", "").strip()
        device_idx = int(device_idx_str) if device_idx_str.isdigit() else None

        self.panel.clear()
        self.panel.show_panel()

        self.gemini = GeminiLiveClient(
            api_key   = api_key,
            context   = self._context,
            on_text   = self.panel.add_text,
            on_status = self.panel.set_status,
            on_error  = lambda e: self.panel.set_status(f"Error: {e}"),
        )
        self.gemini.start()

        self.capture = AudioCapture(device_idx, self.gemini)
        self.capture.start()

        self._session_active = True
        self.title = "🔴"

    @rumps.clicked("End Session")
    def end_session(self, _):
        if not self._session_active:
            return
        if self.capture:
            self.capture.stop()
        if self.gemini:
            self.gemini.stop()
        self._session_active = False
        self.title = "🎤"
        self.panel.set_status("Session ended.")


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    app = CallCopilotApp()
    app.run()


if __name__ == "__main__":
    main()
