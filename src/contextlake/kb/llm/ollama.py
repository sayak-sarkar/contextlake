"""Local text generation via an Ollama server — stdlib HTTP only, no extra deps.

Uses the stable ``POST /api/generate`` endpoint ({"model","prompt","system",
"stream":false} -> {"response": "..."}). The default endpoint is a local daemon,
so prompts never leave the machine.
"""

from __future__ import annotations

from .._util import post_json
from .base import LlmClient


class OllamaLlm(LlmClient):
    name = "ollama"

    def __init__(self, *, model: str = "llama3.1",
                 base_url: str = "http://127.0.0.1:11434", timeout: float = 300):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        payload = {"model": self.model, "prompt": prompt, "stream": False}
        if system:
            payload["system"] = system
        res = post_json(f"{self.base_url}/api/generate", payload, self.timeout)
        return (res.get("response") or "").strip()
