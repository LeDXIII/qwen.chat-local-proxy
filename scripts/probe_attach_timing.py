"""How long does chat.qwen.ai block NATIVE typing while it reads an attachment?

Usage:
    set "PYTHONIOENCODING=utf-8" && venv\\Scripts\\python.exe -u scripts\\probe_attach_timing.py [max_wait_s]

The sequencing fix waits for the site to finish parsing before it types the user text.
That wait has to be size-aware, so the curve is what this measures: for a 60 KB, a 400 KB
and a 1.5 MB .txt it records, second by second, whether real keystrokes land in the
composer (typed==probe length, cleared==0), whether the send button ever reads disabled,
and what the attachment card looks like while the site is busy (spinner / percent / hint
text) — a DOM busy-flag would be a far better oracle than typing at all.

Phase E answers a second question the fix needs: the site clamped 7 .txt down to 5 cards.
Is that cap 5 documents, or 5 files of any kind? 4 .txt + 4 .png in one drop tells us.

Production types with send_keys, so readiness is probed the same way: JS-setter writes
land at every moment and prove nothing.
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

from src.client.qwen_client import JS_MARK_COMPOSER, QwenClient, _js_composer

PROBE = "QQZ"

JS_CARDS = r"""
var cards = [].slice.call(document.querySelectorAll(
  '.file-card-list .fileitem-btn, .file-card-list .vision-item-container'));
var scope = cards.length ? cards[0].closest('.message-input-column-file') || document : document;
return JSON.stringify({
  n: cards.length,
  names: cards.slice(0, 9).map(function(e){ return (e.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 34); }),
  busy: [].slice.call(scope.querySelectorAll('*')).filter(function(e){
      return e.offsetParent && /progress|upload|loading|spin|percent|process|read/i.test(String(e.className));
    }).slice(0, 5).map(function(e){ return e.tagName + '.' + String(e.className).replace(/\s+/g, ' ').slice(0, 44); }),
  hint: (function(){
      var t = [];
      [].slice.call(scope.querySelectorAll('span,div,p')).forEach(function(e){
        var s = (e.childElementCount === 0 ? (e.innerText || '') : '').trim();
        if (s && s.length < 60 && /чита|обрабат|анализ|загруз|жд|load|process|analy|%/i.test(s)) t.push(s);
      });
      return t.slice(0, 3);
    })(),
  btn: cards.length ? (function(){ var b = cards[0].querySelector('button');
      return b ? ('dis=' + (b.disabled ? 'y' : 'n') + ':ad=' + (b.getAttribute('aria-disabled') || '-')) : '-'; })() : '-',
  html: cards.length ? String(cards[0].outerHTML).replace(/\s+/g, ' ').slice(0, 900) : '',
});
"""

STATE = _js_composer(r"""
var out = {};
var c = findComposer();
out.len = c ? String(c.el.value !== undefined ? c.el.value : (c.el.textContent || '')).length : -1;
out.ph = c ? String(c.ph || '').slice(0, 40) : '';
out.send = [].slice.call(document.querySelectorAll('button')).filter(function(b){
  return /send|отправ/i.test((b.getAttribute('aria-label') || '') + ' ' + (b.getAttribute('data-testid') || '')) && b.offsetParent;
}).map(function(b){ return (b.getAttribute('aria-label') || '').slice(0, 12) + ':dis=' + (b.disabled ? 'y' : 'n') + ':ad=' + (b.getAttribute('aria-disabled') || '-'); });
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


