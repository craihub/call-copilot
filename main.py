#!/usr/bin/env python3
"""
Real-Time Call Copilot
Single floating window: API key + context + device + start/stop + scrollable bullets.
System tray icon. Auto-hides when screensharing. macOS 12+ compatible.
Built with PyQt6 — no tkinter, no rumps.
"""

import asyncio
import base64
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

import logging
import pyaudio
import websockets

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_FILE = Path.home() / ".call-copilot" / "copilot.log"
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, mode="a"),
        logging.StreamHandler(sys.stderr),
    ],
)
log = logging.getLogger("copilot")
from PyQt6.QtCore import Qt, QPoint, pyqtSignal, QObject, QTimer
from PyQt6.QtGui import QIcon, QFont, QColor, QPalette, QAction, QPixmap, QPainter
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QComboBox, QPushButton, QTextEdit, QFrame,
    QSystemTrayIcon, QMenu, QSizePolicy,
)

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
MODEL       = "gemini-live-2.5-flash-native-audio"
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
        log.info("Starting Gemini client thread")
        threading.Thread(target=self._run_loop, daemon=True).start()

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect())
        except Exception as e:
            log.exception("Event loop crashed")
            self.on_error(f"Connection failed: {e}")

    async def _connect(self):
        uri = WS_URI_TMPL.format(api_key=self.api_key)
        self.on_status("Connecting...")
        log.info("Connecting to %s", uri[:80] + "...")
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
                log.debug("Sending setup: %s", json.dumps(setup_msg)[:200])
                await ws.send(json.dumps(setup_msg))
                raw_resp = await ws.recv()
                resp = json.loads(raw_resp)
                log.debug("Setup response: %s", json.dumps(resp)[:500])
                if "error" in resp:
                    err_msg = str(resp["error"])
                    log.error("Setup error from Gemini: %s", err_msg)
                    self.on_error(err_msg)
                    return
                self.on_status("[LIVE] Listening...")
                log.info("Connected and listening")
                await asyncio.gather(self._send_loop(ws), self._recv_loop(ws))
        except websockets.exceptions.ConnectionClosedOK:
            log.info("WebSocket closed normally")
            self.on_status("Disconnected")
        except websockets.exceptions.ConnectionClosedError as e:
            log.error("WebSocket closed with error: %s", e)
            self.on_error(f"Connection lost: {e}")
        except Exception as e:
            log.exception("Connection error")
            self.on_error(f"Error: {e}")
        finally:
            self.running = False
            log.info("Gemini client stopped")

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
            # Validate device exists in THIS PyAudio instance to avoid stale index segfault
            dev_idx = self.device_index
            if dev_idx is not None:
                try:
                    info = pa.get_device_info_by_index(dev_idx)
                    log.info("Audio device %d: %s (channels=%d)", dev_idx, info.get("name"), info.get("maxInputChannels", 0))
                    if info.get("maxInputChannels", 0) < 1:
                        self.client.on_error(f"Device {dev_idx} has no input channels")
                        return
                except Exception as e:
                    log.error("Invalid audio device %d: %s", dev_idx, e)
                    self.client.on_error(f"Invalid audio device {dev_idx}: {e}")
                    return
            else:
                # Fall back to default input device
                try:
                    default = pa.get_default_input_device_info()
                    dev_idx = default["index"]
                    log.info("Using default input device %d: %s", dev_idx, default.get("name"))
                except Exception as e:
                    log.error("No default input device: %s", e)
                    self.client.on_error(f"No default input device: {e}")
                    return

            log.info("Opening audio stream: rate=%d, channels=%d, chunk=%d, device=%d", AUDIO_RATE, AUDIO_CHANNELS, CHUNK_SIZE, dev_idx)
            stream = pa.open(
                format=AUDIO_FORMAT,
                channels=AUDIO_CHANNELS,
                rate=AUDIO_RATE,
                input=True,
                input_device_index=dev_idx,
                frames_per_buffer=CHUNK_SIZE,
            )
            log.info("Audio stream opened successfully")
            while not self._stop_event.is_set():
                try:
                    data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
                    self.client.push_audio(data)
                except OSError as e:
                    log.error("Audio read error: %s", e)
                    break
            log.info("Audio capture loop ended")
            stream.stop_stream()
            stream.close()
        except Exception as e:
            log.exception("Audio capture error")
            self.client.on_error(f"Audio error: {e}")
        finally:
            pa.terminate()

    def stop(self):
        self._stop_event.set()


