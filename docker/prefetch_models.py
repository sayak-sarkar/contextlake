"""Bake the built-in models into the image's HuggingFace cache (Docker build step).

Runs at image-build time so a `docker run` needs no model download at runtime —
which also sidesteps corporate-proxy TLS issues for end users entirely.

Shared by both Docker image variants (see Dockerfile): the embedder is always
warmed (pure Python + numpy, no toolchain needed); the wiki-LLM model is only
fetched when `openvino-genai` (the `llm-local` extra) is actually installed,
so the "slim" variant doesn't bake in a model it has no way to run.
"""

import importlib.util
import os

os.environ.setdefault("HF_HOME", "/opt/contextlake/models")

from contextlake.kb.embeddings.builtin import BuiltinEmbedder  # noqa: E402

# Embedder: model2vec potion-base-8M (downloaded + loaded by encode()).
BuiltinEmbedder().embed(["warmup"])

if importlib.util.find_spec("openvino_genai") is not None:
    from huggingface_hub import snapshot_download

    from contextlake.kb.llm.builtin import DEFAULT_REPO

    # Wiki LLM: fetch the whole model directory (just the download, no model
    # load). OpenVINO IR is a model pair plus its tokenizer and detokenizer, so
    # a single-file fetch would bake in something the pipeline cannot open.
    snapshot_download(repo_id=DEFAULT_REPO)
else:
    print("openvino-genai not installed -- skipping wiki-LLM prefetch (slim image)")

print(f"built-in models baked into {os.environ['HF_HOME']}")
