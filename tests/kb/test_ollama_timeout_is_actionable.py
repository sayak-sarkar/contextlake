"""A local-model timeout must say more than "timed out".

urllib raises a read timeout whose entire message is those two words. Callers
print `str(exc)`, so a wiki run against a CPU-only Ollama reported `timed out`
per page: not the provider, not the model, not the budget it waited for, and not
that the budget is configurable.
"""

from __future__ import annotations

import urllib.error

import pytest

from contextlake.kb.config import LlmCfg
from contextlake.kb.llm.ollama import DEFAULT_TIMEOUT, OllamaLlm, _is_timeout


@pytest.mark.parametrize("exc", [
    TimeoutError("timed out"),
    urllib.error.URLError(TimeoutError("timed out")),
])
def test_both_timeout_shapes_are_recognised(exc):
    # urllib presents a timeout bare or wrapped in a URLError's `reason`. Missing
    # the wrapped spelling is how one falls through as a generic error and loses
    # the actionable message. (socket.timeout needs no case of its own: it is an
    # alias of TimeoutError from 3.10, and this package requires >=3.10.)
    assert _is_timeout(exc)


@pytest.mark.parametrize("exc", [
    ValueError("nope"),
    urllib.error.URLError(ConnectionRefusedError("refused")),
    OSError("something else"),
])
def test_non_timeouts_are_not_swallowed_as_timeouts(exc):
    assert not _is_timeout(exc)


def test_a_timeout_names_the_budget_the_model_and_the_knob(monkeypatch):
    llm = OllamaLlm(model="llama3.1", timeout=42)

    def boom(*a, **k):
        raise TimeoutError("timed out")

    monkeypatch.setattr("contextlake.kb.llm.ollama.post_json", boom)
    with pytest.raises(RuntimeError) as caught:
        llm.generate("hi")
    msg = str(caught.value)
    assert "42s" in msg, "the budget actually waited for must appear"
    assert "llama3.1" in msg
    assert "timeout" in msg and "[llm]" in msg, "name the config knob"
    assert "GPU" in msg, "CPU-only is the usual cause; say so"


def test_a_non_timeout_error_propagates_unchanged(monkeypatch):
    llm = OllamaLlm()

    def boom(*a, **k):
        raise ValueError("a real bug, not a timeout")

    monkeypatch.setattr("contextlake.kb.llm.ollama.post_json", boom)
    # Must not be reworded into a timeout message.
    with pytest.raises(ValueError, match="a real bug"):
        llm.generate("hi")


def test_timeout_is_a_declared_config_field_not_an_extra():
    # It reached the client via extra="allow" before, which worked but kept it out
    # of config docs and validation, so it was undiscoverable.
    assert "timeout" in LlmCfg.model_fields
    assert LlmCfg().timeout == DEFAULT_TIMEOUT
    assert LlmCfg(timeout=900).timeout == 900
