"""
Client for chat.qwen.ai over the real SPA UI (browser transport).

Why this design: direct POSTs to /api/v2/chat/completions are gated by the
Aliyun WAF/baxia anti-bot (measured 2026-10: httpx -> TMD punish; synthetic
fetch from page or iframe -> JS-challenge interstitial; replaying captured
signed headers is dead — signatures are one-shot). The SPA's own fetches pass
because baxia signs them. So the only proven transport (E19/E19d, logged-in)
is: drive the real chat.qwen.ai UI in a persistent Chrome profile and tee the
SSE responses with a fetch wrapper installed via CDP before page scripts run.

Ground truths encoded here (E19d):
- new chat: send from "/" — the SPA creates the chat and navigates to /c/<uuid>;
- continuation: parent_id of the next message == response_id of the previous
  assistant message (taken from the SSE stream);
- the Bearer token is the SPA's own localStorage token, rotated often, so it
  is re-read from the page right before every API read;
- GETs (/api/v2/chats/, /api/v2/chats/<id>/) work from page context.
"""

import asyncio
import atexit
import base64
import json
import logging
import mimetypes
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import AsyncGenerator, Optional

from src.config import (
    DEBUG_API,
    DEFAULT_MODELS,
    QWEN_BASE_URL,
    QWEN_BROWSER_HEADLESS,
    QWEN_BROWSER_LANGUAGE,
    QWEN_BROWSER_PROFILE,
    QWEN_CHROME_PATH,
    QWEN_PAGE_TIMEOUT,
    QWEN_SEND_TIMEOUT,
    SPA_HEADER_VERSION,
)

logger = logging.getLogger(__name__)

BASE = QWEN_BASE_URL.rstrip("/")

# --------------------------------------------------------------------------
# page-context JavaScript (ported verbatim from the proven E19/E19d/E21 runs)
# --------------------------------------------------------------------------

# Installs before any page script; wraps the FINAL (baxia-signed) window.fetch
# and tees every /chat/completions SSE body into window.__tap.cur.text.
TAP_JS = r"""
(function(){
  if (window.__tapInstalled) return; window.__tapInstalled = true;
  var of = window.fetch;
  window.__tap = {n:0, cur:null, hooked:(of.__tapmark===true)};
  function F(url, init){
    try {
      var u = String((url && url.url) ? url.url : url);
      if (u.indexOf('/chat/completions') >= 0 && init && init.body) {
        var hdrs = {};
        try {
          var hs = init.headers;
          if (hs) {
            if (Array.isArray(hs)) { hs.forEach(function(p){ hdrs[p[0]] = p[1]; }); }
            else if (hs.forEach) { hs.forEach(function(v,k){ hdrs[k] = v; }); }
            else { for (var k in hs) { hdrs[k] = hs[k]; } }
          }
        } catch(e) {}
        var rec = {url:u, method:(init.method||'GET'), headers:hdrs, body:String(init.body),
                   status:0, ct:'', text:'', done:false, err:'', t0:Date.now()};
        window.__tap.cur = rec; window.__tap.n++;
        var p = of.apply(this, arguments);
        return p.then(function(resp){
          rec.status = resp.status; rec.ct = resp.headers.get('content-type') || '';
          try {
            if (/event-stream/.test(rec.ct) && resp.body) {
              var tp = resp.body.tee();
              var bg = tp[1].getReader(); var dc = new TextDecoder();
              (function loop(){
                return bg.read().then(function(r){
                  if (r.done) { rec.done = true; return; }
                  rec.text += dc.decode(r.value, {stream:true});
                  return loop();
                });
              })().catch(function(e){ rec.err = String(e); rec.done = true; });
              return new Response(tp[0], {status: resp.status, statusText: resp.statusText,
                                          headers: resp.headers});
            } else {
              resp.clone().text().then(function(t){ rec.text = t.slice(0,20000); rec.done = true;})
                  .catch(function(){});
            }
          } catch(e) { rec.err = String(e); rec.done = true; }
          return resp;
        }, function(e){ rec.err = String(e); rec.done = true; throw e; });
      }
    } catch(e) {}
    return of.apply(this, arguments);
  }
  F.__tapmark = true;
  window.fetch = F;
})();
"""

# Incremental tail of the captured SSE: only text up to the last complete line.
JS_TAP_DELTA = r"""
var pos = arguments[0] || 0;
var t = window.__tap;
if (!t || !t.cur) return {n: t?t.n:0, has:false, status:0, done:false, err:'', cut:'', pos:pos, href:location.href};
var c = t.cur, s = c.text || '';
var b = s.lastIndexOf('\n') + 1;
return {n:t.n, has:true, status:c.status, ct:c.ct, done:c.done, err:c.err,
        cut:s.slice(pos, b), pos:b, total:s.length, href:location.href};
"""

JS_TAP_FULL = """
var t = window.__tap;
if (!t || !t.cur) return null;
return {status:t.cur.status, ct:t.cur.ct, text:t.cur.text, done:t.cur.done, err:t.cur.err,
        reqHeaders:t.cur.headers, reqBody:t.cur.body, url:t.cur.url};
"""

# Scored composer locator shared by typing/clicking/probing scripts: the real
# input carries an ask-like placeholder ("Спросить Qwen"); sidebar search and
# chat-title fields must never win. Returns {el, tag, ph, w, top}.
_JS_FIND_COMPOSER = r"""
function vis(e){ return e && e.offsetParent !== null; }
function findComposer(){
  var re = /ask|спрос|message|сообщ|напиш|write|вопрос/i;
  var best = null, bestScore = -1;
  for (const e of document.querySelectorAll('textarea, div[contenteditable="true"], input[type="text"]')) {
    if (!vis(e)) continue;
    var ph = (e.placeholder || '') + ' ' + (e.getAttribute('aria-label') || '') + ' ' +
             (e.getAttribute('data-placeholder') || '') + ' ' + (e.className || '');
    var r = e.getBoundingClientRect();
    var s = r.width + (r.top > innerHeight * 0.35 ? r.width * 0.5 : 0);
    if (re.test(ph)) s += 100000;
    if (e.tagName === 'TEXTAREA') s += 500;
    if (e.isContentEditable) s += 250;
    if (s > bestScore) { bestScore = s; best = e; }
  }
  if (!best) return null;
  var rr = best.getBoundingClientRect();
  var tag = best.tagName === 'DIV' ? 'ce' : best.tagName.toLowerCase();
  return {el: best, tag: tag, ph: (best.placeholder || best.getAttribute('aria-label') || '').slice(0,30),
          w: Math.round(rr.width), top: Math.round(rr.top)};
}
"""


def _js_composer(body: str) -> str:
    """Embed the shared composer finder into a standalone JS snippet."""
    return _JS_FIND_COMPOSER + body


