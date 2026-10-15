"""Test 3 sequential messages and check server-side parent_id tracking."""
import requests
import json
import time

BASE = "http://127.0.0.1:8000"

def send(msg, chat_id=None, parent_id=None):
    payload = {"model": "qwen3.6-plus", "messages": [{"role": "user", "content": msg}], "stream": True}
    if chat_id:
        payload["chatId"] = chat_id
    if parent_id:
        payload["parentId"] = parent_id

    r = requests.post(f"{BASE}/v1/chat/completions", json=payload, stream=True, timeout=60)
    chat_id = None
    parent_id = None
    content = ""

    for line in r.iter_lines():
        if not line:
            continue
        line_str = line.decode("utf-8")
        if line_str.startswith("data: "):
            data_str = line_str[6:]
            if data_str == "[DONE]":
                break
            try:
                data = json.loads(data_str)
                if data.get("chatId"):
                    chat_id = data["chatId"]
                if data.get("parentId"):
                    parent_id = data["parentId"]
                if "choices" in data:
                    choice = data["choices"][0]
                    if "delta" in choice:
                        c = choice["delta"].get("content", "")
                        if c:
                            content += c
            except:
                pass

    return chat_id, parent_id, content[:80]

print("MSG 1: 'привет'")
cid1, pid1, txt1 = send("привет")
print(f"  chatId={cid1[:12]}... parentId={pid1[:12]}...")
print(f"  content: {txt1}")
print()

time.sleep(1)

print("MSG 2: 'как дела?'")
cid2, pid2, txt2 = send("как дела?", chat_id=cid1, parent_id=pid1)
print(f"  chatId={cid2[:12]}... parentId={pid2[:12]}...")
print(f"  content: {txt2}")
print()

time.sleep(1)

print("MSG 3: 'расскажи о python'")
cid3, pid3, txt3 = send("расскажи о python", chat_id=cid2, parent_id=pid2)
print(f"  chatId={cid3[:12]}... parentId={pid3[:12]}...")
print(f"  content: {txt3}")
print()

# Check
same_chat = cid1 == cid2 == cid3
pids_changed = (pid1 != pid2) and (pid2 != pid3) and (pid1 != pid3)
print(f"Same chat: {same_chat}")
print(f"Parent IDs changed: {pids_changed}")
print(f"pid1={pid1[:12]} pid2={pid2[:12]} pid3={pid3[:12]}")

if same_chat and pids_changed:
    print("\nSUCCESS: Conversation continuation works!")
else:
    print("\nFAIL: Conversation NOT continuing")
