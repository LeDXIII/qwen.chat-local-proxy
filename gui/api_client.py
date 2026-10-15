"""
HTTP client for the local Qwen Proxy API (browser/SPA transport backend).

Continuation state (chat_id + last parent_id) is persisted SERVER-SIDE into
the sidecar file logs/.session-<chat_id>.json after every message — this
client only caches the ids in memory so the next request carries them.
"""

import json
import datetime
import threading
import time
from pathlib import Path
import requests
from typing import Generator, Optional

DEBUG = False

# Must match the server's session/transcript directory (src/api/session_store.py):
# logs/ under the project root, independent of the current working directory.
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


class TranscriptLogger:
    """Log chat transcripts to files by chat_id."""

    def __init__(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._current_chat_id: Optional[str] = None
        self._log_file: Optional[Path] = None
        self._lock = threading.Lock()

    def _get_log_file(self, chat_id: str) -> Path:
        if self._current_chat_id != chat_id:
            self._current_chat_id = chat_id
            timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            safe_id = chat_id.replace("/", "_").replace("\\", "_")[:50]
            self._log_file = LOG_DIR / f"{safe_id}-{timestamp}.md"
        return self._log_file

    def log(self, chat_id: str, role: str, content: str):
        if not chat_id:
            return
        with self._lock:
            log_file = self._get_log_file(chat_id)
            timestamp = datetime.datetime.now().strftime("%H:%M:%S")
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"## {role} ({timestamp})\n\n{content}\n\n")


_logger = TranscriptLogger()


