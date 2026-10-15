"""Send one turn with a tiny PNG attached and print what came back.

Usage:
    venv\\Scripts\\python.exe -u scripts\\send_probe_image.py [message text]

The point is to exercise the attachment path end to end (inline base64 ->
server temp file -> SPA file input) against the running server and show the
reply, so the server log can be lined up with what the model actually saw.
"""
import base64
import json
import sys
import zlib
import struct
import requests

BASE = "http://127.0.0.1:8000"


def png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + tag + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))


def red_square_png(size: int = 24) -> bytes:
    """A solid-red PNG built by hand: no Pillow, no asset files in the repo."""
    rows = b"".join(b"\x00" + b"\xff\x00\x00" * size for _ in range(size))
    return (b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + png_chunk(b"IDAT", zlib.compress(rows, 9))
            + png_chunk(b"IEND", b""))


def main():
    text = sys.argv[1] if len(sys.argv) > 1 else "Что на этой картинке? Назови цвет одним словом."
    raw = red_square_png()
    png_path = "probe_red.png"
    with open(png_path, "wb") as fh:
        fh.write(raw)
    payload = {
        "model": "plus",
        "messages": [{"role": "user", "content": text}],
        "stream": True,
        "newChat": True,
        "files": [{
            "fileName": "probe_red.png",
            "fileData": base64.b64encode(raw).decode(),
            "fileType": "image/png",
            "fileSize": len(raw),
        }],
    }
    print(f"POST /v1/chat/completions file={len(raw)}B b64="
          f"{len(payload['files'][0]['fileData'])} chars")
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
    print("meta:", json.dumps(meta, ensure_ascii=False))
    print("reply:", full.strip()[:600])
    return 0


sys.exit(main())
