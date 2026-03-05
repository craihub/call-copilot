#!/usr/bin/env python3
"""
Call Copilot — Real-time call assistant
tkinter + rumps. Gemini Live API via WebSocket.
"""

import asyncio
import base64
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from pathlib import Path
from typing import Optional

import pyaudio
import websockets

try:
    import rumps
    HAS_RUMPS = True
except ImportError:
    HAS_RUMPS = False

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_PATH = Path.home() / ".call-copilot" / "config.json"
CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

GEMINI_MODEL  = "models/gemini-2.0-flash-exp"
WS_URL        = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
AUDIO_RATE    = 16000
AUDIO_CHUNK   = 1024
AUDIO_FORMAT  = pyaudio.paInt16
AUDIO_CHANNELS = 1
MAX_BULLETS   = 30

SYSTEM_PROMPT = """You are a real-time call assistant. Your ONLY job is to help the user respond to their caller.

STRICT RULES — violating any rule is a critical failure:
- Output ONLY bullet points. Never prose, never paragraphs.
- Each bullet: max 10 words. Start with •
- Maximum 3 bullets per response.
- NEVER start with "I", "Here", "Let me", "Sure", "Analyzing", "Based on", "Great", or any meta-commentary.
- NEVER describe what you are doing. Just give the bullets.
- If nothing useful to say: output nothing at all.
- Respond only to the CALLER's questions/statements, not the user's.

Example good output:
• Germany invaded Poland September 1939
• Britain and France declared war days later
• Economic depression and Treaty of Versailles were key causes

Example bad output (NEVER do this):
Here's what happened with World War 2:
The war started when Germany invaded Poland..."""

# ── Persist config ────────────────────────────────────────────────────────────
def load_config():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            pass
    return {}

def save_config(data: dict):
    existing = load_config()
    existing.update(data)
    CONFIG_PATH.write_text(json.dumps(existing, indent=2))

# ── Audio devices ─────────────────────────────────────────────────────────────
def list_audio_devices():
    pa = pyaudio.PyAudio()
    devices = []
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info["maxInputChannels"] > 0:
            devices.append((i, info["name"]))
    pa.terminate()
    return devices

# ── Screenshare detection ─────────────────────────────────────────────────────
def is_screensharing():
    checks = [
        ["pgrep", "-x", "screencaptureuiagent"],
        ["pgrep", "-f", "zoom.us"],
        ["pgrep", "-f", "Google Meet"],
        ["pgrep", "-f", "Microsoft Teams"],
    ]
    for cmd in checks:
        try:
            result = subprocess.run(cmd, capture_output=True)
            if result.returncode == 0:
                return True
        except Exception:
            pass
    return False

# ── Gemini WebSocket client ───────────────────────────────────────────────────
class GeminiClient:
    def __init__(self, api_key: str, context: str, on_bullet, on_status):
        self.api_key   = api_key
        self.context   = context
        self.on_bullet = on_bullet
        self.on_status = on_status
        self.ws        = None
        self.running   = False
        self.loop      = None
        self._thread   = None

    def start(self):
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._close(), self.loop)

    def _run_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._connect())

    async def _close(self):
        if self.ws:
            await self.ws.close()

    async def _connect(self):
        url = f"{WS_URL}?key={self.api_key}"
        try:
            self.on_status("Connecting…")
            async with websockets.connect(url, ping_interval=30) as ws:
                self.ws = ws
                # Send setup
                setup_msg = {
                    "setup": {
                        "model": GEMINI_MODEL,
                        "generation_config": {
                            "response_modalities": ["AUDIO"],
                            "speech_config": {
                                "voice_config": {
                                    "prebuilt_voice_config": {
                                        "voice_name": "Aoede"
                                    }
                                }
                            }
                        },
                        "system_instruction": {
                            "parts": [{"text": SYSTEM_PROMPT}]
                        },
                        "tools": []
                    }
                }
                if self.context.strip():
                    setup_msg["setup"]["system_instruction"]["parts"].append(
                        {"text": f"\nCall context provided by user:\n{self.context}"}
                    )
                await ws.send(json.dumps(setup_msg))
                self.on_status("Connected")
                self.running = True
                await self._receive_loop()
        except Exception as e:
            self.on_status(f"Error: {e}")
            self.running = False

    async def _receive_loop(self):
        try:
            async for raw in self.ws:
                if not self.running:
                    break
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue

                # Extract text parts from modelTurn
                parts = (
                    msg.get("serverContent", {})
                       .get("modelTurn", {})
                       .get("parts", [])
                )
                for part in parts:
                    text = part.get("text", "")
                    if text:
                        self._process_text(text)
        except Exception as e:
            if self.running:
                self.on_status(f"Disconnected: {e}")
            self.running = False

    def _process_text(self, text: str):
        BAD_STARTS = (
            "i ", "here", "let me", "sure", "analyzing",
            "based on", "great", "certainly", "of course",
            "looking at", "processing",
        )
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            # Filter meta-commentary
            if line.lower().startswith(BAD_STARTS):
                continue
            if len(line) > 120:
                continue
            # Normalize bullet prefix
            for prefix in ("-", "*", "–", "•", "·"):
                if line.startswith(prefix):
                    line = line[len(prefix):].strip()
                    break
            if line:
                self.on_bullet(f"• {line}")

    def send_audio(self, pcm_bytes: bytes):
        if not self.running or not self.ws or not self.loop:
            return
        b64 = base64.b64encode(pcm_bytes).decode()
        msg = {
            "realtimeInput": {
                "mediaChunks": [{
                    "mimeType": "audio/pcm;rate=16000",
                    "data": b64
                }]
            }
        }
        asyncio.run_coroutine_threadsafe(
            self.ws.send(json.dumps(msg)), self.loop
        )


