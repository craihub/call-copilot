#!/usr/bin/env python3
"""
Real-Time Call Copilot
Setup screen → paste API key + context → press Start → mic toggle to listen.
Displays AI bullet-point suggestions in an always-on-top overlay.
"""

import asyncio
import base64
import json
import os
import queue
import sys
import threading
from typing import Optional

import pyaudio
import websockets
from PyQt6.QtCore import Qt, pyqtSignal, QObject, QSize
from PyQt6.QtGui import QFont, QColor
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QLineEdit,
    QStackedWidget,
    QFrame,
)

# ── Audio config ───────────────────────────────────────────────────────────────
AUDIO_RATE     = 16000
AUDIO_CHANNELS = 1
AUDIO_FORMAT   = pyaudio.paInt16
CHUNK_SIZE     = 512
AUDIO_MIME     = "audio/pcm;rate=16000"

# ── Gemini Live API ────────────────────────────────────────────────────────────
MODEL       = "gemini-2.5-flash-exp"
WS_URI_TMPL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta"
    ".GenerativeService.BidiGenerateContent?key={api_key}"
)

SYSTEM_PROMPT = (
    "You are a silent real-time call copilot. "
    "Listen to the conversation. Whenever you hear a question directed at the user, "
    "respond ONLY with 2-3 bullet points. "
    "Rules: each bullet max 12 words, start with •, one per line, no intro text, "
    "no filler, no explanations. If no question is asked, output nothing. "
    "Use the provided call context to tailor answers."
)

# ── Shared stylesheet ──────────────────────────────────────────────────────────
DARK_STYLE = """
    QWidget#root {
        background: rgba(10, 12, 24, 225);
        border: 1px solid rgba(90, 160, 255, 70);
        border-radius: 14px;
    }
    QLabel { color: #d0dcff; font-family: -apple-system, sans-serif; }
    QLineEdit, QTextEdit {
        background: rgba(20, 24, 48, 200);
        color: #c0d4ff;
        border: 1px solid rgba(80, 120, 220, 80);
        border-radius: 7px;
        padding: 6px 8px;
        font-size: 12px;
        selection-background-color: rgba(80,140,255,160);
    }
    QLineEdit:focus, QTextEdit:focus {
        border: 1px solid rgba(100, 160, 255, 160);
    }
    QPushButton.action {
        background: rgba(55, 115, 215, 190);
        color: white;
        border: none;
        border-radius: 7px;
        padding: 8px 18px;
        font-size: 13px;
        font-weight: 600;
    }
    QPushButton.action:hover { background: rgba(75, 140, 255, 210); }
    QPushButton.action:pressed { background: rgba(45, 95, 180, 210); }
    QPushButton#mic_btn {
        background: rgba(40, 180, 100, 200);
        color: white;
        border: none;
        border-radius: 28px;
        font-size: 22px;
        font-weight: bold;
    }
    QPushButton#mic_btn:hover { background: rgba(50, 210, 120, 220); }
    QPushButton#mic_btn[active=true] {
        background: rgba(210, 60, 60, 200);
    }
    QPushButton#mic_btn[active=true]:hover { background: rgba(240, 80, 80, 220); }
    QPushButton#end_btn {
        background: rgba(180, 50, 50, 180);
        color: white;
        border: none;
        border-radius: 7px;
        padding: 6px 14px;
        font-size: 12px;
    }
    QPushButton#end_btn:hover { background: rgba(215, 70, 70, 200); }
    QPushButton#close_btn {
        background: rgba(180, 50, 50, 140);
        color: white;
        border: none;
        border-radius: 11px;
        font-size: 11px;
    }
    QPushButton#close_btn:hover { background: rgba(220, 70, 70, 180); }
    QScrollArea { border: none; background: transparent; }
    QScrollBar:vertical {
        background: rgba(30,35,60,120);
        width: 5px;
        border-radius: 2px;
    }
    QScrollBar::handle:vertical {
        background: rgba(90,130,220,140);
        border-radius: 2px;
    }
"""


