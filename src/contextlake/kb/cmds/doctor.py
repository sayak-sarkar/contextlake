"""`contextlake doctor` -- environment/store health checks."""

from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
from pathlib import Path

from ... import style
from ...logging_setup import report_line
from ..config import load_kb_config
from ..state import check_schema
from ..store.sqlite_store import SqliteStore


def _say(text: str = "") -> None:
    """Emit one report line: to the console, and to ``--log-file``.

    doctor renders its report itself rather than through the logger because the
    console formatter appends a right-edge clock to every single-line record,
    which suits a progress stream and ruins an aligned report. Both costs of
    that choice are paid by :func:`report_line`: redaction (``--redact doctor``
    printed the absolute store path in full) and the audit file (doctor wrote
    zero lines to ``--log-file``), on the one command whose entire output is
    what a person pastes into a bug report.
    """
    report_line(text)


#: Faults printed since the last `_start_report()`. A list rather than a flag so the summary
#: can say WHICH check failed, and module-level so `_check` records its own verdict.
_FAULTS: list[str] = []


def _start_report() -> None:
    _FAULTS.clear()


class _Untested:
    """The "no answer was obtained" value for :func:`_check`.

    True, False and None all report the ANSWER a check got. This reports that no
    check ran, which is a different fact: a source of a type nothing can dial is
    not a source that failed. An object rather than the string "untested" so the
    identity test does not rest on literal interning.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNTESTED"


UNTESTED = _Untested()


def _check(label: str, ok, detail: str = "") -> bool:
    """Print one line and RECORD its verdict. Four states: True -> ✓, False -> ✗ (a real
    fault), None -> ⚠ (advisory, present-but-degraded or optional-unavailable), and
    UNTESTED -> ⊘ (nothing was dialled, so the line makes no claim either way).

    The recording is the point. The summary used to be built from a hand-maintained
    `ok &= _check(...)` at three of twenty-odd call sites, so a genuine ✗ -- a repository
    indexed by an older parser, say -- printed in red and then the bottom line said "OK" in
    green and the command exited 0. Two contradictory statements on one screen, and the
    machine-readable one was the wrong one. A check that prints a fault now counts as one by
    construction, so a new check cannot be added and forgotten; a check that is genuinely
    advisory says so by passing None, which is a decision at the call site rather than an
    omission somewhere else.
    """
    if ok is UNTESTED:
        mark, fault = style.dim("⊘"), False
    else:
        mark = style.yellow("⚠") if ok is None else (style.green("✓") if ok else style.red("✗"))
        fault = ok is not None and not ok
    _say(f"  {mark} {label}" + (f" {style.dim('— ' + detail)}" if detail else ""))
    if fault:
        _FAULTS.append(label.strip())
    # `ok is True`, not `bool(ok)`: bool(UNTESTED) is True, so an untested check
    # would report itself to a caller as a pass. Every call site is bare today,
    # so nothing would catch that; a unit test pins it instead. Do not simplify.
    return ok is True


def _local_llm_runtime_present() -> bool:
    """Is the local (openvino-genai) LLM runtime importable here?

    Import-free: the runtime is an optional extra, so the answer is often "no" and
    importing to find out would be the expensive way to learn it."""
    return importlib.util.find_spec("openvino_genai") is not None


def _refused_tier_detail(table: str) -> str:
    """Why a tier is off when workspace trust refused it, and what clears it.

    The sentence the two call sites print when ``KbConfig.refused_tiers`` names
    their table. That field exists so a surface can tell a refused tier from one
    the user switched off; both are the same ``enabled = False`` in the merged
    config, and doctor printed the same line for both: "not enabled in config (set
    [llm] enabled = true, or pass --llm PROVIDER)". That advice does nothing here.
    The key sits in the file whose provider was refused, so setting it there
    re-fires the refusal on the next load.

    The remedies named are the two that were run against ``load_kb_config``: move
    the block out of the discovered file into the global config, or pass
    ``--config`` naming that file. Adding the block to the global config while the
    discovered file keeps its own ``provider`` line does NOT clear it -- the merge
    is last-wins, so that line still wins and the tier goes off again.

    ``GLOBAL_CONFIG`` is read from the module at call time, not bound at import,
    so the file this names is the one ``load_kb_config`` would actually treat as
    privileged. Same reason ``trust.is_privileged_source`` reads it late.
    """
    from ..config import GLOBAL_CONFIG

    return (f"refused: a config file found by walking up from the current directory "
            f"chose the provider for this tier. Move the [{table}] block out of that "
            f"file and into {GLOBAL_CONFIG}, or pass --config PATH to name that file. "
            "See SECURITY.md, 'Workspace trust'")


def _builtin_model_present(cache_dir, model_id: str) -> bool:
    """True if a HuggingFace-cached model dir exists under ``cache_dir/hub``.

    Filesystem-only — never imports the heavy dep or triggers a download."""
    hub = Path(cache_dir).expanduser() / "hub"
    return (hub / ("models--" + model_id.replace("/", "--"))).is_dir()


def cmd_doctor(args) -> int:
    _say("contextlake knowledge layer — doctor")
    _start_report()
    cfg = None

    fts = False
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        fts = True
    except sqlite3.Error:
        fts = False
    _check("SQLite FTS5 available", fts, "" if fts else "search falls back to slower scans")

    _check("git on PATH", shutil.which("git") is not None)
    # Tri-state, not a bool, because the comment that used to sit here said "advisory, not
    # critical" while the mark drawn was a red ✗. Now that a printed ✗ counts towards the
    # verdict, a comment is not where that decision can live: a stock install with no `glab`
    # would fail `doctor`, and CI installs none.
    _check("glab on PATH (for syncing)", True if shutil.which("glab") else None,
           "" if shutil.which("glab") else "only needed for `mirror` against a forge")

    try:
        cfg = load_kb_config(getattr(args, "config", None))
        if cfg.loaded_from:
            _check(
                "config loads",
                True,
                f"{len(cfg.loaded_from)} file(s), "
                f"{len(cfg.sources)} source(s), {len(cfg.rules)} rule(s)",
            )
            for path in cfg.loaded_from:
                _say(f"      {style.dim(path)}")
        else:
            # A green tick here used to be printed whether or not a config existed,
            # so "I have no config at all" and "my config loaded fine" looked
            # identical -- the exact confident-but-wrong reporting this tool exists
            # to prevent. Defaults are legitimate, so this is a warning, not a
            # failure, and it does not change doctor's exit code. The mirror side
            # already lists what it searched; match it.
            _check("config loads", None, "no config found, using built-in defaults")
            for path in cfg.searched:
                _say(f"      {style.dim('[absent] ' + path)}")
            _say(f"      {style.dim('run ' + style.bold('contextlake init') + ' to create one')}")
        # Lazy: source_cmd -> config_edit -> tomlkit, kept off every other kb
        # command's import path (see config_edit's module docstring).
        from ..source_cmd import SURVEY_FAILED, SURVEY_OK, survey_source
        # Per-source reachability, dispatched through survey_source(), which wraps
        # the same verify_source() `source test` uses (DRY) and adds the two states
        # a bool cannot carry. The wizard's survey reads the same function, so
        # "is this source reachable" has one derivation and not three.
        #
        # Advisory only -- no source state fails doctor's overall verdict, since
        # that reflects live external connectivity, not the local environment.
        # Bounded to 8s per source so an unreachable connector (atlassian/mcp
        # default to 120s/60s) can't stall doctor for minutes.
        #
        # `None`, not `False`, for a probed source that did not answer: this block
        # says unreachable never fails the verdict, so it must say that in the mark
        # rather than relying on a caller to leave it out of a sum. A red ✗ that is
        # documented not to matter is the same contradiction from the other side.
        # UNTESTED (⊘) for the other two, and they are a different fact from ⚠: a
        # type with no probe and a source switched off were both dialled and drawn
        # as failures before, so a broken source and a source nothing can dial read
        # the same. That is an absent check rendering as an answer.
        marks = {SURVEY_OK: True, SURVEY_FAILED: None}
        for src in cfg.sources:
            state, detail = survey_source(src, timeout=8)
            _check(f"  {src.name} ({src.type})", marks.get(state, UNTESTED), detail)
        store_dir = cfg.store_path
        store_dir.mkdir(parents=True, exist_ok=True)
        store = SqliteStore(store_dir / "index.sqlite")
        # `_open_store` is not the only door. A store opened without this
        # leaks repository ids under --redact and loses its graph gauges
        # under --metrics-file -- measured on `dashboard --site --redact`.
        from ._common import register_store_for_observability

        register_store_for_observability(store, store_dir / "index.sqlite")
        try:
            check_schema(store)
            st = store.stats()
            _check("store reachable", True,
                   f"{store_dir} · {st.repos} repos, {st.nodes} nodes, {st.edges} edges")

            from ..parse import PARSER_VERSION
            from ..store.shards import read_shard

            # Language-agnostic on purpose. This was once scoped to shards
            # containing C/C++ nodes, back when the only parser change was C++
            # method/class linkage -- which meant a Python or TypeScript repo
            # indexed by an older parser was never flagged at all. PARSER_VERSION
            # has since moved for reasons that change output for every language
            # (capture ordering, and so shard reproducibility), so the language a
            # repo happens to be written in cannot be the gate. It stays a useful
            # *detail* for a shard that does hold C/C++ nodes, so it is reported
            # as one.
            stale, cxx = [], []
            for r in store.list_repos():
                sh = read_shard(store_dir, r.id)
                if sh is None or sh.parser_version == PARSER_VERSION:
                    continue
                stale.append(r.id)
                if any(n.lang in ("c", "cpp") for n in sh.nodes):
                    cxx.append(r.id)
            if stale:
                detail = (f"{len(stale)} repo(s) indexed with an older parser -- re-index "
                          f"with `contextlake kb index`, which now rebuilds them instead "
                          f"of reporting them unchanged: {', '.join(stale[:5])}"
                          + ("…" if len(stale) > 5 else ""))
                if cxx:
                    detail += (f" · {len(cxx)} of them hold C/C++ code, so they also gain "
                               "corrected method/class linkage")
                # ⚠ and not ✗, which resolves a contradiction rather than choosing a side
                # of it. A parser bump makes every existing shard stale, and turning that
                # into a red verdict would fail every user's CI on upgrade -- a decision
                # already recorded here and in `kb lint`. What was wrong was drawing a red
                # ✗ for it and then printing "OK" underneath: two statements about one
                # fact. The advisory mark says the same thing without contradicting the
                # summary, and `kb index` still rebuilds them.
                _check("shards up to date with the current parser", None, detail)
            else:
                _check("shards up to date with the current parser", True)
        finally:
            store.close()

        emb = cfg.embeddings
        if not emb.enabled:
            # ⚠ rather than ✓ for the refused case. An off-by-choice optional tier is
            # not a fault, but a tier the config asked for and contextlake refused is
            # something the operator has to act on, and it is not a ✗ either: the
            # refusal is the tool working. Advisory leaves the exit code alone.
            if "embeddings" in cfg.refused_tiers:
                _check("embeddings", None, _refused_tier_detail("embeddings"))
            else:
                _check("embeddings", True, "disabled")
        else:
            vec_path = store_dir / "embeddings.sqlite"
            count, backend = 0, emb.vector_backend
            if vec_path.exists():
                from ..embeddings.store import build_vector_store

                vs = build_vector_store(vec_path, backend=emb.vector_backend)
                try:
                    count, backend = vs.count(), vs.name
                finally:
                    vs.close()
            detail = f"{emb.provider} · {backend} · {count} vector(s)"
            _check("embeddings", True,
                   detail if vec_path.exists() else f"{detail} (run embed to build)")
            if emb.provider in ("builtin", "auto"):
                from ..embeddings.builtin import BuiltinEmbedder

                be = BuiltinEmbedder(engine=getattr(emb, "engine", "model2vec"),
                                     model=getattr(emb, "model", None),
                                     cache_dir=getattr(emb, "cache_dir", None))
                present = _builtin_model_present(be.cache_dir, be.model_id)
                _check("  built-in embedder model", True,
                       f"{be.model_id} · "
                       f"{'downloaded' if present else 'not downloaded (run embed to fetch)'}"
                       f" · {be.cache_dir}")

            # ANN capability: can sqlite-vec actually load here? (else semantic
            # search silently uses brute-force cosine — fine small, slow at scale)
            from ..embeddings.store import build_vector_store
            try:
                _vs = build_vector_store(":memory:", backend="sqlite-vec")
                _vs.close()
                ann = True
            except Exception:  # noqa: BLE001 - any import/load failure means no ANN
                ann = False
            _check("  ANN index (sqlite-vec)", True if ann else None,
                   "available — native KNN" if ann else
                   "not loadable — brute-force cosine (ok at small scale; install sqlite-vec "
                   "+ a sqlite3 that allows extensions for ANN at scale)")

        llm = cfg.llm
        if not llm.enabled:
            # "disabled" alone conflated three different situations for anyone asking
            # why wiki generation is unavailable: a tier switched off in config, a
            # tier whose local runtime was never installed, and a tier workspace
            # trust refused. They have different fixes, so they get different
            # sentences. The first two stay ✓ -- an off-by-default optional tier is
            # not a fault -- and the refused one is ⚠, because the operator's config
            # asked for it and did not get it.
            if "llm" in cfg.refused_tiers:
                _check("wiki LLM", None, _refused_tier_detail("llm"))
            else:
                detail = "not enabled in config (set [llm] enabled = true, or pass --llm PROVIDER)"
                if llm.provider in ("builtin", "auto") and not _local_llm_runtime_present():
                    detail += ("; the local runtime (openvino-genai) is not installed either: "
                               "contextlake doctor --fix llm-local")
                _check("wiki LLM", True, detail)
        elif llm.provider in ("builtin", "auto"):
            from ..llm.builtin import BuiltinLlm

            kw = {}
            if getattr(llm, "model", None):
                kw["repo_id"] = llm.model
            if getattr(llm, "cache_dir", None):
                kw["cache_dir"] = llm.cache_dir
            bl = BuiltinLlm(**kw)
            present = _builtin_model_present(bl.cache_dir, bl.repo_id)
            # the model alone is not enough: wiki needs the openvino-genai runtime, which is
            # an optional extra. Report ⚠ when the runtime is absent so doctor
            # doesn't show a green ✓ for a tier that will fail at wiki time.
            runtime = _local_llm_runtime_present()
            model_state = "downloaded" if present else "not downloaded (run wiki to fetch)"
            _check("wiki LLM", True if runtime else None,
                   f"{llm.provider} · {bl.repo_id} · {model_state}" if runtime
                   else f"{llm.provider} · {bl.repo_id} · {model_state} · runtime not installed "
                        "(contextlake doctor --fix llm-local)")
        elif llm.provider == "anthropic":
            from ..llm.base import default_api_key_env

            # api_key_env is None unless the user set it explicitly (resolved at
            # read time, not at LlmCfg construction — see llm.base.default_api_key_env).
            env = getattr(llm, "api_key_env", None) or default_api_key_env("anthropic")
            key = os.environ.get(env)
            # wiki is an optional tier: report ⚠ (not a hard ✗ that the OK/exit summary
            # ignores) when the key is missing, matching the builtin-runtime branch above.
            _check("wiki LLM", True if key else None,
                   f"anthropic · {llm.model or 'claude-opus-4-8'} · "
                   + (f"{env} set" if key else f"set {env}"))
        elif llm.provider == "cli":
            cmd = getattr(llm, "command", None) or "claude"
            found = shutil.which(cmd)
            _check("wiki LLM", True if found else None,
                   f"cli · {cmd} · " + (f"found at {found}" if found else "not on PATH"))
        else:
            _check("wiki LLM", True, f"{llm.provider} · {llm.model or 'default model'}")
    except Exception as e:  # noqa: BLE001 - doctor reports, never crashes
        _check("config + store", False, str(e))

    ok = not _FAULTS
    # "Problems found: shards up to date with the current parser" reads as a claim that the
    # shards ARE up to date, because a check label is phrased as the desired state. Naming
    # them as the checks that failed keeps the labels usable in the summary.
    _say(style.bold(style.green("OK")) if ok
         else style.bold(style.red(
             f"Problems found — {len(_FAULTS)} check(s) failed: {', '.join(_FAULTS)}")))

    # --fix is strictly additive: the report above and the verdict line are
    # untouched, so a plain `doctor` (the diagnostic everything else points at)
    # behaves and exits exactly as it always has.
    fix = getattr(args, "fix", None)
    if not fix:
        return 0 if ok else 1

    fixed = False
    if cfg is None:
        _say()
        _say(style.red("cannot plan fixes: the configuration above did not load"))
    else:
        # Lazy, like source_cmd above: plain doctor never pays for the
        # remediation module's imports.
        from .doctor_fix import run_fix

        fixed = run_fix(cfg, fix,
                        dry_run=bool(getattr(args, "dry_run", False)),
                        skip_interactive=bool(getattr(args, "skip_interactive", False)))
    return 0 if (ok and fixed) else 1
