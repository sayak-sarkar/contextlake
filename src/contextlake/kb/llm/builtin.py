"""Built-in, CPU-only generative LLM for the wiki tier — no Ollama / API key.

Runs a small int4 model in-process via ``openvino-genai``, auto-downloaded to the
cache dir (default ``~/.contextlake/models``) on first use. Needs the ``llm-local``
extra. CPU generation is **slow** and the wiki makes several calls per repo, so this
suits small workspaces — prefer a real endpoint (Ollama / OpenAI-compatible) or the
prebuilt Docker image at scale.

The model loads **lazily** on the first ``generate()``, so importing and
constructing this class is cheap and never touches the network (CI-safe).

**Why OpenVINO rather than llama.cpp.** The GGUF path needed a per-accelerator wheel
index, because llama-cpp-python publishes no wheels to PyPI and pip otherwise
compiles C++ from source. It also had no wheel for CPython 3.14 on any x86_64
platform (measured: exactly two cp314 wheels exist upstream, both ``linux_riscv64``),
which pinned the whole project's container base image to an older Python. openvino-genai
ships ordinary manylinux wheels for cp310 through cp314, needs no compiler and no custom
index, and drags in neither torch nor transformers.
"""

from __future__ import annotations

import os
from pathlib import Path

from .base import LlmClient

# One copy, raised from both `preflight` (before any work is announced) and
# `_ensure_model` (the lazy path, for callers that never preflight). Deliberately a
# plain `pip install` now: openvino-genai is an ordinary PyPI wheel, so the
# extra-index and --only-binary machinery the GGUF backend needed is gone rather
# than repointed.
_MISSING_EXTRA = (
    "The built-in LLM needs the 'llm-local' extra (openvino-genai).\n"
    "Install it with:\n"
    "  contextlake doctor --fix llm-local\n"
    "or by hand:\n"
    "  pip install 'contextlake[llm-local]'\n"
    "Or use a hosted model instead: --llm ollama | openai."
)

DEFAULT_CACHE_DIR = "~/.contextlake/models"
# Pre-converted OpenVINO IR, published by the OpenVINO org, Apache-2.0. Chosen to
# match what it replaces rather than to upgrade it under cover of a dependency
# change: same model family and size class as the previous GGUF default
# (Qwen2.5 0.5B instruct), code-specialised, and smaller on disk (349 MB against
# 491 MB). "Pre-converted" is the load-bearing word — converting a stock checkout
# to IR needs optimum-intel, which pulls torch and transformers, and that footprint
# would be worse than the one this change removes.
DEFAULT_REPO = "OpenVINO/Qwen2.5-Coder-0.5B-Instruct-int4-ov"
DEFAULT_DEVICE = "CPU"


class BuiltinLlm(LlmClient):
    """A local CPU LLM backed by a small int4 OpenVINO IR model."""

    name = "builtin"

    def __init__(self, *, repo_id: str = DEFAULT_REPO, cache_dir: str | None = None,
                 device: str = DEFAULT_DEVICE, max_tokens: int = 1024,
                 temperature: float = 0.2):
        self.repo_id = repo_id
        self.cache_dir = Path(os.path.expanduser(cache_dir or DEFAULT_CACHE_DIR))
        self.device = device
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._pipe = None  # loaded lazily on first generate()

    def _ensure_model(self):
        if self._pipe is not None:
            return self._pipe
        os.environ.setdefault("HF_HOME", str(self.cache_dir))
        from .._util import hush_hf_hub
        hush_hf_hub()   # the model is downloaded, not uploaded — quiet HF's notices
        # Imported BY NAME, not as a module then attribute: a present-but-incompatible
        # openvino_genai (a partial install, or a future version that moved the class)
        # then raises ImportError and gets the actionable message, rather than an
        # AttributeError that names an internal symbol and tells the user nothing.
        try:
            from openvino_genai import LLMPipeline
        except ImportError as e:
            raise ImportError(_MISSING_EXTRA) from e
        from huggingface_hub import snapshot_download

        # A whole directory, not one file: OpenVINO IR is a model pair plus its
        # tokenizer and detokenizer models, and the pipeline opens the directory.
        model_dir = snapshot_download(repo_id=self.repo_id)
        self._pipe = LLMPipeline(model_dir, self.device)
        return self._pipe

    def _config(self):
        from openvino_genai import GenerationConfig
        cfg = GenerationConfig()
        # max_new_tokens defaults to 2**64-1, so leaving it unset is not "sensible
        # default", it is "generate until the context runs out".
        cfg.max_new_tokens = self.max_tokens
        # Sampling is off by default, and temperature is ignored while it is off --
        # setting temperature alone would look configured and behave greedily.
        if self.temperature and self.temperature > 0:
            cfg.do_sample = True
            cfg.temperature = self.temperature
        return cfg

    def preflight(self) -> None:
        """Fail now if openvino-genai is absent, rather than mid-run.

        Import-only on purpose: it does not construct a pipeline, so it neither
        downloads the model nor loads weights. The whole point is to be cheap enough
        to run before a caller announces work.
        """
        from .._util import hush_hf_hub
        hush_hf_hub()
        try:
            import openvino_genai  # noqa: F401
        except ImportError as e:
            raise RuntimeError(_MISSING_EXTRA) from e

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        pipe = self._ensure_model()
        # start_chat/finish_chat around each call applies the model's own chat
        # template (which plain `generate` does not) while keeping every call
        # stateless -- the wiki's calls are independent and must not accumulate
        # history across repos.
        pipe.start_chat(system) if system else pipe.start_chat()
        try:
            out = pipe.generate(prompt, self._config())
        finally:
            pipe.finish_chat()
        return str(out).strip()
