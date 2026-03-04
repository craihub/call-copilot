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
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject, QThread
from PyQt6.QtGui import QColor, QFont, QPalette
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# ── Audio config ──────────────────────────────────────────────────────────────
AUDIO_RATE      = 16000
AUDIO_CHANNELS  = 1
AUDIO_FORMAT    = pyaudio.paInt16
CHUNK_SIZE      = 1024          # ~64ms @ 16kHz — small for low latency
AUDIO_MIME      = "audio/pcm;rate=16000"

# ── Gemini Live API ───────────────────────────────────────────────────────────
MODEL           = "gemini-live-2.5-flash-preview"
WS_URI_TEMPLATE = (
    "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta"
    ".GenerativeService.BidiGenerateContent?key={api_key}"
)

SYSTEM_PROMPT = (
    "Act as a silent call copilot. Use the provided context to inform your answers. "
    "When you hear a question directed at the user, immediately provide 2-3 short, "
    "high-impact bullet points. Maximum 15 words per bullet. No conversational filler. "
    "If no question is asked, stay silent."
)

# ── Colours ───────────────────────────────────────────────────────────────────
BG_COLOUR        = "#0D0D0D"
PANEL_COLOUR     = "#1A1A1A"
ACCENT_COLOUR    = "#00FF88"
TEXT_COLOUR      = "#E8E8E8"
DIM_COLOUR       = "#666666"
BORDER_COLOUR    = "#2A2A2A"
BUTTON_ACTIVE    = "#00FF88"
BUTTON_INACTIVE  = "#333333"
BUTTON_STOP      = "#FF4444"


# ─────────────────────────────────────────────────────────────────────────────
#  Signals bridge (worker → Qt UI)
# ─────────────────────────────────────────────────────────────────────────────
class Signals(QObject):
    suggestion     = pyqtSignal(str)   # new AI text chunk
    turn_complete  = pyqtSignal()      # AI finished responding
    status_update  = pyqtSignal(str)   # status bar text
    error          = pyqtSignal(str)   # error message


