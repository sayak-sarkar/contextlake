"""Local embeddings via an Ollama server — stdlib HTTP only, no extra deps.

Code never leaves the machine: the default endpoint is a local Ollama daemon.
Uses the stable ``POST /api/embeddings`` endpoint ({"model","prompt"} ->
{"embedding": [...]}).
"""

from __future__ import annotations

from .._util import post_json
from .base import Embedder


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
            res = post_json(url, {"model": self.model, "prompt": text}, self.timeout)
            out.append([float(x) for x in res.get("embedding", [])])
        return out