# ── Signal bridge (thread-safe Qt updates) ─────────────────────────────────────
class Signals(QObject):
    text_received  = pyqtSignal(str)
    status_changed = pyqtSignal(str)
    error_occurred = pyqtSignal(str)


# ── Main Window ────────────────────────────────────────────────────────────────
class CopilotWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._cfg = load_config()
        self._client: Optional[GeminiLiveClient] = None
        self._audio: Optional[AudioCapture] = None
        self._active = False
        self._drag_pos: Optional[QPoint] = None
        self._key_visible = False

        self._signals = Signals()
        self._signals.text_received.connect(self._append_bullet)
        self._signals.status_changed.connect(self._set_status)
        self._signals.error_occurred.connect(self._show_error)

        self._setup_ui()
        self._load_devices()

        # Screenshare check timer
        self._ss_timer = QTimer(self)
        self._ss_timer.timeout.connect(self._check_screenshare)
        self._ss_timer.start(3000)

    def _setup_ui(self):
        self.setWindowTitle("Call Copilot")
        self.setFixedSize(420, 560)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)

        x = int(self._cfg.get("win_x", 60))
        y = int(self._cfg.get("win_y", 60))
        self.move(x, y)

        central = QWidget()
        central.setStyleSheet(f"background-color: {BG};")
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Title bar ──────────────────────────────────────────────────────────
        bar = QFrame()
        bar.setFixedHeight(32)
        bar.setStyleSheet(f"background-color: {BAR_BG};")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(10, 0, 8, 0)

        title = QLabel("Call Copilot")
        title.setStyleSheet(f"color: {ACCENT}; font-size: 13px; font-weight: bold;")
        bar_layout.addWidget(title)
        bar_layout.addStretch()

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(24, 24)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                color: {MUTED}; background: transparent; border: none; font-size: 14px;
            }}
            QPushButton:hover {{ color: #ff6060; }}
        """)
        close_btn.clicked.connect(self.hide)
        bar_layout.addWidget(close_btn)
        layout.addWidget(bar)

        # ── Config section ─────────────────────────────────────────────────────
        cfg_widget = QWidget()
        cfg_widget.setStyleSheet(f"background-color: {BG};")
        cfg_layout = QVBoxLayout(cfg_widget)
        cfg_layout.setContentsMargins(12, 10, 12, 0)
        cfg_layout.setSpacing(2)

        entry_style = f"""
            QLineEdit {{
                background-color: {ENTRY_BG}; color: {ENTRY_FG}; border: 1px solid {BORDER};
                border-radius: 4px; padding: 5px 8px; font-size: 12px;
            }}
            QLineEdit:focus {{ border-color: {ACCENT}; }}
        """
        label_style = f"color: {MUTED}; font-size: 10px; font-weight: bold;"

        # API Key
        cfg_layout.addWidget(self._make_label("GEMINI API KEY", label_style))
        key_row = QHBoxLayout()
        self._key_entry = QLineEdit(self._cfg.get("api_key", ""))
        self._key_entry.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_entry.setPlaceholderText("Paste API key or set GEMINI_API_KEY env")
        self._key_entry.setStyleSheet(entry_style)
        key_row.addWidget(self._key_entry)

        eye_btn = QPushButton("*")
        eye_btn.setFixedSize(28, 28)
        eye_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        eye_btn.setStyleSheet(f"QPushButton {{ background: transparent; border: none; font-size: 14px; }}")
        eye_btn.clicked.connect(self._toggle_key_visibility)
        key_row.addWidget(eye_btn)
        cfg_layout.addLayout(key_row)

        # Context
        cfg_layout.addSpacing(4)
        cfg_layout.addWidget(self._make_label("CALL CONTEXT", label_style))
        self._context_entry = QLineEdit(self._cfg.get("context", ""))
        self._context_entry.setPlaceholderText("e.g. Sales call with Acme Corp about Q3 renewal")
        self._context_entry.setStyleSheet(entry_style)
        cfg_layout.addWidget(self._context_entry)

        # Audio device
        cfg_layout.addSpacing(4)
        cfg_layout.addWidget(self._make_label("AUDIO INPUT DEVICE", label_style))
        self._device_combo = QComboBox()
        self._device_combo.setStyleSheet(f"""
            QComboBox {{
                background-color: {ENTRY_BG}; color: {ENTRY_FG}; border: 1px solid {BORDER};
                border-radius: 4px; padding: 5px 8px; font-size: 12px;
            }}
            QComboBox:focus {{ border-color: {ACCENT}; }}
            QComboBox::drop-down {{ border: none; }}
            QComboBox QAbstractItemView {{
                background-color: {ENTRY_BG}; color: {ENTRY_FG};
                selection-background-color: {ACCENT};
            }}
        """)
        cfg_layout.addWidget(self._device_combo)

        # Buttons row
        cfg_layout.addSpacing(8)
        btn_row = QHBoxLayout()

        self._start_btn = QPushButton("▶  Start Listening")
        self._start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._start_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {ACCENT}; color: white; border: none;
                border-radius: 6px; padding: 8px 16px; font-size: 12px; font-weight: bold;
            }}
            QPushButton:hover {{ background-color: #5a9aff; }}
        """)
        self._start_btn.clicked.connect(self._toggle_listening)
        btn_row.addWidget(self._start_btn)

        clear_btn = QPushButton("Clear")
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent; color: {MUTED}; border: 1px solid {BORDER};
                border-radius: 6px; padding: 8px 16px; font-size: 12px;
            }}
            QPushButton:hover {{ color: {TEXT}; border-color: {MUTED}; }}
        """)
        clear_btn.clicked.connect(self._clear_bullets)
        btn_row.addWidget(clear_btn)

        cfg_layout.addLayout(btn_row)
        layout.addWidget(cfg_widget)

        # ── Divider ────────────────────────────────────────────────────────────
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background-color: {BORDER};")
        layout.addWidget(divider)

        # ── Status bar ─────────────────────────────────────────────────────────
        self._status_label = QLabel("Ready")
        self._status_label.setStyleSheet(f"color: {MUTED}; font-size: 10px; padding: 4px 12px;")
        layout.addWidget(self._status_label)

        # ── Bullet area ────────────────────────────────────────────────────────
        self._bullet_area = QTextEdit()
        self._bullet_area.setReadOnly(True)
        self._bullet_area.setStyleSheet(f"""
            QTextEdit {{
                background-color: {BG}; color: {TEXT}; border: none;
                padding: 8px 12px; font-size: 13px; line-height: 1.5;
            }}
            QScrollBar:vertical {{
                background: {BG}; width: 6px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {BORDER}; border-radius: 3px; min-height: 20px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
        """)
        layout.addWidget(self._bullet_area, 1)

        self.setStyleSheet(f"""
            QMainWindow {{
                background-color: {BG};
                border: 1px solid {BORDER};
                border-radius: 12px;
            }}
        """)

    def _make_label(self, text: str, style: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(style)
        return lbl

    # ── Drag support ───────────────────────────────────────────────────────────
    def mousePressEvent(self, event):
        if event.position().y() < 32:
            self._drag_pos = event.globalPosition().toPoint() - self.pos()
        else:
            self._drag_pos = None

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        if self._drag_pos is not None:
            self._drag_pos = None
            self._cfg["win_x"] = str(self.x())
            self._cfg["win_y"] = str(self.y())
            save_config(self._cfg)

    # ── Key visibility toggle ──────────────────────────────────────────────────
    def _toggle_key_visibility(self):
        self._key_visible = not self._key_visible
        self._key_entry.setEchoMode(
            QLineEdit.EchoMode.Normal if self._key_visible else QLineEdit.EchoMode.Password
        )

    # ── Device list ────────────────────────────────────────────────────────────
    def _load_devices(self):
        self._devices = list_input_devices()
        self._device_combo.clear()
        saved_idx = self._cfg.get("device_index", "")
        select = 0
        for i, (idx, name) in enumerate(self._devices):
            self._device_combo.addItem(f"[{idx}] {name}", idx)
            if str(idx) == str(saved_idx):
                select = i
        if self._devices:
            self._device_combo.setCurrentIndex(select)

    # ── Start / Stop ───────────────────────────────────────────────────────────
    def _toggle_listening(self):
        if self._active:
            self._stop_listening()
        else:
            self._start_listening()

    def _start_listening(self):
        api_key = self._key_entry.text().strip() or os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            self._set_status("✗ No API key")
            return

        context = self._context_entry.text().strip()

        # Save config
        self._cfg["api_key"] = self._key_entry.text().strip()
        self._cfg["context"] = context
        if self._devices:
            self._cfg["device_index"] = str(self._device_combo.currentData())
        save_config(self._cfg)

        device_idx = self._device_combo.currentData() if self._devices else None

        self._client = GeminiLiveClient(
            api_key=api_key,
            context=context,
            on_text=lambda t: self._signals.text_received.emit(t),
            on_status=lambda s: self._signals.status_changed.emit(s),
            on_error=lambda e: self._signals.error_occurred.emit(e),
        )
        self._client.start()

        self._audio = AudioCapture(device_idx, self._client)
        self._audio.start()

        self._active = True
        self._start_btn.setText("⏹  Stop Listening")
        self._start_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {RED}; color: white; border: none;
                border-radius: 6px; padding: 8px 16px; font-size: 12px; font-weight: bold;
            }}
            QPushButton:hover {{ background-color: #e04848; }}
        """)

    def _stop_listening(self):
        if self._audio:
            self._audio.stop()
            self._audio = None
        if self._client:
            self._client.stop()
            self._client = None
        self._active = False
        self._set_status("Stopped")
        self._start_btn.setText("▶  Start Listening")
        self._start_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {ACCENT}; color: white; border: none;
                border-radius: 6px; padding: 8px 16px; font-size: 12px; font-weight: bold;
            }}
            QPushButton:hover {{ background-color: #5a9aff; }}
        """)

    # ── Signal handlers ────────────────────────────────────────────────────────
    def _append_bullet(self, text: str):
        for line in text.split("\n"):
            line = line.strip()
            if line:
                colored = line.replace("•", f'<span style="color:{ACCENT};">•</span>', 1)
                self._bullet_area.append(colored)
        sb = self._bullet_area.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _set_status(self, text: str):
        log.info("Status: %s", text)
        self._status_label.setText(text)

    def _show_error(self, text: str):
        log.error("Error shown: %s", text)
        self._set_status(f"✗ {text}")
        if self._active:
            self._stop_listening()

    def _clear_bullets(self):
        self._bullet_area.clear()

    # ── Screenshare check ──────────────────────────────────────────────────────
    def _check_screenshare(self):
        if is_screensharing() and self.isVisible():
            self.hide()

    def closeEvent(self, event):
        event.ignore()
        self.hide()


# ── System Tray ────────────────────────────────────────────────────────────────
def create_tray_icon(app: QApplication, window: CopilotWindow) -> QSystemTrayIcon:
    # Create a simple microphone icon
    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(ACCENT))
    # Mic body
    painter.drawRoundedRect(22, 8, 20, 32, 10, 10)
    # Mic stand
    painter.setBrush(QColor(ACCENT))
    painter.drawRect(29, 40, 6, 12)
    painter.drawRect(20, 52, 24, 4)
    painter.end()

    icon = QIcon(pixmap)
    tray = QSystemTrayIcon(icon, app)

    menu = QMenu()
    show_action = QAction("Show / Hide", menu)
    show_action.triggered.connect(lambda: window.show() if window.isHidden() else window.hide())
    menu.addAction(show_action)

    quit_action = QAction("Quit Call Copilot", menu)
    quit_action.triggered.connect(app.quit)
    menu.addAction(quit_action)

    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: (
        window.show() if window.isHidden() else window.hide()
    ) if reason == QSystemTrayIcon.ActivationReason.Trigger else None)

    tray.show()
    return tray


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Call Copilot")
    app.setQuitOnLastWindowClosed(False)

    window = CopilotWindow()
    tray = create_tray_icon(app, window)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