# ─────────────────────────────────────────────────────────────────────────────
#  Gemini WebSocket worker  (runs in its own thread / event loop)
# ─────────────────────────────────────────────────────────────────────────────
class GeminiWorker(QThread):
    def __init__(self, api_key: str, context: str, signals: Signals):
        super().__init__()
        self.api_key   = api_key
        self.context   = context
        self.signals   = signals
        self._running  = False
        self._audio_q: queue.Queue = queue.Queue(maxsize=200)
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # called from audio capture thread — safe
    def enqueue_audio(self, pcm_bytes: bytes):
        try:
            self._audio_q.put_nowait(pcm_bytes)
        except queue.Full:
            pass  # drop oldest-implicit by skipping

    def stop(self):
        self._running = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def run(self):
        self._running = True
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._session())
        except Exception as exc:
            self.signals.error.emit(f"Worker error: {exc}")
        finally:
            self._loop.close()

    # ── Setup payload ─────────────────────────────────────────────────────────
    def _build_setup(self) -> dict:
        system_instruction = SYSTEM_PROMPT
        if self.context.strip():
            system_instruction = (
                f"Call context: {self.context.strip()}\n\n" + SYSTEM_PROMPT
            )
        return {
            "setup": {
                "model": f"models/{MODEL}",
                "generation_config": {
                    "response_modalities": ["TEXT"],
                    "speech_config": {
                        "voice_config": {"prebuilt_voice_config": {"voice_name": "Aoede"}}
                    },
                },
                "system_instruction": {
                    "parts": [{"text": system_instruction}]
                },
                "realtime_input_config": {
                    "automatic_activity_detection": {
                        "disabled": False,
                        "start_of_speech_sensitivity": "START_SENSITIVITY_LOW",
                        "end_of_speech_sensitivity":   "END_SENSITIVITY_HIGH",
                        "prefix_padding_ms":            200,
                        "silence_duration_ms":          800,
                    }
                },
            }
        }

    # ── Main async session ────────────────────────────────────────────────────
    async def _session(self):
        uri = WS_URI_TEMPLATE.format(api_key=self.api_key)
        self.signals.status_update.emit("Connecting…")

        try:
            async with websockets.connect(
                uri,
                max_size=10 * 1024 * 1024,
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                # 1. Send setup
                await ws.send(json.dumps(self._build_setup()))
                self.signals.status_update.emit("Waiting for setup confirmation…")

                # 2. Wait for setupComplete
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10)
                    msg = json.loads(raw)
                    if "setupComplete" in msg:
                        break

                self.signals.status_update.emit("🎙 Listening — Active")

                # 3. Run send + receive concurrently
                await asyncio.gather(
                    self._send_audio(ws),
                    self._receive(ws),
                )
        except websockets.exceptions.ConnectionClosedError as e:
            self.signals.error.emit(f"Connection closed: {e}")
        except asyncio.TimeoutError:
            self.signals.error.emit("Timeout waiting for Gemini setup.")
        except Exception as e:
            self.signals.error.emit(str(e))

    # ── Continuous audio sender ───────────────────────────────────────────────
    async def _send_audio(self, ws):
        loop = asyncio.get_event_loop()
        while self._running:
            try:
                pcm = await loop.run_in_executor(
                    None, lambda: self._audio_q.get(timeout=0.1)
                )
                b64 = base64.b64encode(pcm).decode()
                payload = {
                    "realtime_input": {
                        "media_chunks": [
                            {"mime_type": AUDIO_MIME, "data": b64}
                        ]
                    }
                }
                await ws.send(json.dumps(payload))
            except queue.Empty:
                await asyncio.sleep(0.01)

    # ── Response receiver ─────────────────────────────────────────────────────
    async def _receive(self, ws):
        while self._running:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            msg = json.loads(raw)

            # Extract text parts
            candidates = (
                msg.get("serverContent", {})
                   .get("modelTurn", {})
                   .get("parts", [])
            )
            for part in candidates:
                text = part.get("text", "")
                if text:
                    self.signals.suggestion.emit(text)

            # Turn complete signal
            if msg.get("serverContent", {}).get("turnComplete"):
                self.signals.turn_complete.emit()


# ─────────────────────────────────────────────────────────────────────────────
#  PyAudio capture  (runs in a dedicated daemon thread)
# ─────────────────────────────────────────────────────────────────────────────
class AudioCapture:
    def __init__(self, worker: GeminiWorker, device_index: Optional[int] = None):
        self.worker       = worker
        self.device_index = device_index
        self._pa: Optional[pyaudio.PyAudio] = None
        self._stream      = None
        self._thread      = None
        self._running     = False

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._stream:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
        if self._pa:
            self._pa.terminate()

    def _run(self):
        self._pa = pyaudio.PyAudio()

        kwargs = dict(
            format=AUDIO_FORMAT,
            channels=AUDIO_CHANNELS,
            rate=AUDIO_RATE,
            input=True,
            frames_per_buffer=CHUNK_SIZE,
        )
        if self.device_index is not None:
            kwargs["input_device_index"] = self.device_index

        try:
            self._stream = self._pa.open(**kwargs)
            while self._running:
                data = self._stream.read(CHUNK_SIZE, exception_on_overflow=False)
                self.worker.enqueue_audio(data)
        except Exception as exc:
            print(f"[AudioCapture] {exc}", file=sys.stderr)


