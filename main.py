#!/usr/bin/env python3
"""
Call Copilot — Real-time call assistant
PyQt6 floating overlay. Gemini Live API via WebSocket.
No tkinter. No rumps. Ships its own Qt binaries via pip.
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
from pathlib import Path
from typing import Optional

import pyaudio
import websockets
from PyQt6.QtCore import (
    Qt, QTimer, QPoint, pyqtSignal, QObject, QSize
)
from PyQt6.QtGui import (
    QFont, QColor, QIcon, QPixmap, QPainter, QAction
)
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QLineEdit, QTextEdit, QComboBox, QFrame,
    QScrollArea, QSystemTrayIcon, QMenu, QSizePolicy
)

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

CRITICAL RULES — violating ANY is a critical failure:

1. OUTPUT FORMAT: Only bullet points. Every line starts with •
2. BULLET LENGTH: Max 12 words per bullet. No exceptions.
3. BULLET COUNT: 1-4 bullets per response. Never more.
4. NO META-COMMENTARY: Never say "analyzing", "processing", "let me think",
   "here are", "I can help". Give the ANSWER directly.
5. NO INTROS: No "Here's what I found" or "Based on the context". Just bullets.
6. NO FILLER: No "Great question!" or "That's interesting". Just answer.
7. SILENCE: If nobody is asking a question, output absolutely nothing.
8. ONLY RESPOND TO OTHER SPEAKERS: The user is wearing an earpiece. You hear
   TWO voices — the user and the other caller. Only respond when the OTHER
   person (not the user) asks a question or says something needing a response.
   When the user speaks, stay silent — they don't need help with their own words.

GOOD examples:
  • Germany invaded Poland September 1, 1939
  • Treaty of Versailles created economic resentment
  • Britain and France declared war September 3

BAD examples (NEVER do this):
  • Analyzing the start of WWII
  • Let me break that down for you
  • That's a great question about history
  • Here are some key points

{context_block}"""

# ── Colors ────────────────────────────────────────────────────────────────────
BG       = "#0b0d1a"
BAR_BG   = "#13152a"
ACCENT   = "#4a8fff"
TEXT_CLR = "#d0d8ff"
MUTED    = "#5a6080"
RED      = "#d04040"
GREEN    = "#38c060"
ENTRY_BG = "#181c34"
ENTRY_FG = "#b0bce0"
BORDER   = "#2a2e50"
BODY_FG  = "#e8ecff"
CARD_BG  = "#151830"


# ── Config helpers ────────────────────────────────────────────────────────────
def load_config() -> dict:
    defaults = {"api_key": "", "device_index": "", "context": "",
                "win_x": "60", "win_y": "60"}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
            defaults.update(data)
        except Exception:
            pass
    return defaults


def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


# ── Audio device list ─────────────────────────────────────────────────────────
def list_input_devices() -> list:
    pa = pyaudio.PyAudio()
    devices = []
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info["maxInputChannels"] > 0:
            devices.append((i, info["name"]))
    pa.terminate()
    return devices


# ── Screenshare detection ─────────────────────────────────────────────────────
def is_screensharing() -> bool:
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
    # Check for Zoom/Teams/Meet screen sharing
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


# ── Qt Signal Bridge ──────────────────────────────────────────────────────────
class SignalBridge(QObject):
    """Thread-safe bridge: background threads emit signals, Qt main thread receives."""
    text_received = pyqtSignal(str)
    status_changed = pyqtSignal(str)
    error_occurred = pyqtSignal(str)


