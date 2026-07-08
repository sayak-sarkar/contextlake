"""Text generation by shelling out to a locally-installed agent CLI.

Reuses a subscription the user already has (``claude -p`` / ``gemini`` / ``codex``)
instead of an API key contextlake would have to hold. Data goes to whatever provider
that CLI uses — the user's informed choice, documented as such. Stdlib ``subprocess``
only; the prompt (system prepended) is fed on **stdin**, never as a shell string, so
there is no shell-injection surface and no argv length limit.
"""

from __future__ import annotations

import subprocess

from ...logging_setup import log
from .base import LlmClient

# Non-interactive invocation per known CLI. `args` in config overrides these.
_PRESETS: dict[str, list[str]] = {
    "claude": ["-p"],        # print mode, reads the prompt from stdin
    "gemini": ["-p"],
    "codex": ["exec"],
}


class CliLlm(LlmClient):
    name = "cli"

    def __init__(self, *, command: str = "claude",
                 args: list[str] | None = None, timeout: float = 300):
        self.command = command
        self.args = list(args) if args is not None else _PRESETS.get(command, [])
        self.timeout = timeout

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        text = prompt if not system else f"{system}\n\n{prompt}"
        argv = [self.command, *self.args]
        try:
            res = subprocess.run(argv, input=text, capture_output=True,
                                 text=True, timeout=self.timeout)
        except FileNotFoundError as e:
            # Misconfiguration, not a transient failure — fail fast, actionably.
            raise RuntimeError(
                f"llm provider=cli: command {self.command!r} not found on PATH — "
                f"install it or set [llm] command to a valid CLI") from e
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"  cli llm ({self.command}) failed: {e}")
            return ""
        if res.returncode != 0:
            log(f"  cli llm ({self.command}) exit {res.returncode}: "
                f"{(res.stderr or '').strip()[:200]}")
            return ""
        return (res.stdout or "").strip()