# ── Signals ────────────────────────────────────────────────────────────────────
class Signals(QObject):
    text_received   = pyqtSignal(str)
    status_changed  = pyqtSignal(str)
    error_occurred  = pyqtSignal(str)
    connected       = pyqtSignal()


# ── Gemini Live WebSocket client ───────────────────────────────────────────────
class GeminiLiveClient:
    def __init__(self, api_key: str, context: str, signals: Signals):
        self.api_key  = api_key
        self.context  = context
        self.signals  = signals
        self.ws       = None
        self.audio_queue: queue.Queue = queue.Queue(maxsize=200)
        self.running  = False
        self.mic_active = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _build_setup(self) -> dict:
        instruction = SYSTEM_PROMPT
        if self.context.strip():
            instruction += f"\n\nCall context provided by user:\n{self.context.strip()}"
        return {
            "setup": {
                "model": f"models/{MODEL}",
                "generation_config": {
                    "response_modalities": ["TEXT"],
                    "temperature": 0.15,
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

    async def connect_and_stream(self):
        uri = WS_URI_TMPL.format(api_key=self.api_key)
        self.signals.status_changed.emit("Connecting…")
        try:
            async with websockets.connect(
                uri,
                additional_headers={"Content-Type": "application/json"},
                max_size=10 * 1024 * 1024,
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                self.ws = ws
                self.running = True
                await ws.send(json.dumps(self._build_setup()))
                # wait for setup ack
                raw = await ws.recv()
                data = json.loads(raw)
                if "error" in data:
                    self.signals.error_occurred.emit(str(data["error"]))
                    return
                self.signals.connected.emit()
                self.signals.status_changed.emit("Ready — press mic to start")
                await asyncio.gather(
                    self._send_loop(ws),
                    self._recv_loop(ws),
                )
        except websockets.exceptions.ConnectionClosedOK:
            self.signals.status_changed.emit("Disconnected")
        except Exception as e:
            self.signals.error_occurred.emit(f"Connection error: {e}")
        finally:
            self.running = False
            self.ws = None

    async def _send_loop(self, ws):
        loop = asyncio.get_event_loop()
        while self.running:
            try:
                chunk = await loop.run_in_executor(
                    None, lambda: self.audio_queue.get(timeout=0.05)
                )
                if not self.mic_active:
                    continue
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
                sc = data.get("serverContent", {})
                parts = sc.get("modelTurn", {}).get("parts", [])
                for p in parts:
                    txt = p.get("text", "").strip()
                    if txt:
                        self.signals.text_received.emit(txt)
            except json.JSONDecodeError:
                pass

    def push_audio(self, chunk: bytes):
        if self.running and self.mic_active:
            try:
                self.audio_queue.put_nowait(chunk)
            except queue.Full:
                pass

    def stop(self):
        self.running = False
        if self.ws and self._loop:
            asyncio.run_coroutine_threadsafe(self.ws.close(), self._loop)


# ── Audio capture thread ───────────────────────────────────────────────────────
class AudioCapture(threading.Thread):
    def __init__(self, device_index: Optional[int], client: GeminiLiveClient):
        super().__init__(daemon=True)
        self.device_index = device_index
        self.client       = client
        self.running      = False
        self._pa          = None
        self._stream      = None

    def run(self):
        self._pa = pyaudio.PyAudio()
        self.running = True
        try:
            self._stream = self._pa.open(
                format=AUDIO_FORMAT,
                channels=AUDIO_CHANNELS,
                rate=AUDIO_RATE,
                input=True,
                input_device_index=self.device_index,
                frames_per_buffer=CHUNK_SIZE,
            )
            while self.running:
                try:
                    data = self._stream.read(CHUNK_SIZE, exception_on_overflow=False)
                    self.client.push_audio(data)
                except OSError:
                    break
        finally:
            if self._stream:
                self._stream.stop_stream()
                self._stream.close()
            if self._pa:
                self._pa.terminate()

    def stop(self):
        self.running = False


# ── Main window ────────────────────────────────────────────────────────────────
class OverlayWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.signals       = Signals()
        self.gemini        = None
        self.audio_capture = None
        self._ws_thread    = None
        self._mic_on       = False
        self._drag_pos     = None
        self._pa_enum      = pyaudio.PyAudio()

        self._setup_window()
        self._build_ui()
        self._connect_signals()

    # ── Window flags ───────────────────────────────────────────────────────────
    def _setup_window(self):
        self.setWindowTitle("Call Copilot")
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(420, 580)

    # ── Build UI ───────────────────────────────────────────────────────────────
    def _build_ui(self):
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.setup_page  = self._build_setup_page()
        self.session_page = self._build_session_page()

        self.stack.addWidget(self.setup_page)   # index 0
        self.stack.addWidget(self.session_page) # index 1
        self.stack.setCurrentIndex(0)

    # ── Setup page ─────────────────────────────────────────────────────────────
    def _build_setup_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("root")
        page.setStyleSheet(DARK_STYLE)

        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 14, 18, 18)
        layout.setSpacing(10)

        # Title bar
        layout.addLayout(self._title_bar("🎧  Call Copilot"))

        # Divider
        layout.addWidget(self._divider())

        # API Key
        key_lbl = QLabel("Gemini API Key")
        key_lbl.setStyleSheet("font-size: 11px; color: #8898cc;")
        layout.addWidget(key_lbl)

        self.api_input = QLineEdit()
        self.api_input.setPlaceholderText("Paste your API key here…")
        self.api_input.setEchoMode(QLineEdit.EchoMode.Password)
        env_key = os.environ.get("GEMINI_API_KEY", "")
        if env_key:
            self.api_input.setText(env_key)
        layout.addWidget(self.api_input)

        # Context
        ctx_lbl = QLabel("Call Context  (optional)")
        ctx_lbl.setStyleSheet("font-size: 11px; color: #8898cc; margin-top: 4px;")
        layout.addWidget(ctx_lbl)

        self.context_input = QTextEdit()
        self.context_input.setPlaceholderText(
            "Describe the call so the copilot can give better answers.\n\n"
            "Examples:\n"
            "• Interview for Senior Engineer role at Stripe\n"
            "• Sales call for SaaS product — prospect is a CTO\n"
            "• Client discovery call for web design project"
        )
        self.context_input.setFixedHeight(130)
        layout.addWidget(self.context_input)

        # Audio device
        dev_lbl = QLabel("Audio Input Device  (optional)")
        dev_lbl.setStyleSheet("font-size: 11px; color: #8898cc; margin-top: 4px;")
        layout.addWidget(dev_lbl)

        dev_row = QHBoxLayout()
        self.dev_input = QLineEdit()
        self.dev_input.setPlaceholderText("Device index — blank = default mic")
        dev_row.addWidget(self.dev_input)
        layout.addLayout(dev_row)

        # Device list
        hint = self._list_devices()
        hint_lbl = QLabel(hint)
        hint_lbl.setStyleSheet("font-size: 10px; color: #556; line-height: 150%;")
        hint_lbl.setWordWrap(True)
        layout.addWidget(hint_lbl)

        layout.addStretch()

        # Start button
        start_btn = QPushButton("Start Session")
        start_btn.setProperty("class", "action")
        start_btn.setStyleSheet("""
            QPushButton {
                background: rgba(55, 115, 215, 200);
                color: white; border: none; border-radius: 8px;
                padding: 10px; font-size: 14px; font-weight: 600;
            }
            QPushButton:hover { background: rgba(75, 145, 255, 220); }
            QPushButton:pressed { background: rgba(40, 90, 170, 220); }
        """)
        start_btn.clicked.connect(self._on_start)
        layout.addWidget(start_btn)

        return page

    # ── Session page ───────────────────────────────────────────────────────────
    def _build_session_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("root")
        page.setStyleSheet(DARK_STYLE)

        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 12, 16, 16)
        layout.setSpacing(8)

        # Title bar with status
        title_row = QHBoxLayout()
        title_lbl = QLabel("🎧  Call Copilot")
        title_lbl.setStyleSheet("color: #7ab8ff; font-size: 13px; font-weight: bold;")
        self.status_lbl = QLabel("Starting…")
        self.status_lbl.setStyleSheet("color: #667; font-size: 11px;")
        close_btn = QPushButton("✕")
        close_btn.setObjectName("close_btn")
        close_btn.setFixedSize(22, 22)
        close_btn.clicked.connect(self.close)
        title_row.addWidget(title_lbl)
        title_row.addStretch()
        title_row.addWidget(self.status_lbl)
        title_row.addSpacing(6)
        title_row.addWidget(close_btn)
        layout.addLayout(title_row)

        layout.addWidget(self._divider())

        # Suggestions output
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.output_widget = QWidget()
        self.output_widget.setStyleSheet("background: transparent;")
        self.output_layout = QVBoxLayout(self.output_widget)
        self.output_layout.setContentsMargins(4, 4, 4, 4)
        self.output_layout.setSpacing(6)
        self.output_layout.addStretch()
        scroll.setWidget(self.output_widget)
        layout.addWidget(scroll, stretch=1)

        layout.addWidget(self._divider())

        # Bottom controls: mic button + end session
        ctrl_row = QHBoxLayout()
        ctrl_row.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.mic_btn = QPushButton("🎤")
        self.mic_btn.setObjectName("mic_btn")
        self.mic_btn.setFixedSize(56, 56)
        self.mic_btn.setProperty("active", False)
        self.mic_btn.setToolTip("Toggle microphone")
        self.mic_btn.clicked.connect(self._toggle_mic)

        end_btn = QPushButton("End Session")
        end_btn.setObjectName("end_btn")
        end_btn.clicked.connect(self._on_end_session)

        ctrl_row.addStretch()
        ctrl_row.addWidget(self.mic_btn)
        ctrl_row.addSpacing(16)
        ctrl_row.addWidget(end_btn)
        ctrl_row.addStretch()
        layout.addLayout(ctrl_row)

        mic_hint = QLabel("Tap mic to start / stop listening")
        mic_hint.setStyleSheet("font-size: 10px; color: #445; margin-top: 2px;")
        mic_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(mic_hint)

        return page

    # ── Helpers ────────────────────────────────────────────────────────────────
    def _title_bar(self, title: str) -> QHBoxLayout:
        row = QHBoxLayout()
        lbl = QLabel(title)
        lbl.setStyleSheet("color: #7ab8ff; font-size: 14px; font-weight: bold;")
        close_btn = QPushButton("✕")
        close_btn.setObjectName("close_btn")
        close_btn.setFixedSize(22, 22)
        close_btn.clicked.connect(self.close)
        row.addWidget(lbl)
        row.addStretch()
        row.addWidget(close_btn)
        return row

    def _divider(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: rgba(80,110,200,60);")
        return line

    def _list_devices(self) -> str:
        lines = ["Input devices:"]
        for i in range(self._pa_enum.get_device_count()):
            info = self._pa_enum.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) > 0:
                lines.append(f"  [{i}] {info['name']}")
        return "\n".join(lines)

    # ── Signal wiring ──────────────────────────────────────────────────────────
    def _connect_signals(self):
        self.signals.text_received.connect(self._on_text)
        self.signals.status_changed.connect(self._on_status)
        self.signals.error_occurred.connect(self._on_error)
        self.signals.connected.connect(self._on_connected)

    # ── Slots ──────────────────────────────────────────────────────────────────
    def _on_start(self):
        api_key = self.api_input.text().strip()
        if not api_key:
            self._show_setup_error("API key is required.")
            return

        context = self.context_input.toPlainText().strip()
        dev_text = self.dev_input.text().strip()
        device_index = int(dev_text) if dev_text.isdigit() else None

        # Switch to session view
        self.stack.setCurrentIndex(1)
        self.status_lbl.setText("Connecting…")

        # Build client
        self.gemini = GeminiLiveClient(api_key, context, self.signals)

        # Start WebSocket thread
        def _run_ws():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.gemini._loop = loop
            loop.run_until_complete(self.gemini.connect_and_stream())

        self._ws_thread = threading.Thread(target=_run_ws, daemon=True)
        self._ws_thread.start()

        # Start audio capture (always running; push_audio gates on mic_active)
        self.audio_capture = AudioCapture(device_index, self.gemini)
        self.audio_capture.start()

    def _on_connected(self):
        self._refresh_mic_btn()

    def _toggle_mic(self):
        self._mic_on = not self._mic_on
        if self.gemini:
            self.gemini.mic_active = self._mic_on
        self._refresh_mic_btn()
        if self._mic_on:
            self.status_lbl.setText("🔴 Listening…")
        else:
            self.status_lbl.setText("⏸ Paused — press mic to resume")

    def _refresh_mic_btn(self):
        self.mic_btn.setProperty("active", self._mic_on)
        self.mic_btn.setText("🔴" if self._mic_on else "🎤")
        # Force style refresh
        self.mic_btn.style().unpolish(self.mic_btn)
        self.mic_btn.style().polish(self.mic_btn)

    def _on_text(self, text: str):
        # Each response = one card
        card = QLabel(text)
        card.setWordWrap(True)
        card.setStyleSheet("""
            QLabel {
                background: rgba(25, 35, 70, 200);
                color: #d8e8ff;
                border: 1px solid rgba(80, 120, 200, 80);
                border-radius: 8px;
                padding: 10px 12px;
                font-size: 13px;
                line-height: 160%;
            }
        """)
        card.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # Insert before the trailing stretch
        count = self.output_layout.count()
        self.output_layout.insertWidget(count - 1, card)

        # Auto-scroll to bottom
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(50, lambda: (
            self.output_widget.parentWidget().verticalScrollBar().setValue(
                self.output_widget.parentWidget().verticalScrollBar().maximum()
            ) if self.output_widget.parentWidget() else None
        ))

    def _on_status(self, msg: str):
        if hasattr(self, "status_lbl"):
            self.status_lbl.setText(msg)

    def _on_error(self, msg: str):
        if hasattr(self, "status_lbl"):
            self.status_lbl.setText(f"⚠ {msg}")
            self.status_lbl.setStyleSheet("color: #ff7070; font-size: 11px;")

    def _show_setup_error(self, msg: str):
        # Show inline on setup page
        self.api_input.setStyleSheet(
            "background: rgba(80,20,20,200); border: 1px solid rgba(255,80,80,160);"
            "color: #ffc0c0; border-radius: 7px; padding: 6px 8px; font-size: 12px;"
        )
        self.api_input.setPlaceholderText(msg)

    def _on_end_session(self):
        self._stop_all()
        # Return to setup screen
        self.stack.setCurrentIndex(0)

    def _stop_all(self):
        self._mic_on = False
        if self.gemini:
            self.gemini.stop()
            self.gemini = None
        if self.audio_capture:
            self.audio_capture.stop()
            self.audio_capture = None
        # Clear output cards
        while self.output_layout.count() > 1:
            item = self.output_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    # ── Drag to move ───────────────────────────────────────────────────────────
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None

    # ── Close ──────────────────────────────────────────────────────────────────
    def closeEvent(self, event):
        self._stop_all()
        if self._pa_enum:
            self._pa_enum.terminate()
        event.accept()


# ── Entry point ────────────────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Call Copilot")
    win = OverlayWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