JS_CLICK_SEND = _js_composer(r"""
var strict = !!arguments[0];
function inLayer(e){
  return !!(e.closest && e.closest('[role="menu"],[role="listbox"],[role="dialog"],[aria-modal="true"]'));
}
function badLabel(b){
  var l = (b.getAttribute('aria-label') || '') + ' ' + (b.getAttribute('data-testid') || '');
  return /голос|voice|mic|микро|добав|attach|прикреп|нов|new|закры|close|настр/i.test(l);
}
function usable(b){ return vis(b) && !b.disabled && b.getAttribute('aria-disabled') !== 'true'
                      && !inLayer(b) && !badLabel(b); }
var comp = findComposer();
var cr = comp ? comp.el.getBoundingClientRect() : null;
function nearComposer(b){
  if (!cr) return true;
  var r = b.getBoundingClientRect();
  var dx = Math.max(cr.left - r.right, r.left - cr.right, 0);
  var dy = Math.max(cr.top - r.bottom, r.top - cr.bottom, 0);
  return dx < 140 && dy < 90;
}
var sels = ['button[aria-label*="тправ"]','button[aria-label*="end"]','button[data-testid*="send"]',
            'button:has(svg[data-icon])'];
function info(b){
  var r = b.getBoundingClientRect();
  return { l:Math.round(r.left), t:Math.round(r.top), w:Math.round(r.width), h:Math.round(r.height),
           dx: cr ? Math.round(Math.max(cr.left-r.right, r.left-cr.right, 0)) : -1,
           dy: cr ? Math.round(Math.max(cr.top-r.bottom, r.top-cr.bottom, 0)) : -1,
           v: vis(b)?1:0, ds: b.disabled?1:0, ad: b.getAttribute('aria-disabled')==='true'?1:0,
           y: inLayer(b)?1:0, B: badLabel(b)?1:0, lb: (b.getAttribute('aria-label')||'').slice(0,14) };
}
// A refused click reported only the verdict token, which never said what
// findComposer scored or where the refused button sat. ui_send matches the
// leading token, so this read-only dump may only ever be appended after it.
var diag = { tag: comp?comp.tag:'', ph: comp?String(comp.ph||'').slice(0,28):'',
             cr: cr?[Math.round(cr.left),Math.round(cr.top),Math.round(cr.right),Math.round(cr.bottom)]:null,
             n: -1, all: [], tr: [] };
try { var cd = document.querySelector('.file-card-list');
      if (cd) { var kr = cd.getBoundingClientRect();
                diag.card = [Math.round(kr.left),Math.round(kr.top),Math.round(kr.right),Math.round(kr.bottom)]; } } catch(e){}
try { var z = [...document.querySelectorAll(sels[0])];
      diag.n = z.length;
      for (const b of z.slice(0,5)) diag.all.push(info(b)); } catch(e){}
// Geometry alone cannot explain a refusal: if the field the site reads is
// empty, no button position is the reason. Record what the composer holds.
try { var vh = comp ? (comp.el.value !== undefined ? comp.el.value : comp.el.textContent) : '';
      diag.val = String(vh || '').replace(/\s+/g, ' ').slice(-40); } catch(e){}
var fallback = null;
for (const s of sels) {
  try {
    var bs = [...document.querySelectorAll(s)].filter(usable);
    if (!bs.length) continue;
    var nb = bs.filter(nearComposer);
    // Read the candidate set before anything is consumed: taking the pick with
    // pop() first made the dump report an already-removed element and left
    // fallback[0] short by one, so a refusal could print coordinates that
    // plainly pass the near-composer test.
    if (diag.tr.length < 4) {
      diag.tr.push({ s: s.replace('button:has(svg[data-icon])','svg'),
                     n: bs.length, nb: nb.length, items: bs.slice(0,6).map(info) });
    }
    if (nb.length) {
      var pick = nb[nb.length - 1];
      pick.click();
      return 'clicked:' + s + ':' + (pick.getAttribute('aria-label') || '').slice(0,24);
    }
    if (!fallback) fallback = [bs, s];
  } catch(e){ if (!diag.err) diag.err = String(e).slice(0,60); }
}
if (fallback && !strict) {
  var far = fallback[0][fallback[0].length - 1];
  far.click();
  return 'clicked-far:' + fallback[1] + ':' + (far.getAttribute('aria-label') || '').slice(0,24);
}
// The composer grows as the user types, so the rect taken at the top may be
// stale by the time every selector refused: re-measure to catch a disagreement
// between what nearComposer saw and what the dump prints.
try { var c2 = findComposer();
      if (c2) { var r2 = c2.el.getBoundingClientRect();
                diag.cr2 = [Math.round(r2.left),Math.round(r2.top),Math.round(r2.right),Math.round(r2.bottom)]; } } catch(e){}
var dump = '';
try { dump = '|' + JSON.stringify(diag); } catch(e){}
return (fallback ? 'send-not-near' : 'no-send-button') + dump;
""")

JS_HAS_INPUT = r"""
return [...document.querySelectorAll('div[contenteditable="true"], textarea')]
  .some(e=>e.offsetParent);
"""

# Selenium hit-tests native clicks; on /c/<id> pages a transparent overlay can
# sit over the composer, so typing/focus must have a JS fallback (JS clicks are
# immune to interception). React needs the native value setter + input event.
JS_TYPE = _js_composer(r"""
var text = arguments[0];
var c = findComposer();
if (!c) return 'no-input';
var el = c.el;
try { el.scrollIntoView({block:'center'}); } catch(e){}
try { el.focus(); } catch(e){}
if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
  var proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, text);
  el.dispatchEvent(new Event('input', {bubbles:true}));
  el.dispatchEvent(new Event('change', {bubbles:true}));
  return 'typed:ta:'+el.value.length+':'+c.ph;
}
el.textContent = text;
el.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:text}));
return 'typed:ce:'+el.textContent.length+':'+c.ph;
""")

JS_ENTER = r"""
var el = document.activeElement;
if (!el || el === document.body) return 'no-active';
var opt = {key:'Enter', code:'Enter', keyCode:13, windowsVirtualKey:13, charCode:13,
           bubbles:true, cancelable:true};
el.dispatchEvent(new KeyboardEvent('keydown', opt));
el.dispatchEvent(new KeyboardEvent('keypress', opt));
el.dispatchEvent(new KeyboardEvent('keyup', opt));
return 'entered:'+el.tagName;
"""

JS_DISMISS = r"""
var n = 0;
try {
  document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', code:'Escape',
    keyCode:27, windowsVirtualKey:27, bubbles:true, cancelable:true}));
} catch(e){}
for (const b of document.querySelectorAll('button[aria-label]')) {
  try {
    var l = (b.getAttribute('aria-label') || '').trim();
    if (/^(Закрыть|Close|Dismiss|Skip|No thanks|Not now|Позже|Later)$/i.test(l) && b.offsetParent) {
      b.click(); n++;
    }
  } catch(e){}
}
return n;
"""

# Post-send verification: the fetch tap bumps __tap.n synchronously, so a
# rising counter (or a composer the SPA cleared after submit) proves the request
# really fired; a silent no-send must never be reported as success.
JS_SEND_PROBE = _js_composer(r"""
var n0 = arguments[0] || 0;
var t = window.__tap;
var n = t ? t.n : -1;
var cleared = false, left = '';
var c = findComposer();
if (c) {
  var el = c.el;
  var v = (el.value !== undefined && el.value !== null) ? el.value : (el.textContent || '');
  left = String(v).slice(0, 30);
  cleared = !String(v).trim();
}
return {n: n, sent: n > n0, cleared: cleared, left: left, href: location.href};
""")

# Marks the scored composer so Selenium acts on the SAME element the JS scripts
# pick — first-visible lookups hit the sidebar search / title fields instead.
JS_MARK_COMPOSER = _js_composer(r"""
var c = findComposer();
document.querySelectorAll('[data-qwproxy]').forEach(function(e){ e.removeAttribute('data-qwproxy'); });
if (!c) return 'no-composer';
c.el.setAttribute('data-qwproxy', '1');
return c.tag + ':' + c.ph + ':' + c.w;
""")

