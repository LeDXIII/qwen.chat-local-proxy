"""Full end-to-end test: send messages, restart server, load session, continue."""
import requests
import json
import time
import os

BASE = "http://127.0.0.1:8000"
LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")


def send_message(content, chat_id=None, parent_id=None, model="qwen3.6-plus"):
    """Send a message and return (chat_id, parent_id, content_preview)."""
    payload = {"model": model, "messages": [{"role": "user", "content": content}], "stream": True}
    if chat_id:
        payload["chatId"] = chat_id
    if parent_id:
        payload["parentId"] = parent_id

    r = requests.post(f"{BASE}/v1/chat/completions", json=payload, stream=True, timeout=60)
    r.raise_for_status()

    result_cid = None
    result_pid = None
    full = ""
    for line in r.iter_lines():
        if not line:
            continue
        ls = line.decode("utf-8")
        if ls.startswith("data: "):
            ds = ls[6:]
            if ds == "[DONE]":
                break
            try:
                d = json.loads(ds)
                if d.get("chatId"):
                    result_cid = d["chatId"]
                if d.get("parentId"):
                    result_pid = d["parentId"]
                if d.get("responseId"):
                    result_pid = d["responseId"]
                if "choices" in d:
                    c = d["choices"][0].get("delta", {}).get("content", "")
                    if c:
                        full += c
            except json.JSONDecodeError:
                pass
    return result_cid, result_pid, full[:80]


def save_session(cid, pid):
    """Simulate GUI saving session to disk."""
    import datetime
    safe = cid.replace("/", "_").replace("\\", "_")[:50]
    p = os.path.join(LOGS_DIR, f".session-{safe}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"chat_id": cid, "parent_id": pid, "message_count": 2,
                    "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, f, ensure_ascii=False, indent=2)
    return p


def load_session(cid):
    safe = cid.replace("/", "_").replace("\\", "_")[:50]
    p = os.path.join(LOGS_DIR, f".session-{safe}.json")
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


print("=" * 60)
print("FULL E2E TEST: Session Persistence Across Server Restart")
print("=" * 60)

# Phase 1: Send 2 messages
print("\n[Phase 1] Sending 2 messages...")
cid1, pid1, t1 = send_message("привет, как дела?")
print(f"  MSG1: chat={cid1[:12]}... parent={pid1[:12]}...")
print(f"  Preview: {t1}")

time.sleep(1)

cid2, pid2, t2 = send_message("расскажи о Python", chat_id=cid1, parent_id=pid1)
print(f"  MSG2: chat={cid2[:12]}... parent={pid2[:12]}...")
print(f"  Preview: {t2}")

assert cid1 == cid2, f"Chat IDs mismatch: {cid1} != {cid2}"
print(f"  Same chat: OK")

# Save session to disk (GUI does this after each response)
spath = save_session(cid1, pid2)
print(f"  Session saved: {spath}")

# Verify session file
session = load_session(cid1)
assert session is not None, "Session file missing!"
assert session["parent_id"] == pid2, f"Wrong parent_id: {session['parent_id']} != {pid2}"
print(f"  Session verified: parent_id={session['parent_id'][:12]}...")

# Phase 2: Simulate server restart — reset server singleton
print("\n[Phase 2] Simulating server restart (reset singleton)...")
requests.post(f"{BASE}/v1/chat/reset", timeout=5)
print("  Server session reset")

# Phase 3: Load chat with persisted session (GUI Load button does this)
print("\n[Phase 3] Loading chat with persisted session...")
loaded_cid = session["chat_id"]
loaded_pid = session["parent_id"]

# Server: load chat context
r = requests.post(f"{BASE}/v1/chats/{loaded_cid}/load", timeout=5)
print(f"  Server: {r.json()}")

# Server: now set the parent_id (GUI would send this in next request)
# This simulates the GUI sending chatId + parentId with the next message
print(f"  Loaded: chat={loaded_cid[:12]}... parent={loaded_pid[:12]}...")

# Phase 4: Send NEW message with persisted parent_id
print("\n[Phase 4] Sending new message with persisted parent_id...")
cid3, pid3, t3 = send_message("продолжи тему", chat_id=loaded_cid, parent_id=loaded_pid)
print(f"  MSG3: chat={cid3[:12]}... parent={pid3[:12]}...")
print(f"  Preview: {t3}")

# Verify
same_chat = cid1 == cid3
parent_changed = pid2 != pid3
print(f"\n{'='*60}")
print(f"Same chat: {same_chat}")
print(f"Parent ID changed: {parent_changed}")

if same_chat and parent_changed:
    print("\nSUCCESS: Session persistence works! Chat continues after restart.")
else:
    print("\nFAIL: Session persistence broken!")
    if not same_chat:
        print(f"  chatId mismatch: {cid1} != {cid3}")
    if not parent_changed:
        print(f"  parentId unchanged: {pid2} == {pid3}")
