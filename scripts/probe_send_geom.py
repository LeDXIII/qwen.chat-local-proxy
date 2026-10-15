"""Measure where the send button actually sits once a file card is staged.

The strict attachment path refused its own send button three times in a row
(verdict "send-not-near"), so the message was typed and then never sent — the
exact failure the user described. The guard measures distance to the composer
element, and the hypothesis is that the card row the SPA inserts changes that
geometry. Guessing a new threshold would be another blind edit, so this probe
only MEASURES: it attaches one small document, waits for the site to finish
reading it, types text the way ui_send does, and dumps rectangles.

Nothing here clicks the send button or presses Enter: the turn is left unsent
and the composer is cleared before the browser closes.

Usage:
    set "PYTHONIOENCODING=utf-8" && venv\\Scripts\\python.exe -u scripts\\probe_send_geom.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.client.qwen_client import (  # noqa: E402
    JS_MARK_COMPOSER,
    QwenClient,
    _js_composer,
)

DOC = "probe_geom_doc.txt"
TEXT = "GEOM-PROBE-TEXT"
MODEL = "qwen3.8-max"

JS_GEOM = _js_composer(r"""
var out = {};
function rect(e){
  var r = e.getBoundingClientRect();
  return {l:Math.round(r.left), t:Math.round(r.top), r:Math.round(r.right),
          b:Math.round(r.bottom), w:Math.round(r.width), h:Math.round(r.height)};
}
var comp = findComposer();
if (!comp) return 'no-composer';
out.comp = rect(comp.el);
out.tag = comp.tag;

var chain = [], node = comp.el;
for (var i = 0; i < 7 && node; i++) {
  chain.push(node.tagName + '.' + String(node.className || '').replace(/\s+/g, ' ').slice(0, 42)
             + ' ' + JSON.stringify(rect(node)));
  node = node.parentElement;
}
out.chain = chain;

var cardList = null, up = comp.el;
for (var j = 0; j < 8 && up && !cardList; j++) {
  cardList = up.querySelector ? up.querySelector('.file-card-list') : null;
  up = up.parentElement;
}
out.cardRow = cardList ? rect(cardList) : null;

function inLayer(e){
  return !!(e.closest && e.closest('[role="menu"],[role="listbox"],[role="dialog"],[aria-modal="true"]'));
}
function badLabel(b){
  var l = (b.getAttribute('aria-label') || '') + ' ' + (b.getAttribute('data-testid') || '');
  return /голос|voice|mic|микро|добав|attach|прикреп|нов|new|закры|close|настр/i.test(l);
}
var sels = ['button[aria-label*="тправ"]','button[aria-label*="end"]','button[data-testid*="send"]',
            'button:has(svg[data-icon])'];
var seen = [], items = [];
for (const s of sels) {
  try {
    for (const b of document.querySelectorAll(s)) {
      if (seen.indexOf(b) >= 0) continue;
      seen.push(b);
      var r = b.getBoundingClientRect();
      var dx = Math.max(out.comp.l - r.right, r.left - out.comp.r, 0);
      var dy = Math.max(out.comp.t - r.bottom, r.top - out.comp.b, 0);
      var cdx = 9999, cdy = 9999;
      if (cardList) {
        var kr = cardList.getBoundingClientRect();
        cdx = Math.max(kr.left - r.right, r.left - kr.right, 0);
        cdy = Math.max(kr.top - r.bottom, r.top - kr.bottom, 0);
      }
      items.push({sel: s.replace('button:has(svg[data-icon])', 'svg'),
                  lbl: (b.getAttribute('aria-label') || '').slice(0, 22),
                  tid: (b.getAttribute('data-testid') || '').slice(0, 22),
                  vis: b.offsetParent !== null, dis: !!b.disabled,
                  layer: inLayer(b), bad: badLabel(b),
                  w: Math.round(r.width), h: Math.round(r.height),
                  l: Math.round(r.left), t: Math.round(r.top),
                  dx: Math.round(dx), dy: Math.round(dy),
                  cdx: Math.round(cdx), cdy: Math.round(cdy)});
    }
  } catch (e) {}
}
out.total = items.length;
out.usable = items.filter(function(x){ return x.vis && !x.dis && !x.layer && !x.bad; });
items.sort(function(a, b){ return (a.dx + a.dy) - (b.dx + b.dy); });
out.nearest = items.slice(0, 8);
out.value = String(comp.el.value !== undefined ? comp.el.value
                  : (comp.el.textContent || '')).slice(0, 24);
