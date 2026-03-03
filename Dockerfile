# contextlake — bundled knowledge-base image with built-in CPU models.
#
# Ships the [kb] + built-in model extras and BAKES IN the pinned models
# (model2vec embedder + a small Qwen2.5-0.5B GGUF wiki LLM), so `docker run` needs
# no Ollama, no API key, and no model download at runtime — handy for zero-config
# or air-gapped use. The PyPI wheel remains the primary install; this image is for
# turnkey/offline runs.
#
#   docker run -v "$PWD/repositories:/work/repositories" \
#     ghcr.io/sayak-sarkar/contextlake doctor
FROM python:3.12-slim

# build-essential + cmake: build llama-cpp-python from source (CPU). git/ca-certs:
# runtime sync + TLS.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV HF_HOME=/opt/contextlake/models \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

WORKDIR /src
COPY . /src
RUN pip install '.[kb,kb-local,llm-local]'

# Pre-download the built-in models into HF_HOME (no network needed at runtime).
RUN python docker/prefetch_models.py

WORKDIR /work
ENTRYPOINT ["contextlake"]
CMD ["doctor"]
