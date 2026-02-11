"""Embeddings via an OpenAI-compatible API — hosted (OpenAI, Voyage-compat, …) or
any local server that speaks the same protocol (LM Studio, Jan, llama.cpp, vLLM).

Uses ``POST {base_url}/embeddings`` with ``{"model","input":[...]}`` ->
``{"data":[{"embedding":[...],"index":i}, ...]}``. The API key is read from an
environment variable (never stored in config); local servers that don't require a
key work with the variable unset.
"""

from __future__ import annotations

import json
import os
import urllib.request

from .base import Embedder


def _post_json(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    body = json.dumps(payload).encode()
    head = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=body, headers=head)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - configured URL
        return json.loads(resp.read().decode())


class OpenAIEmbedder(Embedder):
    name = "openai"

    def __init__(self, *, model: str = "text-embedding-3-small",
                 base_url: str = "https://api.openai.com/v1",
                 api_key_env: str = "OPENAI_API_KEY", batch_size: int = 64,
                 timeout: float = 120):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.batch_size = batch_size
        self.timeout = timeout

    def _headers(self) -> dict:
        key = os.environ.get(self.api_key_env)
        return {"Authorization": f"Bearer {key}"} if key else {}

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        step = max(1, self.batch_size)
        for i in range(0, len(texts), step):
            batch = texts[i:i + step]
            res = _post_json(f"{self.base_url}/embeddings",
                             {"model": self.model, "input": batch},
                             self._headers(), self.timeout)
            rows = sorted(res.get("data", []), key=lambda d: d.get("index", 0))
            out.extend([float(x) for x in row.get("embedding", [])] for row in rows)
        return out
