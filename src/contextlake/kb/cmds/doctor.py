"""`contextlake doctor` -- environment/store health checks."""

from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
from pathlib import Path

from ... import observability, style
from ...logging_setup import console_redacting
from ..config import load_kb_config
from ..state import check_schema
from ..store.sqlite_store import SqliteStore


def _say(text: str = "") -> None:
    """Print one report line, scrubbed when --redact asked for it.

    doctor writes its report with print rather than through the logger because
    the console formatter appends a right-edge clock to every single-line record,
    which suits a progress stream and ruins an aligned report. The cost was that
    doctor sat outside redaction altogether: `--redact doctor` printed the
    absolute store path and every config path in full, on the one command whose
    entire output is what a person pastes into a bug report.
    """
    print(observability.redact(text) if console_redacting() else text)


def _check(label: str, ok, detail: str = "") -> bool:
    # tri-state: True -> ✓, False -> ✗, None -> ⚠ (present-but-degraded / optional-unavailable)
    mark = style.yellow("⚠") if ok is None else (style.green("✓") if ok else style.red("✗"))
    _say(f"  {mark} {label}" + (f" {style.dim('— ' + detail)}" if detail else ""))
    return bool(ok)


def _local_llm_runtime_present() -> bool:
    """Is the local (llama-cpp-python) LLM runtime importable here?

    Import-free: some Pythons have no prebuilt wheel, so the answer is often "no"
    and importing to find out would be the expensive way to learn it."""
    return importlib.util.find_spec("llama_cpp") is not None


def _builtin_model_present(cache_dir, model_id: str) -> bool:
    """True if a HuggingFace-cached model dir exists under ``cache_dir/hub``.

    Filesystem-only — never imports the heavy dep or triggers a download."""
    hub = Path(cache_dir).expanduser() / "hub"
    return (hub / ("models--" + model_id.replace("/", "--"))).is_dir()


def cmd_doctor(args) -> int:
    _say("contextlake knowledge layer — doctor")
    ok = True
    cfg = None

    fts = False
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        fts = True
    except sqlite3.Error:
        fts = False
    ok &= _check("SQLite FTS5 available", fts, "" if fts else "search falls back to slower scans")

    ok &= _check("git on PATH", shutil.which("git") is not None)
    _check("glab on PATH (for syncing)", shutil.which("glab") is not None)  # advisory, not critical

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
        from ..source_cmd import verify_source
        # Per-source reachability, dispatched through the same verify_source()
        # `source test` uses (DRY). Advisory only -- a source being unreachable
        # (or of a type with no reachability check) never fails doctor's overall
        # verdict, since that reflects live external connectivity, not the local
        # environment. Bounded to 8s per source so an unreachable connector
        # (atlassian/mcp default to 120s/60s) can't stall doctor for minutes.
        for src in cfg.sources:
            reachable, detail = verify_source(src, timeout=8)
            _check(f"  {src.name} ({src.type})", reachable, detail)
        store_dir = cfg.store_path
        store_dir.mkdir(parents=True, exist_ok=True)
        store = SqliteStore(store_dir / "index.sqlite")
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
                _check("shards up to date with the current parser", False, detail)
            else:
                _check("shards up to date with the current parser", True)
        finally:
            store.close()

        emb = cfg.embeddings
        if not emb.enabled:
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
            _check("  ANN index (sqlite-vec)", ann,
                   "available — native KNN" if ann else
                   "not loadable — brute-force cosine (ok at small scale; install sqlite-vec "
                   "+ a sqlite3 that allows extensions for ANN at scale)")

        llm = cfg.llm
        if not llm.enabled:
            # "disabled" alone conflated two different situations for anyone asking
            # why wiki generation is unavailable: a tier switched off in config, and
            # a tier whose local runtime was never installed. They have different
            # fixes, so they get different sentences. Still a ✓ either way -- an
            # off-by-default optional tier is not a fault.
            detail = "not enabled in config (set [llm] enabled = true, or pass --llm PROVIDER)"
            if llm.provider in ("builtin", "auto") and not _local_llm_runtime_present():
                detail += ("; the local runtime (llama-cpp-python) is not installed either: "
                           "contextlake doctor --fix llm-local")
            _check("wiki LLM", True, detail)
        elif llm.provider in ("builtin", "auto"):
            from ..llm.builtin import BuiltinLlm

            kw = {}
            if getattr(llm, "model", None):
                kw["repo_id"] = llm.model
            if getattr(llm, "model_file", None):
                kw["filename"] = llm.model_file
            if getattr(llm, "cache_dir", None):
                kw["cache_dir"] = llm.cache_dir
            bl = BuiltinLlm(**kw)
            present = _builtin_model_present(bl.cache_dir, bl.repo_id)
            # the model file alone is not enough: wiki needs the llama-cpp-python runtime, which
            # has no prebuilt wheel on some Pythons. Report ⚠ when the runtime is absent so doctor
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
        ok &= _check("config + store", False, str(e))

    _say(style.bold(style.green("OK")) if ok else style.bold(style.red("Problems found")))

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