# ── Gemini Live WebSocket client ──────────────────────────────────────────────
class GeminiLiveClient:
    def __init__(self, api_key: str, context: str, bridge: SignalBridge):
        self.api_key = api_key
        self.context = context
        self.bridge  = bridge
        self.audio_queue: queue.Queue = queue.Queue(maxsize=150)
        self.running = False
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
                    "response_modalities": ["TEXT"],
                    "temperature": 0.2,
                },
                "system_instruction": {"parts": [{"text": instruction}]},
                "realtime_input_config": {
                    "automatic_activity_detection": {
                        "disabled": False,
                        "start_of_speech_sensitivity": "START_SENSITIVITY_LOW",
                        "end_of_speech_sensitivity": "END_SENSITIVITY_HIGH",
                        "prefix_padding_ms": 20,
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
        self.bridge.status_changed.emit("Connecting…")
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
                    self.bridge.error_occurred.emit(str(resp["error"]))
                    return
                self.bridge.status_changed.emit("🟢 Connected — listening")
                log("[WS] Connected successfully")
                await asyncio.gather(self._send_loop(ws), self._recv_loop(ws))
        except websockets.exceptions.ConnectionClosedOK:
            self.bridge.status_changed.emit("Disconnected")
            log("[WS] Connection closed OK")
        except websockets.exceptions.ConnectionClosedError as e:
            self.bridge.error_occurred.emit(f"Connection closed: {e.code} {e.reason}")
            log(f"[WS] Connection closed error: {e.code} {e.reason}")
        except Exception as e:
            self.bridge.error_occurred.emit(f"Error: {e}")
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
                server_content = data.get("serverContent", {})
                parts = server_content.get("modelTurn", {}).get("parts", [])
                for part in parts:
                    text = part.get("text", "").strip()
                    if text:
                        log(f"[RECV] Text: {text[:100]}")
                        self.bridge.text_received.emit(text)
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


# ── Audio capture thread ──────────────────────────────────────────────────────
class AudioCapture(threading.Thread):
    def __init__(self, device_index: Optional[int], client: GeminiLiveClient):
        super().__init__(daemon=True)
        self.device_index = device_index
        self.client = client
        self._stop_event = threading.Event()

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


# ── Tray Icon ─────────────────────────────────────────────────────────────────
def make_circle_icon(color: str, size: int = 64) -> QIcon:
    """Create a solid circle icon for the system tray."""
    pixmap = QPixmap(QSize(size, size))
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(4, 4, size - 8, size - 8)
    painter.end()
    return QIcon(pixmap)


# ── Main Window ───────────────────────────────────────────────────────────────
class CopilotWindow(QWidget):
    def __init__(self):
        super().__init__()
        self._cfg = load_config()
        self._client: Optional[GeminiLiveClient] = None
        self._audio: Optional[AudioCapture] = None
        self._active = False
        self._drag_pos: Optional[QPoint] = None
        self._cfg_visible = True
        self._bullet_count = 0
        self._max_bullets = 30
        self._tray: Optional[QSystemTrayIcon] = None
        self._bridge = SignalBridge()

        # Connect signals
        self._bridge.text_received.connect(self._append_bullet)
        self._bridge.status_changed.connect(self._set_status)
        self._bridge.error_occurred.connect(self._on_error)

        self._init_ui()
        self._init_tray()
        self._init_timers()

    def _init_ui(self):
        self.setWindowTitle("Call Copilot")
        self.setFixedSize(440, 640)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool  # Hides from dock on macOS
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setStyleSheet(f"background-color: {BG};")

        # Restore position
        x = int(self._cfg.get("win_x", 60))
        y = int(self._cfg.get("win_y", 60))
        self.move(x, y)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Title bar ─────────────────────────────────────────────────────
        bar = QWidget()
        bar.setFixedHeight(36)
        bar.setStyleSheet(f"background-color: {BAR_BG};")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(12, 0, 8, 0)

        title = QLabel("🎤  Call Copilot")
        title.setFont(QFont("SF Pro Display", 13, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {ACCENT}; background: transparent;")
        bar_layout.addWidget(title)

        bar_layout.addStretch()

        min_btn = QPushButton("─")
        min_btn.setFixedSize(28, 28)
        min_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        min_btn.setStyleSheet(f"""
            QPushButton {{
                color: {MUTED}; background: transparent;
                border: none; font-size: 14px;
            }}
            QPushButton:hover {{ color: {ACCENT}; }}
        """)
        min_btn.clicked.connect(self._toggle_config)
        bar_layout.addWidget(min_btn)

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(28, 28)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                color: {MUTED}; background: transparent;
                border: none; font-size: 14px;
            }}
            QPushButton:hover {{ color: #ff6060; }}
        """)
        close_btn.clicked.connect(self.hide)
        bar_layout.addWidget(close_btn)

        layout.addWidget(bar)

        # ── Config section ────────────────────────────────────────────────
        self._cfg_widget = QWidget()
        self._cfg_widget.setStyleSheet(f"background-color: {BG};")
        cfg_layout = QVBoxLayout(self._cfg_widget)
        cfg_layout.setContentsMargins(14, 10, 14, 0)
        cfg_layout.setSpacing(4)

        # API Key
        cfg_layout.addWidget(self._make_label("Gemini API Key"))
        key_row = QHBoxLayout()
        key_row.setSpacing(6)
        self._key_entry = QLineEdit(self._cfg.get("api_key", ""))
        self._key_entry.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_entry.setFont(QFont("SF Pro Display", 12))
        self._key_entry.setStyleSheet(self._entry_style())
        self._key_entry.textChanged.connect(self._autosave)
        key_row.addWidget(self._key_entry)

        self._key_visible = False
        toggle_btn = QPushButton("Show")
        toggle_btn.setFixedWidth(50)
        toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        toggle_btn.setStyleSheet(f"""
            QPushButton {{
                color: {MUTED}; background: {ENTRY_BG};
                border: 1px solid {BORDER}; border-radius: 4px;
                font-size: 11px; padding: 4px;
            }}
            QPushButton:hover {{ color: {ACCENT}; }}
        """)
        self._toggle_btn = toggle_btn
        toggle_btn.clicked.connect(self._toggle_key_visibility)
        key_row.addWidget(toggle_btn)
        cfg_layout.addLayout(key_row)

        # Context
        cfg_layout.addWidget(self._make_label("Call Context (optional)"))
        self._ctx_text = QTextEdit()
        self._ctx_text.setPlainText(self._cfg.get("context", ""))
        self._ctx_text.setFont(QFont("SF Pro Display", 12))
        self._ctx_text.setFixedHeight(80)
        self._ctx_text.setStyleSheet(self._entry_style())
        self._ctx_text.textChanged.connect(self._autosave)
        cfg_layout.addWidget(self._ctx_text)

        # Device
        cfg_layout.addWidget(self._make_label("Audio Input Device"))
        self._device_combo = QComboBox()
        self._device_combo.setFont(QFont("SF Pro Display", 12))
        self._device_combo.setStyleSheet(f"""
            QComboBox {{
                background: {ENTRY_BG}; color: {ENTRY_FG};
                border: 1px solid {BORDER}; border-radius: 4px;
                padding: 6px; font-size: 12px;
            }}
            QComboBox::drop-down {{
                border: none; width: 24px;
            }}
            QComboBox QAbstractItemView {{
                background: {ENTRY_BG}; color: {ENTRY_FG};
                selection-background-color: {ACCENT};
                border: 1px solid {BORDER};
            }}
        """)
        self._devices = list_input_devices()
        for idx, name in self._devices:
            self._device_combo.addItem(f"[{idx}] {name}", idx)
        saved_idx = self._cfg.get("device_index", "")
        if saved_idx:
            try:
                si = int(saved_idx)
                for i, (didx, _) in enumerate(self._devices):
                    if didx == si:
                        self._device_combo.setCurrentIndex(i)
                        break
            except ValueError:
                pass
        self._device_combo.currentIndexChanged.connect(self._autosave)
        cfg_layout.addWidget(self._device_combo)

        # Start/Stop button
        self._start_btn = QPushButton("🎤  Start Listening")
        self._start_btn.setFont(QFont("SF Pro Display", 14, QFont.Weight.Bold))
        self._start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._start_btn.setFixedHeight(44)
        self._start_btn.setStyleSheet(f"""
            QPushButton {{
                background: {GREEN}; color: white;
                border: none; border-radius: 6px;
                font-size: 14px; font-weight: bold;
            }}
            QPushButton:hover {{ background: #2aa050; }}
        """)
        self._start_btn.clicked.connect(self._toggle_session)
        cfg_layout.addWidget(self._start_btn)

        layout.addWidget(self._cfg_widget)

        # ── Divider ───────────────────────────────────────────────────────
        self._divider = QFrame()
        self._divider.setFrameShape(QFrame.Shape.HLine)
        self._divider.setStyleSheet(f"color: {BORDER}; background: {BORDER};")
        self._divider.setFixedHeight(1)
        layout.addWidget(self._divider)

        # ── Suggestions header ────────────────────────────────────────────
        feed_header = QWidget()
        feed_header.setStyleSheet(f"background: {BG};")
        fh_layout = QHBoxLayout(feed_header)
        fh_layout.setContentsMargins(14, 6, 14, 2)

        feed_title = QLabel("💡 Suggestions")
        feed_title.setFont(QFont("SF Pro Display", 11, QFont.Weight.Bold))
        feed_title.setStyleSheet(f"color: {MUTED}; background: transparent;")
        fh_layout.addWidget(feed_title)
        fh_layout.addStretch()

        clear_btn = QPushButton("Clear")
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_btn.setStyleSheet(f"""
            QPushButton {{
                color: {MUTED}; background: transparent;
                border: none; font-size: 10px;
            }}
            QPushButton:hover {{ color: {ACCENT}; }}
        """)
        clear_btn.clicked.connect(self._clear_feed)
        fh_layout.addWidget(clear_btn)
        layout.addWidget(feed_header)

        # ── Bullet feed ───────────────────────────────────────────────────
        self._feed = QLabel()
        self._feed.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._feed.setWordWrap(True)
        self._feed.setFont(QFont("SF Pro Display", 16))
        self._feed.setStyleSheet(f"""
            color: {BODY_FG}; background: {BG};
            padding: 8px 14px;
        """)
        self._feed.setTextFormat(Qt.TextFormat.RichText)
        self._feed_html_parts = []

        scroll = QScrollArea()
        scroll.setWidget(self._feed)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"""
            QScrollArea {{
                background: {BG}; border: none;
            }}
            QScrollBar:vertical {{
                background: {BG}; width: 8px;
            }}
            QScrollBar::handle:vertical {{
                background: {BORDER}; border-radius: 4px; min-height: 20px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
        """)
        self._scroll = scroll
        layout.addWidget(scroll, 1)

        # ── Status bar ────────────────────────────────────────────────────
        self._status_label = QLabel("Ready")
        self._status_label.setFont(QFont("SF Pro Display", 10))
        self._status_label.setStyleSheet(f"""
            color: {MUTED}; background: {BAR_BG};
            padding: 4px 12px;
        """)
        layout.addWidget(self._status_label)

    def _init_tray(self):
        self._icon_idle = make_circle_icon(ACCENT)
        self._icon_active = make_circle_icon(RED)

        self._tray = QSystemTrayIcon(self._icon_idle, self)

        menu = QMenu()
        show_action = QAction("Show / Hide", self)
        show_action.triggered.connect(self._tray_toggle)
        menu.addAction(show_action)
        menu.addSeparator()
        quit_action = QAction("Quit Call Copilot", self)
        quit_action.triggered.connect(self._quit_app)
        menu.addAction(quit_action)

        # Style the menu
        menu.setStyleSheet(f"""
            QMenu {{
                background: {BAR_BG}; color: {TEXT_CLR};
                border: 1px solid {BORDER};
                padding: 4px;
            }}
            QMenu::item:selected {{
                background: {ACCENT}; color: white;
            }}
        """)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._tray_activated)
        self._tray.show()

    def _init_timers(self):
        # Enforce topmost every 3s
        self._topmost_timer = QTimer(self)
        self._topmost_timer.timeout.connect(self._enforce_topmost)
        self._topmost_timer.start(3000)

        # Check screenshare every 4s
        self._screenshare_timer = QTimer(self)
        self._screenshare_timer.timeout.connect(self._check_screenshare)
        self._screenshare_timer.start(4000)

    # ── Helpers ───────────────────────────────────────────────────────────
    def _make_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setFont(QFont("SF Pro Display", 10))
        lbl.setStyleSheet(f"color: {MUTED}; background: transparent;")
        return lbl

    def _entry_style(self) -> str:
        return f"""
            background: {ENTRY_BG}; color: {ENTRY_FG};
            border: 1px solid {BORDER}; border-radius: 4px;
            padding: 6px; font-size: 12px;
            selection-background-color: {ACCENT};
        """

    # ── Drag ──────────────────────────────────────────────────────────────
    def mousePressEvent(self, event):
        if event.position().y() < 36:  # Title bar area
            self._drag_pos = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        if self._drag_pos is not None:
            self._cfg["win_x"] = str(self.x())
            self._cfg["win_y"] = str(self.y())
            save_config(self._cfg)
            self._drag_pos = None

    # ── Tray ──────────────────────────────────────────────────────────────
    def _tray_toggle(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
            self.activateWindow()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._tray_toggle()

    def _set_tray_icon(self, active: bool):
        if self._tray:
            self._tray.setIcon(self._icon_active if active else self._icon_idle)

    def _quit_app(self):
        if self._active:
            self._stop_session()
        QApplication.quit()

    # ── Config toggle ─────────────────────────────────────────────────────
    def _toggle_config(self):
        self._cfg_visible = not self._cfg_visible
        self._cfg_widget.setVisible(self._cfg_visible)

    def _toggle_key_visibility(self):
        self._key_visible = not self._key_visible
        self._key_entry.setEchoMode(
            QLineEdit.EchoMode.Normal if self._key_visible
            else QLineEdit.EchoMode.Password
        )
        self._toggle_btn.setText("Hide" if self._key_visible else "Show")

    def _autosave(self):
        self._cfg["api_key"] = self._key_entry.text().strip()
        self._cfg["context"] = self._ctx_text.toPlainText()
        idx = self._device_combo.currentData()
        if idx is not None:
            self._cfg["device_index"] = str(idx)
        save_config(self._cfg)

    def _get_device_index(self) -> Optional[int]:
        return self._device_combo.currentData()

    # ── Topmost enforcement ───────────────────────────────────────────────
    def _enforce_topmost(self):
        if self.isVisible():
            self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
            self.show()

    # ── Session control ───────────────────────────────────────────────────
    def _toggle_session(self):
        if self._active:
            self._stop_session()
        else:
            self._start_session()

    def _start_session(self):
        api_key = self._key_entry.text().strip()
        if not api_key:
            self._set_status("⚠️  Enter API key first")
            return
        context = self._ctx_text.toPlainText()
        device_idx = self._get_device_index()

        self._client = GeminiLiveClient(
            api_key=api_key,
            context=context,
            bridge=self._bridge,
        )
        self._client.start()
        self._audio = AudioCapture(device_idx, self._client)
        self._audio.start()
        self._active = True
        self._start_btn.setText("⏹  Stop")
        self._start_btn.setStyleSheet(f"""
            QPushButton {{
                background: {RED}; color: white;
                border: none; border-radius: 6px;
                font-size: 14px; font-weight: bold;
            }}
            QPushButton:hover {{ background: #b03030; }}
        """)
        self._set_tray_icon(True)

        # Auto-collapse config
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
        self._audio = None
        self._active = False
        self._start_btn.setText("🎤  Start Listening")
        self._start_btn.setStyleSheet(f"""
            QPushButton {{
                background: {GREEN}; color: white;
                border: none; border-radius: 6px;
                font-size: 14px; font-weight: bold;
            }}
            QPushButton:hover {{ background: #2aa050; }}
        """)
        self._set_status("Stopped")
        self._set_tray_icon(False)

        # Re-expand config
        if not self._cfg_visible:
            self._toggle_config()

        log("[SESSION] Stopped")

    # ── Feed ──────────────────────────────────────────────────────────────
    def _append_bullet(self, text: str):
        lines = text.splitlines()
        new_bullets = []

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Strip bullet prefix variants
            clean = line
            for prefix in ("•", "- ", "* ", "– ", "· "):
                if clean.startswith(prefix):
                    clean = clean[len(prefix):].strip()
                    break

            # Skip meta-commentary
            skip_patterns = [
                "analyzing", "processing", "let me", "here's",
                "based on", "great question", "that's interesting",
                "i can help", "i understand", "certainly",
                "of course", "sure thing", "here are",
            ]
            if any(clean.lower().startswith(p) for p in skip_patterns):
                log(f"[FILTER] Skipped: {clean[:60]}")
                continue

            # Skip paragraph-length lines
            if len(clean) > 100:
                log(f"[FILTER] Too long ({len(clean)}ch): {clean[:60]}...")
                continue

            if clean:
                bullet_html = (
                    f'<div style="margin: 4px 0; font-size: 16px;">'
                    f'<span style="color: {ACCENT}; font-size: 20px; font-weight: bold;">  •  </span>'
                    f'<span style="color: {BODY_FG}; font-size: 16px;">{clean}</span>'
                    f'</div>'
                )
                new_bullets.append(bullet_html)
                self._bullet_count += 1

        if new_bullets:
            # Add separator between response groups
            self._feed_html_parts.append(
                '<div style="margin: 2px 0; border-bottom: 1px solid '
                f'{BORDER}; height: 1px;"></div>'
            )
            self._feed_html_parts.extend(new_bullets)

        # Trim old bullets
        while self._bullet_count > self._max_bullets and len(self._feed_html_parts) > 2:
            self._feed_html_parts.pop(0)
            self._bullet_count = max(self._bullet_count - 1, 0)

        self._feed.setText("".join(self._feed_html_parts))

        # Auto-scroll to bottom
        QTimer.singleShot(50, lambda: self._scroll.verticalScrollBar().setValue(
            self._scroll.verticalScrollBar().maximum()
        ))

    def _clear_feed(self):
        self._feed_html_parts.clear()
        self._feed.setText("")
        self._bullet_count = 0

    def _set_status(self, msg: str):
        self._status_label.setText(msg)

    def _on_error(self, msg: str):
        self._set_status(f"⚠️  {msg}")
        log(f"[ERROR] {msg}")

    # ── Screenshare auto-hide ─────────────────────────────────────────────
    def _check_screenshare(self):
        try:
            sharing = is_screensharing()
            if sharing and self.isVisible():
                self._was_visible_before_share = True
                self.hide()
            elif not sharing and hasattr(self, '_was_visible_before_share'):
                if self._was_visible_before_share:
                    self.show()
                    self.raise_()
                    self._was_visible_before_share = False
        except Exception:
            pass


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # Keep running in tray
    window = CopilotWindow()
    window.show()
    sys.exit(app.exec())
