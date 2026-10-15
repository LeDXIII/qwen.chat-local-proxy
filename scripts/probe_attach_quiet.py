"""Watch the attachment cards WITHOUT touching the composer: is the site's own
"Анализ..." state the readiness signal the sequencing fix should wait for?

Usage:
    set "PYTHONIOENCODING=utf-8" && venv\\Scripts\\python.exe -u scripts\\probe_attach_quiet.py [max_wait_s]

The previous probe typed a 3-char sentinel into the composer every 2 s while it waited.
On the 60 KB and 400 KB files that never became ready in 92 s: every tick answered
ElementClickInterceptedException (the composer was covered) and the card kept its
.fileitem-loading-icon spinner. Two readings fit that: the site really holds the composer
for minutes, or my own poking restarted the analysis each time. Phase E of that run says
otherwise — 4 documents dropped together showed «Анализ...» for ~6 s and cleared, and the
only tick that typed was the first.

So this probe never clicks, never types, never focuses. It reads, once a second and a half:
  * how many attachment cards are mounted and how many still carry
    .anticon-spin / «Анализ...»  — the candidate oracle;
  * what elementFromPoint(composer center) actually returns, which names the overlay that
    raises ElementClickInterceptedException in production ui_send;
  * the send button's own disabled state.
When the cards go quiet it makes exactly ONE native typing attempt (JS focus, no click,
then keystrokes — the way that worked while a click was intercepted), reports whether the
text landed, clears it, and for the 400 KB file finishes with a real ui_send so the whole
attachment turn is proven end to end.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from src.client.qwen_client import (
    JS_MARK_COMPOSER,
    QwenClient,
    _js_composer,
)

QUIET = _js_composer(r"""
var out = {n: 0, spin: 0, hint: 0, cards: [], cover: '', covered: false, send: []};
var cards = [].slice.call(document.querySelectorAll(
  '.file-card-list .fileitem-btn, .file-card-list .vision-item-container'));
