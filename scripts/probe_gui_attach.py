"""Drive the real GUI window against the real server and attach a real file.

Usage (server must already be running on 127.0.0.1:8000):
    set "PYTHONIOENCODING=utf-8" && venv\\Scripts\\python.exe -u scripts\\probe_gui_attach.py

send_probe_*.py talk to the HTTP API directly. This probe goes through the GUI
instead, because that is the path the user actually clicks: Attach -> QFileDialog
-> _pending_files -> WorkerThread -> base64 -> /v1/chat/completions. Only the file
dialog is replaced (nobody is there to click it); everything else is the window's
own code, and no HTTP layer is stubbed, so the reply can only come from the site.
"""
import os
import struct
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFileDialog  # noqa: E402

import gui.app as gui_app  # noqa: E402

PNG_PATH = Path("probe_gui_red.png")
QUESTION = "Что на картинке? Назови цвет одним словом."


def png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + tag + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))


def red_square_png(size: int = 24) -> bytes:
    rows = b"".join(b"\x00" + b"\xff\x00\x00" * size for _ in range(size))
    return (b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + png_chunk(b"IDAT", zlib.compress(rows, 9))
            + png_chunk(b"IEND", b""))


def main():
    PNG_PATH.write_bytes(red_square_png())
    print(f"staged {PNG_PATH} ({PNG_PATH.stat().st_size} bytes)")

    app = QApplication.instance() or QApplication([])

    def pump(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.02)

    # The Attach button opens a modal dialog; hand it our probe file instead.
    real_dialog = QFileDialog.getOpenFileName
    QFileDialog.getOpenFileName = staticmethod(
        lambda *a, **k: (str(PNG_PATH.resolve()), ""))

    win = gui_app.MainWindow()
    pump(2.0)
    win.model_combo.setCurrentText("qwen3.7-plus")

    win._attach_file()
    QFileDialog.getOpenFileName = real_dialog
    attached = [f["name"] for f in win._pending_files]
    print(f"after Attach: pending={attached} "
          f"label={win.file_status_label.text()!r}")
    if attached != [PNG_PATH.name]:
        print("VERDICT: the Attach button did not accept the file")
        return 1

    win.input_edit.setPlainText(QUESTION)
    win.send_btn.click()
    print("clicked Send, waiting for the reply...", flush=True)

    deadline = time.monotonic() + 300
    hints = []
    while time.monotonic() < deadline:
        pump(0.25)
        msg = win.statusBar().currentMessage()
        if msg and (not hints or hints[-1] != msg):
            hints.append(msg)
            print("  status:", msg, flush=True)
        done = (len(win.messages) >= 2
                and win.messages[-1].get("role") == "assistant"
                and win.messages[-1].get("content", "").strip()
                and not win._turn_busy)
        if done:
            break
        if win._turn_failed:
            print("the GUI reported a failed turn")
            break

    reply = win.messages[-1].get("content", "") if len(win.messages) >= 2 else ""
    print(f"turn busy={win._turn_busy} failed={win._turn_failed}")
    print(f"chat_id={win.api.chat_id} parent_id={win.api.parent_id}")
    print(f"GUI reply: {reply.strip()[:400]}")
    ok = "красн" in reply.lower() or "red" in reply.lower()
    print("VERDICT:", "the GUI's file reached the model" if ok
          else "the model answered without seeing the file")
    return 0 if ok else 1


sys.exit(main())
