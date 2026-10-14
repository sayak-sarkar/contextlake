"""Pick the built-in wiki model on the first `kb wiki` run that has none set.

Owner decision 5: the user picks at run time from the `--model-check` table. With no one to
ask (no terminal: a script, CI, a scheduled run), the largest model that fits is used, and
its name and download size are printed before anything is downloaded.

The pick is SAVED either way, as `[llm] builtin_model`, so the next run asks nothing and an
unattended run never picks a different model when the free RAM moves. It is saved to the
`--config` file when one was passed, else to the global config: never to a file found by
walking up folders, which may be shared or not the user's own.
"""

from __future__ import annotations

import sys

from .catalogue import DEFAULT, MODELS, fits, largest_fitting


def interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def pick_on_a_terminal(best, machine, disk, log) -> object:
    default = MODELS.index(best) + 1 if best else 1
    while True:
        reply = _ask(f"Pick a wiki model [1-{len(MODELS)}, Enter for {default}]: ")
        if not reply:
            return MODELS[default - 1]
        if reply.isdigit() and 1 <= int(reply) <= len(MODELS):
            model = MODELS[int(reply) - 1]
            if fits(model, machine, on_disk=disk.get(model.repo_id, False)):
                return model
            if _ask(f"{model.label} does not fit this machine; use it anyway? [y/N]: ")[:1] \
                    .lower() == "y":
                return model
            continue
        log(f"Type a number from 1 to {len(MODELS)}.")


def choose_wiki_model(store, store_dir, cfg, *, config_path, ask: bool, log, repo_ids=None):
    """The catalogue model this run uses. Saves the choice; never raises for a failed save."""
    from ... import netguard
    from . import model_check
    from .machine import read_machine

    cache_dir = getattr(cfg.llm, "cache_dir", None) or "~/.contextlake/models"
    machine = read_machine(cache_dir)
    disk = model_check.on_disk(cache_dir)
    if netguard.offline():
        # Nothing can be downloaded, so only a copy on disk is a candidate.
        best = next((m for m in reversed(MODELS)
                     if disk.get(m.repo_id) and fits(m, machine, on_disk=True)), None)
    else:
        best = largest_fitting(machine, disk)
    if ask:
        log("No wiki model is set. This machine can run these:")
        pages = model_check.planned_pages(store, store_dir, repo_ids)
        for line in model_check.render(machine, disk, pages,
                                       int(getattr(cfg.llm, "council_size", 3) or 3)):
            log(line)
        chosen = pick_on_a_terminal(best, machine, disk, log)
    else:
        chosen = best or DEFAULT
        size = "already on disk" if disk.get(chosen.repo_id) else \
            f"a {chosen.download_mb / 1000:.1f} GB download"
        reason = "the largest that fits this machine" if best else \
            "the default; no model fits this machine's free RAM"
        log(f"No wiki model is set. Using {chosen.label} ({size}), {reason}. "
            "See them all with `kb wiki --model-check`.")
    _save(chosen, config_path, ask=ask, log=log)
    return chosen


def _save(model, config_path, *, ask: bool, log) -> None:
    from ...config import expand_path
    from ..config import GLOBAL_CONFIG

    target = expand_path(config_path or GLOBAL_CONFIG)
    if ask and _ask(f"Save {model.label} as your wiki model in {target}? [Y/n]: ")[:1] \
            .lower() == "n":
        return
    try:
        from ..config_edit import set_builtin_model

        set_builtin_model(target, model.repo_id)
    except (ImportError, OSError, ValueError) as e:
        log(f"Could not save the wiki model to {target} ({e}); the next run will ask again.")
        return
    log(f"Saved: [llm] builtin_model = \"{model.repo_id}\" in {target}.")
