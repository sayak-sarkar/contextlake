"""Local text generation via an Ollama server — stdlib HTTP only, no extra deps.

Uses the stable ``POST /api/generate`` endpoint ({"model","prompt","system",
"stream":false} -> {"response": "..."}). The default endpoint is a local daemon,
so prompts never leave the machine.
"""

from __future__ import annotations

import json
import urllib.request

from .base import LlmClient


def _post_json(url: str, payload: dict, timeout: float) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed local URL
        return json.loads(resp.read().decode())


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
        res = _post_json(f"{self.base_url}/api/generate", payload, self.timeout)
        return (res.get("response") or "").strip()
