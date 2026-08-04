# contextlake — container image with the knowledge-base extras.
#
# Two variants share this Dockerfile via build targets (default target is
# "full", i.e. what `docker build .` with no --target produces):
#
#   full (default) — ships the [kb,kb-local,llm-local] extras and BAKES IN the
#                     pinned models (model2vec embedder + a small Qwen2.5-0.5B
#                     GGUF wiki LLM), so `docker run` needs no Ollama, no API
#                     key, and no model download at runtime. Large: bundles a
#                     compiled llama-cpp-python + the GGUF.
#   slim            — ships [kb,kb-local,kb-vec] only: no llama-cpp-python (so
#                     no C++ toolchain needed to build it), no baked GGUF.
#                     Semantic search still works out of the box (model2vec is
#                     pure Python + numpy); point the wiki tier at Ollama /
#                     OpenAI / Anthropic / `cli` instead of the built-in LLM.
#                     Much smaller pull — prefer this unless you need fully
#                     offline wiki generation.
#
#   docker build --target full -t contextlake:full .
#   docker build --target slim -t contextlake:slim .
#
#   docker run -v "$PWD/repositories:/work/repositories" \
#     ghcr.io/sayak-sarkar/contextlake doctor
#
# The PyPI wheel remains the primary install; these images are for
# turnkey/offline/no-toolchain use.

# Base pinned by digest, not just the mutable `3.12-slim` tag, for reproducible
# builds (D-4). Re-resolve when bumping:
#   docker pull python:3.12-slim && \
#   docker inspect python:3.12-slim --format '{{index .RepoDigests 0}}'
FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6 AS base

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/contextlake/models \
    PATH="/opt/venv/bin:$PATH"
RUN python -m venv /opt/venv

# ---- dependency layer -------------------------------------------------------
# Copy only the packaging manifest plus the one source file setuptools needs to
# resolve the dynamic version (src/contextlake/__init__.py's __version__) —
# not the whole tree — so an ordinary source-only edit below can't invalidate
# this layer and force a full llama-cpp-python recompile on every build (D-3).
FROM base AS deps
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src/contextlake/__init__.py src/contextlake/__init__.py

# ---- full: prebuilt llama-cpp-python wheel, bakes the GGUF ------------------
FROM deps AS build-full
# No compiler here any more. llama-cpp-python publishes nothing but sdists to
# PyPI, so a plain `pip install` compiles llama.cpp and needs build-essential +
# cmake. Upstream ships prebuilt wheels on a per-accelerator index instead
# (llama.cpp is built per hardware backend, and one PyPI namespace cannot hold
# the cpu/cuda/metal builds of a single version -- the convention PyTorch
# follows), and the CPU index carries a py3-none-manylinux wheel that is
# ABI-agnostic, so it satisfies any Python 3 on this base image.
#
# --only-binary names just that package rather than :all: so a source fallback
# stays available for every other dependency; a missing wheel here is then a
# one-line error instead of a compiler-error wall. Keeping the toolchain out
# also drops the build stage's own CVE surface and most of the build time.
# ca-certificates: TLS for the pip/HF downloads below.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install '.[kb,kb-local,llm-local]' \
        --only-binary llama-cpp-python \
        --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
COPY . /src
RUN pip install --no-deps .
RUN python docker/prefetch_models.py

# ---- slim: no compiler needed, no GGUF baked -------------------------------
FROM deps AS build-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install '.[kb,kb-local,kb-vec]'
COPY . /src
RUN pip install --no-deps .
# llama-cpp-python isn't installed in this variant; prefetch_models.py detects
# that and skips the GGUF download, only warming the (pure-Python) embedder.
RUN python docker/prefetch_models.py

# ---- shared runtime base: no compiler toolchain, non-root ------------------
FROM base AS runtime-base
LABEL org.opencontainers.image.source="https://github.com/sayak-sarkar/contextlake" \
      org.opencontainers.image.licenses="MIT"
# git: contextlake's own mirror/sync commands shell out to it at runtime.
# ca-certificates: TLS for git/HF/API calls. No build toolchain here (D-1).
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /usr/sbin/nologin contextlake
WORKDIR /work
# Not a long-lived daemon by default (CMD is a one-shot "doctor" run), but this
# still catches a broken interpreter/venv or a corrupted install (D-5): a
# non-zero exit here means `contextlake --version` itself can't run.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD contextlake --version || exit 1
USER contextlake
ENTRYPOINT ["contextlake"]
CMD ["doctor"]

FROM runtime-base AS slim
LABEL org.opencontainers.image.description="contextlake with the knowledge layer and built-in embedder; no baked wiki LLM (smaller pull -- use Ollama/OpenAI/Anthropic/cli for the wiki tier)"
COPY --from=build-slim --chown=contextlake:contextlake /opt/venv /opt/venv
COPY --from=build-slim --chown=contextlake:contextlake /opt/contextlake/models /opt/contextlake/models

FROM runtime-base AS full
LABEL org.opencontainers.image.description="contextlake with the knowledge layer and built-in CPU models baked in (offline turnkey image)"
# libgomp1: the OpenMP runtime the compiled llama.so links against. Pulled in
# transitively by build-essential in the old single-stage image; needs to be
# named explicitly now that the runtime stage no longer has a compiler at all.
USER root
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
USER contextlake
COPY --from=build-full --chown=contextlake:contextlake /opt/venv /opt/venv
COPY --from=build-full --chown=contextlake:contextlake /opt/contextlake/models /opt/contextlake/models
