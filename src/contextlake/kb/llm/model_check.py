"""`kb wiki --model-check`: which built-in wiki model this machine can run, and the cost.

Read-only: it reads the machine, the model cache and the store, and runs no model. The
table is the one the choice at `kb wiki` and `init` will show (owner decision 5).
"""

from __future__ import annotations

import os
from pathlib import Path

from .catalogue import MB, MODELS, REFERENCE_MACHINE, fits, largest_fitting

# Measured on one machine and one repository, so a range rather than one number.
_LOW, _HIGH = 0.7, 1.5


def on_disk(cache_dir: str | os.PathLike) -> dict[str, bool]:
    """Which catalogue models already have a copy on disk. All False when the model
    extra (huggingface_hub) is not installed: nothing can have been downloaded."""
    os.environ.setdefault("HF_HOME", str(Path(cache_dir).expanduser()))
    try:
        from ..model_cache import local_snapshot

        return {m.repo_id: local_snapshot(m.repo_id, m.revision) is not None for m in MODELS}
    except ImportError:
        return {m.repo_id: False for m in MODELS}


def planned_pages(store, store_dir, repo_ids=None) -> tuple[int, int]:
    """``(repo pages, module pages)`` a `kb wiki` run would generate, over ``repo_ids``
    when the run names some (``kb wiki <repo>``), else over every indexed repository.

    Repo pages: the ones whose commit moved or that have no page
    (``wiki_generate_estimate``, a lower bound). Module pages: an upper bound, the
    qualifying modules of each repository, at most the per-run cap each.
    """
    from ..cmds.wiki import _MAX_MODULE_PAGES_PER_REPO, _module_page_plan
    from ..dashboard.mutations import wiki_generate_estimate

    repos = [r for r in store.list_repos() if not repo_ids or r.id in repo_ids]
    repo_pages = sum(wiki_generate_estimate(store, store_dir, repo_id=r.id)["would_regenerate"]
                     for r in repos)
    module_pages = 0
    for repo in repos:
        nodes, _edges = store.repo_counts(repo.id)
        modules, _prune = _module_page_plan(store, repo.id, nodes)
        module_pages += min(len(modules), _MAX_MODULE_PAGES_PER_REPO)
    return repo_pages, module_pages


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{round(seconds)} s"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


def _span(seconds: float) -> str:
    return f"{_duration(seconds * _LOW)} to {_duration(seconds * _HIGH)}"


def _gb(n_bytes: float | None) -> str:
    return "unknown" if n_bytes is None else f"{n_bytes / 1e9:.1f} GB"


def render(machine, disk: dict[str, bool], pages: tuple[int, int] | None,
           council_size: int) -> list[str]:
    """The table, as lines. ``pages`` is ``None`` where no store exists yet (setup)."""
    calls = 1 + council_size
    lines = [f"Machine: {machine.cpus} CPUs, {_gb(machine.ram_available)} RAM free, "
             f"{_gb(machine.disk_free)} disk free where models are stored"]
    total = None
    if pages is not None:
        repo_pages, module_pages = pages
        total = repo_pages + module_pages
        lines.append(f"Wiki run: {repo_pages} repo page(s) and up to {module_pages} module "
                     f"page(s), {calls} model calls per page (1 draft, {council_size} "
                     "reviews)")
    lines.append(f"Times were measured on {REFERENCE_MACHINE}. Yours will differ.")
    lines.append("")
    header = ["#", "Model", "Download", "RAM need", "Fits", "Per page"]
    if total is not None:
        header.append("Whole run")
    rows = [header]
    best = largest_fitting(machine, disk)
    for i, m in enumerate(MODELS, start=1):
        cached = disk.get(m.repo_id, False)
        per_page = m.page_seconds(council_size)
        mark = "" if m.measured else "*"
        row = [str(i), m.label + (" (largest that fits)" if m is best else ""),
               "on disk" if cached else f"{m.download_mb / 1000:.1f} GB",
               f"{m.ram_mb * MB / 1e9:.1f} GB", "yes" if fits(m, machine, on_disk=cached) else "no",
               _span(per_page) + mark]
        if total is not None:
            row.append(_span(per_page * total) + mark if total else "-")
        rows.append(row)
    widths = [max(len(r[c]) for r in rows) for c in range(len(header))]
    lines += ["  " + "  ".join(cell.ljust(w) for cell, w in zip(r, widths, strict=True)).rstrip()
              for r in rows]
    if not all(m.measured for m in MODELS):
        lines.append("  * extrapolated from the measured models, not run")
    return lines


def print_model_check(store, store_dir, cfg, repo_ids=None) -> int:
    from ...logging_setup import log
    from .machine import read_machine

    cache_dir = getattr(cfg.llm, "cache_dir", None) or "~/.contextlake/models"
    machine = read_machine(cache_dir)
    pages = planned_pages(store, store_dir, repo_ids)
    for line in render(machine, on_disk(cache_dir), pages,
                       int(getattr(cfg.llm, "council_size", 3) or 3)):
        log(line)
    return 0
