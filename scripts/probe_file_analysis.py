"""Watch what the SPA does to the composer while it analyses attached files.

Usage:
    set "PYTHONIOENCODING=utf-8" && venv\\Scripts\\python.exe -u scripts\\probe_file_analysis.py [files] [ticks]

Defaults: 2 files, 60 ticks. Our readiness oracle is "an attachment card mounted",
but a card appears BEFORE the site finishes reading a document: while it parses, the
composer refuses text and the send button stays dead, and a click at that moment hits
the far fallback button instead of sending nothing. Guessing the DOM signal for
"parsing done" is how the last wrong claim shipped, so this probe only observes.

Every tick it (a) reads the composer state, the send button, the card text and any
toast, and (b) TRIES to type a sentinel into the composer and restores the field
afterwards, because "the site refuses to accept text yet" is the actual failure the
user reported and only a typing attempt can measure it. The file count is an argument
so we can probe the site's per-message document ceiling (the user says it is 5).
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.client.qwen_client import QwenClient, _js_composer  # noqa: E402

WORDS = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo "
         "lima mike november oscar papa quebec romeo sierra tango uniform "
         "victor whiskey xray yankee zulu ").split()
SENTINEL = "QQZ"

JS_SNAPSHOT = _js_composer(r"""
var out = {};
var cards = [].slice.call(document.querySelectorAll('.file-card-list .vision-item-container'));
out.cards = cards.length;
out.card = cards.length ? (cards[cards.length - 1].innerText || '').replace(/\s+/g, ' ').trim().slice(0, 90) : '';
var busy = [].slice.call(document.querySelectorAll('.file-card-list [class*="loading" i],.file-card-list [class*="spin" i],'
            + '.file-card-list [class*="progress" i],.file-card-list [aria-busy="true"]'))
  .filter(function(e){ return e.offsetParent; });
out.busy = busy.slice(0, 4).map(function(e){
  return e.tagName + '.' + String(e.className).replace(/\s+/g, ' ').slice(0, 45)
       + '|' + (e.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 30);
});
var toast = [].slice.call(document.querySelectorAll('[role="alert"],[class*="toast" i],[class*="notif" i],'
            + '[class*="ant-message" i],[class*="error" i]'))
  .filter(function(e){ return e.offsetParent && (e.innerText || '').trim(); });
out.toast = toast.slice(0, 3).map(function(e){
  return (e.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 90);
});
var c = findComposer();
if (!c) { out.comp = 'none'; return JSON.stringify(out); }
var el = c.el;
var isTA = el.tagName === 'TEXTAREA';
function read(){ return String(isTA ? (el.value || '') : (el.textContent || '')); }
var before = read();
out.comp = c.tag;
out.len = before.length;
out.dis = el.disabled ? 1 : 0;
out.ro = (isTA && el.readOnly) ? 1 : 0;
out.ce = el.getAttribute('contenteditable');
out.aria = el.getAttribute('aria-disabled') || '-';
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
if (out.cards > 0) {
  put(before + 'QQZ');
  out.stuck = read().indexOf('QQZ') >= 0 ? 1 : 0;
  put(before);
  out.restored = read().length === before.length ? 1 : 0;
}
var btns = [].slice.call(document.querySelectorAll('button')).filter(function(b){
  var l = (b.getAttribute('aria-label') || '') + ' ' + (b.getAttribute('data-testid') || '');
  return /send|отправ/i.test(l) && b.offsetParent;
});
out.send = btns.map(function(b){
  return (b.getAttribute('aria-label') || '').slice(0, 16)
       + ':dis=' + (b.disabled ? 'y' : 'n')
       + ':ad=' + (b.getAttribute('aria-disabled') || '-');
});
return JSON.stringify(out);
""")


def stage(count: int):
    paths = []
    for i in range(1, count + 1):
        p = Path(f"probe_fat_{i}.txt")
        p.write_text(" ".join(WORDS * 120) + f" emerald-742 chunk{i}\n", encoding="utf-8")
        paths.append(str(p))
    print("staged:", [Path(p).stat().st_size for p in paths], flush=True)
    return paths


def show(t: float, raw):
    text = str(raw or "")
    try:
        d = json.loads(text)
    except Exception:  # noqa: BLE001
        print(f"[{t:6.1f}s] RAW {text[:300]}", flush=True)
        return
    keys = ("cards", "comp", "len", "dis", "ro", "ce", "aria", "stuck", "restored")
    head = " ".join(f"{k}={d.get(k, '-')}" for k in keys)
    print(f"[{t:6.1f}s] {head}", flush=True)
    if d.get("card"):
        print(f"          card : {d['card']}", flush=True)
    if d.get("busy"):
        print(f"          busy : {d['busy']}", flush=True)
    if d.get("toast"):
        print(f"          toast: {d['toast']}", flush=True)
    if d.get("send"):
        print(f"          send : {d['send']}", flush=True)


async def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    ticks = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    paths = stage(count)
    client = QwenClient()
    await client._safely(client._w.ensure)
    try:
        t0 = time.monotonic()
        ok = await client._safely(client._w.ui_attach, paths)
        print(f"[{time.monotonic() - t0:6.1f}s] ui_attach({count} files) -> {ok}", flush=True)
        for _ in range(ticks):
            show(time.monotonic() - t0, await client._safely(client._w._js, JS_SNAPSHOT))
            await asyncio.sleep(1.0)
    finally:
        await client.shutdown()


asyncio.run(main())
