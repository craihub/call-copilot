#!/usr/bin/env python3
"""
Real-Time Call Copilot
Streams system audio (loopback) to Gemini Live API via WebSocket.
Displays AI bullet-point suggestions in an always-on-top overlay.
"""

import asyncio
import base64
import json
import os
import queue
import sys
import threading
import time
from typing import Optional

import pyaudio
import websockets
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QColor, QFont
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
)

# ── Audio config ──────────────────────────────────────────────────────────────
AUDIO_RATE     = 16000
AUDIO_CHANNELS = 1
AUDIO_FORMAT   = pyaudio.paInt16
CHUNK_SIZE     = 512           # ~32ms @ 16kHz — minimal latency
AUDIO_MIME     = "audio/pcm;rate=16000"

# ── Gemini Live API ───────────────────────────────────────────────────────────
MODEL          = "gemini-2.5-flash-exp"  # use live-capable model
WS_URI_TMPL    = (
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


# ── Signals bridge (async → Qt) ───────────────────────────────────────────────
class Signals(QObject):
    text_received = pyqtSignal(str)
    status_changed = pyqtSignal(str)
    error_occurred = pyqtSignal(str)


# ── Gemini Live WebSocket client ──────────────────────────────────────────────
class GeminiLiveClient:
    def __init__(self, api_key: str, context: str, signals: Signals):
        self.api_key = api_key
        self.context = context
        self.signals = signals
        self.ws = None
        self.audio_queue: queue.Queue = queue.Queue()
        self.running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def build_setup_message(self) -> dict:
        system_instruction = SYSTEM_PROMPT
        if self.context.strip():
            system_instruction += f"\n\nCall context: {self.context.strip()}"

        return {
            "setup": {
                "model": f"models/{MODEL}",
                "generation_config": {
                    "response_modalities": ["TEXT"],
                    "temperature": 0.2,
                },
                "system_instruction": {
                    "parts": [{"text": system_instruction}]
                },
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
                self.signals.status_changed.emit("Connected — listening…")

                # Send setup
                await ws.send(json.dumps(self.build_setup_message()))

                # Wait for setup complete
                setup_resp = await ws.recv()
                data = json.loads(setup_resp)
                if "setupComplete" not in data and "error" not in data:
                    pass  # some models skip explicit setupComplete
                if "error" in data:
                    self.signals.error_occurred.emit(str(data["error"]))
                    return

                # Run sender and receiver concurrently
                await asyncio.gather(
                    self._send_audio_loop(ws),
                    self._receive_loop(ws),
                )

        except websockets.exceptions.ConnectionClosedOK:
            self.signals.status_changed.emit("Disconnected")
        except Exception as e:
            self.signals.error_occurred.emit(f"WS error: {e}")
        finally:
            self.running = False
            self.ws = None

    async def _send_audio_loop(self, ws):
        loop = asyncio.get_event_loop()
        while self.running:
            try:
                # Non-blocking get with short timeout so we can check self.running
                chunk = await loop.run_in_executor(
                    None, lambda: self.audio_queue.get(timeout=0.05)
                )
                msg = {
                    "realtime_input": {
                        "media_chunks": [
                            {
                                "mime_type": AUDIO_MIME,
                                "data": base64.b64encode(chunk).decode("utf-8"),
                            }
                        ]
                    }
                }
                await ws.send(json.dumps(msg))
            except queue.Empty:
                continue
            except Exception:
                break

    async def _receive_loop(self, ws):
        async for raw in ws:
            if not self.running:
                break
            try:
                data = json.loads(raw)
                self._handle_server_message(data)
            except json.JSONDecodeError:
                pass

    def _handle_server_message(self, data: dict):
        # Extract text from serverContent → modelTurn → parts
        server_content = data.get("serverContent", {})
        model_turn = server_content.get("modelTurn", {})
        parts = model_turn.get("parts", [])
        for part in parts:
            text = part.get("text", "")
            if text.strip():
                self.signals.text_received.emit(text.strip())

    def push_audio(self, chunk: bytes):
        if self.running:
            try:
                self.audio_queue.put_nowait(chunk)
            except queue.Full:
                pass

    def stop(self):
        self.running = False
        if self.ws and self._loop:
            asyncio.run_coroutine_threadsafe(self.ws.close(), self._loop)


# ── Audio capture thread ──────────────────────────────────────────────────────
class AudioCapture(threading.Thread):
    def __init__(self, device_index: Optional[int], client: GeminiLiveClient):
        super().__init__(daemon=True)
        self.device_index = device_index
        self.client = client
        self.running = False
        self._pa: Optional[pyaudio.PyAudio] = None
        self._stream = None

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


# ── Overlay UI ────────────────────────────────────────────────────────────────
class OverlayWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.signals = Signals()
        self.gemini: Optional[GeminiLiveClient] = None
        self.audio_capture: Optional[AudioCapture] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._pa_enum = pyaudio.PyAudio()

        self._build_ui()
        self._connect_signals()

    # ── UI construction ───────────────────────────────────────────────────────
    def _build_ui(self):
        self.setWindowTitle("Call Copilot")
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(420, 560)

        root = QWidget()
        root.setObjectName("root")
        root.setStyleSheet("""
            QWidget#root {
                background: rgba(10, 10, 20, 210);
                border: 1px solid rgba(100, 180, 255, 80);
                border-radius: 12px;
            }
            QLabel { color: #e0e8ff; }
            QPushButton {
                background: rgba(60, 120, 220, 180);
                color: white;
                border: none;
                border-radius: 6px;
                padding: 6px 14px;
                font-size: 12px;
            }
            QPushButton:hover { background: rgba(80, 150, 255, 200); }
            QPushButton#stop_btn {
                background: rgba(200, 60, 60, 180);
            }
            QPushButton#stop_btn:hover { background: rgba(230, 80, 80, 200); }
            QTextEdit, QLineEdit {
                background: rgba(20, 20, 40, 180);
                color: #c8d8ff;
                border: 1px solid rgba(100, 140, 255, 60);
                border-radius: 6px;
                padding: 4px;
                font-size: 12px;
            }
        """)

        layout = QVBoxLayout(root)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        # Title bar
        title_row = QHBoxLayout()
        title_lbl = QLabel("🎧 Call Copilot")
        title_lbl.setStyleSheet("color: #7ab8ff; font-size: 14px; font-weight: bold;")
        self.status_lbl = QLabel("Idle")
        self.status_lbl.setStyleSheet("color: #888; font-size: 11px;")
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(22, 22)
        close_btn.setStyleSheet(
            "background: rgba(200,60,60,140); border-radius: 11px; color:white; font-size:11px;"
        )
        close_btn.clicked.connect(self.close)
        title_row.addWidget(title_lbl)
        title_row.addStretch()
        title_row.addWidget(self.status_lbl)
        title_row.addSpacing(6)
        title_row.addWidget(close_btn)
        layout.addLayout(title_row)

        # API key
        api_row = QHBoxLayout()
        api_lbl = QLabel("API Key:")
        api_lbl.setFixedWidth(58)
        self.api_input = QLineEdit()
        self.api_input.setPlaceholderText("Paste Gemini API key…")
        self.api_input.setEchoMode(QLineEdit.EchoMode.Password)
        # Pre-fill from env
        env_key = os.environ.get("GEMINI_API_KEY", "")
        if env_key:
            self.api_input.setText(env_key)
        api_row.addWidget(api_lbl)
        api_row.addWidget(self.api_input)
        layout.addLayout(api_row)

        # Context
        ctx_lbl = QLabel("Call Context:")
        ctx_lbl.setStyleSheet("color: #aac4ff; font-size: 11px;")
        layout.addWidget(ctx_lbl)
        self.context_input = QTextEdit()
        self.context_input.setPlaceholderText(
            "e.g. Interview for Senior Dev role at Acme Corp\n"
            "or: Sales call for Project X with CTO"
        )
        self.context_input.setFixedHeight(68)
        layout.addWidget(self.context_input)

        # Audio device picker
        dev_row = QHBoxLayout()
        dev_lbl = QLabel("Audio In:")
        dev_lbl.setFixedWidth(58)
        self.dev_combo = QLineEdit()
        self.dev_combo.setPlaceholderText("Device index (blank = default mic)")
        dev_row.addWidget(dev_lbl)
        dev_row.addWidget(self.dev_combo)
        layout.addLayout(dev_row)

        # Device list hint
        self.dev_hint = QLabel(self._list_devices())
        self.dev_hint.setStyleSheet(
            "color: #667; font-size: 9px; font-family: monospace;"
        )
        self.dev_hint.setWordWrap(True)
        layout.addWidget(self.dev_hint)

        # Controls
        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("▶  Start Listening")
        self.stop_btn = QPushButton("■  Stop")
        self.stop_btn.setObjectName("stop_btn")
        self.stop_btn.setEnabled(False)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setStyleSheet(
            "background: rgba(40,40,80,180); color: #aac4ff; border-radius:6px; padding:6px 10px; font-size:12px;"
        )
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addWidget(self.clear_btn)
        layout.addLayout(btn_row)

        # Suggestions area
        sugg_lbl = QLabel("Suggestions:")
        sugg_lbl.setStyleSheet("color: #aac4ff; font-size: 11px;")
        layout.addWidget(sugg_lbl)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            "QScrollBar:vertical { width: 6px; background: rgba(20,20,40,80); border-radius: 3px; }"
            "QScrollBar::handle:vertical { background: rgba(100,140,255,120); border-radius: 3px; }"
        )
        self.sugg_area = QTextEdit()
        self.sugg_area.setReadOnly(True)
        self.sugg_area.setStyleSheet(
            "background: rgba(5, 10, 30, 160); color: #d0e8ff; "
            "font-size: 13px; line-height: 1.5; border: none; padding: 6px;"
        )
        self.sugg_area.setMinimumHeight(160)
        scroll.setWidget(self.sugg_area)
        layout.addWidget(scroll)

        self.setCentralWidget(root)

        # Dragging support
        self._drag_pos = None

    def _list_devices(self) -> str:
        lines = []
        for i in range(self._pa_enum.get_device_count()):
            info = self._pa_enum.get_device_info_by_index(i)
            if info["maxInputChannels"] > 0:
                lines.append(f"[{i}] {info['name'][:40]}")
        return "\n".join(lines) if lines else "No input devices found"

    # ── Signal wiring ─────────────────────────────────────────────────────────
    def _connect_signals(self):
        self.start_btn.clicked.connect(self._on_start)
        self.stop_btn.clicked.connect(self._on_stop)
        self.clear_btn.clicked.connect(self.sugg_area.clear)
        self.signals.text_received.connect(self._on_text)
        self.signals.status_changed.connect(self._on_status)
        self.signals.error_occurred.connect(self._on_error)

    # ── Slots ─────────────────────────────────────────────────────────────────
    def _on_start(self):
        api_key = self.api_input.text().strip()
        if not api_key:
            self._on_error("API key required")
            return

        context = self.context_input.toPlainText().strip()
        dev_text = self.dev_combo.text().strip()
        device_index = int(dev_text) if dev_text.isdigit() else None

        self.gemini = GeminiLiveClient(api_key, context, self.signals)
        self.audio_capture = AudioCapture(device_index, self.gemini)

        # Run WebSocket in background thread with its own event loop
        self._ws_thread = threading.Thread(
            target=self._run_ws_loop, daemon=True
        )
        self._ws_thread.start()

        # Start audio after short delay (let WS connect first)
        QTimer.singleShot(1500, self.audio_capture.start)

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_lbl.setText("Starting…")

    def _run_ws_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        if self.gemini:
            self.gemini._loop = loop
            loop.run_until_complete(self.gemini.connect_and_stream())

    def _on_stop(self):
        if self.audio_capture:
            self.audio_capture.stop()
        if self.gemini:
            self.gemini.stop()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_lbl.setText("Stopped")

    def _on_text(self, text: str):
        # Append new suggestion block with timestamp
        timestamp = time.strftime("%H:%M:%S")
        html = (
            f'<div style="margin-bottom:10px;">'
            f'<span style="color:#445577;font-size:10px;">{timestamp}</span><br>'
            f'<span style="color:#d0e8ff;">{text.replace(chr(10), "<br>")}</span>'
            f'</div>'
        )
        self.sugg_area.append(html)
        # Auto-scroll to bottom
        sb = self.sugg_area.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_status(self, status: str):
        self.status_lbl.setText(status)

    def _on_error(self, msg: str):
        self.status_lbl.setText(f"⚠ {msg}")
        self.sugg_area.append(
            f'<div style="color:#ff6666;font-size:11px;">Error: {msg}</div>'
        )

    # ── Drag to move ──────────────────────────────────────────────────────────
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() == Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None

    def closeEvent(self, event):
        self._on_stop()
        self._pa_enum.terminate()
        event.accept()


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = OverlayWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
