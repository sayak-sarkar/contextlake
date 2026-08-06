"""Local text generation via an Ollama server — stdlib HTTP only, no extra deps.

Uses the stable ``POST /api/generate`` endpoint ({"model","prompt","system",
"stream":false} -> {"response": "..."}). The default endpoint is a local daemon,
so prompts never leave the machine.
"""

from __future__ import annotations

import urllib.error

from .._util import describe_ollama_http_error, post_json
from ..resilience import breaker_for, endpoint_key
from .base import LlmClient

DEFAULT_TIMEOUT = 300.0


def _is_timeout(exc: BaseException) -> bool:
    """Whether an exception raised by ``post_json`` is a read/connect timeout.

    urllib does not present a timeout uniformly: it can surface bare or wrapped
    inside a ``URLError``'s ``reason``. Both spellings must be caught, or a wrapped
    one falls through as a generic error and loses the actionable message.

    ``socket.timeout`` needs no separate arm: it has been an alias of
    ``TimeoutError`` since 3.10 and this package requires >=3.10.
    """
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(getattr(exc, "reason", None), TimeoutError)


class OllamaLlm(LlmClient):
    name = "ollama"

    def __init__(self, *, model: str = "llama3.1",
                 base_url: str = "http://127.0.0.1:11434",
                 timeout: float = DEFAULT_TIMEOUT):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        payload = {"model": self.model, "prompt": prompt, "stream": False}
        if system:
            payload["system"] = system
        # Guarded by a per-endpoint circuit breaker: wiki generation calls this
        # once per page, so a daemon that has stopped answering would otherwise
        # cost the whole run one timeout per page. A 404 for an unpulled model is
        # NOT counted against the endpoint (see resilience.is_endpoint_failure),
        # so the actionable message below survives however many pages are left.
        guard = breaker_for(endpoint_key("llm:ollama", self.base_url))
        try:
            res = guard.call(post_json, f"{self.base_url}/api/generate", payload, self.timeout)
        except urllib.error.HTTPError as e:
            # Only an explicit provider="ollama" reaches here for a missing model
            # -- "auto" checks model availability first (see base.py's
            # _resolve_auto_llm).
            raise RuntimeError(describe_ollama_http_error(e, self.model)) from e
        except Exception as e:
            # A read timeout arrives here carrying the message "timed out" and
            # nothing else -- not which provider, not which model, not how long we
            # actually waited, not that the wait is configurable. Callers print
            # str(exc), so a whole wiki run reported three bare words. Say all of
            # it, and name the knob.
            if not _is_timeout(e):
                raise
            raise RuntimeError(
                f"ollama did not answer within {self.timeout:g}s "
                f"(model {self.model!r} at {self.base_url}). "
                "Raise it with `timeout` under [llm] in kb.toml. "
                "A local model with no GPU generates at CPU speed, and wiki "
                "generation runs a council of 3 per page, so this budget is easy "
                "to exceed -- `ollama ps` shows whether the model loaded onto a "
                "GPU. A smaller model is the other lever."
            ) from e
        return (res.get("response") or "").strip()
