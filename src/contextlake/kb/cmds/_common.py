"""Cross-cutting helpers shared by multiple kb command modules."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from ... import observability, style
from ...logging_setup import log
from ..config import load_kb_config
from ..state import check_schema
from ..store.sqlite_store import SqliteStore

# Where a command invocation parks its resolved KbConfig. Underscore-prefixed so
# it can never collide with an argparse `dest` (no flag produces that name).
_CONFIG_ATTR = "_resolved_kb_config"


def kb_config(args):
    """The knowledge config for this command invocation, resolved exactly once.

    Every kb command used to load it twice — once here through :func:`_open_store`,
    once again in the command body — so every warning, every trust screen and every
    TOML parse in ``load_kb_config`` ran twice per run. The visible half of that was
    the gated-key refusal: a config with three gated keys printed six warnings for
    three refusals. Deduping the message (see ``kb.config._WARNED_UNTRUSTED``) hid
    that symptom; this removes the cause, which the unknown-key warnings — never
    deduped — still showed one for one.

    Cached on the argparse ``Namespace``, not in a module-level dict, on purpose: a
    Namespace is built fresh by ``parse_args`` for each invocation, so the cache's
    lifetime is exactly one command. A second run with a different ``--config`` in
    the same process (the test suite does this constantly, and so does anything
    embedding the CLI) arrives with its own Namespace and therefore does its own
    load. A process-wide cache would have to enumerate everything that can change
    the answer — cwd, the ancestor-config walk, ``CONTEXTLAKE_NO_LOCAL_CONFIG``,
    the files' own contents — and would hand back the first caller's config
    whenever that enumeration was incomplete.
    """
    cached = getattr(args, _CONFIG_ATTR, None)
    if cached is not None:
        return cached
    cfg = load_kb_config(getattr(args, "config", None))
    try:
        setattr(args, _CONFIG_ATTR, cfg)
    except (AttributeError, TypeError):
        # A caller passing something more locked-down than a Namespace still gets
        # a correct config; it just pays for the second load, as it does today.
        pass
    return cfg


def register_store_for_observability(store, db_path) -> None:
    """Tell `--metrics-file` where the graph lives and `--redact` what to hide.

    **Call this wherever a store is opened.** ``_open_store`` used to do it inline under
    a comment claiming "every kb command funnels through here", and four constructions
    did not: ``cmds/doctor.py``, ``dashboard/server.py`` and ``dashboard/site.py`` twice.
    Measured consequence: ``kb dashboard --site --redact`` printed a raw repository id
    that ``kb lint --redact`` had just redacted -- on the artefact most likely to be
    shared. Extracting it is the fix; the comment was true of the funnel and false of
    the codebase.

    Registration covers repository ids **and module prefixes**. A redacted export still
    emitted ``repo-<hash>::<real-subsystem-dir>``, because only ``list_repos()`` was
    registered and module names arrive from a different query -- half a redaction reads
    exactly like a whole one.
    """
    observability.note_store_path(db_path)
    names: list[str] = []
    failed: list[str] = []
    try:
        names.extend(r.id for r in store.list_repos())
    except Exception as e:  # noqa: BLE001 - see below; never fail a command over redaction
        failed.append(f"repository ids ({type(e).__name__})")
    try:
        from ..visualize.payload import repo_modules

        for r in list(store.list_repos()):
            # `repo_modules` returns {"prefix": "src/foo", "nodes": N}; the prefix is
            # the real directory name that leaked. Each path segment is registered too,
            # since a subsystem name appears bare as often as it appears in a path.
            for m in repo_modules(store, r.id) or []:
                prefix = str(m.get("prefix") or "")
                if not prefix:
                    continue
                names.append(prefix)
                names.extend(seg for seg in prefix.split("/") if len(seg) > 2)
    except Exception as e:  # noqa: BLE001
        failed.append(f"module names ({type(e).__name__})")
    if names:
        observability.add_repo_names(names)
    if failed and observability.redaction_configured():
        # Previously `except Exception: pass`. Redaction that quietly covers less than
        # it was asked to is worse than redaction that refuses: the operator reads a
        # clean-looking log and shares it. Best-effort stays best-effort -- the command
        # still runs -- but the shortfall is stated.
        log(style.warn(
            f"--redact could not enumerate {', '.join(failed)} from this store, so "
            f"those names are NOT hidden in this run's output."))


def _open_store(args) -> tuple[SqliteStore, Path]:
    cfg = kb_config(args)
    store_dir = cfg.store_path
    db_path = store_dir / "index.sqlite"
    store = SqliteStore(db_path)
    check_schema(store)
    register_store_for_observability(store, db_path)
    return store, store_dir


def _guard_store(store_dir, command: str) -> bool:
    """Take the store's single-writer lock. Returns False (with a clear message)
    if a live peer already holds it — so two writers never interleave on one store.
    Released automatically at process exit; a crashed holder's lock is reclaimed."""
    import atexit

    from ..lock import OVERRIDE_ENV, StoreBusy, StoreLock

    lock = StoreLock(store_dir, command)
    try:
        lock.acquire()
    except StoreBusy as e:
        h = e.holder
        age = max(0, int(time.time()) - int(h.get("started", 0)))
        log(style.warn("Another contextlake process is writing this store — refusing to "
                       "run concurrently (avoids interleaved, corrupting writes)."))
        log(f"  holder: pid {h.get('pid')} · {h.get('command')} · ~{age}s ago · {h.get('host')}")
        log(f"  store:  {store_dir}")
        log(f"  Wait for it to finish, then re-run. To override (rarely correct): "
            f"set {OVERRIDE_ENV}=1.")
        return False
    atexit.register(lock.release)
    return True


