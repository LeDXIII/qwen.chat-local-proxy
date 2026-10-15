"""
Main GUI application for Qwen Local Proxy.

PySide6 + markdown2. Streams replies, keeps the chat/parent continuation ids
visible, copies any message to the clipboard with one click and does all HTTP
in background threads so the window never freezes.
"""

import base64
import mimetypes
import os
import re
import threading
import time
from pathlib import Path

import markdown2
from PySide6.QtCore import Qt, QThread, Signal, QEvent, QTimer, QUrl
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QKeyEvent,
    QMouseEvent,
    QPalette,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from gui.api_client import LocalAPIClient, LOG_DIR, _logger

# ---------------------------------------------------------------- theme tokens
BG = "#15171c"
PANEL = "#1d2027"
FIELD = "#262b35"
FIELD_HOVER = "#2c323e"
BORDER = "#39404d"
BORDER_FOCUS = "#4f8cff"
TEXT = "#e8eaf0"
MUTED = "#98a1b3"
ACCENT = "#4f8cff"
ACCENT_HOVER = "#6ba1ff"
ACCENT_DISABLED = "#31405c"
VIOLET = "#8e5cff"
USER_COLOR = "#4A90D9"
ASSIST_COLOR = "#2ECC71"
NOTE_COLOR = "#f0a35e"
OK_COLOR = "#3ddc97"
ERR_COLOR = "#ff6b6b"
CODE_FG = "#d4d8e2"
CODE_BORDER = "#39404d"

STYLE_SHEET = f"""
QMainWindow, QWidget {{ background: {BG}; color: {TEXT}; font-family: 'Segoe UI'; font-size: 13px; }}
QToolTip {{ background: {FIELD}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 4px; padding: 4px 8px; }}
QLabel {{ background: transparent; }}
QLabel[kind="field"] {{ color: {MUTED}; font-size: 12px; }}
QPushButton {{
    background: {ACCENT}; color: #ffffff; border: none; border-radius: 8px;
    padding: 7px 16px; font-size: 13px; font-weight: 600;
}}
QPushButton:hover {{ background: {ACCENT_HOVER}; }}
QPushButton:pressed {{ background: {BORDER_FOCUS}; }}
QPushButton:disabled {{ background: {ACCENT_DISABLED}; color: {MUTED}; }}
QPushButton[kind="ghost"] {{ background: {FIELD}; color: {TEXT}; border: 1px solid {BORDER}; font-weight: 400; padding: 6px 12px; }}
QPushButton[kind="ghost"]:hover {{ background: {FIELD_HOVER}; border-color: {BORDER_FOCUS}; }}
QPushButton[kind="ghost"]:disabled {{ background: {PANEL}; color: {MUTED}; }}
QComboBox, QLineEdit, QTextEdit {{
    background: {FIELD}; color: {TEXT}; border: 1px solid {BORDER};
    border-radius: 8px; padding: 6px 8px; selection-background-color: {ACCENT}; selection-color: #ffffff;
}}
QComboBox:hover, QLineEdit:hover, QTextEdit:hover {{ border-color: {MUTED}; }}
QComboBox:focus, QLineEdit:focus, QTextEdit:focus {{ border-color: {BORDER_FOCUS}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QLineEdit:disabled, QTextEdit:disabled {{ color: {MUTED}; }}
QTextBrowser {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 10px; padding: 10px; }}
QPlainTextEdit {{
    background: {PANEL}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 10px; padding: 8px;
    font-family: 'Consolas', 'Courier New', monospace; font-size: 11px;
    selection-background-color: {ACCENT}; selection-color: #ffffff;
}}
QSplitter::handle {{ background: transparent; height: 6px; }}
QStatusBar {{ background: {PANEL}; border-top: 1px solid {BORDER}; color: {MUTED}; }}
QStatusBar QLabel {{ color: {MUTED}; font-family: 'Consolas'; font-size: 11px; background: transparent; }}
"""

DARK_PALETTE = QPalette()
DARK_PALETTE.setColor(QPalette.Window, BG)
DARK_PALETTE.setColor(QPalette.WindowText, TEXT)
DARK_PALETTE.setColor(QPalette.Base, FIELD)
DARK_PALETTE.setColor(QPalette.AlternateBase, PANEL)
DARK_PALETTE.setColor(QPalette.ToolTipBase, FIELD)
DARK_PALETTE.setColor(QPalette.ToolTipText, TEXT)
DARK_PALETTE.setColor(QPalette.Text, TEXT)
DARK_PALETTE.setColor(QPalette.Button, FIELD)
DARK_PALETTE.setColor(QPalette.ButtonText, TEXT)
DARK_PALETTE.setColor(QPalette.Highlight, ACCENT)
DARK_PALETTE.setColor(QPalette.HighlightedText, "#ffffff")
DARK_PALETTE.setColor(QPalette.Link, ACCENT)
DARK_PALETTE.setColor(QPalette.LinkVisited, VIOLET)


# Everything the window shows is Russian; only protocol identifiers (chatId,
# parentId, model ids, /v1/health) and the product name stay as written.
NEW_CHAT = "(новый)"


def short_id(value: str) -> str:
    return value[:12] + "..." if len(value) > 12 else value


def ghost(button: QPushButton) -> QPushButton:
    button.setProperty("kind", "ghost")
    return button