out.n = cards.length;
out.cards = cards.map(function(e){
  var t = (e.innerText || '').replace(/\s+/g, ' ').trim();
  return {
    name: t.slice(0, 30),
    spin: !!e.querySelector('.anticon-spin, .fileitem-loading-icon, [class*="loading"], [class*="spin"]'),
    busy: /анализ|чита|обработ|process|analy|loading/i.test(t),
  };
});
out.spin = out.cards.filter(function(c){ return c.spin; }).length;
out.hint = out.cards.filter(function(c){ return c.busy; }).length;
var c = findComposer();
if (c) {
  var r = c.el.getBoundingClientRect();
  var top = document.elementFromPoint((r.left + r.right) / 2, (r.top + r.bottom) / 2);
  out.cover = top ? (top.tagName + '.' + String(top.className).replace(/\s+/g, ' ').slice(0, 70)) : 'null';
  out.covered = !(top === c.el || c.el.contains(top));
  out.cr = [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)];
}
out.send = [].slice.call(document.querySelectorAll('button')).filter(function(b){
  return /send|отправ/i.test((b.getAttribute('aria-label') || '') + ' ' + (b.getAttribute('data-testid') || '')) && b.offsetParent;
}).map(function(b){ return (b.getAttribute('aria-label') || '').slice(0, 12) + ':dis=' + (b.disabled ? 'y' : 'n') + ':ad=' + (b.getAttribute('aria-disabled') || '-'); });
out.busy = [].slice.call(document.querySelectorAll('.anticon-spin, [aria-busy="true"], [class*="processing"]')).filter(function(e){ return e.offsetParent; }).length;
return JSON.stringify(out);
""")

WORDS = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike "
         "november oscar papa quebec romeo sierra tango uniform victor whiskey xray yankee ")


def write_txt(name: str, approx_bytes: int, sentinel: str) -> str:
    body = (WORDS * (approx_bytes // len(WORDS) + 1))[:approx_bytes]
    p = Path(name)
    p.write_text(body + "\n" + sentinel + "\n", encoding="utf-8")
    return str(p)


def quiet(w):
    try:
        return json.loads(str(w._js(QUIET)))
    except Exception as exc:  # noqa: BLE001
        return {"n": -1, "spin": -1, "hint": -1, "cards": [], "cover": f"err:{type(exc).__name__}",
                "covered": False, "send": [], "busy": -1}


def type_no_click(w, text: str):
    """The one attempt: JS focus (no click, no overlay fight), then real keystrokes."""
    mark = str(w._js(JS_MARK_COMPOSER) or "")
    try:
        el = w.driver.find_element(By.CSS_SELECTOR, "[data-qwproxy]")
        w.driver.execute_script("arguments[0].focus();", el)
        el.send_keys(text)
        return f"{mark.split(':')[0]}:len={len(el.get_attribute('value') or el.get_attribute('textContent') or '')}"
    except Exception as exc:  # noqa: BLE001
        return f"err:{type(exc).__name__}:{str(exc)[:60]}"


def clear_no_click(w):
    try:
        el = w.driver.find_element(By.CSS_SELECTOR, "[data-qwproxy]")
        w.driver.execute_script("arguments[0].focus();", el)
        el.send_keys(Keys.CONTROL, "a")
        el.send_keys(Keys.DELETE)
        return f"len={len(el.get_attribute('value') or el.get_attribute('textContent') or '')}"
    except Exception as exc:  # noqa: BLE001
        return f"err:{type(exc).__name__}"


async def main():
    max_wait = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    client = QwenClient()
    await client._safely(client._w.ensure)

    async def call(fn, *args):
        return await client._safely(fn, *args)

    async def phase(tag, paths, wait_s, do_send=False):
        print(f"--- {tag}: sizes={[Path(p).stat().st_size for p in paths]}", flush=True)
        try:
            print(f"    go_home={await call(client._w.go_home)}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"    go_home err {type(exc).__name__}", flush=True)
        t0 = time.monotonic()
        ok = await client._safely(client._w.ui_attach, paths)
        print(f"    ui_attach -> {ok} in {time.monotonic() - t0:.1f}s", flush=True)
        quiet_at = None
        while time.monotonic() - t0 < wait_s:
            d = await call(quiet, client._w)
            el = round(time.monotonic() - t0, 1)
            print(f"    [{el:5.1f}s] cards={d['n']} spin={d['spin']} hint={d['hint']} "
                  f"busy={d['busy']} covered={d['covered']} cover={d['cover'][:60]!r} "
                  f"send={d['send']}", flush=True)
            if d["n"] > 0 and d["spin"] == 0 and d["hint"] == 0:
                quiet_at = el
                break
            await asyncio.sleep(1.5)
        print(f"    cards quiet at {quiet_at}s", flush=True)
        print(f"    first typing attempt (JS focus, no click): {await call(type_no_click, client._w, 'QQZ')}", flush=True)
        print(f"    cleared: {await call(clear_no_click, client._w)}", flush=True)
        d = await call(quiet, client._w)
        print(f"    after typing: cards={d['n']} spin={d['spin']} hint={d['hint']} "
              f"covered={d['covered']} cover={d['cover'][:60]!r}", flush=True)
        if do_send:
            n0 = await call(client._w.tap_n)
            sent = await client._safely(client._w.ui_send, "kakoy fayl ya priložil? nazovite toxnoe imya")
            print(f"    ui_send -> {sent}", flush=True)
            await asyncio.sleep(12.0)
            n1 = await call(client._w.tap_n)
            print(f"    tap {n0} -> {n1}", flush=True)
        return quiet_at

    try:
        await phase("60k", [write_txt("probe_quiet_60k.txt", 60 * 1024, "sentinel-60k")], max_wait)
        await phase("400k", [write_txt("probe_quiet_400k.txt", 400 * 1024, "sentinel-400k")],
                    max_wait, do_send=True)
        mix = [write_txt(f"probe_quiet_d{i}.txt", 150 * 1024, f"doc{i}") for i in range(1, 6)]
        await phase("5docs-150k-each", mix, max_wait)
    finally:
        await client.shutdown()


asyncio.run(main())
