"""
FastAPI routes for Qwen Local Proxy (browser/SPA transport).

Chat-continuation contract: every completed message rewrites the sidecar
logs/.session-<chat_id>.json (chat_id + last parentId == response_id of the
just-finished assistant message) — see src.api.session_store. Clients send
chatId/parentId with each request; when parentId is missing it is restored
from the sidecar, so a restarted proxy/GUI can continue any chat.
"""

import asyncio
import base64
import json
import logging
import mimetypes
import os
import tempfile
import time
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import StreamingResponse

from src.client.qwen_client import PROGRESS_KEY, QwenClient
from src.api import session_store
from src.api.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ModelListResponse,
    ModelInfo,
    HealthResponse,
)
from src.config import DEFAULT_MODELS
from src.api.image_generation import (
    generate_image,
    get_available_image_models,
    check_image_api_available,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")
_qwen_client: QwenClient | None = None

DEFAULT_MODEL = "qwen3.7-plus"

# Model alias mapping — short/legacy names to current chat.qwen.ai model IDs
MODEL_ALIASES = {
    # current generation
    "qwen3.7-plus": "qwen3.7-plus",
    "qwen3.8-max": "qwen3.8-max",
    "qwen3.8-omni-flash": "qwen3.8-omni-flash",
    # short aliases
    "plus": "qwen3.7-plus",
    "max": "qwen3.8-max",
    "omni": "qwen3.8-omni-flash",
    "omni-flash": "qwen3.8-omni-flash",
    # common generic aliases
    "qwen-plus": "qwen3.7-plus",
    "qwen-turbo": "qwen3.7-plus",
    "qwen3-plus": "qwen3.7-plus",
    "qwen-max": "qwen3.8-max",
    "qwen3-max": "qwen3.8-max",
    "27b": "qwen3.7-plus",
    # legacy names from older configs → current equivalents
    "qwen3.6-plus": "qwen3.7-plus",
    "qwen3.6-27b": "qwen3.7-plus",
    "qwen3.5-plus": "qwen3.7-plus",
    "qwen3.5-27b": "qwen3.7-plus",
    "qwen3.6-max": "qwen3.8-max",
    "qwen3.6-max-preview": "qwen3.8-max",
    "qwen3.5-max": "qwen3.8-max",
}


def _resolve_model(model: Optional[str]) -> str:
    """Resolve model name/alias to actual model ID (unknown names pass through)."""
    name = (model or DEFAULT_MODEL).strip()
    return MODEL_ALIASES.get(name.lower(), name)


def get_client() -> QwenClient:
    global _qwen_client
    if _qwen_client is None:
        _qwen_client = QwenClient()
    return _qwen_client


# A silent socket is indistinguishable from a hung one. These SSE comments are
# the liveness proof the GUI reads (gui/api_client.py stamps last_activity on
# every raw line, comment included), so "still thinking" never looks frozen.
HEARTBEAT_AFTER = 3.0


async def _stream_with_heartbeats(upstream, after: float = HEARTBEAT_AFTER):
    """Re-yield upstream chunks as ("chunk", dict); ("ping", None) after `after`
    seconds of SPA silence. A pump task is used instead of wait_for(__anext__())
    because timing out must not cancel the turn itself."""
    queue: asyncio.Queue = asyncio.Queue()

    async def pump():
        try:
            async for item in upstream:
                await queue.put(("chunk", item))
        except Exception as exc:  # re-raised to the consumer below
            await queue.put(("error", exc))
        finally:
            await queue.put(("done", None))

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=after)
            except asyncio.TimeoutError:
                yield ("ping", None)
                continue
            if kind == "chunk":
                yield ("chunk", payload)
            elif kind == "error":
                raise payload
            else:
                return
    finally:
        if not task.done():
            task.cancel()


# ui_attach reads the attachment off disk and re-encodes it for the page, so
# inline base64 uploads are staged into a temp dir for the duration of the turn
# and removed right after it.
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024

# The composer takes five documents at a time (measured 2026-10-14: the sixth
# and seventh .txt of seven never mount a card). Pictures are not part of the
# five — four documents plus four pictures produced eight cards — so only
# non-image files are counted here.
MAX_DOCUMENTS_PER_TURN = 5


def _is_image(item) -> bool:
    """Whether an upload is read by the site as a picture, not a document.

    The GUI fills fileType with a real MIME type, the schema comment promises
    "image"/"document", and other clients send nothing useful — so fall back
    to the name.
    """
    mime = (item.fileType or "").strip().lower()
    if "/" in mime:
        return mime.startswith("image/")
    return (mimetypes.guess_type(item.fileName or "")[0] or "").startswith("image/")