def write_txt(name: str, approx_bytes: int, sentinel: str) -> str:
    line = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike "
    body = (line * (approx_bytes // len(line) + 1))[:approx_bytes]
    p = Path(name)
    p.write_text(body + "\n" + sentinel + "\n", encoding="utf-8")
    return str(p)


def cards(w):
    try:
        return json.loads(str(w._js(JS_CARDS)))
    except Exception as exc:  # noqa: BLE001
        return {"n": -1, "names": [], "busy": [], "hint": [], "btn": "-", "html": f"err:{type(exc).__name__}"}


def state(w):
    try:
        return json.loads(str(w._js(STATE)))
    except Exception:  # noqa: BLE001
        return {"len": -1, "ph": "", "send": []}


def native(w, text: str):
    """Type the way production does — real keystrokes into the marked composer."""
    w._js(JS_MARK_COMPOSER)
    try:
        el = w.driver.find_element(By.CSS_SELECTOR, "[data-qwproxy]")
        el.click()
        if text:
            el.send_keys(text)
    except Exception as exc:  # noqa: BLE001
        return f"err:{type(exc).__name__}"
    return state(w).get("len")


def clear_native(w):
    w._js(JS_MARK_COMPOSER)
    try:
        el = w.driver.find_element(By.CSS_SELECTOR, "[data-qwproxy]")
        el.click()
        el.send_keys(Keys.CONTROL, "a")
        el.send_keys(Keys.DELETE)
    except Exception as exc:  # noqa: BLE001
        return f"err:{type(exc).__name__}"
    return state(w).get("len")


async def main():
    max_wait = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    sizes = [("t60", 60 * 1024, "sentinel-60k"), ("t400", 400 * 1024, "sentinel-400k"),
             ("t1500", 1500 * 1024, "sentinel-1p5m")]
    client = QwenClient()
    await client._safely(client._w.ensure)

    async def call(fn, *args):
        return await client._safely(fn, *args)

    async def phase(tag, paths, wait_s, polls=0):
        print(f"--- {tag}: drop {len(paths)} file(s), sizes="
              f"{[Path(p).stat().st_size for p in paths]}", flush=True)
        try:
            print(f"    go_home={await call(client._w.go_home)}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"    go_home err {type(exc).__name__}", flush=True)
        t0 = time.monotonic()
        ok = await client._safely(client._w.ui_attach, paths)
        print(f"    ui_attach -> {ok} in {time.monotonic() - t0:.1f}s", flush=True)
        deadline = time.monotonic() + wait_s
        first = True
        ready = None
        while time.monotonic() < deadline:
            d = await call(cards, client._w)
            st = await call(state, client._w)
            typed = await call(native, client._w, PROBE)
            cleared = await call(clear_native, client._w)
            el = round(time.monotonic() - t0, 1)
            print(f"    [{el:5.1f}s] cards={d['n']} busy={len(d['busy'])} hint={d['hint']} "
                  f"btn={d['btn']} len={st['len']} ph={st['ph']!r} typed={typed} "
                  f"cleared={cleared} send={st['send']}", flush=True)
            if first and (d["n"] > 0 or d["busy"]):
                first = False
                print(f"      names: {d['names']}", flush=True)
                print(f"      busy : {d['busy']}", flush=True)
                print(f"      html : {d['html'][:700]}", flush=True)
            if typed == len(PROBE) and cleared == 0:
                ready = el
                break
            await asyncio.sleep(2.0)
        print(f"    native typing accepted {ready}s after the drop", flush=True)
        for _ in range(polls):
            d = await call(cards, client._w)
            print(f"      later cards={d['n']} names={d['names']} hint={d['hint']}", flush=True)
            await asyncio.sleep(3.0)
        return ready

    try:
        for tag, size, sentinel in sizes:
            await phase(tag, [write_txt(f"probe_tim_{tag}.txt", size, sentinel)], max_wait)

        # E: is the 5-file clamp about documents or about files of any kind?
        Path("probe_tim_img1.png").write_bytes(png_bytes(360, 240, (30, 160, 90)))
        Path("probe_tim_img2.png").write_bytes(png_bytes(360, 240, (200, 60, 60)))
        Path("probe_tim_img3.png").write_bytes(png_bytes(360, 240, (60, 60, 200)))
        Path("probe_tim_img4.png").write_bytes(png_bytes(360, 240, (200, 180, 40)))
        mix = [write_txt(f"probe_tim_m{i}.txt", 20 * 1024, f"mix{i}") for i in range(1, 5)]
        mix += [str(Path(f"probe_tim_img{i}.png")) for i in range(1, 5)]
        await phase("E-mixed-4txt+4png", mix, 25, polls=3)
    finally:
        await client.shutdown()


asyncio.run(main())
