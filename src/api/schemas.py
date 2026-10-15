"""
Pydantic request/response schemas for the API.
"""

from typing import Optional, Union

from pydantic import BaseModel, model_validator


class Message(BaseModel):
    role: str
    # OpenAI-style content: plain string or a list of typed blocks ({"type":"text",...})
    content: Union[str, list] = ""


class FileInfo(BaseModel):
    """File attachment info for multimodal messages."""
    fileName: Optional[str] = None
    fileUrl: Optional[str] = None
    fileId: Optional[str] = None
    fileType: Optional[str] = None  # "image", "document", "file"
    fileSize: Optional[int] = None
    # Inline base64 body: the browser transport has no upload API, so the GUI
    # hands bytes over in the request and the server stages them on disk.
    fileData: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = "qwen3.7-plus"
    messages: Optional[list[Message]] = None
    # Simplified native format: {message: "..."} instead of a messages array
    message: Optional[str] = None
    stream: bool = True
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    system_message: Optional[str] = None  # System/instruction message
    files: Optional[list[FileInfo]] = None  # File attachments for the current message
    # Conversation continuation (camelCase — as used by GUI and FreeQwenApi)
    chatId: Optional[str] = None
    parentId: Optional[str] = None
    # Snake-case aliases
    chat_id: Optional[str] = None
    parent_id: Optional[str] = None
    # Force new chat creation
    newChat: Optional[bool] = False
    new_chat: Optional[bool] = False

    @model_validator(mode="after")
    def _need_content(self):
        if not self.messages and not (self.message or "").strip():
            raise ValueError("Either 'messages' or 'message' must be provided")
        return self


class ChatCompletionChunk(BaseModel):
    id: str
    object: str
    created: int
    model: str
    choices: list[dict]


class ChatCompletionResponse(BaseModel):
    id: str
    object: str
    created: int
    model: str
    choices: list[dict]
    usage: Optional[dict] = None
    # Conversation continuation (non-standard but useful for clients)
    chatId: Optional[str] = None
    responseId: Optional[str] = None


class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int
    owned_by: str = "qwen"


class ModelListResponse(BaseModel):
    object: str = "list"
    data: list[ModelInfo]


class HealthResponse(BaseModel):
    status: str
    authenticated: bool = False
    browser_started: bool = False
    profile: str = ""
    href: str = ""
    detail: str = ""

    model_config = {"json_schema_extra": {"example": {
        "status": "ok", "authenticated": True, "browser_started": True,
        "profile": "D:\\qwen-code\\qwen-local-proxy2\\browser_profile",
        "href": "https://chat.qwen.ai/", "detail": "",
    }}}
