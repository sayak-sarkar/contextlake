"""Local embeddings via an Ollama server — stdlib HTTP only, no extra deps.

Code never leaves the machine: the default endpoint is a local Ollama daemon.
Uses the stable ``POST /api/embeddings`` endpoint ({"model","prompt"} ->
{"embedding": [...]}).
"""

from __future__ import annotations

import json
import urllib.request

from .base import Embedder


def _post_json(url: str, payload: dict, timeout: float) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed local URL
        return json.loads(resp.read().decode())


class OllamaEmbedder(Embedder):
    name = "ollama"

    def __init__(self, *, model: str = "nomic-embed-text",
                 base_url: str = "http://127.0.0.1:11434",
                 batch_size: int = 64, timeout: float = 120):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.batch_size = batch_size
        self.timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        url = f"{self.base_url}/api/embeddings"
        out: list[list[float]] = []
        for text in texts:
            res = _post_json(url, {"model": self.model, "prompt": text}, self.timeout)
            out.append([float(x) for x in res.get("embedding", [])])
        return out
