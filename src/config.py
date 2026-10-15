"""
Configuration module for Qwen Local Proxy.
Loads settings from .env file.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Project root (parent of src/)
PROJECT_ROOT = Path(__file__).parent.parent
_project_root = PROJECT_ROOT

# Load .env from project root
_env_path = _project_root / ".env"
if _env_path.exists():
    load_dotenv(_env_path)


def _abs_path(value: str) -> str:
    """Resolve a possibly-relative path against the project root."""
    p = Path(value)
    return str(p.resolve() if p.is_absolute() else (_project_root / p).resolve())


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("true", "1", "yes", "on")


# Qwen Chat API
QWEN_BASE_URL: str = os.getenv("QWEN_BASE_URL", "https://chat.qwen.ai")

# Browser transport (Selenium + Chrome, persistent profile = SPA session store)
QWEN_BROWSER_PROFILE: str = _abs_path(os.getenv("QWEN_BROWSER_PROFILE", "./browser_profile"))
QWEN_CHROME_PATH: str = os.getenv("QWEN_CHROME_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
QWEN_BROWSER_HEADLESS: bool = _env_bool("QWEN_BROWSER_HEADLESS", False)
QWEN_BROWSER_LANGUAGE: str = os.getenv("QWEN_BROWSER_LANGUAGE", "ru-RU")
QWEN_PAGE_TIMEOUT: int = int(os.getenv("QWEN_PAGE_TIMEOUT", "90"))
QWEN_SEND_TIMEOUT: int = int(os.getenv("QWEN_SEND_TIMEOUT", "300"))

# Version header the SPA itself sends on API reads (observed in DevTools)
SPA_HEADER_VERSION: str = os.getenv("SPA_HEADER_VERSION", "0.3.16")

# API Server
API_HOST: str = os.getenv("API_HOST", "127.0.0.1")
API_PORT: int = int(os.getenv("API_PORT", "8000"))

# Default models
DEFAULT_MODELS: list = [
    m.strip()
    for m in os.getenv("DEFAULT_MODELS", "qwen3.7-plus,qwen3.8-max,qwen3.8-omni-flash").split(",")
    if m.strip()
]

# Logging
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
DEBUG_API: bool = _env_bool("DEBUG_API", False)