def field_label(text: str) -> QLabel:
    """Grey caption in front of a control, so labels never compete with values."""
    label = QLabel(text)
    label.setProperty("kind", "field")
    return label


def md_to_html(md: str) -> str:
    """Convert Markdown to HTML for the chat view."""
    extras = [
        "fenced-code-blocks",
        "tables",
        "strike",
        "task_list",
        "footnotes",
        "break-on-newline",
    ]
    html = markdown2.markdown(md, extras=extras)
    return f"""
    <style>
        body {{ font-family: 'Segoe UI', sans-serif; font-size: 14px; color: {TEXT}; background: {PANEL}; }}
        p {{ margin: 6px 0; }}
        pre {{ background: {FIELD}; border: 1px solid {CODE_BORDER}; border-radius: 6px; padding: 8px; color: {CODE_FG}; overflow-x: auto; }}
        code {{ background: {FIELD}; color: {CODE_FG}; padding: 2px 4px; border-radius: 4px; font-family: 'Consolas', monospace; font-size: 13px; }}
        pre code {{ background: none; padding: 0; }}
        blockquote {{ border-left: 3px solid {ACCENT}; margin: 0; padding-left: 12px; color: {MUTED}; }}
        table {{ border-collapse: collapse; margin: 8px 0; }}
        th, td {{ border: 1px solid {CODE_BORDER}; padding: 6px 10px; }}
        th {{ background: {FIELD}; }}
        a {{ color: {ACCENT}; }}
        hr {{ border: none; border-top: 1px solid {BORDER}; margin: 12px 0; }}
        ul, ol {{ padding-left: 24px; }}
        li {{ margin: 4px 0; }}
        h1, h2, h3, h4 {{ color: #ffffff; margin-top: 16px; margin-bottom: 8px; }}
        strong {{ color: #ffffff; }}
        em {{ color: #d4ac6e; }}
    </style>
    {html}
    """


class WorkerThread(QThread):
    """Runs one chat turn: encodes the attached files inline (no longer on the
    GUI thread), then streams the reply and mirrors the continuation ids."""

    chunk_received = Signal(str)
    chat_id_received = Signal(str)
    parent_id_received = Signal(str)
    upload_status = Signal(str)
    finished_with_result = Signal(str)
    error_occurred = Signal(str)

    # Mirrors _stage_attachments in src/api/routes.py: encoding a file the server
    # would drop only bloats the request, so the GUI declines it first.
    MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024

    def __init__(self, api: LocalAPIClient, messages: list, model: str,
                 system_message: str = None, pending_files: list = None):
        super().__init__()
        self.api = api
        self.messages = messages
        self.model = model
        self.system_message = system_message
        self.pending_files = pending_files or []

    def _encode_files(self) -> list:
        """Reads the picked files as base64 for the request body.

        The browser transport has no upload API, so the server stages these bytes
        into a temp file the SPA's file <input> can take. An unreadable, empty or
        oversized file is skipped with a status hint — never a failed turn.
        """
        out = []
        limit_mb = self.MAX_ATTACHMENT_BYTES // (1024 * 1024)
        for f in self.pending_files:
            name = f["name"]
            try:
                raw = Path(f["path"]).read_bytes()
            except OSError as e:
                self.upload_status.emit(f"Не удалось прочитать {name}: {e} — отправлено без него")
                continue
            if not raw:
                self.upload_status.emit(f"{name} пустой — пропущен")
                continue
            if len(raw) > self.MAX_ATTACHMENT_BYTES:
                self.upload_status.emit(f"{name} больше {limit_mb} МБ — пропущен")
                continue
            out.append({
                "fileName": name,
                "fileData": base64.b64encode(raw).decode("ascii"),
                "fileType": mimetypes.guess_type(name)[0] or "application/octet-stream",
                "fileSize": len(raw),
            })
        return out

    def run(self):
        try:
            files = self._encode_files() or None
            if files:
                self.upload_status.emit(f"Прикрепляю файлы к сообщению: {len(files)}")

            full = ""
            chat_id_updated = False
            for chunk in self.api.send_message(
                messages=self.messages,
                model=self.model,
                stream=True,
                system_message=self.system_message,
                files=files,
            ):
                if chunk:
                    full += chunk
                    if not chat_id_updated and self.api.chat_id:
                        chat_id_updated = True
                        self.chat_id_received.emit(self.api.chat_id)
                        if self.api.parent_id:
                            self.parent_id_received.emit(self.api.parent_id)
                    self.chunk_received.emit(chunk)

            # The server persists chat_id/parent_id into the sidecar file after
            # every message; the GUI only mirrors the cached values in labels.
            if self.api.chat_id:
                self.chat_id_received.emit(self.api.chat_id)
            if self.api.parent_id:
                self.parent_id_received.emit(self.api.parent_id)

            self.finished_with_result.emit(full)
        except Exception as e:
            self.error_occurred.emit(str(e))