# Attachments. chat.qwen.ai uploads only what arrives on the composer's drag
# channel: input#filesUpload sits outside the composer, so filling it with
# Selenium fires real change events that nothing uploads from (measured 2026-10:
# zero upload requests, "files":[] in the outgoing body), while the very same
# bytes dropped on the composer produce getstsToken + OSS PUT 200 and a
# populated "files" array. Bytes therefore travel as base64 and are rebuilt
# into File objects in page context.
JS_DROP_FILES = _js_composer(r"""
var files = arguments[0] || [];
var c = findComposer();
if (!c) return 'no-composer';
if (!window.DataTransfer || !window.DragEvent || !window.atob) return 'no-drop-api';
var dt = new DataTransfer();
for (const f of files) {
  try {
    var bin = atob(f.b64), arr = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    dt.items.add(new File([arr], f.name, {type: f.mime || 'application/octet-stream',
                                          lastModified: Date.now()}));
  } catch(err){ return 'file-build-failed:' + String(f.name) + ':' + err.message; }
}
if (!dt.items.length) return 'empty-datatransfer';
var r = c.el.getBoundingClientRect();
var shared = {bubbles: true, cancelable: true, dataTransfer: dt,
              clientX: Math.round(r.left + r.width / 2),
              clientY: Math.round(r.top + r.height / 2)};
// One dispatch on the composer alone: its delegated handlers already see the
// bubbled event, and dispatching again up the chain uploads every file twice.
for (const k of ['dragenter', 'dragover', 'drop']) {
  c.el.dispatchEvent(new DragEvent(k, shared));
}
return 'dropped:' + files.length + ':' + c.tag;
""")

# The composer mounts an attachment card only once the site's own upload has
# answered, so the card count doubles as the readiness signal: waiting on it
# turns a lost attachment into a logged degradation instead of a silent one.
# An image mounts .vision-item-container, a document (.txt/.pdf/...) mounts
# .fileitem-btn — counting images alone made every document attach read as
# "never attached", burning two 45 s waits and then re-dropping the same file.
JS_ATTACH_CARDS = r"""
return document.querySelectorAll(
  '.file-card-list .fileitem-btn, .file-card-list .vision-item-container').length;
"""

# A mounted card is not a read card: the site keeps a spinner (.anticon-spin
# .fileitem-loading-icon) and an «Анализ...» line on the card while it parses the
# file, and only then takes text. Measured 2026-10-14 with a probe that never
# touched the composer: quiet at 5.5 s (60 KB), 10.1 s (400 KB), 9.0 s
# (5 x 150 KB) — and every click/keystroke sent during that window restarts the
# analysis, which is what turned into the 92 s ElementClickInterceptedException
# of the earlier probe. So this is a pure read of the card state; the waiter
# around it must not click, focus or type.
JS_ATTACH_QUIET = r"""
var cards = [].slice.call(document.querySelectorAll(
  '.file-card-list .fileitem-btn, .file-card-list .vision-item-container'));
var spin = 0, hint = 0;
for (const c of cards) {
  if (c.querySelector('.anticon-spin, .fileitem-loading-icon, [class*="loading"], [class*="spin"]')) spin++;
  if (/анализ|чита|обработ|process|analy|loading/i.test((c.innerText || '').replace(/\s+/g, ' '))) hint++;
}
return {n: cards.length, spin: spin, hint: hint};
"""

# A card mounting only means the upload answered: the site then spends seconds
# reading the file, and a message sent during that window goes out with the file
# attached and the text missing — the report of 2026-10-14. Measured with a
# probe that never touched the composer, the cards go quiet (no spinner, no
# «Анализ...») at 5.5 s for 60 KB, 10.1 s for 400 KB and 9.0 s for five 150 KB
# documents, so the wait is seconds, not proportional to size — but nothing of
# the composer may be touched meanwhile, because every click or keystroke
# restarts the analysis. The floor is what the site needs on top of the card
# state to finish server-side; the ceiling bounds the wait when it never says so.
ATTACH_PARSE_FLOOR = 10.0
ATTACH_PARSE_CEILING = 90.0


PROGRESS_KEY = "__qw_progress__"


def _progress(text: str) -> dict:
    """One progress event of a staged turn.

    It carries no choices, so a client that reads only completion chunks skips
    it; the SSE layer in routes turns it into a comment line, which is the one
    channel a browser client can show while the answer has not started.
    """
    return {PROGRESS_KEY: text}


def _parse_floor(paths: list) -> float:
    """Seconds to keep waiting AFTER the cards went quiet, before typing.

    A quiet card means the SPA stopped rendering progress, not that the file is
    read server-side, and the site gives no signal for the rest. The numbers are
    the 2026-10-14 measurements plus the 10-20 s band asked for: an image card
    never shows «Анализ...» and reads in seconds, while documents went quiet at
    5.5 s (60 KB), 10.1 s (400 KB) and 9.0 s (5x150 KB), so the floor only grows
    with the total bytes handed over (2 s per started MB over 1 MB), never with
    the count.
    """
    image_only = paths and all(
        (mimetypes.guess_type(str(p))[0] or "").startswith("image/") for p in paths)
    if image_only:
        return 4.0
    total_mb = 0
    for p in paths:
        try:
            total_mb += Path(p).stat().st_size / (1024 * 1024)
        except OSError:  # noqa: PERF203
            continue
    extra = max(0.0, (total_mb - 1.0)) * 2.0
    return min(ATTACH_PARSE_CEILING - 10.0, ATTACH_PARSE_FLOOR + extra)

JS_PROBE = r"""
var hd = document.querySelector('header') || document.querySelector('[class*=header]') || document.body;
var t = hd ? hd.innerText.slice(0,400) : '';
return {href: location.href,
        loggedOut: /Войти|Log in|Зарегистрир/.test(t),
        token: !!(localStorage.getItem('token') || localStorage.getItem('auth_token') || localStorage.getItem('access_token'))};
"""

# SPA token lives in localStorage (raw or JSON-wrapped); it is short-lived and
# rotated by the SPA, hence always re-read immediately before use.
JS_TOKEN = r"""
var ks = ['token','auth_token','access_token'];
for (var i=0;i<ks.length;i++){
  var v = localStorage.getItem(ks[i]);
  if (!v) continue;
  if (v[0] === '{') {
    try {
      var o = JSON.parse(v);
      var t = o.token || o.access_token || o.auth_token;
      if (t) return String(t);
    } catch(e){}
    continue;
  }
  if (v[0] === '"') { try { return JSON.parse(v); } catch(e){} }
  return v;
}
return null;
"""

# Header values must be ISO-8859-1: under ru-RU new Date().toString() contains
# Cyrillic ("(Москва…)") and fetch() throws — hence sane().
JS_PAGE_GET = r"""
var tok = arguments[0], path = arguments[1];
function sane(s){ return String(s===undefined||s===null?'':s).replace(/[\x00-\x1F\x7F-\uFFFF]/g,''); }
var h = {'accept':'application/json','content-type':'application/json',
  'version':'%s','source':'web','timezone':sane(new Date().toString()),
  'x-request-id':crypto.randomUUID(),'accept-language':'ru-RU,ru;q=0.8',
  'x-request-origin':location.origin,'authorization':'Bearer '+sane(tok)};
return fetch(path,{headers:h}).then(function(r){
  return r.text().then(function(t){ return {status:r.status, ct:r.headers.get('content-type'), url:String(r.url||''), body:t}; });
});
""" % SPA_HEADER_VERSION

