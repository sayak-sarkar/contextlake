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


def _open_store(args) -> tuple[SqliteStore, Path]:
    cfg = kb_config(args)
    store_dir = cfg.store_path
    db_path = store_dir / "index.sqlite"
    # Every kb command funnels through here, so this is the one place that knows
    # where the graph lives -- which is what `--metrics-file` needs to publish
    # its node/edge gauges at the end of the run, and what `--redact` needs to
    # keep the store's location out of a shared log.
    observability.note_store_path(db_path)
    store = SqliteStore(db_path)
    check_schema(store)
    # The mirror path registers its repo ids (core.add_repo_names) so a bare
    # "namespace/name" is hashed wherever it stands alone, with no workspace root
    # in front of it to key off. A kb command's output is nothing but those ids --
    # per-repo index lines, query hits, lint rows -- and it registered none of
    # them, which is why a redacted kb log still named every repository. The store
    # is the kb side's equivalent of the fleet list.
    try:
        observability.add_repo_names(r.id for r in store.list_repos())
    except Exception:  # noqa: BLE001 - redaction is best-effort; never fail a command over it
        pass
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


def _content_targets(args, store) -> list[str]:
    """Partition ids holding content to embed -- ids only, never paths.

    ``_connect_targets`` is named for ``connect``/``enrich``, which scrape a working
    tree, so its ``if r.path`` filter is right for them: no clone, nothing to scrape.
    ``embed`` was bolted onto the same helper and inherited that filter, which is wrong
    for it -- it discards the path (``for repo_id, _ in ...``) and works purely from the
    shard. So every partition was excluded twice over: no ``repos`` row, and no path.

    The effect was that connector, enrichment and ingested content was scoped for
    search but never embedded. ``embeddings.store._repo_scope`` expands a ``repo=``
    filter to ``@connect:<repo>``/``@enrich:<repo>`` at query time, which is correct and
    shipped -- and could never match anything, because nothing wrote vectors there.

    Returning ``list[str]`` rather than ``(id, path)`` pairs is deliberate: a partition
    has no path, and a ``(id, None)`` tuple would invite exactly the ``AttributeError``
    at a call site that this split exists to prevent.

    Sentinels (``(shared)``, ``(packages)``, ...) are excluded. They own nodes, so
    ``list_partitions`` truthfully reports them, but they are cross-repo aggregates
    rather than content anyone asked to index.
    """
    from ..model import is_sentinel_repo

    explicit = _connect_targets(args, store)
    if getattr(args, "workspace", None) or getattr(args, "source", None):
        # An explicit tree or source names exactly what to work on; do not widen it
        # to unrelated partitions that happen to sit in the same store.
        return [rid for rid, _ in explicit]

    # Union, not replacement. Every indexed repo keeps a `repos` row, so listing repos
    # reproduces the old work set exactly -- including a repo whose node rows are absent
    # (embed reads shards, not rows, so those must still be offered). `list_partitions`
    # then adds what has no row at all. Taking only the latter would have narrowed the
    # work set instead of widening it, which is the wrong direction for a fix like this.
    seen = [r.id for r in store.list_repos()]
    known = set(seen)
    ids = [r for r in seen + [p for p in store.list_partitions() if p not in known]
           if not is_sentinel_repo(r)]
    wanted = {a for a in (getattr(args, "args", None) or []) if a}
    if wanted:
        # `embed myrepo` means the repo AND the partitions hanging off it, since those
        # hold that repo's connector/ingested content -- not a different repo's.
        ids = [r for r in ids
               if r in wanted or any(r.endswith(f":{w}") for w in wanted)]
    return ids


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
