"""
Image generation endpoint for Qwen Local Proxy.
Adapted from FreeQwenApi src/api/imageGeneration.js
Uses DashScope API for image generation.
"""

import logging
import os
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

DASHSCOPE_API_BASE = "https://dashscope-intl.aliyuncs.com/api/v1"

IMAGE_GENERATION_MODELS = [
    "qwen-image-max",
    "qwen-image-plus",
    "qwen-image",
    "wan2.6-t2i",
    "wan2.5-t2i-preview",
    "wan2.2-t2i-flash",
]


def generate_image(
    prompt: str,
    model: str = "qwen-image-plus",
    size: str = "1024*1024",
    n: int = 1,
    negative_prompt: str = " ",
    prompt_extend: bool = True,
    watermark: bool = False,
) -> dict:
    """Generate an image from text prompt using DashScope API.

    Requires DASHSCOPE_API_KEY environment variable.

    Args:
        prompt: Text description of the image
        model: Model to use (qwen-image-plus, qwen-image-max, etc.)
        size: Image size (1024*1024, 1024*1792, 1792*1024, 512*512, 768*768, 960*960)
        n: Number of images
        negative_prompt: Negative prompt
        prompt_extend: Extend prompt with AI
        watermark: Add watermark

    Returns:
        Result with imageUrl or error
    """
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        return {"error": "DASHSCOPE_API_KEY environment variable not set"}

    try:
        logger.info(f"Generating image via {model}...")

        payload = {
            "model": model,
            "input": {
                "prompt": prompt,
                "negative_prompt": negative_prompt,
            },
            "parameters": {
                "size": size,
                "n": n,
                "prompt_extend": prompt_extend,
                "watermark": watermark,
            },
        }

        # Wan models use async mode
        is_wan = model.startswith("wan")
        endpoint = f"{DASHSCOPE_API_BASE}/services/aigc/text2image/image-synthesis"

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if is_wan:
            headers["X-DashScope-Async"] = "enable"

        response = requests.post(endpoint, json=payload, headers=headers, timeout=120)
        response.raise_for_status()
        data = response.json()

        # Async mode - get task_id and poll
        if data.get("output", {}).get("task_id"):
            task_id = data["output"]["task_id"]
            logger.info(f"Task created: {task_id}")
            return _poll_task_status(task_id, api_key)

        # Sync mode - direct result
        if data.get("output", {}).get("results"):
            image_url = data["output"]["results"][0].get("url", "")
            logger.info(f"Image generated: {image_url}")
            return {
                "success": True,
                "imageUrl": image_url,
                "model": model,
                "prompt": prompt,
            }

        return {"error": "Unexpected response format", "rawData": data}

    except Exception as e:
        logger.error(f"Error generating image: {e}")
        return {"error": str(e)}


def _poll_task_status(task_id: str, api_key: str, max_attempts: int = 60, interval: int = 2) -> dict:
    """Poll task status for async image generation."""
    for attempt in range(max_attempts):
        try:
            response = requests.get(
                f"{DASHSCOPE_API_BASE}/tasks/{task_id}",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
            )
            response.raise_for_status()
            task = response.json()
            task_status = task.get("output", {}).get("task_status", "unknown")

            logger.debug(f"Task status: {task_status} (attempt {attempt + 1}/{max_attempts})")

            if task_status == "SUCCEEDED":
                image_url = task.get("output", {}).get("results", [{}])[0].get("url", "")
                if image_url:
                    logger.info(f"Image generated: {image_url}")
                    return {
                        "success": True,
                        "imageUrl": image_url,
                        "taskId": task_id,
                        "model": task.get("input", {}).get("model", "unknown"),
                    }
                return {"error": "Image URL not found in result"}

            if task_status in ("FAILED", "CANCELLED"):
                return {
                    "error": f"Task failed: {task_status}",
                    "message": task.get("output", {}).get("message", "Unknown error"),
                }

            # PENDING or RUNNING - continue polling
            time.sleep(interval)

        except Exception as e:
            logger.error(f"Error polling task {task_id}: {e}")
            if attempt == max_attempts - 1:
                return {"error": f"Polling error: {str(e)}"}
            time.sleep(interval)

    return {"error": "Image generation timed out"}


def get_available_image_models() -> list:
    """Get list of available image generation models."""
    return IMAGE_GENERATION_MODELS


def check_image_api_available() -> bool:
    """Check if DashScope API is available."""
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        return False

    try:
        response = requests.get(
            f"{DASHSCOPE_API_BASE}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5,
        )
        return response.status_code == 200
    except Exception:
        return False