JS_SELECT_MODEL = r"""
var ver = arguments[0], tail = arguments[1];
function vis(e){ return e && e.offsetParent; }
var opener = null;
var cand = [...document.querySelectorAll('button, [role="button"], [class*=cursor-pointer]')].filter(vis);
for (const e of cand) {
  var r = e.getBoundingClientRect();
  var t = (e.innerText||'').trim();
  if (r.top < 140 && /^Qwen/i.test(t)) { opener = e; break; }
}
if (!opener) return 'no-opener';
opener.click();
return 'opener-clicked';
"""

JS_PICK_MODEL_OPTION = r"""
var ver = arguments[0], tail = arguments[1];
function vis(e){ return e && e.offsetParent; }
var want = (ver + ' ' + tail).replace(/\s+/g,' ').trim();
var best = null;
var cand = [...document.querySelectorAll('button, [role="menuitem"], [role="option"], li, div')].filter(vis);
for (const e of cand) {
  var t = (e.innerText||'').trim();
  if (!t || t.length > 120) continue;
  var lt = t.toLowerCase();
  if (lt.indexOf(ver) < 0) continue;
  if (tail && lt.indexOf(tail) < 0) continue;
  best = e;
}
if (!best) return 'no-option:' + want;
best.click();
return 'picked:' + (best.innerText||'').trim().slice(0,60);
"""


def parse_sse(text: str):
    """(full_text, response_id, finish, errors) from raw SSE captured by the tap."""
    full, rid, finish, errs = "", None, None, []
    for L in (text or "").split("\n"):
        L = L.strip()
        if not L.startswith("data:"):
            continue
        d = L[5:].strip()
        if d == "[DONE]":
            finish = "[DONE]"
            continue
        try:
            ev = json.loads(d)
        except Exception:  # noqa: BLE001
            continue
        rc = ev.get("response.created")
        if isinstance(rc, dict) and rc.get("response_id"):
            rid = rc["response_id"]
        if ev.get("response_id"):
            rid = ev["response_id"]
        for ch in ev.get("choices") or []:
            delta = ch.get("delta") or {}
            piece = delta.get("content") or ""
            if isinstance(piece, list):
                piece = "".join(x.get("text", "") for x in piece if isinstance(x, dict))
            full += piece
            if delta.get("status") == "finished" or ch.get("finish_reason"):
                finish = str(ch.get("finish_reason") or delta.get("status"))
        if ev.get("code") or ev.get("error"):
            errs.append(json.dumps(ev, ensure_ascii=False)[:200])
    return full, rid, finish, errs


def _split_model(model: str):
    """'qwen3.8-max-preview' -> ('3.8', 'max-preview'); 'qwen3.7-plus' -> ('3.7','plus')."""
    m = model.lower()
    if m.startswith("qwen"):
        m = m[4:]
    m = m.lstrip("-")
    parts = m.split("-", 1)
    ver = parts[0] if parts else ""
    tail = parts[1] if len(parts) > 1 else ""
    return ver, tail


class _AuthLost(RuntimeError):
    pass


