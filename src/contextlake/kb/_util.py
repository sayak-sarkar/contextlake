"""Tiny shared helpers for the knowledge layer — stdlib only, no extra deps.

These were duplicated verbatim across the llm/ and embeddings/ providers and the
connectors; kept here as the single source of truth.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


def chunks(seq, n):
    """Yield successive ``n``-sized slices of ``seq``."""
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _or_default(value, default):
    """``value`` unless it was never supplied, in which case ``default``.

    Deliberately not ``value or default``: every numeric flag used that spelling,
    so an explicit ``--limit 0`` / ``--max-nodes 0`` / ``--hops 0`` was indistinguishable
    from "unset" and silently became the default -- the same bug in three commands.
    Only ``None`` means unset.
    """
    return default if value is None else value


def hush_hf_hub() -> None:
    """Quiet Hugging Face Hub download noise before a model fetch.

    These calls *download* a model file into the local cache (read-only) — nothing of
    the user's is uploaded. But HF prints two notices that can read, to someone glancing
    at the terminal, like outbound data transfer: a ``local_dir_use_symlinks``
    deprecation, and an anonymous-rate-limit line ("You are sending unauthenticated
    requests to the HF Hub…"). For a local-first tool that is a misleading first
    impression, so we silence them.

    The download's own ``tqdm`` progress bars are a separate switch
    (``disable_progress_bars``, not the verbosity/logging settings above -- those
    only gate HF's own logger and warnings, never tqdm) and are hushed too unless
    ``--verbose`` was passed: three of them render per fetch, two of which show no
    file name or percentage (only a byte count that starts and ends at ``0.00B``),
    so at default verbosity they are noise with no actionable content. A verbose
    run still wants to see them, e.g. to confirm a large model is actually moving.
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

    from ..logging_setup import console_verbose
    if not console_verbose():
        try:
            from huggingface_hub.utils import disable_progress_bars
        except ImportError:
            pass  # not installed yet -- the caller's own ImportError handling covers this
        else:
            disable_progress_bars()


def ollama_reachable(base_url: str, timeout: float = 1.5) -> bool:
    """True if a local Ollama daemon answers quickly (so 'auto' never hangs)."""
    try:
        url = base_url.rstrip("/") + "/api/tags"
        with urllib.request.urlopen(url, timeout=timeout):  # noqa: S310 - local URL
            return True
    except Exception:  # noqa: BLE001 - any failure means "not reachable"
        return False


def ollama_list_models(base_url: str, timeout: float = 1.5) -> list[str]:
    """Names of every model Ollama actually has pulled, or ``[]`` on any
    failure (daemon down, timeout, malformed response) -- a caller checking
    "is model X usable" should treat that the same as "no, it isn't", not
    raise. The daemon being reachable at all (see ``ollama_reachable``) says
    nothing about which models it can actually serve -- a very common real
    setup is Ollama running for chat models with no embedding model pulled.
    """
    try:
        url = base_url.rstrip("/") + "/api/tags"
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - local URL
            data = json.loads(resp.read().decode())
        return [m["name"] for m in data.get("models", []) if "name" in m]
    except Exception:  # noqa: BLE001 - any failure means "treat as no models"
        return []


def ollama_has_model(base_url: str, model: str, timeout: float = 1.5) -> bool:
    """Whether ``model`` is among Ollama's pulled models. Matches with or
    without Ollama's implicit ``:latest`` tag on either side -- a config that
    just says ``model = "nomic-embed-text"`` should match Ollama listing it
    as ``nomic-embed-text:latest``, and vice versa."""
    def _bare(name: str) -> str:
        return name.split(":", 1)[0] if name.endswith(":latest") else name

    wanted = _bare(model)
    return any(_bare(m) == wanted for m in ollama_list_models(base_url, timeout))


def post_json(url: str, payload: dict, timeout: float, headers: dict | None = None) -> dict:
    """POST ``payload`` as JSON and return the decoded JSON response.

    ``headers`` are merged over the default Content-Type (e.g. an Authorization
    bearer for a hosted OpenAI-compatible API); omit for local servers.
    """
    body = json.dumps(payload).encode()
    head = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=body, headers=head)  # noqa: S310 - URL from trusted config
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - configured URL
        return json.loads(resp.read().decode())


def describe_ollama_http_error(e: urllib.error.HTTPError, model: str) -> str:
    """A clear, actionable reason for an Ollama API call's HTTPError, shared by
    the embeddings and LLM Ollama clients (both hit this the same way).

    Ollama's real reason for a 404 (``{"error": "model \\"X\\" not found, try
    pulling it first"}``) is in the response body, which bare urllib discards
    -- surfacing only the generic status-line reason ("Not Found") reads as a
    network/connectivity problem when it's almost always just an unpulled
    model. Only reached for an *explicit* ``provider = "ollama"`` config;
    ``provider = "auto"`` checks model availability before ever picking Ollama
    (see embeddings/llm ``base.py``'s ``_resolve_auto_*``), so it never hits
    this for a missing model.
    """
    try:
        parsed_body = json.loads(e.read().decode())
        reason = parsed_body.get("error") or str(e)
    except Exception:  # noqa: BLE001 - a malformed/non-JSON body is still an HTTPError
        reason = str(e)
    if e.code == 404 and "not found" in reason.lower():
        return (f"Ollama: {reason} -- run 'ollama pull {model}' "
                "(or point config's model at one you've already pulled)")
    return f"Ollama returned HTTP {e.code}: {reason}"
