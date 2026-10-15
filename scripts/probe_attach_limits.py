"""One Chrome session, all the attachment facts the fix needs.

Usage:
    set "PYTHONIOENCODING=utf-8" && venv\\Scripts\\python.exe -u scripts\\probe_attach_limits.py [wait_s]

The old oracle counted `.file-card-list .vision-item-container`. A DOM dump proved that
is the IMAGE card; a .txt mounts `.file-card-list .fileitem-btn` instead, so every
document attach read as "never attached" for ~90 s, the retry re-dropped the same file
(two identical cards appeared) and production then sent a text-only turn. That is the
bug the user reported, so everything below counts the UNION selector `.fileitem-btn,
.vision-item-container`:

  A. one .txt: does NATIVE typing stick while the site reads it (production types with
     real keystrokes), then type + send for real and confirm a turn went out;
  B. five .txt in one drop: does the site take all five (the user says the cap is 5);
  C. seven .txt in one drop: refused entirely, clamped, or all seven mounted.

Durations are printed per phase because the wait we have to build is size-dependent.
"""
import asyncio
import json
import struct
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from src.client.qwen_client import (  # noqa: E402
    JS_MARK_COMPOSER,
    QwenClient,
    _js_composer,
)

WORDS = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo "
         "lima mike november oscar papa quebec romeo sierra tango uniform "
         "victor whiskey xray yankee zulu ").split()

JS_CARDS = "return " + r"""
var cards = [].slice.call(document.querySelectorAll(
  '.file-card-list .fileitem-btn, .file-card-list .vision-item-container'));
return JSON.stringify({
  n: cards.length,
  names: cards.slice(0, 9).map(function(e){ return (e.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 30); }),
  busy: [].slice.call(document.querySelectorAll('.file-card-list *')).filter(function(e){
      return e.offsetParent && /progress|upload|loading|spin|percent|process/i.test(String(e.className));
    }).slice(0, 4).map(function(e){ return e.tagName + '.' + String(e.className).replace(/\s+/g, ' ').slice(0, 46); }),
  btn: cards.length ? (function(){ var b = cards[0].querySelector('button');
      return b ? ('dis=' + (b.disabled ? 'y' : 'n') + ':ad=' + (b.getAttribute('aria-disabled') || '-')) : '-'; })() : '-',
  html: cards.length ? String(cards[0].outerHTML).replace(/\s+/g, ' ').slice(0, 1300) : '',
});
"""

STATE = _js_composer(r"""
var out = {};
var c = findComposer();
out.len = c ? String(c.el.value !== undefined ? c.el.value : (c.el.textContent || '')).length : -1;
out.send = [].slice.call(document.querySelectorAll('button')).filter(function(b){
  return /send|отправ/i.test((b.getAttribute('aria-label') || '') + ' ' + (b.getAttribute('data-testid') || '')) && b.offsetParent;
}).map(function(b){ return (b.getAttribute('aria-label') || '').slice(0, 14) + ':dis=' + (b.disabled ? 'y' : 'n'); });
return JSON.stringify(out);
""")


def png_bytes(w: int, h: int, rgb=(40, 120, 220)) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        head = tag + data
        return struct.pack(">I", len(data)) + head + struct.pack(">I", zlib.crc32(head) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def stage(n: int, tag: str):
    paths = []
    for i in range(1, n + 1):
        p = Path(f"probe_lim_{tag}_{i}.txt")
        p.write_text(" ".join(WORDS * 120) + f" emerald-742 {tag}{i}\n", encoding="utf-8")
        paths.append(str(p))
    return paths


def cards(w):
    try:
        return json.loads(str(w._js(JS_CARDS)))
    except Exception:  # noqa: BLE001
        return {"n": -1, "names": [], "busy": [], "btn": "-", "html": ""}


def state(w):
    try:
        return json.loads(str(w._js(STATE)))
    except Exception:  # noqa: BLE001
        return {"len": -1, "send": []}


def native_type(w, text: str):
    """Type the way production does — real keystrokes into the marked composer."""
    w._js(JS_MARK_COMPOSER)
    try:
        el = w.driver.find_element(By.CSS_SELECTOR, "[data-qwproxy]")
        el.send_keys(Keys.CONTROL, "a")
        el.send_keys(Keys.DELETE)
        if text:
            el.send_keys(text)
    except Exception as exc:  # noqa: BLE001
        return f"err:{type(exc).__name__}"
    return state(w).get("len")


def new_chat(w):
    """go_home() is the real new-chat entry point: driver.get(BASE + '/')."""
    try:
        return f"go_home={w.go_home()}"
    except Exception as exc:  # noqa: BLE001
        return f"err:{type(exc).__name__}"


async def main():
    wait_s = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    Path("probe_lim_img.png").write_bytes(png_bytes(420, 300))
    client = QwenClient()
    await client._safely(client._w.ensure)

    async def call(fn, *args):
        return await client._safely(fn, *args)

    async def drop(paths):
        t0 = time.monotonic()
        ok = await client._safely(client._w.ui_attach, paths)
        return ok, time.monotonic() - t0

    try:
        print("=== A: one .txt, native typing while the site reads it ===", flush=True)
        print("  new chat:", await call(new_chat, client._w), flush=True)
        ok, dt = await drop(stage(1, "a"))
        print(f"  ui_attach(1 txt) -> {ok} in {dt:.1f}s (old oracle counts images only)", flush=True)
        dumped = False
        ready = None
        t0 = time.monotonic()
        while time.monotonic() - t0 < wait_s:
            d = await call(cards, client._w)
            st = await call(state, client._w)
            typed = await call(native_type, client._w, "QQZ")
            cleared = await call(native_type, client._w, "")
            el = round(time.monotonic() - t0, 1)
            print(f"  [{el:5.1f}s] cards={d['n']} busy={len(d['busy'])} btn={d['btn']} "
                  f"len={st['len']} typed={typed} cleared={cleared} send={st['send']}", flush=True)
            if not dumped and d["n"] > 0:
                dumped = True
                print(f"    html : {d['html'][:1200]}", flush=True)
                print(f"    names: {d['names']}", flush=True)
                print(f"    busy : {d['busy']}", flush=True)
            if typed == 3 and cleared == 0 and ready is None:
                ready = el
                break
            await asyncio.sleep(2.0)
        print(f"  native typing accepted after {ready} s", flush=True)
        sent = await client._safely(client._w.ui_send, "zapomni slovo emerald-742: kakoy fayl ya priložil?")
        print(f"  ui_send with a document attached -> {sent}", flush=True)
        await asyncio.sleep(15.0)

        print("=== B: five .txt in one drop ===", flush=True)
        print("  new chat:", await call(new_chat, client._w), flush=True)
        ok, dt = await drop(stage(5, "b"))
        print(f"  ui_attach(5 txt) -> {ok} in {dt:.1f}s", flush=True)
        for _ in range(4):
            d = await call(cards, client._w)
            print(f"  cards={d['n']} names={d['names']}", flush=True)
            await asyncio.sleep(2.0)

        print("=== C: seven .txt in one drop ===", flush=True)
        print("  new chat:", await call(new_chat, client._w), flush=True)
        ok, dt = await drop(stage(7, "c"))
        print(f"  ui_attach(7 txt) -> {ok} in {dt:.1f}s", flush=True)
        for _ in range(5):
            d = await call(cards, client._w)
            st = await call(state, client._w)
            print(f"  cards={d['n']} names={d['names']} send={st['send']}", flush=True)
            await asyncio.sleep(3.0)
    finally:
        await client.shutdown()


asyncio.run(main())
