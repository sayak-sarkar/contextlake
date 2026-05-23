"""Tiny shared helpers for the knowledge layer — stdlib only, no extra deps.

These were duplicated verbatim across the llm/ and embeddings/ providers and the
connectors; kept here as the single source of truth.
"""

from __future__ import annotations

import json
import urllib.request


def chunks(seq, n):
    """Yield successive ``n``-sized slices of ``seq``."""
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def ollama_reachable(base_url: str, timeout: float = 1.5) -> bool:
    """True if a local Ollama daemon answers quickly (so 'auto' never hangs)."""
    try:
        url = base_url.rstrip("/") + "/api/tags"
        with urllib.request.urlopen(url, timeout=timeout):  # noqa: S310 - local URL
            return True
    except Exception:  # noqa: BLE001 - any failure means "not reachable"
        return False


def post_json(url: str, payload: dict, timeout: float, headers: dict | None = None) -> dict:
    """POST ``payload`` as JSON and return the decoded JSON response.

    ``headers`` are merged over the default Content-Type (e.g. an Authorization
    bearer for a hosted OpenAI-compatible API); omit for local servers.
    """
    body = json.dumps(payload).encode()
    head = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=body, headers=head)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - configured URL
        return json.loads(resp.read().decode())
