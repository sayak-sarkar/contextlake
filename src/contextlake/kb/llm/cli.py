"""Text generation by shelling out to a locally-installed agent CLI.

Reuses a subscription the user already has (``claude -p`` / ``gemini`` / ``codex``)
instead of an API key contextlake would have to hold. Data goes to whatever provider
that CLI uses — the user's informed choice, documented as such. Stdlib ``subprocess``
only; the prompt (system prepended) is fed on **stdin**, never as a shell string, so
there is no shell-injection surface and no argv length limit.
"""

from __future__ import annotations

import os
import subprocess

from ...logging_setup import log
from .base import LlmClient

# Non-interactive invocation per known CLI. `args` in config overrides these.
# `claude` also gets `--safe-mode`: this call exists to get a plain completion,
# not an interactive Claude Code session, but a `claude -p` subprocess still
# reads the same account's CLAUDE.md/skills/plugins/output-style config -- live
# repro showed a wiki draft coming back polluted with unrelated "insight
# callout" formatting because the caller's account happened to have an
# explanatory output-style plugin enabled globally. `--safe-mode` disables all
# of that (auth/model/tools still work), which is what a text-generation
# backend wants: deterministic output independent of the user's personal
# customizations. Not yet verified whether `gemini`/`codex` have an equivalent.
_PRESETS: dict[str, list[str]] = {
    "claude": ["-p", "--safe-mode"],
    "gemini": ["-p"],
    "codex": ["exec"],
}

# `claude` and `gemini` both document that their own API-key env var takes
# *precedence* over an existing subscription login and must be explicitly unset
# to fall back to it -- confirmed for `claude` by live repro (a stray
# ANTHROPIC_API_KEY made `claude -p` refuse the claude.ai connector and fail with
# a credit-balance error) and for `gemini` by its own auth docs ("if you have
# previously set GOOGLE_API_KEY or GEMINI_API_KEY, you must unset them" to use
# any other method). `codex`'s docs describe API-key auth as a separate,
# explicitly-opted-into mode (`codex login --with-api-key`) rather than an
# env-var override, so OPENAI_API_KEY being merely present may not have the same
# effect there -- it's stripped anyway as a harmless defensive measure in case
# that changes, not because it's confirmed to hijack auth today.
#
# In all cases: a key sitting in the environment for an unrelated reason (testing
# a different [llm] provider, another tool) must not silently defeat this
# provider's entire point ("no API key touches contextlake") and bill a
# pay-per-token account the user never meant to use here. Stripped from the
# child's environment only, never touched in this process or logged. An
# unrecognised ``command`` (a user's own CLI) strips nothing -- we don't know its
# auth model, and guessing could break a setup that needs the var.
_AUTH_ENV_VARS: dict[str, tuple[str, ...]] = {
    "claude": ("ANTHROPIC_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "codex": ("OPENAI_API_KEY",),
}


class CliLlm(LlmClient):
    name = "cli"

    def __init__(self, *, command: str = "claude",
                 args: list[str] | None = None, timeout: float = 300):
        self.command = command
        # Look presets/auth-vars up by basename so a path-qualified command
        # (e.g. `/usr/local/bin/claude` or a shim) still matches `claude`'s
        # entry -- an exact-string match would silently miss both.
        known = os.path.basename(command)
        self.args = list(args) if args is not None else _PRESETS.get(known, [])
        self.timeout = timeout

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        text = prompt if not system else f"{system}\n\n{prompt}"
        argv = [self.command, *self.args]
        env = os.environ.copy()
        for var in _AUTH_ENV_VARS.get(os.path.basename(self.command), ()):
            env.pop(var, None)
        try:
            res = subprocess.run(argv, input=text, capture_output=True,
                                 text=True, timeout=self.timeout, env=env)
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
