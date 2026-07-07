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


def hush_hf_hub() -> None:
    """Quiet Hugging Face Hub download noise before a model fetch.

    These calls *download* a model file into the local cache (read-only) — nothing of
    the user's is uploaded. But HF prints two notices that can read, to someone glancing
    at the terminal, like outbound data transfer: a ``local_dir_use_symlinks``
    deprecation, and an anonymous-rate-limit line ("You are sending unauthenticated
    requests to the HF Hub…"). For a local-first tool that is a misleading first
    impression, so we silence them (the actual "Downloading … / Download complete"
    progress still shows).
    """
    import logging
    import os
    import warnings

    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_VERBOSITY", "error")
    warnings.filterwarnings("ignore", category=UserWarning, module=r"huggingface_hub.*")
    # Post-import safety net (the env vars only bite if read before HF initialised):
    for name in ("huggingface_hub", "huggingface_hub.file_download",
                 "huggingface_hub.utils._http", "huggingface_hub.utils._auth"):
        logging.getLogger(name).setLevel(logging.ERROR)


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