def _stage_attachments(files) -> list[str]:
    """Write inline base64 attachments to a temp dir; returns the file paths.

    Undecodable or oversized entries are skipped with a log line: a broken
    attachment must not sink the whole chat turn. Going over the site's own
    five-document limit is the opposite kind of failure — there the composer
    silently keeps the first five and the answer then talks about files the
    user believes it read — so that request is refused with the count instead.
    """
    staged: list[str] = []
    documents: list[str] = []
    for item in files or []:
        name = os.path.basename((item.fileName or "attachment").strip() or "attachment")
        if not item.fileData:
            logger.warning("Attachment %s has no inline data; skipped", name)
            continue
        try:
            raw = base64.b64decode(item.fileData, validate=True)
        except Exception:
            logger.warning("Attachment %s: invalid base64; skipped", name)
            continue
        if not raw:
            logger.warning("Attachment %s is empty; skipped", name)
            continue
        if len(raw) > MAX_ATTACHMENT_BYTES:
            logger.warning("Attachment %s is %d bytes (limit %d); skipped",
                           name, len(raw), MAX_ATTACHMENT_BYTES)
            continue
        fd, path = tempfile.mkstemp(prefix="qwen_att_", suffix=os.path.splitext(name)[1][:16])
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
        except OSError:
            logger.exception("Could not stage attachment %s", name)
            try:
                os.unlink(path)
            except OSError:
                pass
            continue
        staged.append(path)
        if not _is_image(item):
            documents.append(name)
    if len(documents) > MAX_DOCUMENTS_PER_TURN:
        # Refused here rather than left to the SPA: past five documents the
        # composer keeps the first five in silence, and the answer then discusses
        # a file set the user thinks the site read in full.
        _discard_attachments(staged)
        raise HTTPException(
            status_code=413,
            detail=(f"Qwen accepts {MAX_DOCUMENTS_PER_TURN} documents per message, "
                    f"this request carries {len(documents)}: {', '.join(documents)}. "
                    f"Send fewer documents; pictures are not counted in the limit."))
    return staged


def _discard_attachments(paths) -> None:
    for path in paths or []:
        try:
            os.unlink(path)
        except OSError:
            logger.warning("Left staging file behind: %s", path)


@router.get("/models", response_model=ModelListResponse)
async def list_models():
    now = int(time.time())
    try:
        ids = await get_client().get_models()
    except Exception as e:
        logger.warning("Live model list unavailable (%s); falling back to DEFAULT_MODELS", e)
        ids = []
    if not ids:
        ids = list(DEFAULT_MODELS)
    return ModelListResponse(data=[ModelInfo(id=m, created=now) for m in ids])