class MainWindow(QMainWindow):
    """Main chat window with Markdown rendering and per-message copy links."""

    # A plain threading.Thread has no Qt event loop, so QTimer.singleShot from
    # such a thread never fires (Qt: timers need a QThread event loop). Signal
    # emit is thread-safe and delivers queued into the GUI loop — the only
    # legal way to hand background HTTP results (and their errors) to widgets.
    ui_call = Signal(object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Qwen Local Proxy")
        self.resize(1100, 780)
        app = QApplication.instance()
        app.setPalette(DARK_PALETTE)
        app.setFont(QFont("Segoe UI", 10))
        self.setStyleSheet(STYLE_SHEET)

        self.api = LocalAPIClient()
        self.messages: list[dict] = []
        self._chat_map: dict[str, str] = {}
        self._pending_files: list[dict] = []
        self._turn_busy = False
        self._turn_failed = False
        self._chat_html = ""
        self._assistant_raw_text = ""

        # "Is it thinking or hung?" readout: the worker thread stamps
        # api.last_activity on every SSE line the server sends (text chunks AND
        # heartbeat comments), so this 1 Hz timer can separate "server silent"
        # from "server alive, SPA still thinking". No new transport logic.
        self._turn_started = 0.0
        # Last staging step already written into the chat (": progress ..." SSE
        # comments of a turn that carries attachments). Kept so one step is
        # printed once instead of every second the timer fires.
        self._last_progress = None
        self._activity_timer = QTimer(self)
        self._activity_timer.setInterval(1000)
        self._activity_timer.timeout.connect(self._tick_activity)

        self.ui_call.connect(self._run_on_ui)

        self._init_ui()
        self._check_connection()
        self._refresh_models()
        self._refresh_chats()

    def _run_on_ui(self, fn):
        fn()

    def _post(self, fn):
        self.ui_call.emit(fn)

    def _hint(self, text: str):
        self.statusBar().showMessage(text, 2500)

    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(8)
        self._build_top_bar(main_layout)
        self._build_panels(main_layout)
        self._build_chat_area(main_layout)
        self._build_input_area(main_layout)
        self._build_status_bar()

    def _build_top_bar(self, parent: QVBoxLayout):
        row = QHBoxLayout()
        row.setSpacing(6)
        self.model_combo = QComboBox()
        self.model_combo.addItems(["qwen3.7-plus", "qwen3.8-max", "qwen3.8-omni-flash"])
        # A minimum (not a fixed) width: the row must stay shrinkable so the
        # window can be narrowed; the popup still lists ids in full.
        self.model_combo.setMinimumWidth(130)
        self.model_combo.setToolTip("Модель для новых запросов")
        row.addWidget(self.model_combo)
        btn = ghost(QPushButton("⟳"))
        btn.setFixedWidth(40)
        btn.setFont(QFont("Segoe UI Symbol", 14))
        btn.setToolTip("Обновить список моделей")
        btn.clicked.connect(self._refresh_models)
        row.addWidget(btn)
        row.addWidget(field_label("Чат:"))
        self.chat_combo = QComboBox()
        self.chat_combo.addItem(NEW_CHAT)
        # Chat titles are long, so the combo sizes itself to a bounded number of
        # characters instead of to the longest title (that alone used to force the
        # window wider than 1200 px); the popup still shows each title in full.
        # 10 rather than 14: with the Russian captions this row is what sets the
        # window's minimum width, and it has to stay below the 1100 px the window
        # opens at, otherwise the user cannot shrink the window at all.
        self.chat_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.chat_combo.setMinimumContentsLength(10)
        self.chat_combo.currentTextChanged.connect(self._on_chat_selected)
        row.addWidget(self.chat_combo)
        btn = ghost(QPushButton("⟳"))
        btn.setFixedWidth(40)
        btn.setFont(QFont("Segoe UI Symbol", 14))
        btn.setToolTip("Обновить список чатов")
        btn.clicked.connect(self._refresh_chats)
        row.addWidget(btn)
        btn = QPushButton("Загрузить")
        btn.setToolTip("Загрузить выбранный чат и его историю")
        btn.clicked.connect(self._load_chat_context)
        row.addWidget(btn)
        row.addStretch()
        row.addSpacing(10)
        # Fixed minimum widths: the text of both labels changes length ("Подключение..."
        # → "Подключено", "сессия: ?" → "сессия: неактивна") and without a floor the
        # Check button visibly jumps sideways on every state change.
        self.status_label = QLabel("Подключение...")
        self.status_label.setMinimumWidth(104)
        self.status_label.setStyleSheet(f"color: {MUTED}; font-weight: 600;")
        row.addWidget(self.status_label)
        self.auth_label = QLabel("сессия: ?")
        self.auth_label.setMinimumWidth(132)
        self.auth_label.setStyleSheet(f"color: {MUTED}; font-weight: 600;")
        self.auth_label.setToolTip("Состояние сессии Qwen в браузерном профиле прокси — нажмите «Проверить»")
        row.addWidget(self.auth_label)
        btn_auth = QPushButton("Проверить")
        btn_auth.setToolTip("Опросить /v1/health (первый опрос запускает Chrome прокси)")
        btn_auth.clicked.connect(self._check_auth)
        row.addWidget(btn_auth)
        parent.addLayout(row)

    def _build_panels(self, parent: QVBoxLayout):
        sys_row = QHBoxLayout()
        sys_row.setSpacing(6)
        sys_row.addWidget(field_label("Система:"))
        self.system_edit = QLineEdit()
        self.system_edit.setPlaceholderText("Необязательная системная инструкция...")
        sys_row.addWidget(self.system_edit)
        parent.addLayout(sys_row)

        files_row = QHBoxLayout()
        files_row.setSpacing(6)
        files_row.addWidget(field_label("Файлы:"))
        btn = ghost(QPushButton("Прикрепить"))
        btn.setToolTip("Файлы уйдут вместе со следующим сообщением (не более 15 МБ каждый)")
        btn.clicked.connect(self._attach_file)
        files_row.addWidget(btn)
        # The one place a file answer belongs: an attached list is only
        # meaningful until Send, so it sits next to Attach, not in the log.
        self.file_status_label = QLabel("")
        self.file_status_label.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        files_row.addWidget(self.file_status_label, 1)
        files_row.addStretch()
        parent.addLayout(files_row)

    def _build_chat_area(self, parent: QVBoxLayout):
        self.chat_view = QTextBrowser()
        # External links are opened manually so "copy:<n>" anchors can be
        # intercepted instead of being handed to the browser.
        self.chat_view.setOpenExternalLinks(False)
        # setOpenExternalLinks(False) alone does NOT stop QTextBrowser from
        # navigating to an absolute custom-scheme href ("copy:3"): Qt loads the
        # empty resource and the whole transcript vanishes from the view.
        # Both link-open modes off + consuming the click in eventFilter is what
        # keeps the document in place.
        self.chat_view.setOpenLinks(False)
        self.chat_view.anchorClicked.connect(self._on_link)
        self.chat_view.setFont(QFont("Segoe UI", 13))
        self.chat_view.viewport().installEventFilter(self)
        self.chat_view.setMinimumHeight(110)

        # The two conversation actions live above the transcript, not in the top
        # bar: the top bar then holds only the widgets whose width actually
        # matters, which is what lets the window shrink to a laptop screen.
        chat_head = QHBoxLayout()
        chat_head.setSpacing(6)
        chat_head.addWidget(field_label("Переписка"))
        chat_head.addStretch()
        btn = ghost(QPushButton("Скопировать ответ"))
        btn.setToolTip("Скопировать последний ответ ассистента в буфер обмена")
        btn.clicked.connect(self._copy_last_reply)
        chat_head.addWidget(btn)
        btn = ghost(QPushButton("Новый чат"))
        btn.setToolTip("Начать новый диалог (старые чаты останутся продолженными)")
        btn.clicked.connect(self._clear_chat)
        chat_head.addWidget(btn)

        chat_box = QWidget()
        chat_layout = QVBoxLayout(chat_box)
        chat_layout.setContentsMargins(0, 0, 0, 0)
        chat_layout.setSpacing(4)
        chat_layout.addLayout(chat_head)
        chat_layout.addWidget(self.chat_view)

        # System notes, errors and the staging progress of a turn used to be
        # printed into the chat and had to be kept alive through every streamed
        # rebuild by a side buffer. They now have their own read-only pane below
        # the transcript, so the chat shows only what Qwen and the user said.
        log_head = QHBoxLayout()
        log_head.setSpacing(6)
        log_head.addWidget(field_label("Системный лог"))
        log_head.addStretch()
        btn = ghost(QPushButton("Копировать лог"))
        btn.setToolTip("Скопировать весь лог в буфер обмена (для отчёта о проблеме)")
        btn.clicked.connect(self._copy_log)
        log_head.addWidget(btn)
        btn = ghost(QPushButton("Очистить лог"))
        btn.setToolTip("Очистить панель лога")
        btn.clicked.connect(self._clear_log)
        log_head.addWidget(btn)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        # PlainTextEdit has no HTML, so per-line colour comes from a
        # QTextCharFormat; a monospaced face keeps progress counters aligned.
        self.log_view.setFont(QFont("Consolas", 10))
        self.log_view.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.log_view.setFrameShape(QFrame.Shape.NoFrame)
        # A long turn writes a progress line per phase; the pane must not grow
        # without bound over a day-long session.
        self.log_view.document().setMaximumBlockCount(1200)

        log_box = QWidget()
        log_layout = QVBoxLayout(log_box)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(4)
        log_layout.addLayout(log_head)
        log_layout.addWidget(self.log_view)
        log_box.setMinimumHeight(120)

        # Resizable split: the log keeps close to half the pane height by default
        # because a cold start (Chrome launch, file staging) narrates a dozen
        # lines, and the drag handle lets either side take the rest.
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.setHandleWidth(8)
        self.splitter.addWidget(chat_box)
        self.splitter.addWidget(log_box)
        self.splitter.setCollapsible(0, False)
        self.splitter.setCollapsible(1, True)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([430, 320])
        parent.addWidget(self.splitter, 1)

    def _append_log(self, msg: str, color: str = NOTE_COLOR):
        """One timestamped, coloured log line (multi-line notes keep the stamp)."""
        stamp = time.strftime("%H:%M:%S")
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.End)
        muted = QTextCharFormat()
        muted.setForeground(QColor(MUTED))
        body = QTextCharFormat()
        body.setForeground(QColor(color))
        pad = " " * (len(stamp) + 2)
        for num, line in enumerate((msg or "").splitlines() or [""]):
            if num:
                cursor.insertText("\n")
            # QPlainTextEdit has no HTML, so colour is a char format per insert.
            cursor.insertText(f"{stamp}  " if not num else pad, muted)
            cursor.insertText(line, body)
        cursor.insertText("\n")
        self.log_view.setTextCursor(cursor)
        self.log_view.ensureCursorVisible()

    def _clear_log(self):
        self.log_view.clear()

    def _copy_log(self):
        try:
            QApplication.clipboard().setText(self.log_view.toPlainText())
            self._hint("Лог скопирован в буфер обмена")
        except Exception as e:
            self._append_log(f"Буфер обмена: {e}", ERR_COLOR)

    def _build_input_area(self, parent: QVBoxLayout):
        row = QHBoxLayout()
        row.setSpacing(6)
        self.input_edit = QTextEdit()
        self.input_edit.setFixedHeight(84)
        self.input_edit.setFont(QFont("Segoe UI", 13))
        self.input_edit.setPlaceholderText("Сообщение для Qwen — отправка по Ctrl+Enter")
        row.addWidget(self.input_edit)
        self.send_btn = QPushButton("Отправить")
        self.send_btn.setFixedWidth(110)
        self.send_btn.setToolTip("Отправить (Ctrl+Enter)")
        self.send_btn.clicked.connect(self._send_message)
        row.addWidget(self.send_btn)
        parent.addLayout(row)

        # Live "thinking vs stuck" line, refreshed by _tick_activity: without it
        # a turn that waits minutes on the browser transport looks identical to
        # a frozen program.
        live_row = QHBoxLayout()
        live_row.setSpacing(6)
        self.activity_label = QLabel("")
        self.activity_label.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        self.activity_label.setTextInteractionFlags(Qt.NoTextInteraction)
        live_row.addWidget(self.activity_label, 1)
        parent.addLayout(live_row)

        self.input_edit.installEventFilter(self)

    def _build_status_bar(self):
        bar = self.statusBar()
        bar.setSizeGripEnabled(False)
        self.chat_id_label = QLabel("чат не выбран")
        self.chat_id_label.setToolTip("chatId — цель продолжения следующего запроса")
        bar.addWidget(self.chat_id_label)
        self.parent_id_label = QLabel("родитель не выбран")
        self.parent_id_label.setToolTip("parentId (последний responseId) — точка ветвления внутри чата")
        bar.addWidget(self.parent_id_label)

    def eventFilter(self, obj, event):
        # The chat-viewport filter is installed while the window is still
        # half-built, so widget identity checks must not assume the composer
        # already exists.
        if obj is getattr(self, "input_edit", None) and event.type() == QEvent.KeyPress:
            key_event = event
            if key_event.key() == Qt.Key_Return and key_event.modifiers() == Qt.ControlModifier:
                self._send_message()
                return True
        if (obj is self.chat_view.viewport() and isinstance(event, QMouseEvent)
                and event.type() == QEvent.MouseButtonRelease
                and event.button() == Qt.LeftButton):
            href = self._anchor_href_at(event.position().toPoint())
            if href.startswith("copy:"):
                # Consume the release: left to Qt it also drives QTextBrowser's
                # own link handling, which "navigates" to copy:<idx> and blanks
                # the transcript while the clipboard copy still succeeds.
                self._on_link(QUrl(href))
                return True
            if href.split(":", 1)[0].lower() in ("http", "https", "mailto"):
                QDesktopServices.openUrl(QUrl(href))
                return True
        return super().eventFilter(obj, event)

    def _anchor_href_at(self, pos) -> str:
        """Anchor href under a chat-viewport position ("" for plain text)."""
        cur = self.chat_view.cursorForPosition(pos)
        fmt = cur.charFormat()
        if not fmt.isAnchor() and cur.position() > 0:
            # cursorForPosition reports the char at the caret; a click on the
            # last glyph of a link lands just past it.
            cur.setPosition(cur.position() - 1)
            fmt = cur.charFormat()
        return fmt.anchorHref() if fmt.isAnchor() else ""

    # ------------------------------------------------------------ connection
    def _check_connection(self):
        def check():
            try:
                ok = self.api.check_connection()
            except Exception:
                ok = False
            self._post(lambda: self._set_status(ok))
        threading.Thread(target=check, daemon=True).start()

    def _set_status(self, ok: bool):
        self.status_label.setText("Подключено" if ok else "Отключено")
        self.status_label.setStyleSheet(f"color: {OK_COLOR if ok else ERR_COLOR}; font-weight: 600;")

    def _check_auth(self):
        self.auth_label.setText("сессия: проверяю...")
        self.auth_label.setStyleSheet(f"color: {MUTED}; font-weight: 600;")
        # The probe serializes behind any running browser request, so say so
        # up front — otherwise the small label looks frozen / dead.
        self._append_system("Проверка сессии начата — она подождёт завершения активного запроса, "
                            "итог появится в системном логе ниже.")

        def probe():
            try:
                st = self.api.get_health()
                args = (bool(st.get("authenticated")), st.get("href", ""), st.get("detail", ""))
            except Exception as e:
                args = (None, "", str(e))
            self._post(lambda: self._set_auth(*args))
        threading.Thread(target=probe, daemon=True).start()

    def _set_auth(self, ok, href: str, detail: str):
        if ok is None:
            self.auth_label.setText("сессия: сервер молчит")
            self.auth_label.setStyleSheet(f"color: {ERR_COLOR}; font-weight: 600;")
            verdict = ("Проверка сессии: НЕТ ОТВЕТА от сервера"
                       + (f" — {detail[:160]}" if detail else "")
                       + " (возможно, запрос ещё идёт; повторите после его завершения)")
        elif ok:
            self.auth_label.setText("сессия: активна")
            self.auth_label.setStyleSheet(f"color: {OK_COLOR}; font-weight: 600;")
            verdict = f"Проверка сессии: ВЫПОЛНЕНА — {href or 'сессия браузера прокси авторизована'}"
        else:
            self.auth_label.setText("сессия: неактивна")
            self.auth_label.setStyleSheet(f"color: {NOTE_COLOR}; font-weight: 600;")
            verdict = f"Проверка сессии: НЕ ВЫПОЛНЕНА — {detail or 'войдите в аккаунт в окне Chrome'}"
        if href or detail:
            self.auth_label.setToolTip(f"{href}\n{detail}".strip())
        # A one-word label is easy to miss — the full verdict goes to the system log.
        self._append_system(verdict)

    # ---------------------------------------------------------- model / chat
    def _refresh_models(self):
        def refresh():
            try:
                models = self.api.get_models()
            except Exception as e:
                # Was a bare `pass`: the ⟳ next to the model box looked dead.
                self._post(lambda err=str(e): self._append_system(
                    f"Список моделей: {err} (оставлены встроенные идентификаторы)"))
                return
            if models:
                self._post(lambda: self._apply_models(models))
        threading.Thread(target=refresh, daemon=True).start()

    def _apply_models(self, models: list):
        current = self.model_combo.currentText()
        self.model_combo.clear()
        self.model_combo.addItems(models)
        if current in models:
            self.model_combo.setCurrentText(current)

    def _refresh_chats(self):
        def load():
            try:
                chats = self.api.get_chats()
                chat_options = [NEW_CHAT]
                chat_map = {}
                for chat in chats:
                    title = chat.get("title", "Без названия")[:30]
                    cid = chat.get("id", "")
                    display = f"{title} ({cid[:8]}...)"
                    chat_options.append(display)
                    chat_map[display] = cid
                self._post(lambda: self._apply_chats(chat_options, chat_map))
                if not chats:
                    self._post(lambda: self._append_system(
                        "Список чатов: сервер не вернул ни одного чата "
                        "(возможно, не удалось получить данные аккаунта — нажмите ⟳ ещё раз)"))
            except Exception as e:
                # A bare `pass` here is what made the ⟳ button look dead.
                self._post(lambda err=str(e): self._append_system(f"Список чатов: {err}"))
        threading.Thread(target=load, daemon=True).start()

    def _apply_chats(self, options: list, chat_map: dict):
        self.chat_combo.clear()
        self.chat_combo.addItems(options)
        self._chat_map = chat_map

    def _on_chat_selected(self, display_name: str):
        if display_name == NEW_CHAT:
            return
        self._load_chat_context()

    def _load_chat_context(self):
        display_name = self.chat_combo.currentText()
        if display_name == NEW_CHAT:
            self._append_system("Загрузка: сначала выберите существующий чат в поле «Чат» (⟳ обновляет список)")
            return
        chat_id = self._chat_map.get(display_name)
        if not chat_id:
            return

        # Server restores parentId from the sidecar next to the history —
        # both round-trips run off the GUI thread so the window stays alive.
        def work():
            try:
                result = self.api.load_chat_context(chat_id)
                restored_parent_id = result.get("parent_id") if isinstance(result, dict) else None
            except Exception as e:
                self._post(lambda err=str(e): self._append_error(f"Загрузка чата: {err}"))
                return
            self._post(lambda: self._apply_restored_parent(chat_id, restored_parent_id))
            try:
                messages = self.api.get_chat_messages(chat_id)
                if not messages:
                    messages = self._load_history(chat_id)
            except Exception as e:
                self._post(lambda err=str(e): self._append_error(f"Загрузка сообщений: {err}"))
                return
            if not messages:
                self._post(lambda: self._append_system(
                    f"Истории чата нет.\nChat ID: {chat_id}\nОтправьте сообщение, чтобы начать."
                ))
            else:
                self._post(lambda: self._display_loaded_messages(messages))
        threading.Thread(target=work, daemon=True).start()

    def _apply_restored_parent(self, chat_id: str, parent_id):
        self.chat_id_label.setText(short_id(chat_id))
        if parent_id:
            self.api._parent_id = parent_id
            self.parent_id_label.setText(short_id(parent_id))
        else:
            self.api._parent_id = None
            self.parent_id_label.setText("родитель не выбран")

    def _load_history(self, chat_id: str) -> list:
        safe_id = chat_id.replace("/", "_").replace("\\", "_")[:50]
        if not LOG_DIR.exists():
            return []
        log_files = sorted(LOG_DIR.glob(f"{safe_id}-*.md"), reverse=True)
        if not log_files:
            return []
        all_content = ""
        for log_file in reversed(log_files):
            with open(log_file, "r", encoding="utf-8") as f:
                all_content += f.read() + "\n"
        messages = []
        blocks = re.split(r"## (\w+) \(\d+:\d+:\d+\)\n\n", all_content)
        for i in range(1, len(blocks), 2):
            if i + 1 < len(blocks):
                role = blocks[i].strip()
                content = blocks[i + 1].strip()
                if role and content:
                    if messages and messages[-1]["role"] == role and messages[-1]["content"] == content:
                        continue
                    messages.append({"role": role, "content": content})
        return messages

    def _display_loaded_messages(self, messages: list):
        self.messages = messages
        self._chat_html = ""
        self._assistant_raw_text = ""
        self._rebuild_chat_html()
        self._scroll_to_bottom()

    def _clear_chat(self):
        self.messages = []
        self._pending_files = []
        self.file_status_label.setText("")
        self.system_edit.clear()
        self._chat_html = ""
        self._assistant_raw_text = ""
        self.chat_view.clear()
        self.chat_id_label.setText("чат не выбран")
        self.parent_id_label.setText("родитель не выбран")
        self.chat_combo.setCurrentText(NEW_CHAT)
        # Sidecar files are kept: old chats stay continuable after a reload.
        try:
            self.api.reset_chat()
        except Exception:
            pass

    # -------------------------------------------------------------- chat turn
    def _send_message(self):
        if self._turn_busy:
            return
        user_message = self.input_edit.toPlainText().strip()
        if not user_message:
            return
        self.input_edit.clear()
        self.messages.append({"role": "user", "content": user_message})
        self._assistant_raw_text = ""
        self._turn_failed = False
        self._rebuild_chat_html()
        self._scroll_to_bottom()
        self._turn_busy = True
        self.send_btn.setEnabled(False)

        # Liveness baseline: the worker thread stamps the api signals while the
        # SSE stream runs; this timer only reads them (floats are GIL-atomic).
        self._turn_started = time.monotonic()
        self.api.last_activity = self._turn_started
        self.api.got_first_token = False
        # A previous turn's staging step must not flash on screen before this
        # one has produced its own.
        self.api.progress = None
        self._last_progress = None
        self._activity_timer.start()
        self._tick_activity()

        # Attachments upload inside the worker now — the old per-file .wait()
        # froze the whole window for the duration of every upload.
        pending = list(self._pending_files)
        self._pending_files = []
        # Saying "Uploading" was a lie: the bytes travel in the request body and
        # the server stages them at the SPA. Name what actually happens.
        self.file_status_label.setText(
            f"Отправляю с {len(pending)} файл(ами) — сначала сайт их прочитает"
            if pending else "")

        self.worker = WorkerThread(
            self.api, self.messages, self.model_combo.currentText(),
            self.system_edit.text().strip() or None, pending)
        self.worker.chunk_received.connect(self._on_chunk_received)
        self.worker.chat_id_received.connect(self._on_chat_id_received)
        self.worker.parent_id_received.connect(self._on_parent_id_received)
        self.worker.upload_status.connect(self._hint)
        self.worker.finished_with_result.connect(self._on_response_finished)
        self.worker.error_occurred.connect(self._on_turn_error)
        self.worker.finished.connect(lambda: self.send_btn.setEnabled(True))
        self.worker.finished.connect(lambda: setattr(self, "_turn_busy", False))
        self.worker.start()

    # ------------------------------------------------------------ activity line
    def _tick_activity(self):
        """Answers "is it thinking or hung?" from the stream's liveness stamps.

        The server heartbeats SSE comments while it waits on the SPA, so a long
        gap here is real evidence that nothing is coming — not just slowness.
        """
        if not self._turn_busy:
            self._end_activity()
            return
        now = time.monotonic()
        waited = int(now - self._turn_started)
        idle = now - (self.api.last_activity or self._turn_started)
        # A turn that carries files walks through phases the server reports as
        # SSE comments (attach, the site reading, send), so the wait is narrated
        # instead of looking like a freeze. The busy phase repeats its wording
        # with a fresh second on every poll, so the chat sees it once and the
        # live line keeps the count.
        step = (self.api.progress or "").strip()
        if step:
            phase_key = re.sub(r"\d+", "", step).strip()
            if phase_key != self._last_progress:
                self._last_progress = phase_key
                self._append_system(step)
        if idle > 30:
            phase = "данных" if self.api.got_first_token else "первого токена"
            text = (f"Нет {phase} уже {int(idle)} с (оборот {waited} с) — "
                    f"возможно, зависло, проверьте окно Chrome")
        elif step:
            text = f"{step} (всего {waited} с)"
        elif not self.api.got_first_token:
            text = (f"Жду первый токен — {waited} с "
                    f"(холодный старт запускает Chrome, до ~2 мин)")
        else:
            text = f"Думает — последние данные сервера {int(idle)} с назад (всего {waited} с)"
        self.activity_label.setStyleSheet(
            f"color: {ERR_COLOR if idle > 30 else MUTED}; font-size: 11px;")
        self.activity_label.setText(text)

    def _end_activity(self, keep: str = "", danger: bool = False):
        """Stop the watch dog; `keep` leaves a final line in the live strip."""
        self._activity_timer.stop()
        self.activity_label.setStyleSheet(f"color: {ERR_COLOR if danger else MUTED}; font-size: 11px;")
        self.activity_label.setText(keep)

    def _on_turn_error(self, msg: str):
        # The log pane holds the full error; the live strip says why the reply
        # never came, so a failed turn doesn't look like an empty success.
        self._end_activity(f"Оборот не удался — ответа нет. Подробности в системном логе ниже ({time.strftime('%H:%M:%S')})",
                           danger=True)
        self._turn_failed = True
        self._append_error(msg)

    def _on_chunk_received(self, chunk: str):
        # Accumulate all chunks for the current assistant response
        self._assistant_raw_text += chunk
        # Rebuild the entire chat HTML from scratch each time to avoid duplication
        self._rebuild_chat_html()
        self._scroll_to_bottom()

    def _message_meta_html(self, idx: int, sender: str, color: str) -> str:
        # The copy link is an in-document anchor (handled by _on_link), not a
        # real URL — index points straight into self.messages.
        return (f'<p style="margin:0"><b style="color:{color}">{sender}</b>'
                f'&nbsp;&nbsp;<a href="copy:{idx}" style="color:{MUTED}; font-size:11px;'
                f'text-decoration:none;" title="Скопировать это сообщение в буфер обмена">копировать</a></p>')

    def _rebuild_chat_html(self):
        """Rebuild the entire chat HTML from messages list."""
        html = ""
        for idx, msg in enumerate(self.messages):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            sender = "Вы" if role == "user" else ("Ассистент" if role == "assistant" else role.capitalize())
            color = USER_COLOR if role == "user" else ASSIST_COLOR
            html += self._message_meta_html(idx, sender, color)
            html += md_to_html(content)
            html += "<hr>"

        # Add current streaming assistant response if any (its copy link
        # appears once it becomes a real message in the list).
        if self._assistant_raw_text:
            html += f'<p style="margin:0"><b style="color:{ASSIST_COLOR}">Ассистент</b></p>'
            html += md_to_html(self._assistant_raw_text)

        self._chat_html = html
        self.chat_view.setHtml(html)

    def _on_response_finished(self, full_text: str):
        self._end_activity()
        self.messages.append({"role": "assistant", "content": full_text})
        # Drop the streaming copy and re-render: keeping it would duplicate the
        # reply the moment anything else triggers a rebuild.
        self._assistant_raw_text = ""
        self._rebuild_chat_html()
        self._scroll_to_bottom()
        chat_id = self.api.chat_id
        # A failed turn may still hand back partial text plus an [Error] note —
        # that must not poison the transcript the history fallback reads.
        if chat_id and full_text and not self._turn_failed:
            user_text = next((m["content"] for m in reversed(self.messages)
                              if m.get("role") == "user"), "")
            _logger.log(chat_id, "user", user_text)
            _logger.log(chat_id, "assistant", full_text)

    def _on_chat_id_received(self, cid: str):
        self.chat_id_label.setText(short_id(cid))

    def _on_parent_id_received(self, pid: str):
        self.parent_id_label.setText(short_id(pid))

    def _append_error(self, msg: str):
        # Both sinks are the log pane: the transcript stays a clean You/Assistant
        # dialogue while everything the program says about itself is timestamped
        # and scrollable below it.
        self._append_log(f"[Ошибка] {msg}", ERR_COLOR)

    def _append_system(self, msg: str):
        self._append_log(msg, NOTE_COLOR)

    def _scroll_to_bottom(self):
        bar = self.chat_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ----------------------------------------------------------------- copy
    def _on_link(self, url):
        target = url.toString()
        if target.startswith("copy:"):
            try:
                idx = int(target[len("copy:"):])
            except ValueError:
                return
            if 0 <= idx < len(self.messages):
                try:
                    QApplication.clipboard().setText(self.messages[idx].get("content", ""))
                    self._hint("Сообщение скопировано в буфер обмена")
                except Exception as e:
                    self._append_error(f"Буфер обмена: {e}")
            return
        if target.startswith(("http://", "https://", "mailto:")):
            QDesktopServices.openUrl(url)

    def _copy_last_reply(self):
        for msg in reversed(self.messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                try:
                    QApplication.clipboard().setText(msg["content"])
                    self._hint("Последний ответ скопирован в буфер обмена")
                except Exception as e:
                    self._append_error(f"Буфер обмена: {e}")
                return
        self._hint("Пока нечего копировать — ответ ассистента ещё не получен")

    # ----------------------------------------------------------------- files
    # Mirrors MAX_DOCUMENTS_PER_TURN in src/api/routes.py. Past five documents
    # the site keeps only the first five and says nothing about the rest, so the
    # answer would then discuss files the user believes it read. Pictures are
    # not part of the five, so they stay out of the count.
    MAX_DOCUMENTS_PER_TURN = 5

    def _is_image_name(self, name: str) -> bool:
        return (mimetypes.guess_type(name)[0] or "").startswith("image/")

    def _document_count(self, files: list[dict]) -> int:
        return sum(1 for f in files if not self._is_image_name(f["name"]))

    def _attach_file(self):
        filepath, _ = QFileDialog.getOpenFileName(
            self, "Выбрать файл", "",
            "Изображения (*.png *.jpg *.jpeg *.gif *.webp *.bmp);;Документы (*.pdf *.doc *.docx *.txt);;Все файлы (*)"
        )
        if not filepath:
            return
        fname = os.path.basename(filepath)
        fsize = os.path.getsize(filepath)
        if not self._is_image_name(fname) and \
                self._document_count(self._pending_files) >= self.MAX_DOCUMENTS_PER_TURN:
            self.file_status_label.setText(
                f"Qwen принимает не более {self.MAX_DOCUMENTS_PER_TURN} документов в сообщении: "
                f"{fname} не прикреплён — сначала уберите один документ "
                f"(картинки в этот лимит не входят)")
            return
        self._pending_files.append({"path": filepath, "name": fname, "size": fsize})
        names = ", ".join(f["name"] for f in self._pending_files)
        docs = self._document_count(self._pending_files)
        limit = "" if not docs else f" — документов {docs}/{self.MAX_DOCUMENTS_PER_TURN}"
        self.file_status_label.setText(
            f"Прикреплено: {names} (уйдёт со следующим сообщением{limit})")


def main():
    app = QApplication([])
    app.setStyle("Fusion")
    w = MainWindow()
    w.show()
    app.exec()


if __name__ == "__main__":
    main()
