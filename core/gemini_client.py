"""
Gemini API Client Wrapper
=========================
Thin wrapper around google-genai with retry, thinking control, and image support.
"""
import os
import time
import logging
from typing import Optional

import cv2
import numpy as np
from PIL import Image

from google import genai
from google.genai import types

from settings import (
    GEMINI_MODEL, GEMINI_DELAY_SEC, GEMINI_MAX_RETRIES, GEMINI_THINKING_TOKENS
)

log = logging.getLogger(__name__)


class GeminiClient:
    """Wrapper around Gemini API for vision + text tasks."""

    def __init__(self, model: str = GEMINI_MODEL, api_key: Optional[str] = None):
        key = api_key or os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise ValueError(
                "Gemini API key not found. Set GOOGLE_API_KEY environment variable.\n"
                "Get a free key at: https://aistudio.google.com/apikey"
            )
        self.client = genai.Client(api_key=key)
        self.model = model
        log.info(f"GeminiClient initialized with model: {model}")

    def ask(self, prompt: str, image=None, max_tokens: int = 8192) -> str:
        """
        Send prompt (with optional image) to Gemini, return text response.

        Args:
            prompt: Text prompt
            image: Either numpy BGR array, PIL Image, or None
            max_tokens: Max output tokens

        Returns:
            Response text (empty string on failure)
        """
        # Convert numpy BGR → PIL RGB if needed
        if image is not None and isinstance(image, np.ndarray):
            image = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))

        contents = [prompt, image] if image is not None else [prompt]

        for attempt in range(GEMINI_MAX_RETRIES):
            try:
                cfg = types.GenerateContentConfig(
                    temperature=0.1,
                    max_output_tokens=max_tokens,
                )
                # Cap thinking to prevent empty responses
                try:
                    cfg.thinking_config = types.ThinkingConfig(
                        thinking_budget=GEMINI_THINKING_TOKENS
                    )
                except Exception:
                    pass  # older SDK versions may not support this

                response = self.client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=cfg,
                )

                # Extract text from response parts
                raw = ""
                if (response.candidates and
                        response.candidates[0].content and
                        response.candidates[0].content.parts):
                    for part in response.candidates[0].content.parts:
                        if hasattr(part, 'text') and part.text:
                            raw += part.text

                return raw.strip()

            except Exception as e:
                log.warning(f"API attempt {attempt+1}/{GEMINI_MAX_RETRIES} failed: {e}")
                if attempt < GEMINI_MAX_RETRIES - 1:
                    time.sleep(GEMINI_DELAY_SEC * 2)

        log.error("All API retries exhausted, returning empty response")
        return ""
