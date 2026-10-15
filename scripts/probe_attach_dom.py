"""Dump the real attachment DOM after dropping a .txt and a .png.

Usage:
    set "PYTHONIOENCODING=utf-8" && venv\\Scripts\\python.exe -u scripts\\probe_attach_dom.py [ticks]

Our attachment oracle counts `.file-card-list .vision-item-container`, a class that
provenly appears for images. It reported 0 cards for one 19 KB .txt for 45 straight
seconds, which means either the drop was refused or documents mount a different
markup. Production decides "the file never attached" from that count and then sends a
text-only turn, so the selector has to be settled from the real DOM, not guessed.

The probe drops one .txt and one .png, then every 3 s dumps the visible class inventory
plus the composer container's outerHTML (truncated), and tries to type a sentinel to
see when the SPA starts accepting text while it reads the document.
"""
import asyncio
import json
import struct
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.client.qwen_client import QwenClient, _js_composer  # noqa: E402

WORDS = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo "
         "lima mike november oscar papa quebec romeo sierra tango uniform "
         "victor whiskey xray yankee zulu ").split()
SENTINEL = "QQZ"

JS_DOM = _js_composer(r"""
var out = {};
out.elems = [].slice.call(document.querySelectorAll('[class*="file"],[class*="upload"],[class*="attach"],'
            + '[class*="preview"],[class*="thumb"],[class*="card"],[class*="vision"],[class*="doc"]'))
  .filter(function(e){ return e.offsetParent; }).slice(0, 30).map(function(e){
    return e.tagName + '.' + String(e.className).replace(/\s+/g, ' ').slice(0, 55)
         + '>k' + e.children.length
         + '|' + (e.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 45);
  });
var c = findComposer();
if (!c) { out.comp = 'none'; return JSON.stringify(out); }
out.comp = c.tag;
out.len = String(c.el.value !== undefined ? c.el.value : (c.el.textContent || '')).length;
var box = c.el;
for (var i = 0; i < 5 && box.parentElement; i++) {
  box = box.parentElement;
  if ((box.innerText || '').length > 12 || box.children.length > 2) break;
}
out.html = (box.outerHTML || '').replace(/\s+/g, ' ').slice(0, 1500);
return JSON.stringify(out);
""")

JS_TYPE_TEST = _js_composer(r"""
var c = findComposer();
if (!c) return 'no-composer';
var el = c.el, isTA = el.tagName === 'TEXTAREA';
function read(){ return String(isTA ? (el.value || '') : (el.textContent || '')); }
var before = read();
var set = null;
if (isTA) {
  var d = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value');
  set = d ? d.set : null;
}
function put(v){
  if (isTA) { if (set) { set.call(el, v); } else { el.value = v; }
    el.dispatchEvent(new Event('input', { bubbles: true })); }
  else { el.textContent = v;
    el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: v.slice(-1) })); }
}
put(before + 'QQZ');
var stuck = read().indexOf('QQZ') >= 0;
put(before);
return (stuck ? 'accepts' : 'refuses') + ':len=' + before.length;
""")


def png_bytes(w: int, h: int, rgb=(220, 40, 40)) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        head = tag + data
        return struct.pack(">I", len(data)) + head + struct.pack(">I", zlib.crc32(head) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def stage():
    txt = Path("probe_fat_1.txt")
    txt.write_text(" ".join(WORDS * 120) + " emerald-742 chunk1\n", encoding="utf-8")
    img = Path("probe_dom.png")
    img.write_bytes(png_bytes(400, 300))
    print("staged:", txt.stat().st_size, img.stat().st_size, flush=True)
    return [str(txt), str(img)]


async def main():
    ticks = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    paths = stage()
    client = QwenClient()
    await client._safely(client._w.ensure)
    t0 = time.monotonic()
    try:
        for p in paths:
            ok = await client._safely(client._w.ui_attach, [p])
            print(f"[{time.monotonic() - t0:6.1f}s] ui_attach({Path(p).name}) -> {ok}", flush=True)
        for i in range(ticks):
            raw = str(await client._safely(client._w._js, JS_DOM) or "")
            typed = str(await client._safely(client._w._js, JS_TYPE_TEST) or "")
            try:
                d = json.loads(raw)
                print(f"[{time.monotonic() - t0:6.1f}s] comp={d.get('comp')} len={d.get('len')} "
                      f"type={typed} k={len(d.get('elems') or [])}", flush=True)
                if i in (0, 1, 2, 3, 5, 8, 12, 16):
                    print(f"          html: {str(d.get('html'))[:700]}", flush=True)
                    for line in (d.get("elems") or [])[:18]:
                        print(f"            · {line}", flush=True)
            except Exception:  # noqa: BLE001
                print(f"[{time.monotonic() - t0:6.1f}s] RAW {raw[:400]} type={typed}", flush=True)
            await asyncio.sleep(3.0)
    finally:
        await client.shutdown()


asyncio.run(main())
