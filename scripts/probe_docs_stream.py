"""Drive the running server through a real attachment turn and print raw SSE.

The GUI talks to this same endpoint, so the stream printed here is exactly what
gui/api_client.py has to parse: ": ping" heartbeats, the ": progress ..." staging
comments of an attachment turn, the data: deltas, the meta chunk, [DONE]. The
answer text is checked against markers planted in the documents, so a green run
means the site really read the files, not just that it accepted a request.

Run the server first (start.bat), then:
    venv\Scripts\python.exe -u scripts\probe_docs_stream.py six     # expect 413
    venv\Scripts\python.exe -u scripts\probe_docs_stream.py fat     # one 400 KB doc
    venv\Scripts\python.exe -u scripts\probe_docs_stream.py five    # five documents
"""
import base64
import json
import os
import sys
import time

import requests

BASE = "http://127.0.0.1:8000"
MODEL = "qwen3.8-max"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIMEOUT = 300

PARA = ("Квен читает приложенный документ по своей карточке, и пока карточка "
        "крутится, поле ввода ещё не готово к отправке текста. ")


def make_doc(name: str, number: int, kb: int) -> str:
    """Document whose head and tail both carry the control number."""
    head = f"КОНТРОЛЬНОЕ ЧИСЛО ДОКУМЕНТА {name}: {number}\n\n"
    tail = f"\n\nКОНЕЦ ДОКУМЕНТА {name}. КОНТРОЛЬНОЕ ЧИСЛО: {number}.\n"
    step = len(PARA.encode("utf-8"))
    body = PARA * max(1, int(kb * 1024 / step))
    path = os.path.join(ROOT, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(head + body + tail)
    return path


def part(path: str) -> dict:
    with open(path, "rb") as fh:
        raw = fh.read()
    return {
        "fileName": os.path.basename(path),
        "fileData": base64.b64encode(raw).decode("ascii"),
        "fileType": "text/plain",
        "fileSize": len(raw),
    }


PROMPT = ("Перечисли через запятую все контрольные числа, указанные в приложенных "
          "документах, в порядке вложения. Ответь только числами, без пояснений.")


def ask(files: list[dict]) -> tuple[int, str, str, list[str]]:
    """Returns (http status, body-or-stream text, answer text, progress steps)."""
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": True,
        "newChat": True,
        "files": files,
    }
    t0 = time.monotonic()
    response = requests.post(f"{BASE}/v1/chat/completions", json=payload,
                             stream=True, timeout=TIMEOUT)
    if response.status_code != 200:
        return response.status_code, response.text, "", []

    answer = ""
    steps: list[str] = []
    for raw in response.iter_lines():
        if not raw:
            continue
        line = raw.decode("utf-8", errors="replace")
        stamp = f"[{time.monotonic() - t0:6.1f}s]"
        if line.startswith(": progress "):
            step = line[len(": progress "):].strip()
            steps.append(step)
            print(f"{stamp} PROGRESS {step}", flush=True)
            continue
        if line.startswith(":"):
            print(f"{stamp} comment   {line}", flush=True)
            continue
        if not line.startswith("data: "):
            print(f"{stamp} raw       {line}", flush=True)
            continue
        body = line[6:]
        if body == "[DONE]":
            print(f"{stamp} DONE", flush=True)
            break
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            print(f"{stamp} bad json  {body[:200]}", flush=True)
            continue
        if data.get("error"):
            print(f"{stamp} ERROR     {data['error']}", flush=True)
            continue
        if "chatId" in data and "parentId" in data and "choices" not in data:
            print(f"{stamp} meta      chat={data['chatId']} parent={data['parentId']}",
                  flush=True)
            continue
        delta = (data.get("choices") or [{}])[0].get("delta") or {}
        text = delta.get("content") or ""
        if text:
            answer += text
            print(f"{stamp} token     {text!r}", flush=True)
    return 200, "", answer, steps


def show(steps: list[str], answer: str) -> None:
    print(f"\nsteps: {len(steps)}")
    for step in steps:
        print(f"  - {step}")
    print(f"answer: {answer.strip()[:400]}")


def case_six() -> int:
    files = [part(make_doc(f"probe_cap6_{i}.txt", 600 + i, 20)) for i in range(1, 7)]
    status, body, answer, steps = ask(files)
    print(f"status: {status}")
    print(f"body: {body[:600]}")
    show(steps, answer)
    ok = status == 413 and "5 documents per message" in body
    print("\nsix documents refused with the site's limit named:", "OK" if ok else "FAIL")
    return 0 if ok else 1


def case_fat() -> int:
    files = [part(make_doc("probe_fat_stream.txt", 4719, 400))]
    status, body, answer, steps = ask(files)
    print(f"status: {status}")
    if body:
        print(f"body: {body[:600]}")
    show(steps, answer)
    ok = status == 200 and "4719" in answer and bool(steps)
    print("\nfat document read and the turn narrated:", "OK" if ok else "FAIL")
    return 0 if ok else 1


def case_five() -> int:
    numbers = [7301, 7302, 7303, 7304, 7305]
    files = [part(make_doc(f"probe_five_{i}.txt", n, 120))
             for i, n in enumerate(numbers, 1)]
    status, body, answer, steps = ask(files)
    print(f"status: {status}")
    if body:
        print(f"body: {body[:600]}")
    show(steps, answer)
    missing = [n for n in numbers if str(n) not in answer]
    ok = status == 200 and not missing and bool(steps)
    print("five documents: all control numbers came back:",
          "OK" if ok else f"FAIL (missing {missing})")
    return 0 if ok else 1


def main() -> int:
    which = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
    cases = {"six": case_six, "fat": case_fat, "five": case_five}
    if which not in cases:
        print(__doc__)
        return 2
    return cases[which]()


if __name__ == "__main__":
    sys.exit(main())