class _BrowserWorker:
    """All Selenium calls; confined to the client's single executor thread."""

    def __init__(self):
        self.driver = None

    def ensure(self):
        if self.driver is not None:
            try:
                self.driver.current_url
                return
            except Exception:  # noqa: BLE001
                self.close()
        try:
            from selenium import webdriver
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("selenium is not installed — run setup.bat (pip install -r requirements.txt)") from e
        if not QWEN_BROWSER_HEADLESS and not Path(QWEN_CHROME_PATH).exists():
            raise RuntimeError(f"Chrome not found at {QWEN_CHROME_PATH} — set QWEN_CHROME_PATH in .env")
        Path(QWEN_BROWSER_PROFILE).mkdir(parents=True, exist_ok=True)

        from selenium.webdriver.chrome.options import Options

        opts = Options()
        if Path(QWEN_CHROME_PATH).exists():
            opts.binary_location = QWEN_CHROME_PATH
        args = [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-features=Translate",
            f"--lang={QWEN_BROWSER_LANGUAGE}",
            f"--user-data-dir={QWEN_BROWSER_PROFILE}",
            "--window-size=1280,950",
        ]
        if QWEN_BROWSER_HEADLESS:
            args.append("--headless=new")
        for a in args:
            opts.add_argument(a)
        opts.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
        opts.add_experimental_option("useAutomationExtension", False)
        opts.add_experimental_option("prefs", {
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
            "profile.password_manager_leak_detection": False,
            "profile.default_content_setting_values.notifications": 2,
        })

        driver = webdriver.Chrome(options=opts)
        driver.set_page_load_timeout(QWEN_PAGE_TIMEOUT)
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": TAP_JS})
        self.driver = driver
        logger.info("Browser started (profile=%s, headless=%s)", QWEN_BROWSER_PROFILE, QWEN_BROWSER_HEADLESS)
        driver.get(BASE + "/")
        time.sleep(4)
        self._wait_input()

    def close(self):
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:  # noqa: BLE001
                pass
            self.driver = None

    # -- small sync primitives used through the executor --------------------

    def _js(self, script, *args):
        return self.driver.execute_script(script, *args)

    def _wait_input(self, timeout: int = 45):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                if self._js(JS_HAS_INPUT):
                    return True
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1)
        return False

    def probe(self) -> dict:
        try:
            return self._js(JS_PROBE) or {}
        except Exception:  # noqa: BLE001
            return {}

    def token(self) -> Optional[str]:
        try:
            return self._js(JS_TOKEN)
        except Exception:  # noqa: BLE001
            return None

    def go_home(self):
        self.driver.get(BASE + "/")
        time.sleep(2)
        return self._wait_input()

    def open_chat(self, chat_id: str) -> str:
        """Open /c/<id>; the SPA bounces to '/' for chats it cannot render."""
        self.driver.get(f"{BASE}/c/{chat_id}")
        time.sleep(2)
        self._wait_input()
        return self.driver.current_url or ""

    def page_get(self, tok: str, path: str) -> dict:
        r = self._js(JS_PAGE_GET, tok, path)
        return r or {"status": 0, "ct": "", "body": ""}

    def ui_send(self, text: str, strict: bool = False) -> bool:
        """Stale-safe: React re-renders after typing, so re-locate the element.

        The composer is marked by the shared scored finder (JS_MARK_COMPOSER),
        so native typing and the JS fallback act on the element the SPA really
        uses. Success is VERIFIED against the fetch tap (True only when the
        request was observed or the SPA consumed the text we saw typed): a
        blind True made dead clicks (disabled/wrong button, empty composer)
        look like success and surfaced as a 30 s timeout upstream.

        strict=True marks an attachment turn, where the two shortcuts that are
        safe for a plain text turn both destroy the message: the JS value setter
        can leave React state empty while the text is visible (the send then
        carries an empty body next to a perfect-looking attachment), and the far
        send-button fallback clicks a button that belongs to something else.
        With an attachment staged both are refused and the attempt is retried.
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys

        for attempt in range(3):
            try:
                try:
                    self._js(JS_DISMISS)
                except Exception:  # noqa: BLE001
                    pass
                mark = str(self._js(JS_MARK_COMPOSER) or "")
                if mark == "no-composer":
                    logger.info("ui_send attempt %d: no composer yet", attempt)
                    time.sleep(2)
                    continue
                n0 = self.tap_n()
                typed_natively = False
                if mark.split(":", 1)[0] in ("textarea", "input", "ce"):
                    try:
                        el = self.driver.find_element(By.CSS_SELECTOR, "[data-qwproxy]")
                        try:
                            el.click()
                        except Exception as e:  # noqa: BLE001
                            # While the site holds the composer the click is
                            # intercepted; focus through JS reaches the same
                            # element and keystrokes land anyway (measured
                            # 2026-10-14 on a parsing composer).
                            logger.info("ui_send native click %d failed (%s), focusing instead",
                                        attempt, type(e).__name__)
                            self._js("arguments[0].focus();", el)
                        try:
                            el.send_keys(Keys.CONTROL, "a")
                            el.send_keys(Keys.DELETE)
                        except Exception:  # noqa: BLE001
                            pass
                        el.send_keys(text)
                        typed_natively = True
                    except Exception as e:  # noqa: BLE001
                        logger.warning("ui_send native path %d: %s %s",
                                       attempt, type(e).__name__, str(e)[:120])
                if not typed_natively:
                    if strict:
                        # See the docstring: a JS-setter write is not evidence the
                        # SPA holds the text, and with an attachment in play that
                        # is exactly how a message goes out empty.
                        logger.warning("ui_send attempt %d: native typing failed with an "
                                       "attachment staged, not falling back to the JS setter",
                                       attempt)
                        time.sleep(1.5)
                        continue
                    r = str(self._js(JS_TYPE, text) or "")
                    logger.info("ui_send JS type %d: %s", attempt, r)
                    if not r.startswith("typed"):
                        time.sleep(1.5)
                        continue
                time.sleep(0.6)
                p1 = self._js(JS_SEND_PROBE, n0) or {}
                if not str(p1.get("left") or "").strip():
                    logger.warning("ui_send attempt %d: composer stayed empty after typing "
                                   "(native=%s)", attempt, typed_natively)
                    time.sleep(1.0)
                    continue
                r = str(self._js(JS_CLICK_SEND, strict) or "")
                logger.info("ui_send attempt %d send-click: %s", attempt, r)
                # The verdict is the leading token; JS may append a read-only
                # geometry dump after it, so match on prefix, not equality.
                if r.startswith("no-send-button") or r.startswith("send-not-near"):
                    if strict and r.startswith("send-not-near"):
                        # Clicking a far button would take the turn somewhere
                        # else; retry and let the next pass find the real one.
                        logger.warning("ui_send attempt %d: send button not near the "
                                       "composer with an attachment staged", attempt)
                        time.sleep(1.5)
                        continue
                    try:
                        if typed_natively:
                            self.driver.find_element(
                                By.CSS_SELECTOR, "[data-qwproxy]").send_keys(Keys.ENTER)
                        else:
                            self._js(JS_ENTER)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("ui_send enter fallback %d: %s", attempt, str(e)[:100])
                if self._wait_send_fired(n0, 6.0):
                    return True
                p2 = self._js(JS_SEND_PROBE, n0) or {}
                logger.warning("ui_send attempt %d: no /chat/completions request seen "
                               "(click=%s tap=%s composer=%r href=%s)", attempt, r,
                               p2.get("n"), str(p2.get("left"))[:40], p2.get("href"))
                time.sleep(1.5)
            except Exception as e:  # noqa: BLE001
                logger.warning("ui_send attempt %d crashed: %s %s",
                               attempt, type(e).__name__, str(e)[:120])
                time.sleep(1.5)
        return False

    def ui_attach(self, paths: list) -> bool:
        """Hands staged files to the composer's drop channel before the send.

        The bytes are read here and rebuilt as File objects in page context
        because the drop is the only intake the SPA uploads from (see
        JS_DROP_FILES). Success means one attachment card per file mounted in
        the composer, which is the site's own upload-finished signal: a turn
        that lost its attachments is reported, not logged away as handed over.
        """
        staged = []
        for p in paths:
            fp = Path(p)
            if not fp.exists():
                continue
            try:
                raw = fp.read_bytes()
            except OSError as e:  # noqa: BLE001
                logger.warning("ui_attach: unreadable %s: %s", fp.name, str(e)[:120])
                continue
            staged.append({
                "name": fp.name,
                "mime": mimetypes.guess_type(fp.name)[0] or "application/octet-stream",
                "b64": base64.b64encode(raw).decode(),
            })
        if not staged:
            logger.warning("ui_attach: no staged file survived to the browser step")
            return False

        def cards() -> int:
            try:
                n = self._js(JS_ATTACH_CARDS)
            except Exception:  # noqa: BLE001
                return 0
            return n if isinstance(n, int) else 0

        base_cards = cards()
        for attempt in (1, 2):
            try:
                res = str(self._js(JS_DROP_FILES, staged) or "")
                logger.info("ui_attach attempt %d: %s", attempt, res)
                if not res.startswith("dropped"):
                    time.sleep(1.0)
                    continue
                if self._wait_attach_cards(base_cards, len(staged)):
                    logger.info("ui_attach: %d file(s) uploaded into the composer",
                                len(staged))
                    return True
                took = max(cards() - base_cards, 0)
                if took:
                    # Re-dropping the whole set would send the taken file twice,
                    # so a partial hand-off is reported and left alone.
                    logger.warning("ui_attach: the composer took %d of %d file(s); "
                                   "not re-dropping to avoid duplicates",
                                   took, len(staged))
                    return False
                logger.warning("ui_attach attempt %d: the composer showed no "
                               "attachment card for the dropped file(s)", attempt)
            except Exception as e:  # noqa: BLE001
                logger.warning("ui_attach attempt %d failed: %s %s",
                               attempt, type(e).__name__, str(e)[:150])
                time.sleep(1.0)
        return False

    def attach_cards(self) -> int:
        """How many attachment cards the composer shows right now.

        Reads the DOM and nothing else: while the site is reading a document,
        a click or a keystroke aimed at the composer restarts that reading. A
        failed read counts as zero cards so the caller reports a missed
        attachment instead of losing the session over a transient JS error.
        """
        try:
            n = self._js(JS_ATTACH_CARDS)
        except Exception as e:  # noqa: BLE001
            logger.warning("attachment card count failed: %s", str(e)[:120])
            return 0
        return n if isinstance(n, int) else 0

    def attach_state(self) -> dict:
        """The site's own card state: n (cards), spin (still spinning),
        hint (still labelled as being read).

        An empty dict means the read itself failed, so the caller keeps
        waiting rather than typing while files may still be loading.
        """
        try:
            state = self._js(JS_ATTACH_QUIET)
        except Exception as e:  # noqa: BLE001
            logger.warning("attachment state failed: %s", str(e)[:120])
            return {}
        return state if isinstance(state, dict) else {}

    def _wait_attach_cards(self, base_cards: int, want: int, timeout: float = 45.0) -> bool:
        """Polls for base_cards+want attachment cards and holds one extra poll,
        so a card whose upload is still running is not read as ready."""
        deadline = time.time() + timeout
        held = 0
        while time.time() < deadline:
            time.sleep(1.0)
            try:
                n = self._js(JS_ATTACH_CARDS)
            except Exception:  # noqa: BLE001
                n = None
            n = n if isinstance(n, int) else 0
            if n >= base_cards + want:
                held += 1
                if held >= 2:
                    return True
            else:
                held = 0
        return False

    def _wait_send_fired(self, n0: int, timeout: float) -> bool:
        """Poll until the tap counter rises (request fired) or the composer
        empties. Safe to treat 'cleared' as sent because the caller verified
        the text in the composer right before the send click."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                p = self._js(JS_SEND_PROBE, n0) or {}
            except Exception:  # noqa: BLE001
                p = {}
            if p.get("sent") or p.get("cleared"):
                return True
            time.sleep(0.4)
        return False

    def tap_n(self) -> int:
        try:
            return int(self._js("return window.__tap ? window.__tap.n : 0;") or 0)
        except Exception:  # noqa: BLE001
            return 0

    def tap_delta(self, pos: int) -> dict:
        return self._js(JS_TAP_DELTA, pos) or {}

    def tap_full(self):
        return self._js(JS_TAP_FULL)

    def try_select_model(self, model: str) -> bool:
        ver, tail = _split_model(model)
        try:
            r = self._js(JS_SELECT_MODEL, ver, tail)
            if not str(r or "").startswith("opener"):
                logger.info("Model combo not found (%s) — SPA default will be used", r)
                return False
            time.sleep(0.8)
            r2 = self._js(JS_PICK_MODEL_OPTION, ver, tail)
            logger.info("Model select: %s -> %s", model, r2)
            if not str(r2 or "").startswith("picked"):
                self._dismiss_menu()
            return str(r2 or "").startswith("picked")
        except Exception as e:  # noqa: BLE001
            logger.warning("try_select_model(%s) failed: %s", model, str(e)[:120])
            self._dismiss_menu()
            return False

    def _dismiss_menu(self) -> None:
        """Close a stuck Radix dropdown with a real Escape key: a synthetic
        keydown on document never reaches the React container, while an open
        backdrop silently intercepts every later click."""
        from selenium.webdriver.common.keys import Keys
        try:
            self.driver.switch_to.active_element.send_keys(Keys.ESCAPE)
            return
        except Exception:  # noqa: BLE001
            pass
        try:
            self._js(JS_DISMISS)
        except Exception:  # noqa: BLE001
            pass


