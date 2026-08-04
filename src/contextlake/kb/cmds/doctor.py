"""`contextlake doctor` -- environment/store health checks."""

from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
from pathlib import Path

from ... import style
from ..config import load_kb_config
from ..state import check_schema
from ..store.sqlite_store import SqliteStore


def _check(label: str, ok, detail: str = "") -> bool:
    # tri-state: True -> ✓, False -> ✗, None -> ⚠ (present-but-degraded / optional-unavailable)
    mark = style.yellow("⚠") if ok is None else (style.green("✓") if ok else style.red("✗"))
    print(f"  {mark} {label}" + (f" {style.dim('— ' + detail)}" if detail else ""))
    return bool(ok)


def _builtin_model_present(cache_dir, model_id: str) -> bool:
    """True if a HuggingFace-cached model dir exists under ``cache_dir/hub``.

    Filesystem-only — never imports the heavy dep or triggers a download."""
    hub = Path(cache_dir).expanduser() / "hub"
    return (hub / ("models--" + model_id.replace("/", "--"))).is_dir()


def cmd_doctor(args) -> int:
    print("contextlake knowledge layer — doctor")
    ok = True

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
        _check("config loads", True, f"{len(cfg.sources)} source(s), {len(cfg.rules)} rule(s)")
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

            stale = [r.id for r in store.list_repos()
                     if (sh := read_shard(store_dir, r.id)) is not None
                     and sh.parser_version != PARSER_VERSION
                     and any(n.lang in ("c", "cpp") for n in sh.nodes)]
            if stale:
                _check("C/C++ shards up to date with the current parser", False,
                        f"{len(stale)} repo(s) indexed with an older parser -- re-index for "
                        f"corrected method/class linkage: {', '.join(stale[:5])}"
                        + ("…" if len(stale) > 5 else ""))
            else:
                _check("C/C++ shards up to date with the current parser", True)
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
            _check("wiki LLM", True, "disabled")
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
            runtime = importlib.util.find_spec("llama_cpp") is not None
            model_state = "downloaded" if present else "not downloaded (run wiki to fetch)"
            _check("wiki LLM", True if runtime else None,
                   f"{llm.provider} · {bl.repo_id} · {model_state}" if runtime
                   else f"{llm.provider} · {bl.repo_id} · {model_state} · runtime not installed "
                        "(pip install 'contextlake[llm-local]')")
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

    print(style.bold(style.green("OK")) if ok else style.bold(style.red("Problems found")))
    return 0 if ok else 1
