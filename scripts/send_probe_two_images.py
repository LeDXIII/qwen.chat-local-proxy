"""Send one turn with TWO images attached and print what came back.

Usage:
    venv\\Scripts\\python.exe -u scripts\\send_probe_two_images.py

The GUI keeps every picked file in a list, so several attachments per turn is a
user-reachable path, and the new drop hands them all over in a single
DataTransfer. Whether the site uploads every file in one drop or only the first
is exactly the kind of thing that has already burned us once, so this probe asks
for both colours and only accepts both.
"""
import base64
import json
import struct
import sys
import zlib

import requests

BASE = "http://127.0.0.1:8000"


def png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + tag + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))


def solid_png(rgb: tuple, size: int = 24) -> bytes:
    rows = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))
    return (b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + png_chunk(b"IDAT", zlib.compress(rows, 9))
            + png_chunk(b"IEND", b""))


def main():
    images = [("probe_a.png", (255, 0, 0)), ("probe_b.png", (0, 0, 255))]
    files = []
    for name, rgb in images:
        raw = solid_png(rgb)
        with open(name, "wb") as fh:
            fh.write(raw)
        files.append({"fileName": name,
                      "fileData": base64.b64encode(raw).decode(),
                      "fileType": "image/png",
                      "fileSize": len(raw)})
    payload = {
        "model": "plus",
        "messages": [{"role": "user",
                      "content": "Сколько тут картинок и какого они цвета? "
                                 "Назови число и каждый цвет одним словом."}],
        "stream": True,
        "newChat": True,
        "files": files,
    }
    print(f"POST /v1/chat/completions files={len(files)}")
    r = requests.post(f"{BASE}/v1/chat/completions", json=payload, stream=True, timeout=300)
    print("HTTP", r.status_code)
    if r.status_code != 200:
        print(r.text[:600])
        return 1
    full = ""
    meta = {}
    for line in r.iter_lines():
        if not line:
            continue
        ls = line.decode("utf-8", "replace")
        if ls.startswith(":"):
            print("  [heartbeat]", flush=True)
            continue
        if not ls.startswith("data: "):
            continue
        ds = ls[6:]
        if ds == "[DONE]":
            break
        try:
            d = json.loads(ds)
        except json.JSONDecodeError:
            continue
        if d.get("chatId"):
            meta["chatId"] = d["chatId"]
        if d.get("parentId"):
            meta["parentId"] = d["parentId"]
        for ch in d.get("choices") or []:
            full += (ch.get("delta") or {}).get("content") or ""
    low = full.lower()
    both = ("красн" in low or "red" in low) and ("син" in low or "blue" in low)
    print("meta:", json.dumps(meta, ensure_ascii=False))
    print("reply:", full.strip()[:600])
    print("VERDICT:", "both images reached the model" if both
          else "at least one image did NOT reach the model")
    return 0


sys.exit(main())
