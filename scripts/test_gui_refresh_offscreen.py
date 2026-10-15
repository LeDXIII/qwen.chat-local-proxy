"""
Off-screen regression test for the GUI (no server, no browser).

Bug it pins down (original): the background HTTP handlers ran in plain
threading.Thread and handed results back to widgets via QTimer.singleShot — a
timer never fires in a thread without a Qt event loop, so the status stayed
at «Подключение...», the auth label stayed at «сессия: ?» and the chat combo
showed only «(новый)», while errors were swallowed silently. The fix marshals
through a Signal(object) queued into the GUI event loop.

Extended for the three live bugs:
  1. a click on a per-message "copy:" anchor must copy WITHOUT blanking the
     transcript (QTextBrowser used to navigate to the custom-scheme href);
  2. attachments travel inline as base64 in the request (the browser transport
     has no upload API), encoded off the GUI thread, and a bad file degrades to
     a text-only turn instead of a failed one;
  3. the "thinking vs hung" line: the client stamps liveness on every raw SSE
     line (server ": ping" heartbeats included) and the GUI timer turns that
     into waiting / thinking / possibly-stuck states;
  4. an attachment turn is narrated instead of looking frozen: the server's
     ": progress ..." SSE comments land in api.progress (never in the answer,
     cleared by the first content chunk), each phase is written into the chat
     ONCE while only the seconds of the busy phase move, and a sixth document
     is refused up front — the site takes five documents per message and keeps
     quiet about the rest.
Plus the refactor's send-cycle, id labels, transcript logging and the rule that
a failed turn must NOT append a fake assistant message.

Run:
    cd /d D:\qwen-code\qwen-local-proxy2
    set "PYTHONIOENCODING=utf-8" && set "QT_QPA_PLATFORM=offscreen" && venv\Scripts\python.exe -u scripts\test_gui_refresh_offscreen.py
"""
import base64
import json
import os
import requests
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui.api_client as api_module
from gui.api_client import LocalAPIClient, _logger

# Stub the whole HTTP surface BEFORE MainWindow is built — __init__ fires the
# refresh handlers immediately.
LocalAPIClient.check_connection = lambda self: True
LocalAPIClient.get_health = lambda self: {
    "authenticated": True,
    "href": "https://chat.qwen.ai/c/test-chat",
    "detail": "",
}
LocalAPIClient.get_models = lambda self: ["qwen3.7-plus", "qwen3.8-max", "qwen3.8-omni-flash"]
LocalAPIClient.get_chats = lambda self: [
    {"id": f"chat{i:02d}", "title": f"Chat {i}"} for i in range(14)
]
LocalAPIClient.load_chat_context = lambda self, cid: {}

# The real streaming parser, kept so the SSE-level checks further down can still
# reach it after send_message gets stubbed for the widget tests.
real_send = LocalAPIClient.send_message

# Transcript logger: patch the METHOD on the singleton object — gui.app binds
# the same object at import, so rebinding the module attribute would not stick.
logged = []
_logger.log = lambda chat_id, role, content: logged.append((role, content))

from PySide6.QtCore import QEvent, QPointF, QPoint, Qt, QUrl
from PySide6.QtGui import QMouseEvent, QTextCursor
from PySide6.QtWidgets import QApplication
from gui.app import ERR_COLOR, MainWindow

app = QApplication.instance() or QApplication([])
win = MainWindow()
win._check_auth()  # the Auth button is not auto-kicked in __init__


