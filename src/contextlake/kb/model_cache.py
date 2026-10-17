"""Find a Hugging Face model on disk before asking the network for it.

A model on disk is never downloaded again (owner decision 5, 2026-10-09). Both loaders
went online on every load before this: ``snapshot_download(repo_id=...)`` asks the Hub
for the latest commit each time, and model2vec's ``from_pretrained`` defaults to
``force_download=True``, which does the same. An upstream commit then pulled a new copy
of a model the user already had, and a machine without network could not start a model
it held.

The lookup order, so no install downloads a model it already holds:

1. the pinned commit, when the caller has one;
2. any cached snapshot of the same repo (an older install, or a container image's baked
   copy, can hold a different commit than the pin);
3. only then the network, and never in offline mode.
"""

from __future__ import annotations

import os
from pathlib import Path


def hub_dir(cache_dir) -> Path:
    """The ``hub`` folder Hugging Face reads models from, worked out from the environment.

    Same order as ``huggingface_hub``: ``HF_HUB_CACHE``, the older ``HUGGINGFACE_HUB_CACHE``,
    then ``HF_HOME/hub``. A loader sets ``HF_HOME`` to its ``cache_dir`` only when the
    variable is unset (the Docker image sets it to the folder its models are baked into),
    so ``cache_dir/hub`` is the last choice. Reads ``os.environ`` instead of
    ``huggingface_hub.constants`` so a caller such as ``doctor`` imports nothing heavy and
    sees the environment as it is now, not as it was when that module was first imported.
    """
    hub = os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    if not hub:
        hub = os.path.join(os.environ.get("HF_HOME") or str(cache_dir), "hub")
    return Path(os.path.expandvars(os.path.expanduser(hub)))


def is_cached(cache_dir, repo_id: str) -> bool:
    """True if a folder for ``repo_id`` exists in the hub cache the loader reads. No import,
    no network. It does not check that the folder holds a complete copy."""
    return (hub_dir(cache_dir) / f"models--{repo_id.replace('/', '--')}").is_dir()


def local_snapshot(repo_id: str, revision: str | None = None) -> str | None:
    """The folder of a copy of ``repo_id`` already on disk, or ``None``. No network."""
    from huggingface_hub import constants, snapshot_download
    from huggingface_hub.utils import LocalEntryNotFoundError

    if revision:
        try:
            return snapshot_download(repo_id=repo_id, revision=revision, local_files_only=True)
        except LocalEntryNotFoundError:
            pass   # the pinned commit is not on disk
    # Any other copy of the repo: read the cache folder itself, which also finds a commit no
    # ref names. The newest snapshot wins, as model2vec does. A partial one (an interrupted
    # download) is completed by the loader's retry, `fetch_snapshot(complete=True)`.
    snapshots = Path(constants.HF_HUB_CACHE) / f"models--{repo_id.replace('/', '--')}" / "snapshots"
    found = [p for p in snapshots.iterdir() if p.is_dir() and any(p.iterdir())] \
        if snapshots.is_dir() else []
    return str(max(found, key=lambda p: p.stat().st_mtime)) if found else None


def fetch_snapshot(repo_id: str, revision: str | None = None, *,
                   complete: bool = False) -> str:
    """A folder holding ``repo_id``: the copy on disk, else one downloaded now.

    ``complete=True`` skips the disk lookup and asks the network. A loader passes it once,
    after a copy from disk failed to load: an interrupted download leaves a snapshot
    folder with some files missing, and the online call fetches only what is missing.

    Raises ``RuntimeError`` in offline mode when the network is needed, rather than
    letting the download fail inside the network guard with a socket error that names
    no model.
    """
    if not complete:
        found = local_snapshot(repo_id, revision)
        if found is not None:
            return found
    from .. import netguard

    if netguard.offline():
        raise RuntimeError(netguard.refuse(f"downloading the model {repo_id}"))
    from huggingface_hub import snapshot_download

    return snapshot_download(repo_id=repo_id, revision=revision)