class QwenClient:
    """Qwen Chat client with persistent chat sessions (chat_id + parent_id).

    Public surface kept compatible with the previous httpx implementation;
    the transport is now the SPA UI inside Chrome (see module docstring).
    """

    def __init__(self):
        self._base_url = BASE
        self._chat_id: Optional[str] = None
        self._parent_id: Optional[str] = None
        self._logged_in: Optional[bool] = None
        self._w = _BrowserWorker()
        self._ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qwen-browser")
        self._lock = asyncio.Lock()
        atexit.register(self._w.close)

    # ------------------------------------------------------------------ misc

    def _run(self, fn, *args):
        loop = asyncio.get_event_loop()
        return loop.run_in_executor(self._ex, partial(fn, *args))

    async def _safely(self, fn, *args):
        """Run on the browser thread; reap the driver on any browser failure."""
        try:
            return await self._run(fn, *args)
        except Exception:
            await self._run(self._w.close)
            raise

    @staticmethod
    def _extract_text(messages: list) -> str:
        # Only the newest user turn may reach the composer: the SPA posts the
        # typed text as one user message, and earlier turns already live in the
        # site-side chat history (chatId/parentId). Joining the whole array is
        # what posted the entire transcript as a single user bubble.
        def content_to_text(content) -> str:
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "".join(
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            return ""

        last_user = ""
        turn_count = 0
        for msg in messages or []:
            if not isinstance(msg, dict):
                continue
            if msg.get("files"):
                # The GUI sends files request-level (payload "files"), never
                # per message, so a per-message list really is unsupported here.
                logger.warning("Message-level files are ignored; attachments travel in the request-level files field")
            turn_count += 1
            text = content_to_text(msg.get("content")).strip()
            if text and msg.get("role") in ("user", None):
                last_user = text
        if last_user:
            if turn_count > 1:
                logger.info(
                    "Typed only the latest user message (%d turns in the array) —"
                    " earlier turns stay in the site-side chat history", turn_count)
            return last_user
        # No user turn (e.g. a lone message without a role): last non-empty content.
        for msg in reversed(messages or []):
            if isinstance(msg, dict):
                text = content_to_text(msg.get("content")).strip()
                if text:
                    return text
        return ""

    # ------------------------------------------------------------- auth/state

    async def auth_state(self) -> dict:
        """{logged_in, href, profile, detail} — starts the browser if needed."""
        await self._safely(self._w.ensure)
        p = await self._safely(self._w.probe)
        tok = await self._safely(self._w.token)
        self._logged_in = bool(tok) and not p.get("loggedOut")
        return {
            "logged_in": bool(self._logged_in),
            "href": p.get("href", ""),
            "browser_started": True,
            "profile": QWEN_BROWSER_PROFILE,
            "detail": "" if self._logged_in else "Open the Chrome window and log in to chat.qwen.ai once.",
        }

    async def check_token(self) -> str:
        tok = await self._safely(self._w.token)
        if not tok:
            p = await self._safely(self._w.probe)
            self._logged_in = False
            raise _AuthLost(
                "Chrome session is not logged in (or token expired). Log in once in the "
                f"proxy's Chrome window (profile: {QWEN_BROWSER_PROFILE}). Current page: {p.get('href','?')}"
            )
        self._logged_in = True
        return tok

    # ---------------------------------------------------------------- session

    def reset_chat(self):
        """Forget the current chat — the next message starts a brand-new one."""
        self._chat_id = None
        self._parent_id = None

    def set_session(self, chat_id: str, parent_id: Optional[str] = None):
        self._chat_id = chat_id
        self._parent_id = parent_id

    @property
    def chat_id(self) -> Optional[str]:
        return self._chat_id

    @property
    def parent_id(self) -> Optional[str]:
        return self._parent_id

    @property
    def is_authenticated(self) -> bool:
        return bool(self._logged_in) if self._logged_in is not None else False

    @property
    def saved_at(self) -> Optional[str]:
        p = Path(QWEN_BROWSER_PROFILE)
        if p.exists():
            try:
                ts = p.stat().st_mtime
                return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            except OSError:
                pass
        return None

    async def shutdown(self):
        await self._run(self._w.close)

    # ------------------------------------------------------------------ reads

    async def _page_get(self, path: str) -> dict:
        await self._safely(self._w.ensure)
        tok = await self.check_token()
        return await self._safely(self._w.page_get, tok, path)

    async def get_models(self) -> list:
        for path in ("/api/v2/models/", "/api/v2/models", "/api/models"):
            try:
                r = await self._page_get(path)
            except _AuthLost:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("get_models %s failed: %s", path, str(e)[:120])
                continue
            if r.get("status") != 200:
                continue
            try:
                j = json.loads(r.get("body") or "{}")
            except json.JSONDecodeError:
                continue
            data = j.get("data", j) if isinstance(j, dict) else j
            if isinstance(data, dict):
                data = data.get("data") or data.get("models") or []
            out = []
            if isinstance(data, list):
                for m in data:
                    if isinstance(m, str):
                        out.append(m)
                    elif isinstance(m, dict):
                        mid = m.get("id") or m.get("name") or m.get("model_name")
                        if mid:
                            out.append(str(mid))
            if out:
                return out
        logger.warning("Live model list unavailable — falling back to DEFAULT_MODELS")
        return list(DEFAULT_MODELS)

    async def get_chats(self) -> list:
        # A long-lived SPA silently lets its access token go stale and the site
        # then answers HTTP 200 WITHOUT the success envelope (e.g. {"detail":...}).
        # Reloading the home page makes the SPA rotate the token, so retry once
        # after go_home before declaring the list unreadable. A genuinely empty
        # account still carries "data": [] and is never retried.
        for attempt in (1, 2):
            r = await self._page_get("/api/v2/chats/?page=1")
            status = r.get("status")
            body = r.get("body") or ""
            if status in (401, 403) and attempt == 1:
                logger.warning("Chats list HTTP %s — reloading SPA to refresh the token", status)
                await self._safely(self._w.go_home)
                await asyncio.sleep(1.0)
                continue
            if status != 200:
                raise RuntimeError(f"Chats list HTTP {status}: {body[:200]}")
            try:
                j = json.loads(body or "{}")
            except json.JSONDecodeError as e:
                raise RuntimeError(f"Chats list is not JSON: {body[:200]}") from e
            if isinstance(j, dict) and "data" in j:
                raw = j.get("data")
                if isinstance(raw, list):
                    return raw
                if isinstance(raw, dict):
                    return raw.get("chats", raw.get("sessions", []))
                return []
            if attempt == 1:
                logger.warning("Chats list: 200 without data envelope (url=%s body=%.160s) — reloading SPA and retrying",
                               r.get("url"), body)
                await self._safely(self._w.go_home)
                await asyncio.sleep(1.0)
                continue
            raise RuntimeError(f"Chats list: unexpected answer after retry: {body[:200]}")
        return []

    async def get_chat_detail(self, chat_id: str) -> dict:
        r = await self._page_get(f"/api/v2/chats/{chat_id}/")
        if r.get("status") != 200:
            logger.warning("Chat detail %s HTTP %s", chat_id, r.get("status"))
            return {}
        try:
            j = json.loads(r.get("body") or "{}")
        except json.JSONDecodeError:
            return {}
        return j.get("data", {}) if isinstance(j, dict) else {}

    async def get_chat_messages(self, chat_id: str) -> list:
        """Messages live in the chat detail (get_chat_detail) and in local logs."""
        return []

    # ------------------------------------------------------------------- send

    async def _prepare_page(self):
        """Point the SPA at the target chat (or at home for a brand-new one)."""
        await self._safely(self._w.ensure)
        await self.check_token()
        if self._chat_id:
            url = await self._safely(self._w.open_chat, self._chat_id)
            if self._chat_id in (url or ""):
                return
            # The SPA bounces to '/' for chats it cannot render (e.g. without
            # messages): continue as a NEW chat — the caller re-syncs the sidecar.
            logger.warning("Chat %s is not openable (SPA bounced to %s) — starting a new chat", self._chat_id, url)
            self._chat_id = None
            self._parent_id = None
        else:
            await self._safely(self._w.go_home)

    async def _wait_attachments_read(self, mounted: int,
                                     attachments: list) -> AsyncGenerator[dict, None]:
        """Waits until the SPA itself stops reading the staged files.

        The composer is not touched for the whole wait. Every click or
        keystroke aimed at it restarts the site's own analysis — measured
        2026-10-14, a 60 KB text file that pokes every 2s stayed busy for 92s
        while the same file left alone went quiet in 5.5s. Readiness is read
        off the attachment cards: no spinner and no «reading» label on any of
        them, held over two polls. The floor covers the site's own upload that
        never shows on a card; the ceiling is the give-up point, after which
        the turn goes out anyway rather than being lost.
        """
        floor = _parse_floor(attachments)
        t0 = time.time()
        deadline = t0 + ATTACH_PARSE_CEILING
        held = 0
        while time.time() < deadline:
            await asyncio.sleep(1.5)
            state = await self._safely(self._w.attach_state)
            elapsed = time.time() - t0
            if not state:
                # A failed read is not the same as a quiet composer: keep waiting.
                held = 0
                continue
            cards = int(state.get("n") or 0)
            busy = int(state.get("spin") or 0) + int(state.get("hint") or 0)
            if cards == 0:
                logger.warning("the %d attachment card(s) vanished before the SPA "
                               "finished reading them", mounted)
                yield _progress("Файлы пропали из поля ввода — отправляю только текст")
                return
            held = held + 1 if busy == 0 else 0
            if held >= 2:
                if elapsed >= floor:
                    logger.info("the SPA read %d card(s) in %.1fs (floor %.1fs)",
                                cards, elapsed, floor)
                    yield _progress(f"Квен прочитал файлы за {elapsed:.0f} с")
                    return
                yield _progress(f"Файлы готовы, отправлю через {floor - elapsed:.0f} с")
                continue
            yield _progress(f"Сайт читает файлы: занято {busy} из {cards} карточек, "
                            f"{elapsed:.0f} с")
        logger.warning("the SPA never settled the attachment state within %.0fs — "
                       "sending anyway", ATTACH_PARSE_CEILING)
        yield _progress(f"Чтение не завершилось за {ATTACH_PARSE_CEILING:.0f} с — "
                        f"отправляю как есть")

    async def send_message(
        self,
        messages: list,
        model: str = "qwen3.7-plus",
        stream: bool = True,
        system_message: str = None,
        attachments: Optional[list] = None,
    ) -> AsyncGenerator[dict, None]:
        text = self._extract_text(messages)
        if not text:
            raise RuntimeError("Empty message content")
        if system_message:
            logger.warning("system_message is ignored by the browser transport (SPA UI has no system field)")

        async with self._lock:
            for attempt in (1, 2):
                # The turn is staged in the order the site needs: page and model
                # first, then the whole attach cycle, then the site's own reading
                # of the files, and only then the text. Typing earlier throws the
                # message away — the composer still holds a message it will never
                # send once the SPA starts reading a document (user report and
                # 2026-10-14 timing probe).
                yield _progress(f"Готовлю чат и модель {model}")
                await self._prepare_page()
                await self._safely(self._w.try_select_model, model)
                if attachments:
                    yield _progress(f"Прикрепляю {len(attachments)} файл(ов)")
                    base_cards = await self._safely(self._w.attach_cards)
                    attached = await self._safely(self._w.ui_attach, attachments)
                    # Counted from the cards, not from the request: the composer
                    # takes what it takes (5 documents at most) and re-dropping
                    # the rest would send what it already took twice.
                    mounted = max(int(await self._safely(self._w.attach_cards) or 0)
                                  - int(base_cards or 0), 0)
                    if mounted < len(attachments):
                        logger.warning("attachments: the composer took %d of %d file(s)%s",
                                       mounted, len(attachments),
                                       "" if attached else " (ui_attach reported failure)")
                    if not mounted:
                        # Losing the file is bad enough; losing the message on top
                        # of it is worse, so the turn still goes out as text only —
                        # but the client is told, this is never a silent degrade.
                        logger.warning("Attachments never reached the SPA — the turn "
                                       "goes out as text only")
                        yield _progress("Файлы не прикрепились — отправляю только текст")
                    else:
                        if mounted < len(attachments):
                            yield _progress(f"Сайт принял {mounted} из {len(attachments)} файл(ов)")
                        async for ev in self._wait_attachments_read(mounted, attachments):
                            yield ev
                n0 = await self._safely(self._w.tap_n)
                yield _progress("Отправляю сообщение")
                if not await self._safely(self._w.ui_send, text, bool(attachments)):
                    # What failed is the send, not the input: the composer was already
                    # found and typed into, and no /chat/completions ever left the
                    # browser. Blaming reload/login sends the reader to the wrong page —
                    # the ui_send attempt lines above carry the verdict and the dump.
                    raise RuntimeError("The turn never left the browser — the send "
                                       "click or the typed text was refused (see the "
                                       "ui_send attempt lines in the server log)")
                yield _progress("Запрос ушёл на сайт, жду ответ")

                pos = 0
                seen_request = False
                raw_parts: list = []
                rid = None
                cid_live = None
                saw_finish = False
                full_text = ""
                t0 = time.time()
                st: dict = {}

                while True:
                    if time.time() - t0 > QWEN_SEND_TIMEOUT:
                        raise TimeoutError(f"SPA did not finish the answer in {QWEN_SEND_TIMEOUT}s")
                    await asyncio.sleep(0.3 if seen_request else 0.7)
                    st = await self._safely(self._w.tap_delta, pos)
                    if st.get("err"):
                        raise RuntimeError(f"SPA fetch failed: {str(st['err'])[:200]}")
                    if st.get("n", 0) > n0:
                        seen_request = True
                    href = st.get("href") or ""
                    if "/c/" in href:
                        cid_live = href.rsplit("/c/", 1)[-1].split("?")[0].split("#")[0]
                    if not seen_request and time.time() - t0 > 30:
                        raise RuntimeError(
                            "The SPA did not send the request — no /chat/completions fetch "
                            "was seen (captcha / anti-bot challenge / logged out?). "
                            f"href={href!r} tap_n={st.get('n', 0)} "
                            f"status={st.get('status') or 0} model={model!r}. "
                            "Check the Chrome window."
                        )
                    cut = st.get("cut") or ""
                    if cut:
                        pos = int(st.get("pos") or (pos + len(cut)))
                        raw_parts.append(cut)
                        chunks, r2, fin2, pieces = self._consume_sse_lines(cut, model)
                        if r2:
                            rid = r2
                        saw_finish = saw_finish or bool(fin2)
                        full_text += "".join(pieces)
                        for ch in chunks:
                            if stream:
                                yield ch
                    if st.get("has") and st.get("done"):
                        break

                raw = "".join(raw_parts)
                full2, rid2, finish, errs = parse_sse(raw)
                if full2:
                    full_text = full2
                rid = rid2 or rid

                status = st.get("status") or 0
                auth_bad = status in (401, 403) or any(
                    ("nauthorized" in e or "Токен" in e or "token" in e.lower()) for e in errs
                ) or "Токен отозван" in raw
                if auth_bad and attempt == 1:
                    logger.warning("Auth error in SSE (status=%s errs=%s) — reloading SPA and retrying once", status, errs[:2])
                    await self._safely(self._w.go_home)
                    await self.check_token()
                    continue
                if errs:
                    raise RuntimeError("Qwen returned an error: " + " | ".join(errs[:3]))
                if not rid and not full_text:
                    fin0 = await self._safely(self._w.tap_full)
                    raise RuntimeError(
                        f"No answer captured (status={status}, ct={(fin0 or {}).get('ct','')!r}, "
                        f"body_head={raw[:200]!r})"
                    )

                if cid_live:
                    self._chat_id = cid_live
                if rid:
                    self._parent_id = rid
                fin = await self._safely(self._w.tap_full)
                self._log_payload_model(fin, model)
                self._logged_in = True

                if DEBUG_API:
                    logger.debug("[SPA] answer len=%d rid=%s chat=%s", len(full_text), rid, self._chat_id)

                if not stream:
                    yield {
                        "id": f"chatcmpl-{uuid.uuid4()}",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": model,
                        "chatId": self._chat_id,
                        "responseId": rid,
                        "choices": [{"index": 0,
                                     "message": {"role": "assistant", "content": full_text},
                                     "finish_reason": "stop"}],
                    }
                    return

                if not saw_finish:
                    yield self._final_chunk(model, rid)
                return

    def _consume_sse_lines(self, cut: str, model: str):
        """(chunks, rid, finished, content_pieces) for a freshly arrived SSE block."""
        chunks, rid, finished, pieces = [], None, False, []
        for L in cut.split("\n"):
            L = L.strip()
            if not L.startswith("data:"):
                continue
            d = L[5:].strip()
            if d == "[DONE]":
                finished = True
                continue
            try:
                ev = json.loads(d)
            except json.JSONDecodeError:
                continue
            rc = ev.get("response.created")
            if isinstance(rc, dict) and rc.get("response_id"):
                rid = rc["response_id"]
            if ev.get("response_id"):
                rid = ev["response_id"]
            piece = ""
            for ch in ev.get("choices") or []:
                delta = ch.get("delta") or {}
                p = delta.get("content") or ""
                if isinstance(p, list):
                    p = "".join(x.get("text", "") for x in p if isinstance(x, dict))
                piece += p
                if delta.get("status") == "finished" or ch.get("finish_reason"):
                    finished = True
            if piece:
                pieces.append(piece)
            chunks.append(self._qwen_event_to_openai_chunk(ev, model))
        return chunks, rid, finished, pieces

    def _qwen_event_to_openai_chunk(self, event: dict, model: str) -> dict:
        base = {
            "id": event.get("id", event.get("response_id", f"chatcmpl-{uuid.uuid4()}")),
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
        }
        if "chat_id" in event:
            base["chatId"] = event["chat_id"]
        elif self._chat_id:
            base["chatId"] = self._chat_id
        if "response_id" in event:
            base["responseId"] = event["response_id"]
        if "response.created" in event and isinstance(event["response.created"], dict):
            rc = event["response.created"]
            if rc.get("response_id"):
                base["responseId"] = rc["response_id"]
        if event.get("parentId") or event.get("parent_id"):
            base["parentId"] = event.get("parentId") or event.get("parent_id")

        if "choices" in event and event["choices"]:
            base["choices"] = event["choices"]
        elif "response.created" in event:
            base["response.created"] = event["response.created"]
        elif "output" in event:
            output = event["output"]
            if isinstance(output, dict):
                base["choices"] = [{"index": 0, "delta": {"role": "assistant", "content": output.get("text", "")}}]
            elif isinstance(output, str):
                base["choices"] = [{"index": 0, "delta": {"role": "assistant", "content": output}}]
        return base

    def _final_chunk(self, model: str, rid: Optional[str]) -> dict:
        chunk = {
            "id": f"chatcmpl-{rid or uuid.uuid4()}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        if self._chat_id:
            chunk["chatId"] = self._chat_id
        if rid:
            chunk["responseId"] = rid
        return chunk

    def _log_payload_model(self, fin: Optional[dict], requested: str):
        try:
            body = json.loads((fin or {}).get("reqBody") or "{}")
            got = body.get("model")
            if got and got != requested:
                logger.warning("SPA sent model=%r instead of requested %r — pick it in the Chrome window once", got, requested)
        except Exception:  # noqa: BLE001
            pass
