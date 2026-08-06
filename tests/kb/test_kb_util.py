"""Unit tests for contextlake.kb._util."""

from __future__ import annotations

import pytest

from contextlake import logging_setup as logging_setup_mod
from contextlake.kb import _util as util_mod


def test_hush_hf_hub_disables_progress_bars_by_default(monkeypatch):
    """Pins F8: 'kb connect'/'kb embed' leaked HF Hub's tqdm progress bars
    ('Fetching N files: 100%|...', 'Download complete: | 0.00B',
    'Reconstruction complete: | 0.00B / 0.00B') even though hush_hf_hub() was
    already called at every download site. The env vars and logger levels it
    sets (HF_HUB_VERBOSITY, HF_HUB_DISABLE_TELEMETRY, the warnings filter) gate
    HF's own logger and deprecation notices -- never the tqdm progress bars,
    which are a separate switch (disable_progress_bars). At default
    (non-verbose) output the bars must be off.
    """
    hub_utils = pytest.importorskip("huggingface_hub.utils")
    monkeypatch.setattr(logging_setup_mod, "console_verbose", lambda: False)
    hub_utils.enable_progress_bars()  # start from "not yet hushed" so the assert is real
    try:
        util_mod.hush_hf_hub()
        assert hub_utils.are_progress_bars_disabled() is True
    finally:
        hub_utils.enable_progress_bars()


def test_hush_hf_hub_leaves_progress_bars_on_under_verbose(monkeypatch):
    """A user who passed --verbose still sees HF download progress -- hushing
    is the default, not an unconditional suppression."""
    hub_utils = pytest.importorskip("huggingface_hub.utils")
    monkeypatch.setattr(logging_setup_mod, "console_verbose", lambda: True)
    hub_utils.enable_progress_bars()
    try:
        util_mod.hush_hf_hub()
        assert hub_utils.are_progress_bars_disabled() is False
    finally:
        hub_utils.enable_progress_bars()


def test_hush_hf_hub_tolerates_huggingface_hub_not_installed(monkeypatch):
    """hush_hf_hub() is called from llm/builtin.py's preflight() before it is
    known whether the optional huggingface_hub package is even installed
    (llama-cpp-python imports it lazily, inside its own try/except). It must
    not raise -- the caller's own ImportError handling is what should surface,
    not one from inside the hushing step."""
    import builtins

    monkeypatch.setattr(logging_setup_mod, "console_verbose", lambda: False)
    real_import = builtins.__import__

    def _no_hub(name, *args, **kwargs):
        if name == "huggingface_hub.utils" or name.startswith("huggingface_hub"):
            raise ImportError(f"simulated absence of {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_hub)
    util_mod.hush_hf_hub()  # must not raise