def pump(seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


pump(2.5)

results = []
skips = []


def check(name, ok, got=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  -> got: {got!r}"))


def skip(name, why):
    skips.append(name)
    print(f"SKIP  {name}  ({why})")


html = win.chat_view.toHtml()
log = win.log_view.toPlainText()
check("status label reached the Russian connected caption",
      win.status_label.text() == "Подключено", win.status_label.text())
check("auth label reached the Russian logged-in caption",
      win.auth_label.text() == "сессия: активна", win.auth_label.text())
check("chat combo loaded (новый) + 14 chats",
      win.chat_combo.count() == 15, win.chat_combo.count())
check("auth verdict went to the system log",
      "Проверка сессии: ВЫПОЛНЕНА" in log, log[-200:])
check("the log is kept out of the chat transcript", "Проверка сессии:" not in html)
check("the log pane is read-only", win.log_view.isReadOnly())
check("the log pane is bounded so it can't grow without end",
      win.log_view.document().maximumBlockCount() == 1200,
      win.log_view.document().maximumBlockCount())
check("no chat-list error note", "Список чатов:" not in log)
check("no model-list error note", "Список моделей:" not in log)

# The old bug: the per-chunk rebuild wiped all system notes from the chat view.
# Notes now live in their own pane, so a rebuild cannot reach them at all.
win._assistant_raw_text = "streamed chunk"
win._rebuild_chat_html()
check("a streamed-chunk rebuild leaves the log untouched",
      "Проверка сессии: ВЫПОЛНЕНА" in win.log_view.toPlainText(),
      win.log_view.toPlainText()[-200:])
check("a streamed-chunk rebuild still renders the reply",
      "streamed chunk" in win.chat_view.toHtml(), win.chat_view.toHtml()[-200:])

# Bug 1 lives in the link policy: setOpenExternalLinks(False) does NOT stop Qt
# from navigating an absolute custom-scheme href, so both must stay off.
check("chat view navigates to no link at all", win.chat_view.openLinks() is False)
check("chat view copy handler still wired", hasattr(win, "_on_link"))

# ---------------------------------------------------------------- send cycle
sent_payloads = []


def fake_send(self, messages, model="qwen3.7-plus", stream=True,
              system_message=None, files=None):
    sent_payloads.append({"messages": list(messages), "model": model, "files": files})
    self._chat_id = "test-chat-id-0123456789"
    self._parent_id = "resp-0123456789"
    yield "Hello "
    yield "world"


LocalAPIClient.send_message = fake_send
win.input_edit.setPlainText("ping")
win._send_message()
win.worker.wait(5000)
pump(0.5)

check("assistant message appended",
      len(win.messages) == 2 and win.messages[-1] == {"role": "assistant", "content": "Hello world"},
      win.messages)
check("streamed reply rendered", "Hello world" in win.chat_view.toHtml())
check("chat id label shows sid chat",
      win.chat_id_label.text() == "test-chat-id...", win.chat_id_label.text())
check("parent id label shows sid parent",
      win.parent_id_label.text() == "resp-0123456...", win.parent_id_label.text())
check("no error line on a good turn", "[Ошибка]" not in win.log_view.toPlainText(),
      win.log_view.toPlainText()[-200:])
check("per-message copy anchors present",
      "copy:0" in win.chat_view.toHtml() and "copy:1" in win.chat_view.toHtml())
check("transcript logged for good turn",
      logged == [("user", "ping"), ("assistant", "Hello world")], logged)

# ------------------------------------------------- bug 1: click must not blank
# The copy action itself was never the problem; the blank screen was. Drive a
# real mouse release over the anchor rect, the way a user click does.
win.resize(1200, 800)
win.show()
pump(0.4)
win.chat_view.verticalScrollBar().setValue(0)
pump(0.1)


def anchor_viewport_point(prefix):
    """A viewport point that really lands on a copy anchor (None if not found).

    Pixel-hunting over the whole viewport is slow and flaky, so the anchor's own
    layout rect is asked for and only a couple of pixels around it are tried —
    cursorForPosition (the same call the click handler uses) is the judge.
    """
    doc = win.chat_view.document()
    block = doc.begin()
    while block.isValid():
        # PySide6 hands out the block's iterators, not fragments: the fragment
        # (and so its char format) only comes from .fragment().
        for it in block:
            frag = it.fragment()
            fmt = frag.charFormat()
            if fmt.isAnchor() and fmt.anchorHref().startswith(prefix):
                cur = QTextCursor(block)
                cur.setPosition(frag.position() + 1)
                # QTextEdit.cursorRect already speaks viewport coordinates (the
                # scroll offsets are baked in), unlike the document layout.
                center = win.chat_view.cursorRect(cur).center()
                href = fmt.anchorHref()
                for dx in (0, 1, -1, 2, -2, 3, -3):
                    for dy in (0, 1, -1, 2, -2):
                        p = QPoint(int(center.x()) + dx, int(center.y()) + dy)
                        if win._anchor_href_at(p).startswith(prefix):
                            return p, href
                return None, href
        block = block.next()
    return None, ""


click_pos, click_href = anchor_viewport_point("copy:")
if click_pos is None:
    skip("copy click keeps the transcript", "no copy anchor inside the viewport")
else:
    before_html = win.chat_view.toHtml()
    try:
        QApplication.clipboard().setText("sentinel-clear")
    except Exception:
        pass
    ev = QMouseEvent(QEvent.MouseButtonRelease, QPointF(click_pos),
                     Qt.LeftButton, Qt.NoButton, Qt.NoModifier)
    QApplication.sendEvent(win.chat_view.viewport(), ev)
    pump(0.3)
    after_html = win.chat_view.toHtml()
    check(f"copy click on {click_href} left the transcript intact",
          after_html == before_html and "ping" in after_html and "Hello world" in after_html,
          f"{len(before_html)} -> {len(after_html)} chars")
    check("copy click consumed the event (no Qt link navigation)",
          ev.isAccepted() and win.chat_view.source().isEmpty(),
          f"accepted={ev.isAccepted()} source={win.chat_view.source()}")
    try:
        got = QApplication.clipboard().text()
        if got == "sentinel-clear":
            skip("copy click fills the clipboard", "clipboard not available in this session")
        else:
            check("copy click fills the clipboard with that message",
                  got in ("ping", "Hello world"), got)
    except Exception as e:
        skip("copy click fills the clipboard", f"clipboard unavailable: {e}")

# -------------------------------------------------------------------- copy
try:
    QApplication.clipboard().setText("sentinel-clear")
    win._on_link(QUrl("copy:0"))
    pump(0.2)
    got = QApplication.clipboard().text()
    if got == "sentinel-clear":
        skip("copy link fills clipboard", "clipboard not available in this session")
    else:
        check("copy link fills clipboard with message 0", got == "ping", got)

    QApplication.clipboard().setText("sentinel-clear")
    win._copy_last_reply()
    pump(0.2)
    got = QApplication.clipboard().text()
    if got == "sentinel-clear":
        skip("'Copy reply' fills clipboard", "clipboard not available in this session")
    else:
        check("'Copy reply' fills clipboard with last reply", got == "Hello world", got)
except Exception as e:
    skip("clipboard copy checks", f"clipboard unavailable: {e}")

# -------------------------------------------------- failed turn: no fake reply
def boom_send(self, messages, model="qwen3.7-plus", stream=True,
              system_message=None, files=None):
    yield "partial"
    raise RuntimeError("HTTP 500 simulated")


LocalAPIClient.send_message = boom_send
win.input_edit.setPlainText("again")
win._send_message()
win.worker.wait(5000)
pump(0.5)

# The user message of the failed turn STAYS (it really reached the site),
# only the fake assistant reply must not appear.
check("failed turn appends no assistant message",
      sum(1 for m in win.messages if m["role"] == "assistant") == 1
      and win.messages[-1] == {"role": "user", "content": "again"},
      win.messages)
check("failed turn shown as an error line in the log",
      "[Ошибка]" in win.log_view.toPlainText() and "HTTP 500 simulated" in win.log_view.toPlainText(),
      win.log_view.toPlainText()[-200:])
check("the failed turn leaves no fake reply in the transcript",
      "HTTP 500 simulated" not in win.chat_view.toHtml())
check("failed turn not logged to transcript",
      logged == [("user", "ping"), ("assistant", "Hello world")], logged)
check("failed turn stopped the activity watch dog",
      not win._activity_timer.isActive(), win.activity_label.text())
check("failed turn leaves a red pointer in the live line",
      "Оборот не удался" in win.activity_label.text()
      and ERR_COLOR in win.activity_label.styleSheet(), win.activity_label.text())

# ------------------------- bug 2: attachments go inline as base64 (no upload)
tmpdir = Path(tempfile.mkdtemp(prefix="qwen-gui-test-"))
sample = tmpdir / "note.txt"
sample.write_bytes(b"hello proxy")
empty = tmpdir / "empty.bin"
empty.write_bytes(b"")
gone = tmpdir / "gone.txt"  # deliberately never created


def fake_send2(self, messages, model="qwen3.7-plus", stream=True,
               system_message=None, files=None):
    sent_payloads.append({"messages": list(messages), "model": model, "files": files})
    self._chat_id = "test-chat-id-0123456789"
    self._parent_id = "resp-0123456789"
    yield "ok"


LocalAPIClient.send_message = fake_send2
err_notes_before = win.log_view.toPlainText().count("[Ошибка]")
win._pending_files.append({"path": str(sample), "name": sample.name,
                           "size": sample.stat().st_size})
win.input_edit.setPlainText("with file")
win._send_message()
win.worker.wait(5000)
pump(0.5)

sent_files = sent_payloads[-1]["files"]
check("file travelled as an inline base64 part (no upload round-trip)",
      sent_files and set(sent_files[0]) == {"fileName", "fileData", "fileType", "fileSize"},
      sent_files)
check("inline bytes survive the base64 round-trip",
      bool(sent_files) and base64.b64decode(sent_files[0]["fileData"]) == b"hello proxy"
      and sent_files[0]["fileSize"] == 11, sent_files)
check("MIME guessed from the file name",
      bool(sent_files) and sent_files[0]["fileType"] == "text/plain", sent_files)
check("pending list cleared after send", win._pending_files == [], win._pending_files)
check("attachment confirmed in the status bar",
      "Прикрепляю файлы к сообщению: 1" in win.statusBar().currentMessage(),
      win.statusBar().currentMessage())

win.input_edit.setPlainText("no file this time")
win._send_message()
win.worker.wait(5000)
pump(0.4)
check("a turn without files sends no files field",
      sent_payloads[-1]["files"] is None, sent_payloads[-1]["files"])

# -------- unreadable / empty / oversized files: skip the file, keep the turn
hints = []
from gui.app import WorkerThread

bad_worker = WorkerThread(
    win.api, [{"role": "user", "content": "x"}], "qwen3.7-plus", None,
    [{"path": str(gone), "name": "gone.txt", "size": 1},
     {"path": str(empty), "name": "empty.bin", "size": 0},
     {"path": str(sample), "name": sample.name, "size": 11}])
bad_worker.upload_status.connect(hints.append)
bad_worker.MAX_ATTACHMENT_BYTES = 4  # force the 11-byte sample over the limit
skipped = bad_worker._encode_files()
check("oversized file skipped with a size hint",
      skipped == [] and any("gone.txt" in h for h in hints)
      and any("empty.bin" in h for h in hints)
      and any("note.txt" in h and "МБ" in h for h in hints), hints)

missing_worker = WorkerThread(
    win.api, [{"role": "user", "content": "x"}], "qwen3.7-plus", None,
    [{"path": str(gone), "name": "gone.txt", "size": 1}])
missing_hints = []
missing_worker.upload_status.connect(missing_hints.append)
check("unreadable file yields no attachment and a read hint",
      missing_worker._encode_files() == []
      and any("gone.txt" in h and "Не удалось прочитать" in h for h in missing_hints),
      missing_hints)

# The GUI-level degrade path: the one file the user picked is gone by send time.
win._pending_files.append({"path": str(gone), "name": "gone.txt", "size": 1})
before_count = len(win.messages)
notes_before = win.log_view.toPlainText().count("[Ошибка]")
win.input_edit.setPlainText("file vanished")
win._send_message()
win.worker.wait(5000)
pump(0.5)

check("vanished attachment does not abort the turn",
      len(win.messages) == before_count + 2 and win.messages[-1]["role"] == "assistant"
      and win.messages[-2] == {"role": "user", "content": "file vanished"},
      win.messages)
check("vanished attachment sends the text with no files",
      sent_payloads[-1]["files"] is None, sent_payloads[-1]["files"])
check("vanished attachment surfaced as a hint, not an error",
      "gone.txt" in win.statusBar().currentMessage()
      and win.log_view.toPlainText().count("[Ошибка]") == notes_before,
      win.statusBar().currentMessage())
check("send button re-enabled after the attachment turns", win.send_btn.isEnabled())
check("attachment turns added no error line",
      win.log_view.toPlainText().count("[Ошибка]") == err_notes_before,
      win.log_view.toPlainText().count("[Ошибка]"))

# ------------- bug 3 (client side): heartbeat comments count as liveness
captured = {}


class FakeResponse:
    """The slice of requests.Response that send_message actually touches.

    status_code/json()/close() are part of the real contract of every
    requests.Response, streamed or not, so the fake carries them too: the
    client reads status_code before iter_lines() to pull the refusal sentence
    out of a failed turn's JSON body.
    """

    def __init__(self, lines, status_code=200, body_json=None):
        self._lines = lines
        self.status_code = status_code
        self._body_json = body_json
        self.closed = False

    def json(self):
        if self._body_json is None:
            # Real responses raise on a body that is not JSON (e.g. an HTML
            # error page), which is the case the client must survive.
            raise ValueError("no JSON body")
        return self._body_json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f"{self.status_code} Client Error ... for url ...")

    def close(self):
        self.closed = True

    def iter_lines(self):
        return iter(self._lines)


class StubRequests:
    """Minimal stand-in for the requests module used by api_client."""

    exceptions = requests.exceptions

    def __init__(self, lines, status_code=200, body_json=None):
        self.lines = lines
        self.status_code = status_code
        self.body_json = body_json
        self.response = None

    def post(self, url, json=None, timeout=None, stream=None, **kw):
        captured["payload"] = json
        self.response = FakeResponse(self.lines, self.status_code, self.body_json)
        return self.response


ping_only = [b": ping", b"", b": ping\n"]
api_module.requests = StubRequests(ping_only)
try:
    probe = LocalAPIClient()
    probe.last_activity = 0.0
    probe.got_first_token = False
    chunks = list(real_send(probe, [{"role": "user", "content": "hi"}],
                            files=[{"fileName": "note.txt"}]))
    check("heartbeat-only stream yields no text", chunks == [], chunks)
    check("heartbeat comment proves the turn is alive",
          probe.got_first_token is True and probe.last_activity > 0.0,
          (probe.got_first_token, probe.last_activity))
    check("files field forwarded to the server verbatim",
          captured["payload"]["files"] == [{"fileName": "note.txt"}], captured["payload"])
    check("no chat id asks the server for a fresh chat",
          captured["payload"].get("newChat") is True, captured["payload"])

    api_module.requests = StubRequests([
        b": ping",
        b"data: " + json.dumps({"choices": [{"delta": {"content": "hi"}}],
                                "chatId": "cid-1", "parentId": "pid-1"}).encode(),
        b"data: [DONE]",
    ])
    probe2 = LocalAPIClient()
    got_text = "".join(real_send(probe2, [{"role": "user", "content": "hi"}]))
    check("delta content still parsed after a heartbeat", got_text == "hi", got_text)
    check("continuation ids cached from the meta chunk",
          probe2.chat_id == "cid-1" and probe2.parent_id == "pid-1",
          (probe2.chat_id, probe2.parent_id))

    # A refusal of the whole turn (over the document cap, over the size cap)
    # comes back as JSON whose "detail" is the sentence saying what to change.
    # The GUI shows str(exception), so that sentence — not requests' "413
    # Client Error ... for url ..." plumbing text — is what has to surface.
    limit_detail = ("Qwen accepts 5 documents per message, this request carries 6: "
                    "split them into at least 2 turns (pictures are not counted "
                    "in the limit).")
    refused = StubRequests([], status_code=413, body_json={"detail": limit_detail})
    api_module.requests = refused
    refused_error = ""
    try:
        list(real_send(LocalAPIClient(), [{"role": "user", "content": "hi"}],
                       files=[{"fileName": f"n{i}.txt"} for i in range(6)]))
    except Exception as exc:
        refused_error = str(exc)
    check("a refused turn surfaces the server's own detail sentence",
          refused_error == limit_detail, refused_error)
    check("a refused turn closes the streamed response",
          refused.response is not None and refused.response.closed,
          refused.response)

    # Nothing readable to show (an HTML error page from a proxy, for instance):
    # the plain raise_for_status() path is untouched, so no failure is swallowed.
    bare = StubRequests([], status_code=500)
    api_module.requests = bare
    bare_error = ""
    try:
        list(real_send(LocalAPIClient(), [{"role": "user", "content": "hi"}]))
    except Exception as exc:
        bare_error = type(exc).__name__
    check("an error response with no detail still raises",
          bare_error == "HTTPError", bare_error)
finally:
    api_module.requests = requests

# --------------- bug 3 (widget side): the three states of the activity line
win._turn_busy = True
win._turn_started = time.monotonic() - 5
win.api.last_activity = time.monotonic()
win.api.got_first_token = False
win._tick_activity()
check("no token yet says the Russian waiting line",
      "Жду первый токен" in win.activity_label.text()
      and ERR_COLOR not in win.activity_label.styleSheet(),
      win.activity_label.text())

win.api.got_first_token = True
win._tick_activity()
check("tokens flowing says the Russian thinking line",
      win.activity_label.text().startswith("Думает"), win.activity_label.text())

win.api.last_activity = time.monotonic() - 31
win._tick_activity()
check("30s of silence turns the line red and says it may be stuck",
      "зависло" in win.activity_label.text()
      and ERR_COLOR in win.activity_label.styleSheet(),
      (win.activity_label.text(), win.activity_label.styleSheet()))

win._activity_timer.start()
win._turn_busy = False
win._tick_activity()
check("an idle window clears the activity line and the timer",
      win.activity_label.text() == "" and not win._activity_timer.isActive(),
      (win.activity_label.text(), win._activity_timer.isActive()))

check("send button re-enabled at the end", win.send_btn.isEnabled())

# ------------- attachment turn: ": progress ..." comments narrate the staging
STEP_BUSY = "Сайт читает файлы: занято 2 из 3 карточек, 4 с"
STEP_BUSY_LATER = "Сайт читает файлы: занято 1 из 3 карточек, 6 с"


class SniffResponse:
    """Canned SSE lines that hand back control after every processed line, which
    is the only moment the client-side staging slot can be observed."""

    def __init__(self, api, lines):
        self._api = api
        self._lines = lines
        self.seen = []
        # A real requests.Response always reports a status_code; send_message
        # reads it before it starts iterating the stream.
        self.status_code = 200

    def raise_for_status(self):
        return None

    def close(self):
        return None

    def iter_lines(self):
        for idx, line in enumerate(self._lines):
            yield line
            self.seen.append((idx, self._api.progress))


class StubProgressRequests:
    exceptions = requests.exceptions

    def __init__(self, api, lines):
        self._api = api
        self._lines = lines
        self.response = None

    def post(self, url, json=None, timeout=None, stream=None, **kw):
        captured["payload"] = json
        self.response = SniffResponse(self._api, self._lines)
        return self.response


chunk_line = lambda text: b"data: " + json.dumps(
    {"choices": [{"delta": {"content": text}}]}).encode("utf-8")

api_p = LocalAPIClient()
api_p.progress = "stale step left over from the previous turn"
stub_p = StubProgressRequests(api_p, [
    b": ping",
    b": progress " + "Прикрепляю 2 файла".encode("utf-8"),
    b": progress " + STEP_BUSY.encode("utf-8"),
    chunk_line("hi"),
    b": ping",
    chunk_line("!"),
    b"data: [DONE]",
])
api_module.requests = stub_p
try:
    got_p = "".join(real_send(api_p, [{"role": "user", "content": "hi"}]))
finally:
    api_module.requests = requests

progress_slots = dict(stub_p.response.seen)
check("progress comments never reach the answer text", got_p == "hi!", got_p)
check("a staging step is dropped before the request starts",
      progress_slots.get(0) is None, stub_p.response.seen)
check("a progress comment sets the staging step",
      progress_slots.get(1) == "Прикрепляю 2 файла"
      and progress_slots.get(2) == STEP_BUSY, stub_p.response.seen)
check("the first content chunk clears the staging step",
      progress_slots.get(3) is None and api_p.progress is None, stub_p.response.seen)

# --------------- the same steps on screen: one log line per phase, live line
log_lines = lambda: win.log_view.toPlainText().splitlines()

win._turn_busy = True
win._turn_started = time.monotonic() - 12
win.api.last_activity = time.monotonic()
win.api.got_first_token = False
win._last_progress = None
notes0 = len(log_lines())

win.api.progress = "Прикрепляю 2 файла"
win._tick_activity()
check("a staging step is written into the system log",
      len(log_lines()) == notes0 + 1 and any("Прикрепляю 2 файла" in l for l in log_lines()),
      log_lines()[-1] if log_lines() else "")
check("the activity line shows the live step and the elapsed turn",
      "Прикрепляю 2 файла" in win.activity_label.text()
      and "всего 12 с" in win.activity_label.text(), win.activity_label.text())

win.api.progress = STEP_BUSY
win._tick_activity()
check("a new phase adds another log line",
      len(log_lines()) == notes0 + 2 and any(STEP_BUSY in l for l in log_lines()),
      (len(log_lines()), log_lines()[-1] if log_lines() else ""))

win.api.progress = STEP_BUSY_LATER
win._tick_activity()
check("the busy phase is not repeated while only the seconds move",
      len(log_lines()) == notes0 + 2
      and sum("Сайт читает файлы" in l for l in log_lines()) == 1
      and "занято 1 из 3" in win.activity_label.text(),
      (len(log_lines()), win.activity_label.text()))

win.api.last_activity = time.monotonic() - 31
win._tick_activity()
check("the stuck warning still outranks a staging step",
      "зависло" in win.activity_label.text()
      and ERR_COLOR in win.activity_label.styleSheet(), win.activity_label.text())

win.api.last_activity = time.monotonic()
win.api.got_first_token = True
win.api.progress = None
win._tick_activity()
check("the line falls back to the thinking caption once the step is over",
      win.activity_label.text().startswith("Думает"), win.activity_label.text())
win._turn_busy = False
win._tick_activity()

# ---------------- the five-document cap: refuse the sixth, exempt the pictures
import gui.app as gui_app


class FakeDialog:
    """QFileDialog stand-in: hands back the queued paths, then presses Cancel."""

    queue = []

    @staticmethod
    def getOpenFileName(*args, **kwargs):
        return (FakeDialog.queue.pop(0) if FakeDialog.queue else ""), ""


docs = [tmpdir / f"doc{i}.txt" for i in range(1, 7)]
pics = [tmpdir / f"pic{i}.png" for i in range(1, 3)]
for path in docs + pics:
    path.write_bytes(b"x")

real_dialog = gui_app.QFileDialog
gui_app.QFileDialog = FakeDialog
try:
    win._pending_files = []
    FakeDialog.queue = [str(p) for p in docs + pics]
    for _ in range(5):
        win._attach_file()
    check("five documents are accepted and counted on screen",
          len(win._pending_files) == 5 and "документов 5/5" in win.file_status_label.text(),
          win.file_status_label.text())

    win._attach_file()
    check("the sixth document is refused with the site's limit named",
          len(win._pending_files) == 5
          and "не более 5 документов" in win.file_status_label.text()
          and "doc6.txt" in win.file_status_label.text()
          and "doc6.txt" not in str(win._pending_files),
          (win._pending_files, win.file_status_label.text()))

    for _ in range(2):
        win._attach_file()
    check("pictures stay outside the five-document limit",
          len(win._pending_files) == 7 and win._document_count(win._pending_files) == 5
          and "документов 5/5" in win.file_status_label.text(),
          (win._pending_files, win.file_status_label.text()))

    win._attach_file()  # dialog returns Cancel
    check("a cancelled dialog changes nothing",
          len(win._pending_files) == 7, win._pending_files)
finally:
    gui_app.QFileDialog = real_dialog
    win._pending_files = []

print("RESULT:", "ALL PASS" if all(results) else f"{results.count(False)} FAILED",
      f"({len(results) - results.count(False)} passed, {results.count(False)} failed, {len(skips)} skipped)")
sys.exit(0 if all(results) else 1)