# ─────────────────────────────────────────────────────────────────────────────
#  Suggestion bubble widget
# ─────────────────────────────────────────────────────────────────────────────
class SuggestionBubble(QFrame):
    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"""
            QFrame {{
                background: {PANEL_COLOUR};
                border: 1px solid {BORDER_COLOUR};
                border-left: 3px solid {ACCENT_COLOUR};
                border-radius: 6px;
                padding: 2px;
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(0)

        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color: {TEXT_COLOUR}; font-size: 13px; background: transparent; border: none;")
        layout.addWidget(lbl)


# ─────────────────────────────────────────────────────────────────────────────
#  Main overlay window
# ─────────────────────────────────────────────────────────────────────────────
class CopilotWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._worker:  Optional[GeminiWorker] = None
        self._capture: Optional[AudioCapture] = None
        self._current_text = ""
        self._suggestion_count = 0

        self._setup_ui()
        self._apply_styles()

    # ── UI construction ───────────────────────────────────────────────────────
    def _setup_ui(self):
        self.setWindowTitle("Call Copilot")
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(420, 620)
        self.move(60, 60)

        # ── Root ──────────────────────────────────────────────────────────────
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ── Title bar ─────────────────────────────────────────────────────────
        title_bar = QWidget()
        title_bar.setObjectName("titleBar")
        title_bar.setFixedHeight(40)
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(14, 0, 10, 0)

        dot = QLabel("●")
        dot.setStyleSheet(f"color: {ACCENT_COLOUR}; font-size: 10px;")
        title_lbl = QLabel("CALL COPILOT")
        title_lbl.setStyleSheet(
            f"color: {TEXT_COLOUR}; font-size: 11px; font-weight: 700; letter-spacing: 2px;"
        )
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(24, 24)
        close_btn.setObjectName("closeBtn")
        close_btn.clicked.connect(self.close)

        title_layout.addWidget(dot)
        title_layout.addSpacing(6)
        title_layout.addWidget(title_lbl)
        title_layout.addStretch()
        title_layout.addWidget(close_btn)
        root_layout.addWidget(title_bar)

        # ── Divider ───────────────────────────────────────────────────────────
        div = QFrame()
        div.setFrameShape(QFrame.Shape.HLine)
        div.setStyleSheet(f"background: {BORDER_COLOUR}; max-height: 1px;")
        root_layout.addWidget(div)

        # ── Body ──────────────────────────────────────────────────────────────
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(14, 12, 14, 14)
        body_layout.setSpacing(10)

        # Context label
        ctx_lbl = QLabel("CALL CONTEXT")
        ctx_lbl.setStyleSheet(
            f"color: {DIM_COLOUR}; font-size: 10px; font-weight: 600; letter-spacing: 1px;"
        )
        body_layout.addWidget(ctx_lbl)

        # Context input
        self.context_box = QTextEdit()
        self.context_box.setPlaceholderText(
            'e.g. "Interview for Senior Dev role at Acme Corp"'
        )
        self.context_box.setFixedHeight(68)
        self.context_box.setObjectName("contextBox")
        body_layout.addWidget(self.context_box)

        # API Key label
        key_lbl = QLabel("GEMINI API KEY")
        key_lbl.setStyleSheet(
            f"color: {DIM_COLOUR}; font-size: 10px; font-weight: 600; letter-spacing: 1px;"
        )
        body_layout.addWidget(key_lbl)

        # API Key input
        self.api_key_box = QTextEdit()
        self.api_key_box.setPlaceholderText("AIza…")
        self.api_key_box.setFixedHeight(40)
        self.api_key_box.setObjectName("apiKeyBox")
        # pre-fill from env
        env_key = os.environ.get("GEMINI_API_KEY", "")
        if env_key:
            self.api_key_box.setPlainText(env_key)
        body_layout.addWidget(self.api_key_box)

        # Device selector hint
        self.device_lbl = QLabel("Audio device: default (mic / BlackHole loopback)")
        self.device_lbl.setStyleSheet(f"color: {DIM_COLOUR}; font-size: 10px;")
        self.device_lbl.setWordWrap(True)
        body_layout.addWidget(self.device_lbl)
        self._populate_device_hint()

        # Start / Stop button
        self.toggle_btn = QPushButton("▶  START LISTENING")
        self.toggle_btn.setObjectName("startBtn")
        self.toggle_btn.setFixedHeight(38)
        self.toggle_btn.clicked.connect(self._toggle)
        body_layout.addWidget(self.toggle_btn)

        # Status bar
        self.status_lbl = QLabel("Ready")
        self.status_lbl.setStyleSheet(f"color: {DIM_COLOUR}; font-size: 10px;")
        body_layout.addWidget(self.status_lbl)

        # Divider
        div2 = QFrame()
        div2.setFrameShape(QFrame.Shape.HLine)
        div2.setStyleSheet(f"background: {BORDER_COLOUR}; max-height: 1px;")
        body_layout.addWidget(div2)

        # Suggestions label
        sug_header = QHBoxLayout()
        sug_lbl = QLabel("SUGGESTIONS")
        sug_lbl.setStyleSheet(
            f"color: {DIM_COLOUR}; font-size: 10px; font-weight: 600; letter-spacing: 1px;"
        )
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setObjectName("clearBtn")
        self.clear_btn.setFixedHeight(22)
        self.clear_btn.clicked.connect(self._clear_suggestions)
        sug_header.addWidget(sug_lbl)
        sug_header.addStretch()
        sug_header.addWidget(self.clear_btn)
        body_layout.addLayout(sug_header)

        # Scroll area for suggestions
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setObjectName("scrollArea")
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)

        self.suggestions_widget = QWidget()
        self.suggestions_layout = QVBoxLayout(self.suggestions_widget)
        self.suggestions_layout.setContentsMargins(0, 0, 0, 0)
        self.suggestions_layout.setSpacing(6)
        self.suggestions_layout.addStretch()

        self.scroll_area.setWidget(self.suggestions_widget)
        body_layout.addWidget(self.scroll_area, stretch=1)

        root_layout.addWidget(body, stretch=1)

        # ── Drag support ──────────────────────────────────────────────────────
        self._drag_pos = None
        title_bar.mousePressEvent   = self._drag_start
        title_bar.mouseMoveEvent    = self._drag_move
        title_bar.mouseReleaseEvent = self._drag_end

    def _populate_device_hint(self):
        """List available input devices — user picks loopback by env var."""
        try:
            pa    = pyaudio.PyAudio()
            count = pa.get_device_count()
            names = []
            for i in range(count):
                info = pa.get_device_info_by_index(i)
                if info.get("maxInputChannels", 0) > 0:
                    names.append(f"[{i}] {info['name']}")
            pa.terminate()
            if names:
                self.device_lbl.setText(
                    "Input devices: " + " | ".join(names[:4])
                    + ("\n  Set AUDIO_DEVICE_INDEX env var to choose loopback (e.g. BlackHole)" if len(names) > 1 else "")
                )
        except Exception:
            pass

    # ── Styles ────────────────────────────────────────────────────────────────
    def _apply_styles(self):
        self.setStyleSheet(f"""
            QWidget#root {{
                background: {BG_COLOUR};
                border-radius: 12px;
                border: 1px solid {BORDER_COLOUR};
            }}
            QWidget#titleBar {{
                background: {BG_COLOUR};
                border-radius: 12px 12px 0 0;
            }}
            QPushButton#closeBtn {{
                background: transparent;
                color: {DIM_COLOUR};
                border: none;
                font-size: 14px;
                border-radius: 4px;
            }}
            QPushButton#closeBtn:hover {{
                color: {TEXT_COLOUR};
                background: #2A2A2A;
            }}
            QPushButton#startBtn {{
                background: {BUTTON_ACTIVE};
                color: #000;
                border: none;
                border-radius: 6px;
                font-size: 12px;
                font-weight: 700;
                letter-spacing: 1px;
            }}
            QPushButton#startBtn:hover {{
                background: #00CC6A;
            }}
            QPushButton#startBtn[active=true] {{
                background: {BUTTON_STOP};
                color: #fff;
            }}
            QPushButton#startBtn[active=true]:hover {{
                background: #CC3333;
            }}
            QPushButton#clearBtn {{
                background: transparent;
                color: {DIM_COLOUR};
                border: 1px solid {BORDER_COLOUR};
                border-radius: 4px;
                font-size: 10px;
                padding: 0 8px;
            }}
            QPushButton#clearBtn:hover {{
                color: {TEXT_COLOUR};
                border-color: {TEXT_COLOUR};
            }}
            QTextEdit#contextBox, QTextEdit#apiKeyBox {{
                background: {PANEL_COLOUR};
                color: {TEXT_COLOUR};
                border: 1px solid {BORDER_COLOUR};
                border-radius: 6px;
                font-size: 12px;
                padding: 6px;
            }}
            QTextEdit#contextBox:focus, QTextEdit#apiKeyBox:focus {{
                border-color: {ACCENT_COLOUR};
            }}
            QScrollArea#scrollArea {{
                background: transparent;
            }}
            QScrollBar:vertical {{
                background: {BG_COLOUR};
                width: 4px;
                border-radius: 2px;
            }}
            QScrollBar::handle:vertical {{
                background: {BORDER_COLOUR};
                border-radius: 2px;
            }}
        """)

    # ── Drag ──────────────────────────────────────────────────────────────────
    def _drag_start(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def _drag_move(self, e):
        if e.buttons() == Qt.MouseButton.LeftButton and self._drag_pos:
            self.move(e.globalPosition().toPoint() - self._drag_pos)

    def _drag_end(self, e):
        self._drag_pos = None

    # ── Start / Stop ──────────────────────────────────────────────────────────
    def _toggle(self):
        if self._worker and self._worker.isRunning():
            self._stop()
        else:
            self._start()

    def _start(self):
        api_key = self.api_key_box.toPlainText().strip()
        if not api_key:
            self._set_status("⚠ Enter a Gemini API key first.")
            return

        context = self.context_box.toPlainText().strip()

        # Signals
        self._signals = Signals()
        self._signals.suggestion.connect(self._on_suggestion)
        self._signals.turn_complete.connect(self._on_turn_complete)
        self._signals.status_update.connect(self._set_status)
        self._signals.error.connect(self._on_error)

        # Worker
        self._worker = GeminiWorker(api_key, context, self._signals)
        self._worker.start()

        # Audio capture
        device_index = None
        env_idx = os.environ.get("AUDIO_DEVICE_INDEX", "")
        if env_idx.isdigit():
            device_index = int(env_idx)

        self._capture = AudioCapture(self._worker, device_index)
        self._capture.start()

        self.toggle_btn.setText("■  STOP")
        self.toggle_btn.setProperty("active", True)
        self.toggle_btn.style().unpolish(self.toggle_btn)
        self.toggle_btn.style().polish(self.toggle_btn)
        self.context_box.setEnabled(False)
        self.api_key_box.setEnabled(False)

    def _stop(self):
        if self._capture:
            self._capture.stop()
        if self._worker:
            self._worker.stop()
            self._worker.wait(3000)

        self.toggle_btn.setText("▶  START LISTENING")
        self.toggle_btn.setProperty("active", False)
        self.toggle_btn.style().unpolish(self.toggle_btn)
        self.toggle_btn.style().polish(self.toggle_btn)
        self.context_box.setEnabled(True)
        self.api_key_box.setEnabled(True)
        self._set_status("Stopped.")

    # ── Suggestion handling ───────────────────────────────────────────────────
    def _on_suggestion(self, text: str):
        self._current_text += text

    def _on_turn_complete(self):
        text = self._current_text.strip()
        self._current_text = ""
        if not text:
            return

        self._suggestion_count += 1
        # Insert before the trailing stretch
        bubble = SuggestionBubble(text)
        idx = self.suggestions_layout.count() - 1  # before stretch
        self.suggestions_layout.insertWidget(idx, bubble)

        # Auto-scroll to bottom
        QTimer.singleShot(50, lambda: self.scroll_area.verticalScrollBar().setValue(
            self.scroll_area.verticalScrollBar().maximum()
        ))

    def _clear_suggestions(self):
        while self.suggestions_layout.count() > 1:
            item = self.suggestions_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._suggestion_count = 0

    # ── Status / error ────────────────────────────────────────────────────────
    def _set_status(self, msg: str):
        self.status_lbl.setText(msg)

    def _on_error(self, msg: str):
        self._set_status(f"⚠ {msg}")
        self._stop()

    # ── Close ─────────────────────────────────────────────────────────────────
    def closeEvent(self, event):
        self._stop()
        event.accept()


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Call Copilot")

    # High-DPI support
    app.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)

    window = CopilotWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
