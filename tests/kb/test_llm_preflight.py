"""A backend with a missing prerequisite must fail before work is announced.

`kb wiki --llm builtin` on a machine without the `llm-local` extra printed a
reviewer-quality advisory and "Generating wiki for 1 repo(s) with builtin
(council of 3)", and only then reported that the backend was never installed.
"""

from __future__ import annotations

import pytest

from contextlake.kb.llm.base import LlmClient
from contextlake.kb.llm.builtin import _MISSING_EXTRA, BuiltinLlm


class _Bare(LlmClient):
    name = "bare"

    def generate(self, prompt, *, system=None):
        return ""


def test_preflight_defaults_to_a_no_op():
    # Only a client with a real local prerequisite should override it; a network-only
    # failure mode must not pay for a probe here.
    _Bare().preflight()


def test_builtin_preflight_raises_when_the_extra_is_absent(monkeypatch):
    import builtins

    real = builtins.__import__

    def no_llama(name, *a, **k):
        if name == "llama_cpp":
            raise ImportError("no llama_cpp")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_llama)
    with pytest.raises(RuntimeError) as caught:
        BuiltinLlm().preflight()
    msg = str(caught.value)
    # The actionable parts, not just "unavailable".
    assert "doctor --fix llm-local" in msg
    assert "--only-binary llama-cpp-python" in msg
    assert "--extra-index-url" in msg


def test_the_missing_extra_message_has_one_definition():
    # It is raised from both preflight and the lazy _ensure_model path; two copies
    # would drift.
    src = (__import__("pathlib").Path(
        __import__("contextlake.kb.llm.builtin", fromlist=["x"]).__file__).read_text())
    assert src.count("The built-in LLM needs the 'llm-local' extra") == 1
    assert _MISSING_EXTRA.count("doctor --fix llm-local") == 1


def test_builtin_preflight_does_not_download_or_load_weights(monkeypatch):
    # Import-only by contract: it must not reach Llama.from_pretrained, or a
    # pre-announcement check would pull a multi-GB GGUF.
    called = []
    llm = BuiltinLlm()
    monkeypatch.setattr(llm, "_ensure_model", lambda: called.append(1))
    try:
        llm.preflight()
    except RuntimeError:
        pass  # extra genuinely absent in this environment; still must not load
    assert not called
