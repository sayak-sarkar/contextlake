"""Tiny shared helpers for the knowledge layer — stdlib only, no extra deps.

These were duplicated verbatim across the llm/ and embeddings/ providers and the
connectors; kept here as the single source of truth.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from ..logging_setup import log


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


# The opener every credential-bearing HTTP client uses: the ingest sources and `post_json`
# (the hosted LLM and embeddings clients). `urlopen` follows a redirect and copies the
# request's headers onto it, so a redirect from the configured endpoint to another host
# carried the API key there.

_DEFAULT_PORTS = {"http": 80, "https": 443}

# Lower-case names of the headers contextlake puts a secret in. `Request` stores a name
# capitalised (`x-api-key` becomes `X-api-key`), so the comparison lower-cases both sides.
_CREDENTIAL_HEADERS = frozenset({"authorization", "x-api-key", "proxy-authorization"})


def _origin(url: str) -> tuple[str, str, int | None]:
    """``(scheme, host, port)`` of ``url``, lower-cased, with the default port filled in,
    so ``http://h`` and ``http://H:80/x`` compare equal."""
    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "").lower()
    try:
        port = parts.port or _DEFAULT_PORTS.get(scheme)
    except ValueError:      # a malformed port is its own origin, never a match
        port = -1
    return scheme, (parts.hostname or "").lower(), port


def _same_origin(a: str, b: str) -> bool:
    """True when both URLs share scheme, host and port: the unit a credential may travel in."""
    return _origin(a) == _origin(b)


class _OriginGuardedRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but drop every credential header when the target is a different origin.

    urllib builds the redirected request from the original's headers, so the credential
    went wherever a ``Location`` pointed. An open redirect on the API host, or a
    compromised endpoint, then received the token (for ``auth="basic"`` on Atlassian that
    is an account-wide ``email:token``). A same-origin redirect keeps the headers, because
    APIs do redirect within their own host and that request still needs to authenticate.
    The check is on the header names in ``_CREDENTIAL_HEADERS``, so one handler covers the
    bearer sources, the OpenAI clients (``Authorization``) and the Anthropic client
    (``x-api-key``).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and not _same_origin(req.full_url, new.full_url):
            dropped = False
            for store in (new.headers, new.unredirected_hdrs):
                for name in [k for k in store if k.lower() in _CREDENTIAL_HEADERS]:
                    del store[name]
                    dropped = True
            if dropped:
                log(f"a redirect left {_origin(req.full_url)[1]} for "
                    f"{_origin(new.full_url)[1]}; the credential header was not sent there",
                    level=logging.WARNING)
        return new


# Built once: an opener carries no per-request state, and `urlopen` builds a default one
# per call anyway. Same proxy and HTTPS handling as `urlopen`; only the redirect handler
# differs.
_OPENER = urllib.request.build_opener(_OriginGuardedRedirect)


def post_json(url: str, payload: dict, timeout: float, headers: dict | None = None) -> dict:
    """POST ``payload`` as JSON and return the decoded JSON response.

    ``headers`` are merged over the default Content-Type (e.g. an Authorization
    bearer for a hosted OpenAI-compatible API); omit for local servers.
    """
    body = json.dumps(payload).encode()
    head = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=body, headers=head)  # noqa: S310 - URL from trusted config
    # The shared opener, not `urlopen`: urlopen follows a redirect WITH the key, so a
    # gateway that redirected to another host received `Authorization` or `x-api-key`.
    with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310 - configured URL
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
