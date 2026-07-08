"""Text generation via the Anthropic Messages API.

Anthropic is not OpenAI-compatible: it uses ``POST {base_url}/v1/messages`` with
``x-api-key`` + ``anthropic-version`` headers and returns a ``content`` block list.
Stdlib-only (via ``_util.post_json`` — no ``anthropic`` SDK dependency, matching
``openai.py`` and the offline/stdlib-core principle). The key is read from an env
var, never stored in config.
"""

from __future__ import annotations

import os

from .._util import post_json
from .base import LlmClient

_ANTHROPIC_VERSION = "2023-06-01"


class AnthropicLlm(LlmClient):
    name = "anthropic"

    def __init__(self, *, model: str = "claude-opus-4-8",
                 base_url: str = "https://api.anthropic.com",
                 api_key_env: str = "ANTHROPIC_API_KEY",
                 max_tokens: int = 4096, timeout: float = 300):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.max_tokens = max_tokens
        self.timeout = timeout

    def _headers(self) -> dict:
        key = os.environ.get(self.api_key_env, "")
        return {"x-api-key": key, "anthropic-version": _ANTHROPIC_VERSION}

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system
        res = post_json(f"{self.base_url}/v1/messages", payload, self.timeout,
                        headers=self._headers())
        blocks = res.get("content") or []
        return "".join(
            b.get("text", "") for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