return JSON.stringify(out);
""")


def type_native(w, text: str) -> str:
    """Types exactly like ui_send's production path (native keystrokes)."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys

    w._js(JS_MARK_COMPOSER)
    el = w.driver.find_element(By.CSS_SELECTOR, "[data-qwproxy]")
    el.click()
    el.send_keys(Keys.CONTROL, "a")
    el.send_keys(Keys.DELETE)
    el.send_keys(text)
    return "typed"


def clear_native(w) -> str:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys

    try:
        el = w.driver.find_element(By.CSS_SELECTOR, "[data-qwproxy]")
        el.click()
        el.send_keys(Keys.CONTROL, "a")
        el.send_keys(Keys.DELETE)
        return "cleared"
    except Exception as e:  # noqa: BLE001
        return f"clear-failed:{type(e).__name__}"


def show(tag: str, raw) -> None:
    text = str(raw or "")
    print(f"\n===== {tag} =====", flush=True)
    try:
        d = json.loads(text)
    except Exception:  # noqa: BLE001
        print(f"RAW {text[:500]}", flush=True)
        return
    print(f"composer tag={d.get('tag')} rect={d.get('comp')} value={d.get('value')!r}", flush=True)
    print(f"cardRow={d.get('cardRow')}", flush=True)
    for line in d.get("chain") or []:
        print(f"  chain {line}", flush=True)
    print(f"candidates total={d.get('total')}", flush=True)
    for b in d.get("usable") or []:
        near = "NEAR" if (b["dx"] < 140 and b["dy"] < 90) else "far "
        print(f"  {near} usable sel={b['sel']} lbl={b['lbl']!r} tid={b['tid']!r} "
              f"w={b['w']} h={b['h']} l={b['l']} t={b['t']} dx={b['dx']} dy={b['dy']} "
              f"cardDx={b['cdx']} cardDy={b['cdy']}", flush=True)
    for b in d.get("nearest") or []:
        print(f"  nearlist sel={b['sel']} lbl={b['lbl']!r} vis={b['vis']} dis={b['dis']} "
              f"layer={b['layer']} bad={b['bad']} dx={b['dx']} dy={b['dy']} "
              f"cardDx={b['cdx']} cardDy={b['cdy']}", flush=True)


async def main() -> None:
    body = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo "
            "lima mike november oscar papa quebec romeo sierra tango uniform victor "
            "whiskey xray yankee zulu " * 400)
    Path(DOC).write_text(body, encoding="utf-8")
    print(f"staged {DOC} = {Path(DOC).stat().st_size} bytes", flush=True)

    client = QwenClient()
    await client._safely(client._w.ensure)
    try:
        picked = await client._safely(client._w.try_select_model, MODEL)
        print(f"model {MODEL} -> {picked}", flush=True)
        show("before-attach", await client._safely(client._w._js, JS_GEOM))

        ok = await client._safely(client._w.ui_attach, [DOC])
        print(f"\nui_attach -> {ok}", flush=True)
        for _ in range(60):
            await asyncio.sleep(1.0)
            state = await client._safely(client._w.attach_state)
            if state and state.get("n", 0) > 0 and not state.get("spin") and not state.get("hint"):
                print(f"site finished reading: {state}", flush=True)
                break
        else:
            print(f"STILL BUSY: {state}", flush=True)
        show("after-attach-quiet", await client._safely(client._w._js, JS_GEOM))

        print(f"\n{await client._safely(type_native, client._w, TEXT)}", flush=True)
        await asyncio.sleep(1.0)
        show("after-typing", await client._safely(client._w._js, JS_GEOM))
        print(f"\n{await client._safely(clear_native, client._w)}", flush=True)
    finally:
        await client.shutdown()


asyncio.run(main())
