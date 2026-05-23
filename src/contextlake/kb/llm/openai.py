"""Text generation via an OpenAI-compatible chat API — hosted (OpenAI, …) or any
local server that speaks the same protocol (LM Studio, Jan, llama.cpp, vLLM).

Uses ``POST {base_url}/chat/completions`` with ``{"model","messages":[...]}`` ->
``{"choices":[{"message":{"content":...}}]}``. The API key is read from an
environment variable (never stored in config); local servers that don't require a
key work with the variable unset.
"""

from __future__ import annotations

import os

from .._util import post_json
from .base import LlmClient


class OpenAILlm(LlmClient):
    name = "openai"

    def __init__(self, *, model: str = "gpt-4o-mini",
                 base_url: str = "https://api.openai.com/v1",
                 api_key_env: str = "OPENAI_API_KEY", timeout: float = 300):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.timeout = timeout

    def _headers(self) -> dict:
        key = os.environ.get(self.api_key_env)
        return {"Authorization": f"Bearer {key}"} if key else {}

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        res = post_json(f"{self.base_url}/chat/completions",
                        {"model": self.model, "messages": messages},
                        self.timeout, headers=self._headers())
        choices = res.get("choices") or []
        if not choices:
            return ""
        return (choices[0].get("message", {}).get("content") or "").strip()
