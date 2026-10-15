"""Send one turn with a PDF attached and print what came back.

Usage:
    venv\\Scripts\\python.exe -u scripts\\send_probe_file.py [message text]

send_probe_image.py proved the composer's drop channel carries pictures. Whether
it carries documents is a separate question the site never answered for us, so
this probe puts a made-up keyword inside a hand-built PDF and asks for it back.
A keyword in the reply is the evidence the bytes reached the model; "no file" in
the reply plus "Attachments never reached the SPA" in the server log is the
evidence they did not.
"""
import base64
import json
import sys

import requests

BASE = "http://127.0.0.1:8000"
KEYWORD = "emerald-742"


def minimal_pdf(text: str) -> bytes:
    """A one-page PDF built byte by byte: no PDF library is installed here."""
    content = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode("ascii", "replace")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for num, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n").encode()
    return bytes(out)


def main():
    text = (sys.argv[1] if len(sys.argv) > 1
            else "Какое кодовое слово написано в приложенном файле? Ответь одним словом.")
    raw = minimal_pdf(f"Keyword: {KEYWORD}")
    with open("probe_keyword.pdf", "wb") as fh:
        fh.write(raw)
    payload = {
        "model": "plus",
        "messages": [{"role": "user", "content": text}],
        "stream": True,
        "newChat": True,
        "files": [{
            "fileName": "probe_keyword.pdf",
            "fileData": base64.b64encode(raw).decode(),
            "fileType": "application/pdf",
            "fileSize": len(raw),
        }],
    }
    print(f"POST /v1/chat/completions file={len(raw)}B keyword={KEYWORD}")
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
    print("VERDICT:", "pdf reached the model" if KEYWORD in full else "pdf did NOT reach the model")
    return 0


sys.exit(main())