# ── Menu bar app (rumps) ──────────────────────────────────────────────────────
class MenuBarApp(rumps.App):
    def __init__(self, overlay):
        super().__init__("🎤", quit_button=None)
        self.overlay = overlay
        self.menu = [
            rumps.MenuItem("Show / Hide", callback=self.toggle_overlay),
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]

    def toggle_overlay(self, _):
        self.overlay.toggle_visibility()

    def quit_app(self, _):
        self.overlay.root.quit()
        rumps.quit_application()

    def set_active(self, active: bool):
        self.title = "🔴" if active else "🎤"


# ── Floating overlay (tkinter) ────────────────────────────────────────────────
class Overlay:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Call Copilot")
        self.root.configure(bg="#1a1a2e")
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.95)
        self.root.overrideredirect(False)  # keep title bar for drag
        self.root.geometry("460x660+40+40")
        self.root.resizable(True, True)

        self.cfg         = load_config()
        self.mic_active  = False
        self.session_on  = False
        self.gemini      = None
        self.pa          = None
        self.audio_stream = None
        self.bullet_count = 0
        self.menu_app    = None
        self._topmost_job = None

        self._build_ui()
        self._enforce_topmost()

        # Check screenshare periodically
        self._check_screenshare()

    # ── UI construction ───────────────────────────────────────────────────────
    def _build_ui(self):
        root = self.root

        # ── Setup frame ──
        self.setup_frame = tk.Frame(root, bg="#1a1a2e")
        self.setup_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=12)

        tk.Label(self.setup_frame, text="📞 Call Copilot",
                 bg="#1a1a2e", fg="#e0e0ff",
                 font=("SF Pro Display", 20, "bold")).pack(pady=(8, 16))

        # API key
        tk.Label(self.setup_frame, text="Gemini API Key",
                 bg="#1a1a2e", fg="#9090b0",
                 font=("SF Pro Text", 12)).pack(anchor="w")
        self.api_var = tk.StringVar(value=self.cfg.get("api_key", ""))
        self.api_entry = tk.Entry(self.setup_frame, textvariable=self.api_var,
                                  show="•", bg="#2a2a4e", fg="#e0e0ff",
                                  insertbackground="#e0e0ff",
                                  font=("SF Pro Text", 13),
                                  relief=tk.FLAT, bd=6)
        self.api_entry.pack(fill=tk.X, pady=(2, 12))

        # Audio device
        tk.Label(self.setup_frame, text="Input Device",
                 bg="#1a1a2e", fg="#9090b0",
                 font=("SF Pro Text", 12)).pack(anchor="w")
        self.devices     = list_audio_devices()
        device_names     = [f"{i}: {n}" for i, n in self.devices]
        self.device_var  = tk.StringVar()
        saved_dev        = self.cfg.get("device_index", 0)
        default_name     = next((f"{i}: {n}" for i, n in self.devices if i == saved_dev), "")
        self.device_var.set(default_name or (device_names[0] if device_names else ""))
        self.device_menu = tk.OptionMenu(self.setup_frame, self.device_var, *device_names)
        self.device_menu.configure(bg="#2a2a4e", fg="#e0e0ff",
                                   activebackground="#3a3a6e",
                                   font=("SF Pro Text", 13), relief=tk.FLAT)
        self.device_menu["menu"].configure(bg="#2a2a4e", fg="#e0e0ff")
        self.device_menu.pack(fill=tk.X, pady=(2, 12))

        # Context
        tk.Label(self.setup_frame, text="Call Context (optional)",
                 bg="#1a1a2e", fg="#9090b0",
                 font=("SF Pro Text", 12)).pack(anchor="w")
        self.context_text = tk.Text(self.setup_frame, height=6,
                                    bg="#2a2a4e", fg="#e0e0ff",
                                    insertbackground="#e0e0ff",
                                    font=("SF Pro Text", 13),
                                    relief=tk.FLAT, bd=6, wrap=tk.WORD)
        saved_ctx = self.cfg.get("context", "")
        if saved_ctx:
            self.context_text.insert("1.0", saved_ctx)
        self.context_text.pack(fill=tk.BOTH, expand=True, pady=(2, 16))

        # Start button
        self.start_btn = tk.Button(self.setup_frame, text="Start Session",
                                   command=self._start_session,
                                   bg="#4f46e5", fg="white",
                                   font=("SF Pro Text", 14, "bold"),
                                   relief=tk.FLAT, bd=0,
                                   padx=12, pady=10, cursor="hand2")
        self.start_btn.pack(fill=tk.X)

        # ── Session frame ──
        self.session_frame = tk.Frame(root, bg="#1a1a2e")

        # Header row
        hdr = tk.Frame(self.session_frame, bg="#1a1a2e")
        hdr.pack(fill=tk.X, padx=12, pady=(10, 4))

        self.status_lbl = tk.Label(hdr, text="⚪ Connecting…",
                                   bg="#1a1a2e", fg="#9090b0",
                                   font=("SF Pro Text", 11))
        self.status_lbl.pack(side=tk.LEFT)

        end_btn = tk.Button(hdr, text="End", command=self._end_session,
                            bg="#3a0a0a", fg="#ff6060",
                            font=("SF Pro Text", 11), relief=tk.FLAT,
                            padx=8, pady=3, cursor="hand2")
        end_btn.pack(side=tk.RIGHT)

        # Bullet canvas with scrollbar
        canvas_frame = tk.Frame(self.session_frame, bg="#1a1a2e")
        canvas_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)

        self.canvas    = tk.Canvas(canvas_frame, bg="#1a1a2e",
                                   highlightthickness=0)
        self.scrollbar = tk.Scrollbar(canvas_frame, orient=tk.VERTICAL,
                                      command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.bullet_frame = tk.Frame(self.canvas, bg="#1a1a2e")
        self.canvas_window = self.canvas.create_window(
            (0, 0), window=self.bullet_frame, anchor="nw"
        )
        self.bullet_frame.bind("<Configure>", self._on_frame_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        # Mic button
        mic_row = tk.Frame(self.session_frame, bg="#1a1a2e")
        mic_row.pack(fill=tk.X, padx=12, pady=(4, 14))

        self.mic_btn = tk.Button(mic_row, text="🎤  Start Listening",
                                 command=self._toggle_mic,
                                 bg="#1e3a5f", fg="#60b0ff",
                                 font=("SF Pro Text", 14, "bold"),
                                 relief=tk.FLAT, bd=0,
                                 padx=12, pady=12, cursor="hand2")
        self.mic_btn.pack(fill=tk.X)

        # Queue for thread-safe UI updates
        self._ui_queue = queue.Queue()
        self.root.after(100, self._drain_queue)

    # ── Topmost enforcement ───────────────────────────────────────────────────
    def _enforce_topmost(self):
        try:
            self.root.attributes("-topmost", True)
            self.root.lift()
        except Exception:
            pass
        self._topmost_job = self.root.after(3000, self._enforce_topmost)

    # ── Screenshare hide/show ─────────────────────────────────────────────────
    def _check_screenshare(self):
        if is_screensharing():
            self.root.withdraw()
        else:
            if not self.root.winfo_viewable():
                self.root.deiconify()
        self.root.after(5000, self._check_screenshare)

    # ── Canvas resize helpers ─────────────────────────────────────────────────
    def _on_frame_configure(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfig(self.canvas_window, width=event.width)

    # ── Session control ───────────────────────────────────────────────────────
    def _start_session(self):
        api_key = self.api_var.get().strip()
        if not api_key:
            self.api_entry.configure(bg="#5a1a1a")
            return

        context = self.context_text.get("1.0", tk.END).strip()
        dev_str = self.device_var.get()
        dev_idx = int(dev_str.split(":")[0]) if dev_str else 0

        save_config({"api_key": api_key, "device_index": dev_idx, "context": context})

        self.setup_frame.pack_forget()
        self.session_frame.pack(fill=tk.BOTH, expand=True)

        self.gemini = GeminiClient(
            api_key=api_key,
            context=context,
            on_bullet=lambda b: self._ui_queue.put(("bullet", b)),
            on_status=lambda s: self._ui_queue.put(("status", s)),
        )
        self.gemini.start()
        self.session_on = True

    def _end_session(self):
        if self.gemini:
            self.gemini.stop()
            self.gemini = None
        self._stop_mic()
        self.session_on = False

        # Clear bullets
        for w in self.bullet_frame.winfo_children():
            w.destroy()
        self.bullet_count = 0

        self.session_frame.pack_forget()
        self.setup_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=12)

        if self.menu_app:
            self.menu_app.set_active(False)

    # ── Mic control ───────────────────────────────────────────────────────────
    def _toggle_mic(self):
        if self.mic_active:
            self._stop_mic()
        else:
            self._start_mic()

    def _start_mic(self):
        dev_str = self.device_var.get()
        dev_idx = int(dev_str.split(":")[0]) if dev_str else None

        self.pa = pyaudio.PyAudio()
        try:
            self.audio_stream = self.pa.open(
                format=AUDIO_FORMAT,
                channels=AUDIO_CHANNELS,
                rate=AUDIO_RATE,
                input=True,
                input_device_index=dev_idx,
                frames_per_buffer=AUDIO_CHUNK,
                stream_callback=self._audio_callback,
            )
            self.audio_stream.start_stream()
            self.mic_active = True
            self.mic_btn.configure(text="🔴  Stop Listening",
                                   bg="#5a1a1a", fg="#ff6060")
            if self.menu_app:
                self.menu_app.set_active(True)
        except Exception as e:
            self._ui_queue.put(("status", f"Mic error: {e}"))

    def _stop_mic(self):
        self.mic_active = False
        if self.audio_stream:
            try:
                self.audio_stream.stop_stream()
                self.audio_stream.close()
            except Exception:
                pass
            self.audio_stream = None
        if self.pa:
            try:
                self.pa.terminate()
            except Exception:
                pass
            self.pa = None
        try:
            self.mic_btn.configure(text="🎤  Start Listening",
                                   bg="#1e3a5f", fg="#60b0ff")
        except Exception:
            pass
        if self.menu_app:
            self.menu_app.set_active(False)

    def _audio_callback(self, in_data, frame_count, time_info, status):
        if self.gemini and self.mic_active:
            self.gemini.send_audio(in_data)
        return (None, pyaudio.paContinue)

    # ── UI queue drain ────────────────────────────────────────────────────────
    def _drain_queue(self):
        try:
            while True:
                kind, val = self._ui_queue.get_nowait()
                if kind == "bullet":
                    self._append_bullet(val)
                elif kind == "status":
                    self._set_status(val)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)

    def _append_bullet(self, text: str):
        # Prune old bullets
        children = self.bullet_frame.winfo_children()
        if len(children) >= MAX_BULLETS:
            children[0].destroy()

        row = tk.Frame(self.bullet_frame, bg="#1a1a2e")
        row.pack(fill=tk.X, pady=3, padx=4)

        dot = tk.Label(row, text="•", bg="#1a1a2e", fg="#4f46e5",
                       font=("SF Pro Text", 20, "bold"))
        dot.pack(side=tk.LEFT, anchor="n", padx=(0, 6))

        # Strip leading bullet char if present
        display = text.lstrip("•").strip()
        lbl = tk.Label(row, text=display, bg="#1a1a2e", fg="#e0e0ff",
                       font=("SF Pro Text", 16),
                       wraplength=360, justify=tk.LEFT, anchor="w")
        lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.root.update_idletasks()
        self.canvas.yview_moveto(1.0)

    def _set_status(self, text: str):
        icons = {"Connected": "🟢", "Connecting": "⚪", "Error": "🔴", "Disconnected": "🔴"}
        icon  = next((v for k, v in icons.items() if k in text), "⚪")
        try:
            self.status_lbl.configure(text=f"{icon} {text}")
        except Exception:
            pass

    # ── Visibility ────────────────────────────────────────────────────────────
    def toggle_visibility(self):
        if self.root.winfo_viewable():
            self.root.withdraw()
        else:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)

    # ── Run ───────────────────────────────────────────────────────────────────
    def run(self):
        if HAS_RUMPS:
            self.menu_app = MenuBarApp(self)
            t = threading.Thread(target=self.menu_app.run, daemon=True)
            t.start()
        self.root.mainloop()


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = Overlay()
    app.run()
