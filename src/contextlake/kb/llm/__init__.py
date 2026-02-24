"""Optional local-first LLM tier for the curated wiki and its review council.

Nothing here runs unless ``[llm] enabled = true``. The ``LlmClient`` interface
keeps generation provider-agnostic; a local Ollama provider ships first.
"""

from .base import LlmClient, build_llm

__all__ = ["LlmClient", "build_llm"]