def _connect_targets(args, store) -> list[tuple[str, str]]:
    """Repos to enrich: --workspace tree, a single --source dir, else indexed repos —
    scoped to positional repo id(s) when given (``wiki <repo>`` does just that repo)."""
    workspace = getattr(args, "workspace", None)
    if workspace:
        from ..parse import discover_repos  # lazy

        return discover_repos(str(workspace))
    source = getattr(args, "source", None)
    if source and Path(source).is_dir():
        repo_id = getattr(args, "repo", None) or Path(source).name
        return [(repo_id, str(Path(source).resolve()))]
    targets = [(r.id, r.path) for r in store.list_repos() if r.path]
    wanted = {a for a in (getattr(args, "args", None) or []) if a}
    if wanted:
        targets = [t for t in targets if t[0] in wanted]
    return targets


def _repo_id_suggestions(store, target: str, n: int = 3) -> list[str]:
    """Stored repo ids closest to an unknown ``target``.

    Covers typos (fuzzy match) and a partial/suffix id: repo_id is now canonical
    (host/namespace/path, see repo_identity.py), so a user typing just the
    namespace/path tail -- ``team/billing/api`` -- should still point at the
    stored ``gitlab.example.com/acme/team/billing/api``.
    """
    import difflib

    ids = [r.id for r in store.list_repos()]
    tail = [i for i in ids if i == target or i.endswith("/" + target)]
    close = difflib.get_close_matches(target, ids, n=n, cutoff=0.5)
    out: list[str] = []
    for i in tail + close:
        if i not in out:
            out.append(i)
    return out[:n]


def _unknown_repo_msg(store, target: str) -> str:
    sugg = _repo_id_suggestions(store, target)
    if sugg:
        return f"Unknown repo {target!r}. Did you mean: {', '.join(sugg)}?"
    return f"Unknown repo {target!r}: index it first, or pass a path on disk"


def _watch_loop(run_once, *, interval: float = 60, iterations=None, sleep=time.sleep) -> int:
    """Run ``run_once`` every ``interval`` seconds until interrupted or ``iterations``
    is reached. Returns the number of runs; a KeyboardInterrupt stops gracefully."""
    runs = 0
    while iterations is None or runs < iterations:
        run_once()
        runs += 1
        if iterations is not None and runs >= iterations:
            break
        try:
            sleep(interval)
        except KeyboardInterrupt:
            break
    return runs


def _git_head(path: Path) -> str | None:
    """HEAD of the repository rooted AT ``path``, or None when there isn't one.

    ``git -C <dir> rev-parse HEAD`` walks *up* the filesystem to the nearest real
    repository, so a plain directory that happens to sit anywhere inside another
    working tree reported THAT tree's commit and was stored under it. Nothing
    downstream can recover from that: needs_reindex, `kb lint` staleness and
    graph_health all compare against a commit with no relation to the indexed
    content, so the directory is called current or stale according to an unrelated
    project's history.

    The product already detects this exactly, in repo_identity, and already words
    the warning well -- it was simply never wired in here, so the bad row was
    written first and only noticed on a later run's id-migration pass.
    """
    from ..repo_identity import describe_gitdir_mismatch, is_own_gitdir

    if not is_own_gitdir(str(path)):
        # Distinguish "no git anywhere near this" (ordinary, silent) from "git
        # answered, about somebody else" (worth saying out loud).
        why = describe_gitdir_mismatch(str(path))
        if "DIFFERENT" in why:
            log(style.warn(f"{path}: {why}. Indexing it with no commit recorded, "
                           "so staleness checks will not compare it against an "
                           "unrelated repository's history."))
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True, text=True, errors="replace", timeout=10,
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _git_commit_state(path: Path | None) -> str:
    """Why :func:`_git_head` could not name a commit: ``"missing"``, ``"shard"``,
    ``"unreadable"``, ``"empty"``, or ``"ok"`` when it can.

    ``_git_head`` collapses four genuinely different situations into one ``None``,
    and a caller that reports all four the same way ends up telling the user
    something that cannot be true. ``kb lint`` did exactly that: it called a
    repository with no commits at all "stale, re-run index", on every run,
    forever, when re-running index is the one thing that cannot help.

    ``rev-list -n 1 --all`` is the emptiness probe: an initialised repository with
    no objects answers with an empty string and exit 0, which no other state
    produces. It is gated on :func:`is_own_gitdir` for the reason ``_git_head``
    documents -- git walks up past a broken ``.git`` and would otherwise answer
    about an ancestor repository's history.
    """
    from ..repo_identity import is_own_gitdir

    if path is None:
        return "unreadable"
    if not path.exists():
        return "missing"
    if path.is_file():
        # `kb index --source graph.json` records the shard FILE as the repo's
        # path. There was never a checkout, so "git cannot read a repository
        # there, re-clone it" would be the same species of impossible instruction
        # this function exists to stop printing.
        return "shard"
    if not is_own_gitdir(str(path)):
        return "unreadable"
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-list", "-n", "1", "--all"],
            capture_output=True, text=True, errors="replace", timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unreadable"
    if out.returncode != 0:
        return "unreadable"
    return "ok" if out.stdout.strip() else "empty"
