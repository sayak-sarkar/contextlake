"""Built-in, CPU-only generative LLM for the wiki tier — no Ollama / API key.

Runs a small quantized GGUF model in-process via ``llama-cpp-python``,
auto-downloaded to the cache dir (default ``~/.contextlake/models``) on first use.
Needs the ``llm-local`` extra. CPU generation is **slow** and the wiki makes
several calls per repo, so this suits small workspaces — prefer a real endpoint
(Ollama / OpenAI-compatible) or the prebuilt Docker image at scale.

The model loads **lazily** on the first ``generate()``, so importing and
constructing this class is cheap and never touches the network (CI-safe).
"""

from __future__ import annotations

import os
from pathlib import Path

from .base import LlmClient

DEFAULT_CACHE_DIR = "~/.contextlake/models"
DEFAULT_REPO = "Qwen/Qwen2.5-0.5B-Instruct-GGUF"  # Apache-2.0
DEFAULT_FILE = "qwen2.5-0.5b-instruct-q4_k_m.gguf"


class BuiltinLlm(LlmClient):
    """A local CPU LLM backed by a small GGUF via llama-cpp-python."""

    name = "builtin"

    def __init__(self, *, repo_id: str = DEFAULT_REPO, filename: str = DEFAULT_FILE,
                 cache_dir: str | None = None, n_ctx: int = 4096,
                 max_tokens: int = 1024, temperature: float = 0.2):
        self.repo_id = repo_id
        self.filename = filename
        self.cache_dir = Path(os.path.expanduser(cache_dir or DEFAULT_CACHE_DIR))
        self.n_ctx = n_ctx
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._llm = None  # loaded lazily on first generate()

    def _ensure_model(self):
        if self._llm is not None:
            return self._llm
        os.environ.setdefault("HF_HOME", str(self.cache_dir))
        from .._util import hush_hf_hub
        hush_hf_hub()   # the GGUF is downloaded, not uploaded — quiet HF's noisy notices
        try:
            from llama_cpp import Llama
        except ImportError as e:
            # One complete, copy-pasteable command. llama-cpp-python publishes no
            # wheels on PyPI at all (llama.cpp is built per hardware backend, so
            # upstream ships one index per accelerator), so the extra index is not
            # an "if that fails" fallback: without it pip always compiles C++.
            raise ImportError(
                "The built-in LLM needs the 'llm-local' extra (llama-cpp-python).\n"
                "Install it with:\n"
                "  contextlake doctor --fix llm-local\n"
                "or by hand:\n"
                "  pip install 'contextlake[llm-local]' --only-binary llama-cpp-python "
                "--extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu\n"
                "The index is required, not a fallback: llama-cpp-python publishes no "
                "wheels to PyPI, so without it pip compiles from source and needs cmake "
                "plus a C++ compiler.\n"
                "Or use a hosted model instead: --llm ollama | openai."
            ) from e
        self._llm = Llama.from_pretrained(
            repo_id=self.repo_id, filename=self.filename,
            n_ctx=self.n_ctx, verbose=False,
        )
        return self._llm

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        llm = self._ensure_model()
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        res = llm.create_chat_completion(
            messages=messages, max_tokens=self.max_tokens, temperature=self.temperature,
        )
        return (res["choices"][0]["message"]["content"] or "").strip()