@router.post("/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    # Everything before the StreamingResponse is sync: an exception here would
    # escape as a raw FastAPI 500, poisoning the turn before the stream's own
    # handler can turn failures into structured SSE error chunks.
    attachments: list[str] = []
    try:
        client = get_client()
        model = _resolve_model(request.model or DEFAULT_MODEL)

        # Force new chat if requested
        if request.newChat or request.new_chat:
            client.reset_chat()

        # Session sync: explicit ids from the request win; a missing parentId is
        # restored from the sidecar file next to the history — a corrupt sidecar
        # must never sink the request itself.
        req_cid = request.chatId or request.chat_id
        req_pid = request.parentId or request.parent_id
        if req_cid:
            if req_cid != client.chat_id:
                if not req_pid:
                    try:
                        req_pid = session_store.load_parent(req_cid)
                    except Exception:
                        logger.exception("Bad session sidecar for %s; continuing without parentId", req_cid)
                client.set_session(req_cid, req_pid or None)
            elif req_pid:
                client.set_session(req_cid, req_pid)

        # Full OpenAI messages array or simplified native {message: "..."}
        if request.messages:
            messages = [m.model_dump() for m in request.messages]
        else:
            messages = [{"role": "user", "content": request.message or ""}]

        # Extract system message (SPA input takes only user text)
        system_message = request.system_message
        if not system_message:
            for msg in messages:
                if msg.get("role") == "system":
                    c = msg.get("content")
                    system_message = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
                    break
        messages = [m for m in messages if m.get("role") != "system"]

        # Inline base64 uploads → staged paths: ui_attach reads them back off
        # disk and drops them on the composer, which is the SPA's only file intake.
        attachments = _stage_attachments(request.files)

        before_cid = client.chat_id
    except HTTPException:
        # A refusal the staging itself raised (e.g. more than the site's five
        # documents) is a final answer, not a prep failure: 503 would invite a
        # retry of a request that can never succeed.
        _discard_attachments(attachments)
        raise
    except Exception as e:
        logger.exception("Could not prepare the chat turn")
        _discard_attachments(attachments)
        raise HTTPException(status_code=503, detail=str(e)[:500])

    if request.stream:
        async def event_stream() -> AsyncGenerator[str, None]:
            response_id = None
            chat_id_from_response = None
            try:
                upstream = _stream_with_heartbeats(client.send_message(
                    messages=messages, model=model, stream=True,
                    system_message=system_message, attachments=attachments,
                ))
                async for kind, payload in upstream:
                    if kind == "ping":
                        # A bare SSE comment: OpenAI clients ignore it, while
                        # this GUI reads it as "the turn is alive", not frozen.
                        yield ": ping\n\n"
                        continue
                    chunk = payload
                    if isinstance(chunk, dict) and chunk.get(PROGRESS_KEY) is not None:
                        # A staging step (attaching, the site reading the files)
                        # is not answer text, so it goes out as a comment too —
                        # the same channel the GUI already reads as liveness.
                        text = str(chunk.get(PROGRESS_KEY) or "").replace("\n", " ")
                        yield f": progress {text}\n\n"
                        continue
                    # Track responseId and chatId as they appear in the stream
                    if chunk.get("responseId"):
                        response_id = chunk["responseId"]
                    if chunk.get("chatId"):
                        chat_id_from_response = chunk["chatId"]
                    rc = chunk.get("response.created")
                    if isinstance(rc, dict):
                        response_id = rc.get("response_id") or response_id
                        chat_id_from_response = rc.get("chat_id") or chat_id_from_response
                    if not chunk.get("chatId"):
                        cid = chat_id_from_response or client.chat_id
                        if cid:
                            chunk["chatId"] = cid
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

                # Success path: persist continuation state NEXT TO the history
                cid = client.chat_id or chat_id_from_response or ""
                final_parent = response_id or client.parent_id or ""
                if cid:
                    session_store.save(cid, final_parent)

                final_meta = {"chatId": cid, "parentId": final_parent}
                if before_cid and cid and before_cid != cid:
                    final_meta["newChat"] = True
                    final_meta["previousChatId"] = before_cid
                yield f"data: {json.dumps(final_meta, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                logger.exception("Error in chat completions stream")
                error_chunk = {
                    "error": str(e),
                    "choices": [{"delta": {"content": f"Error: {e}"}, "finish_reason": "stop"}],
                    "x-error": True,
                }
                yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\ndata: [DONE]\n\n"
            finally:
                # The SPA copied the bytes at send time, so once this turn is
                # done either way, the staging copies have no purpose left.
                _discard_attachments(attachments)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    try:
        result: dict = {}
        async for chunk in client.send_message(
            messages=messages, model=model, stream=False,
            system_message=system_message, attachments=attachments,
        ):
            if isinstance(chunk, dict) and chunk.get(PROGRESS_KEY) is not None:
                # A staged-turn progress event carries no completion; keeping it
                # would replace the answer with a status line.
                continue
            result = chunk or {}
        cid = result.get("chatId") or client.chat_id or ""
        rid = result.get("responseId") or client.parent_id or ""
        if cid:
            session_store.save(cid, rid)
        return ChatCompletionResponse(
            id=result.get("id") or f"chatcmpl-{int(time.time())}",
            object=result.get("object") or "chat.completion",
            created=result.get("created") or int(time.time()),
            model=result.get("model") or model,
            choices=result.get("choices") or [
                {"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}
            ],
            usage=result.get("usage"),
            chatId=cid,
            responseId=rid,
        )
    except Exception as e:
        logger.exception("Error in chat completions")
        raise HTTPException(status_code=502, detail=str(e))
    finally:
        _discard_attachments(attachments)


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Auth/browser state probe — the first call starts the proxy Chrome profile."""
    # get_client() is inside the try on purpose: a client that cannot even be
    # constructed must answer as a degraded 200 probe, never as a raw 500.
    try:
        client = get_client()
        st = await client.auth_state()
        return HealthResponse(
            status="ok",
            authenticated=bool(st.get("logged_in")),
            browser_started=bool(st.get("browser_started", True)),
            profile=str(st.get("profile", "")),
            href=str(st.get("href", "")),
            detail=str(st.get("detail", "")),
        )
    except Exception as e:
        logger.exception("Health check failed")
        return HealthResponse(
            status="degraded", authenticated=False, browser_started=False,
            detail=str(e)[:300],
        )


@router.post("/chat/reset")
async def reset_chat():
    """Reset chat session — the next message starts a brand-new chat."""
    try:
        client = get_client()
        client.reset_chat()
    except Exception as e:
        logger.exception("Chat reset failed")
        raise HTTPException(status_code=503, detail=str(e)[:300])
    return {"status": "ok", "chat_id": "", "parent_id": ""}


@router.get("/chats")
async def list_chats():
    """List chats from the Qwen account (page-context GET)."""
    try:
        chats = await get_client().get_chats()
        return {"chats": chats}
    except Exception as e:
        logger.error(f"Error listing chats: {e}")
        return {"chats": [], "error": str(e)}


@router.get("/chats/{chat_id}/messages")
async def get_chat_messages(chat_id: str):
    """Best-effort remote history (site detail keeps assistant text out of the
    payload) — the GUI loads local .md transcripts as the primary source."""
    try:
        messages = await get_client().get_chat_messages(chat_id)
        return {"messages": messages}
    except Exception as e:
        logger.warning(f"Error getting chat messages for {chat_id}: {e}")
        return {"messages": [], "error": str(e)}


@router.post("/chats/{chat_id}/load")
async def load_chat_context(chat_id: str):
    """Make a chat the current session. parentId is restored from the sidecar
    file next to the history (logs/.session-<chat_id>.json)."""
    try:
        client = get_client()
    except Exception as e:
        logger.exception("Chat load failed")
        raise HTTPException(status_code=503, detail=str(e)[:300])
    try:
        data = session_store.load(chat_id)
    except Exception:
        # A corrupt sidecar must not 500 a load — treat the chat as unknown.
        logger.exception("Bad session sidecar for %s; loading without parentId", chat_id)
        data = None
    parent_id = (data or {}).get("parent_id")
    client.set_session(chat_id, parent_id=parent_id)
    return {
        "status": "ok",
        "chat_id": chat_id,
        "parent_id": parent_id or "",
        "known": bool(data),
        "message_count": (data or {}).get("message_count", 0),
    }


# ─── File upload endpoints (not supported by the browser transport yet) ─────

_NO_UPLOAD_MSG = (
    "Attachments are not supported by the browser transport yet — "
    "send the text without files."
)


@router.post("/files/getstsToken")
async def get_sts_token_endpoint():
    """Legacy STS upload removed with the cookie transport."""
    raise HTTPException(status_code=501, detail=_NO_UPLOAD_MSG)


@router.post("/files/upload")
async def upload_file():
    """Legacy cookie upload removed; endpoint kept so GUI gets a clear 501."""
    raise HTTPException(status_code=501, detail=_NO_UPLOAD_MSG)


# ─── Image generation endpoint (DashScope, works independently) ──────────────

@router.post("/images/generations")
async def generate_images(
    prompt: str = Form(...),
    model: str = Form(default="qwen-image-plus"),
    n: int = Form(default=1),
    size: str = Form(default="1024*1024"),
):
    """Generate images via DashScope API (requires DASHSCOPE_API_KEY).

    Supported models: qwen-image-plus, qwen-image-max, wan2.6-t2i, etc.
    Supported sizes: 1024*1024, 1024*1792, 1792*1024, 512*512, 768*768, 960*960
    """
    if not os.environ.get("DASHSCOPE_API_KEY"):
        raise HTTPException(
            status_code=503,
            detail="DASHSCOPE_API_KEY not set. Image generation requires this environment variable.",
        )

    result = generate_image(prompt=prompt, model=model, size=size, n=n)
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    return result


@router.get("/images/models")
async def list_image_models():
    """List available image generation models."""
    return {
        "models": get_available_image_models(),
        "available": check_image_api_available(),
    }


# ─── OpenWebUI compatibility routes (aliases) ────────────────────────────────

@router.post("/chat")
async def chat_native(request: ChatCompletionRequest):
    """Native chat endpoint (OpenWebUI compatible).

    Accepts the simplified {message, model, chatId, parentId} format as well as
    a full messages array; handling is identical to /chat/completions.
    """
    return await chat_completions(request)
