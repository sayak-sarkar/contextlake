"""Bake the built-in models into the image's HuggingFace cache (Docker build step).

Runs at image-build time so a `docker run` needs no model download at runtime —
which also sidesteps corporate-proxy TLS issues for end users entirely.
"""

import os

os.environ.setdefault("HF_HOME", "/opt/contextlake/models")

from huggingface_hub import hf_hub_download  # noqa: E402

from contextlake.kb.embeddings.builtin import BuiltinEmbedder  # noqa: E402
from contextlake.kb.llm.builtin import DEFAULT_FILE, DEFAULT_REPO  # noqa: E402

# Embedder: model2vec potion-base-8M (downloaded + loaded by encode()).
BuiltinEmbedder().embed(["warmup"])

# Wiki LLM: fetch the pinned GGUF file (just the download, no model load).
hf_hub_download(DEFAULT_REPO, DEFAULT_FILE)

print(f"built-in models baked into {os.environ['HF_HOME']}")
