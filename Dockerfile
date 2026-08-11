# contextlake — container image with the knowledge-base extras.
#
# Two variants share this Dockerfile via build targets (default target is
# "full", i.e. what `docker build .` with no --target produces):
#
#   full (default) — ships the [kb,kb-local,llm-local] extras and BAKES IN the
#                     pinned models (model2vec embedder + a small Qwen2.5-0.5B
#                     wiki LLM), so `docker run` needs no Ollama, no API
#                     key, and no model download at runtime. Large: bundles a
#                     openvino-genai runtime + the model.
#   slim            — ships [kb,kb-local,kb-vec] only: no openvino-genai (so
#                     no C++ toolchain needed to build it), no baked model.
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
FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de AS base

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/contextlake/models \
    PATH="/opt/venv/bin:$PATH"
RUN python -m venv /opt/venv

# ---- dependency layer -------------------------------------------------------
# Copy only the packaging manifest plus the one source file setuptools needs to
# resolve the dynamic version (src/contextlake/__init__.py's __version__) —
# not the whole tree — so an ordinary source-only edit below can't invalidate
# this layer and re-resolve the whole dependency set on every build (D-3).
FROM base AS deps
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src/contextlake/__init__.py src/contextlake/__init__.py

# ---- full: bakes the wiki LLM runtime and its model -------------------------
FROM deps AS build-full
# No compiler and no custom index. The wiki LLM runtime is openvino-genai, an
# ordinary manylinux wheel on PyPI, so this is a plain install -- the
# per-accelerator wheel index and the --only-binary pin the llama.cpp backend
# needed are gone with it, along with the toolchain's build time and CVE surface.
# ca-certificates: TLS for the pip/HF downloads below.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install '.[kb,kb-local,llm-local]'
COPY . /src
RUN pip install --no-deps .
RUN python docker/prefetch_models.py

# ---- slim: no wiki-LLM runtime, no baked model -----------------------------
FROM deps AS build-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install '.[kb,kb-local,kb-vec]'
COPY . /src
RUN pip install --no-deps .
# openvino-genai isn't installed in this variant; prefetch_models.py detects
# that and skips the model download, only warming the (pure-Python) embedder.
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
# HOME must follow WORKDIR into the bind mount, or the image quietly throws away
# its own output. The knowledge store defaults to ~/.contextlake/kb, and useradd
# put this user's home at /home/contextlake -- inside the container's writable
# layer, not inside the volume. So the documented `docker run -v "$PWD:/work" ...
# kb index` would spend minutes building a store and then discard it when the
# container exited, with nothing on the host to show for it and no error. Pointing
# HOME at the mount means everything contextlake persists (store, config, caches)
# lands in the directory the user mounted. Without -v it is an empty container
# directory and the run is ephemeral, exactly as before; with a read-only mount it
# now fails loudly instead of succeeding into the void. HF_HOME is set explicitly
# above, so the baked models are unaffected by this.
ENV HOME=/work
# Not a long-lived daemon by default (CMD is a one-shot "doctor" run), but this
# still catches a broken interpreter/venv or a corrupted install (D-5): a
# non-zero exit here means `contextlake --version` itself can't run.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD contextlake --version || exit 1
# WORKDIR creates a missing /work while the build is still root, so it lands as
# root:root and uid 1000 cannot write a thing in it. That did not matter while
# HOME pointed at /home/contextlake, but now that the store, config and caches
# all resolve under /work, an unmounted `docker run <image> doctor` would fail
# with EACCES on the very first write. Hand the directory to the runtime user.
# Only affects the unmounted case: a bind mount replaces this directory (and
# carries the host's ownership) at run time.
RUN install -d -o contextlake -g contextlake /work
USER contextlake
ENTRYPOINT ["contextlake"]
CMD ["doctor"]

FROM runtime-base AS slim
LABEL org.opencontainers.image.description="contextlake with the knowledge layer and built-in embedder; no baked wiki LLM (smaller pull -- use Ollama/OpenAI/Anthropic/cli for the wiki tier)"
COPY --from=build-slim --chown=contextlake:contextlake /opt/venv /opt/venv
COPY --from=build-slim --chown=contextlake:contextlake /opt/contextlake/models /opt/contextlake/models

FROM runtime-base AS full
LABEL org.opencontainers.image.description="contextlake with the knowledge layer and built-in CPU models baked in (offline turnkey image)"
# libgomp1: the OpenMP runtime OpenVINO's native libraries link against. Named
# explicitly because the runtime stage carries no compiler and no build-essential
# to pull it in transitively.
USER root
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
USER contextlake
COPY --from=build-full --chown=contextlake:contextlake /opt/venv /opt/venv
COPY --from=build-full --chown=contextlake:contextlake /opt/contextlake/models /opt/contextlake/models
