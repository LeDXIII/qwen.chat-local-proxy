"""
Sidecar session store: chat continuation state kept NEXT TO the chat history.

Layout: <project>/logs/.session-<safe chat_id>.json holding
{"chat_id", "parent_id", "message_count", "updated_at"} — the same file format
the GUI reads. The server is the single authoritative writer: the sidecar is
rewritten after every completed message, so a restarted proxy/GUI can continue
any chat by loading parent_id from this file (the site exposes it nowhere
else — it only travels inside the SSE stream as response_id).
"""

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

SESSION_DIR = Path(PROJECT_ROOT) / "logs"

_lock = threading.Lock()


def _safe_id(chat_id: str) -> str:
    return chat_id.replace("/", "_").replace("\\", "_")[:50]


def session_path(chat_id: str) -> Path:
    return SESSION_DIR / f".session-{_safe_id(chat_id)}.json"


def load(chat_id: str) -> Optional[dict]:
    path = session_path(chat_id)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Session file %s unreadable: %s", path, e)
        return None


def load_parent(chat_id: str) -> Optional[str]:
    data = load(chat_id)
    return (data or {}).get("parent_id") or None


def save(chat_id: str, parent_id: Optional[str]) -> Optional[Path]:
    """Rewrite the sidecar after a completed message (atomic, count +1)."""
    if not chat_id:
        return None
    path = session_path(chat_id)
    with _lock:
        try:
            SESSION_DIR.mkdir(parents=True, exist_ok=True)
            old = load(chat_id) or {}
            data = {
                "chat_id": chat_id,
                "parent_id": parent_id or old.get("parent_id"),
                "message_count": int(old.get("message_count") or 0) + 1,
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            tmp = path.with_name(path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            logger.debug("Session sidecar updated: %s parent=%s", path.name, data["parent_id"])
            return path
        except OSError as e:
            logger.error("Failed to write session sidecar %s: %s", path, e)
            return None


def delete(chat_id: str):
    path = session_path(chat_id)
    with _lock:
        try:
            if path.exists():
                path.unlink()
        except OSError as e:
            logger.warning("Failed to delete session sidecar %s: %s", path, e)


def list_sessions() -> list:
    out = []
    if not SESSION_DIR.exists():
        return out
    for f in SESSION_DIR.glob(".session-*.json"):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and data.get("chat_id"):
                out.append(data["chat_id"])
        except (json.JSONDecodeError, OSError):
            continue
    return out
