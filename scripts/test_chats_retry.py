"""Offline checks for QwenClient.get_chats (no browser, no network).

The live site answers HTTP 200 WITHOUT the {"success":..,"data":[..]} envelope
when the SPA's access token silently goes stale; get_chats must reload the SPA
(go_home) and retry once instead of returning a silently empty list.

Run:  venv\Scripts\python.exe scripts\test_chats_retry.py
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.client.qwen_client import QwenClient


def make_client(pages):
    t = object.__new__(QwenClient)
    t._w = SimpleNamespace(go_home=lambda: None)
    queue = list(pages)
    calls = {"page_get": 0, "reload": 0}

    async def fake_page_get(path):
        calls["page_get"] += 1
        assert path == "/api/v2/chats/?page=1", path
        return queue.pop(0)

    async def fake_safely(fn, *a):
        calls["reload"] += 1

    t._page_get = fake_page_get
    t._safely = fake_safely
    return t, calls


def ok(data):
    return {"status": 200, "ct": "application/json",
            "url": "https://chat.qwen.ai/api/v2/chats/?page=1",
            "body": json.dumps({"success": True, "request_id": "x", "data": data})}


def soft_fail():
    # The stale-token answer observed in the wild: HTTP 200, no "data" key.
    return {"status": 200, "ct": "application/json",
            "url": "https://chat.qwen.ai/api/v2/chats/?page=1",
            "body": '{"detail":"Unauthorized"}'}


async def test_soft_fail_recovers():
    t, calls = make_client([soft_fail(), ok([{"id": "a"}, {"id": "b"}])])
    got = await t.get_chats()
    assert [c["id"] for c in got] == ["a", "b"], got
    assert calls["page_get"] == 2 and calls["reload"] == 1, calls
    print("PASS soft-fail 200 -> SPA reload -> recovered list")


async def test_401_retries_once():
    t, calls = make_client([{"status": 401, "ct": "", "url": "u", "body": "nope"},
                            ok([{"id": "z"}])])
    got = await t.get_chats()
    assert [c["id"] for c in got] == ["z"], got
    assert calls["page_get"] == 2 and calls["reload"] == 1, calls
    print("PASS HTTP 401 -> SPA reload -> recovered list")


async def test_genuine_empty_not_retried():
    t, calls = make_client([ok([])])
    got = await t.get_chats()
    assert got == [], got
    assert calls["page_get"] == 1 and calls["reload"] == 0, calls
    print("PASS genuine \"data\": [] -> [] without any reload")


async def test_data_dict_shape():
    t, calls = make_client([ok({"chats": [{"id": "d"}]})])
    got = await t.get_chats()
    assert [c["id"] for c in got] == ["d"], got
    assert calls["page_get"] == 1 and calls["reload"] == 0, calls
    print("PASS data as {\"chats\": [...]} -> chats list")


async def test_persistent_garbage_raises():
    t, calls = make_client([soft_fail(), soft_fail()])
    try:
        await t.get_chats()
    except RuntimeError as e:
        assert "after retry" in str(e), e
        assert calls["page_get"] == 2 and calls["reload"] == 1, calls
        print("PASS persistent garbage -> RuntimeError after exactly one reload")
        return
    raise AssertionError("expected RuntimeError, got no exception")


async def test_401_twice_raises():
    t, calls = make_client([{"status": 401, "ct": "", "url": "u", "body": "x"},
                            {"status": 401, "ct": "", "url": "u", "body": "x"}])
    try:
        await t.get_chats()
    except RuntimeError as e:
        assert "401" in str(e), e
        assert calls["page_get"] == 2 and calls["reload"] == 1, calls
        print("PASS HTTP 401 twice -> RuntimeError mentioning 401")
        return
    raise AssertionError("expected RuntimeError, got no exception")


async def test_500_raises_immediately():
    t, calls = make_client([{"status": 500, "ct": "", "url": "u", "body": "boom"}])
    try:
        await t.get_chats()
    except RuntimeError as e:
        assert "500" in str(e) and "boom" in str(e), e
        assert calls["page_get"] == 1 and calls["reload"] == 0, calls
        print("PASS HTTP 500 -> RuntimeError immediately (no reload)")
        return
    raise AssertionError("expected RuntimeError, got no exception")


async def main():
    await test_soft_fail_recovers()
    await test_401_retries_once()
    await test_genuine_empty_not_retried()
    await test_data_dict_shape()
    await test_persistent_garbage_raises()
    await test_401_twice_raises()
    await test_500_raises_immediately()
    print("ALL get_chats retry checks passed")


if __name__ == "__main__":
    asyncio.run(main())