class LocalAPIClient:
    """Client for the local FastAPI proxy (browser-backed Qwen transport)."""

    def __init__(self, base_url: str = "http://127.0.0.1:8000"):
        self.base_url = base_url
        self.timeout = 10.0
        self.stream_timeout = 300.0
        self._chat_id: Optional[str] = None
        self._parent_id: Optional[str] = None
        # Liveness signals for the GUI "thinking vs hung" indicator: the worker
        # thread stamps last_activity on EVERY raw SSE line (including server
        # heartbeat comments) and flips got_first_token on the first byte. A
        # float read/written across threads is atomic under the GIL, which is
        # all this needs — no lock, no extra plumbing.
        self.last_activity = 0.0
        self.got_first_token = False
        # Staging step of a turn that carries attachments — the server sends it
        # as a ": progress ..." SSE comment while the SPA is still attaching and
        # reading the files. It is not answer text, so it lives in its own slot
        # and is cleared by the first content chunk: a stale step must never
        # outlive the phase it describes.
        self.progress: Optional[str] = None

    @property
    def chat_id(self) -> Optional[str]:
        return self._chat_id

    @property
    def parent_id(self) -> Optional[str]:
        return self._parent_id

    @chat_id.setter
    def chat_id(self, value: Optional[str]):
        self._chat_id = value

    @parent_id.setter
    def parent_id(self, value: Optional[str]):
        self._parent_id = value

    def check_connection(self) -> bool:
        try:
            response = requests.get(f"{self.base_url}/v1/health", timeout=self.timeout)
            return response.status_code == 200
        except requests.RequestException:
            return False

    def get_health(self) -> dict:
        """Auth/browser probe — the first call starts the proxy Chrome profile,
        so it gets a generous timeout."""
        response = requests.get(f"{self.base_url}/v1/health", timeout=120)
        response.raise_for_status()
        return response.json()

    def get_models(self) -> list[str]:
        response = requests.get(f"{self.base_url}/v1/models", timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        return [model["id"] for model in data.get("data", [])]

    def send_message(
        self,
        messages: list[dict],
        model: str = "qwen3.7-plus",
        stream: bool = True,
        system_message: Optional[str] = None,
        files: Optional[list[dict]] = None,
    ) -> Generator[str, None, None]:
        """Send messages and stream the response.

        Yields content chunks as they arrive.
        Includes chatId/parentId for conversation continuation.
        """
        payload = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }
        if system_message:
            payload["system_message"] = system_message
        if files:
            payload["files"] = files

        if self._chat_id:
            payload["chatId"] = self._chat_id
        else:
            # GUI holds no chat id (fresh start / Clear / "(новый)"): tell the
            # server to drop its own session, otherwise a fresh GUI silently
            # continues a stale chat left over from a previous run.
            payload["newChat"] = True
        if self._parent_id:
            payload["parentId"] = self._parent_id

        # Fresh liveness baseline: the gap between this POST and the first byte
        # is exactly where "is it hung or still thinking?" has an answer.
        self.last_activity = time.monotonic()
        self.got_first_token = False
        self.progress = None

        response = requests.post(
            f"{self.base_url}/v1/chat/completions",
            json=payload,
            timeout=self.stream_timeout,
            stream=True,
        )
        if response.status_code >= 400:
            # A refusal of the whole turn (too many documents, a file over the
            # size cap) comes back as JSON whose "detail" says what to change.
            # raise_for_status() alone would flatten it to "413 Client Error ...
            # for url ...", which shows the user nothing actionable, so that
            # sentence is lifted out first — same reasoning as the error field
            # in get_chats(). With no detail to show, behavior is unchanged.
            detail = ""
            try:
                detail = str(response.json().get("detail") or "")
            except Exception:
                detail = ""
            if detail:
                response.close()
                raise RuntimeError(detail)
        response.raise_for_status()

        try:
            for line in response.iter_lines():
                if not line:
                    continue

                # Every line from the server proves the turn is alive, server
                # and SPA included — heartbeat comments (": ping") carry no
                # content but are still liveness evidence.
                self.last_activity = time.monotonic()
                self.got_first_token = True

                line_str = line.decode("utf-8")
                if line_str.startswith(": progress "):
                    # A staging step of the turn (attaching the files, the site
                    # reading them) sent as an SSE comment. It is not answer
                    # text, so it goes to its own slot for the activity line.
                    self.progress = line_str[len(": progress "):].strip()
                    continue
                if line_str.startswith("data: "):
                    data_str = line_str[6:]
                    if data_str == "[DONE]":
                        break

                    try:
                        data = json.loads(data_str)

                        # Final meta chunk (chatId + parentId, no choices)
                        if "chatId" in data and "parentId" in data and "choices" not in data:
                            if data["chatId"]:
                                self._chat_id = data["chatId"]
                            if data["parentId"]:
                                self._parent_id = data["parentId"]
                            continue

                        if "chatId" in data and data["chatId"]:
                            self._chat_id = data["chatId"]
                        if "parentId" in data and data["parentId"]:
                            self._parent_id = data["parentId"]
                        if "responseId" in data and data["responseId"]:
                            self._parent_id = data["responseId"]

                        if data.get("error"):
                            # This one lands in the answer text the user reads,
                            # so the label is Russian while the server's own
                            # sentence stays exactly as the API sent it.
                            yield f"\n[Ошибка] {data['error']}\n"
                            continue

                        if "choices" in data:
                            choice = data["choices"][0]
                            if "delta" in choice:
                                content = choice["delta"].get("content", "")
                            elif "message" in choice:
                                content = choice["message"].get("content", "")
                            else:
                                content = ""
                            if content:
                                # The answer itself has started: the staging step
                                # it was waiting behind is over, so the activity
                                # line must stop showing it.
                                self.progress = None
                                yield content
                    except json.JSONDecodeError:
                        continue
        except (requests.exceptions.ChunkedEncodingError, ConnectionError, OSError) as e:
            # Same reasoning as the error chunk above: this line is read as
            # answer text in the chat pane, so the label is Russian. The
            # exception text comes from the transport and is kept verbatim.
            yield f"\n[Поток прерван: {e}]\n"

    def reset_chat(self) -> dict:
        response = requests.post(f"{self.base_url}/v1/chat/reset", timeout=self.timeout)
        response.raise_for_status()
        result = response.json()
        self._chat_id = result.get("chat_id") or None
        self._parent_id = result.get("parent_id") or None
        return result

    def get_chats(self) -> list:
        response = requests.get(f"{self.base_url}/v1/chats", timeout=self.timeout * 3)
        response.raise_for_status()
        data = response.json()
        # The route swallows account-fetch failures into HTTP 200 +
        # {"chats": [], "error": "..."} — surface the cause instead of an
        # empty list that looks like "no chats exist".
        if data.get("error"):
            raise RuntimeError(data["error"])
        return data.get("chats", [])

    def get_chat_messages(self, chat_id: str) -> list:
        response = requests.get(
            f"{self.base_url}/v1/chats/{chat_id}/messages",
            timeout=self.timeout * 2,
        )
        response.raise_for_status()
        return response.json().get("messages", [])

    def load_chat_context(self, chat_id: str) -> dict:
        response = requests.post(
            f"{self.base_url}/v1/chats/{chat_id}/load",
            timeout=self.timeout,
        )
        response.raise_for_status()
        result = response.json()
        self._chat_id = result.get("chat_id")
        self._parent_id = result.get("parent_id") or None
        return result

    def upload_file(self, file_path: str) -> dict:
        """Upload a file to the server."""
        with open(file_path, "rb") as f:
            response = requests.post(
                f"{self.base_url}/v1/files/upload",
                files={"file": f},
                timeout=60,
            )
        response.raise_for_status()
        return response.json()

    def generate_image(self, prompt: str, model: str = "qwen-image-plus", size: str = "1024*1024", n: int = 1) -> dict:
        """Generate an image via DashScope API (requires DASHSCOPE_API_KEY on server)."""
        response = requests.post(
            f"{self.base_url}/v1/images/generations",
            data={"prompt": prompt, "model": model, "size": size, "n": str(n)},
            timeout=120,
        )
        response.raise_for_status()
        return response.json()
