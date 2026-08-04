"""Bake the built-in models into the image's HuggingFace cache (Docker build step).

Runs at image-build time so a `docker run` needs no model download at runtime —
which also sidesteps corporate-proxy TLS issues for end users entirely.

Shared by both Docker image variants (see Dockerfile): the embedder is always
warmed (pure Python + numpy, no toolchain needed); the wiki-LLM GGUF is only
fetched when `llama-cpp-python` (the `llm-local` extra) is actually installed,
so the "slim" variant doesn't bake in a model it has no way to run.
"""

import importlib.util
import os

os.environ.setdefault("HF_HOME", "/opt/contextlake/models")

from contextlake.kb.embeddings.builtin import BuiltinEmbedder  # noqa: E402

# Embedder: model2vec potion-base-8M (downloaded + loaded by encode()).
BuiltinEmbedder().embed(["warmup"])

if importlib.util.find_spec("llama_cpp") is not None:
    from huggingface_hub import hf_hub_download

    from contextlake.kb.llm.builtin import DEFAULT_FILE, DEFAULT_REPO

    # Wiki LLM: fetch the pinned GGUF file (just the download, no model load).
    hf_hub_download(DEFAULT_REPO, DEFAULT_FILE)
else:
    print("llama-cpp-python not installed -- skipping wiki-LLM GGUF prefetch (slim image)")

print(f"built-in models baked into {os.environ['HF_HOME']}")
