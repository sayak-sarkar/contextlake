"""`contextlake steer` -- write agent-facing steering files (AGENTS.md, MCP config, skills)."""

from __future__ import annotations

from pathlib import Path

from ... import style
from ...logging_setup import log
from ._common import (
    _open_store,
    kb_config,
)


def _implicit_binding(cfg, out: Path) -> tuple[str | None, str | None]:
    """Which config the generated launcher should pin when no ``--config`` was
    given, and any warning owed to the user. Returns ``(path, warning)``.

    The generated ``.mcp.json`` runs ``contextlake kb serve``, which an MCP
    client execs with its cwd set to the workspace -- not to wherever steer ran.
    With no ``--config`` on that command line the server re-resolves the store
    by walking up from that cwd, so it can serve a different store than the one
    these files describe. Pinning the config that was actually used removes the
    ambiguity.

    Not every config may be pinned, though. Writing ``--config <path>`` onto a
    command line is exactly the act ``kb.trust`` treats as choosing a file, and
    it promotes that file's gated keys (the ones that become subprocess argv)
    to trusted. An ancestor-discovered ``.contextlake.kb.toml`` is deliberately
    *not* trusted -- so auto-pinning one here would launder a file the user
    never named into a privileged one, which is the exact hole that gate
    exists to close. Those are warned about instead.
    """
    from ..config import GLOBAL_CONFIG
    from ..trust import is_privileged_source

    loaded = list(getattr(cfg, "loaded_from", None) or [])
    if not loaded:
        # Nothing on disk anywhere: the default store is the same from any cwd,
        # so an unpinned launcher already resolves what these files describe.
        return None, None
    resolved = loaded[-1]  # highest precedence wins the store_dir
    if resolved == str(Path(GLOBAL_CONFIG).expanduser()):
        # The global config is the one path worth NOT pinning, even though it is
        # privileged. `.mcp.json` is committed and shared, and pinning writes an
        # absolute `/home/<user>/...` into it: a teammate who clones then gets a
        # launcher naming a path that does not exist on their machine, plus the
        # committer's home layout in version control. Leaving it unpinned costs
        # nothing here, because an unpinned launcher walks up from the workspace
        # and lands on the global config anyway -- on *their* machine, which is
        # the store they should be served. Pinning is still right for a config
        # somewhere non-default, since nothing else would find it.
        return None, None
    if is_privileged_source(resolved, None, global_config=GLOBAL_CONFIG):
        return resolved, None
    # Non-privileged (ancestor-discovered). Only a problem if the walk from the
    # workspace wouldn't reach it anyway.
    config_dir = Path(resolved).parent
    if config_dir == out or config_dir in out.parents:
        return None, None
    # ...and only if this file is what chose the store. It wins the precedence
    # chain overall, but a config that never sets store_dir leaves the global
    # one's value standing, and the global config is found from any directory.
    # Warning about a store that cannot actually change would send the reader
    # chasing a problem they do not have.
    from ..config import _read_toml

    if "store_dir" not in (_read_toml(resolved).get("kb") or {}):
        return None, None
    return None, (
        f"{out} is outside {config_dir}, so the generated MCP entry will resolve a "
        f"different store than this run used ({cfg.store_path}). Re-run with "
        "--config <path> to pin it (naming it on the command line is also what "
        "makes its gated keys trusted)."
    )


def cmd_steer(args) -> int:
    """Generate per-tool steering files (AGENTS.md, CLAUDE.md, .windsurfrules,
    .kiro/steering, .mcp.json, .vscode/mcp.json) that point local AI tools at
    the knowledge graph."""
    import json as _json

    from ..steer.generate import (
        BEGIN,
        END,
        mcp_server_entry,
        render_agents_md,
        render_claude_md,
        render_kiro_steering,
        render_windsurfrules,
        workspace_facts,
    )
    from ..steer.skills import skill_files

    def _upsert_block(path: Path, body: str) -> bool:
        """Write our managed block, preserving any existing user content.

        Fresh file -> write the block. Already has our block -> refresh just that
        block. User's own file -> append our block at the end (nothing of theirs
        is removed). Returns False (writes nothing) if a marker pair is present
        but doesn't cleanly bound a single well-formed block -- e.g. a BEGIN or
        END string appears more than once (possible if interpolated content ever
        smuggled in a literal marker) -- rather than splicing at the wrong
        occurrence and corrupting the file."""
        block = f"{BEGIN}\n{body.strip()}\n{END}"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(block + "\n", encoding="utf-8")
            return True
        existing = path.read_text(encoding="utf-8", errors="ignore")
        # Refresh an existing managed block in place.
        if BEGIN in existing and END in existing:
            if existing.count(BEGIN) != 1 or existing.count(END) != 1:
                return False
            b, e = existing.index(BEGIN), existing.index(END) + len(END)
            if e <= b:
                return False
            path.write_text(existing[:b] + block + existing[e:], encoding="utf-8")
            return True
        # No managed block yet — append ours, keeping all the user's own content.
        glue = "\n\n" if not existing.endswith("\n") else (
            "" if existing.endswith("\n\n") else "\n")
        path.write_text(existing + glue + block + "\n", encoding="utf-8")
        return True

    store, store_dir = _open_store(args)
    try:
        out = Path(getattr(args, "out", None) or getattr(args, "workspace", None) or ".").resolve()
        config_path = getattr(args, "config", None)
        if config_path:
            # Steering files may be read/launched (e.g. an MCP client execing the
            # generated `contextlake serve --config ...`) from `out`, not from
            # wherever `contextlake steer` itself was invoked -- a relative path
            # would only resolve by accident. Store it absolute.
            config_path = str(Path(config_path).expanduser().resolve())
        else:
            config_path, unbound = _implicit_binding(kb_config(args), out)
            if unbound:
                log(style.warn(unbound))
        force = getattr(args, "force", False)
        facts = workspace_facts(store, store_dir)
        out.mkdir(parents=True, exist_ok=True)

        # Markdown steering: enhanced in place (managed block), never overwriting
        # a user's own content.
        steering = {
            "AGENTS.md": render_agents_md(facts, config_path=config_path),
            "CLAUDE.md": render_claude_md(config_path),
            ".windsurfrules": render_windsurfrules(facts, config_path=config_path),
            ".kiro/steering/workspace.md": render_kiro_steering(facts, config_path=config_path),
        }
        malformed = [rel for rel, content in steering.items()
                     if not _upsert_block(out / rel, content)]
        if malformed:
            log(style.warn(f"Skipped refreshing {', '.join(malformed)} — its BEGIN/END "
                            "contextlake marker appears more than once or out of order; "
                            "fix the file manually, then re-run"))

        # Skills/workflows are whole files in named dirs: write ours, refresh ours,
        # but never clobber a same-named file the user already wrote OR has since
        # edited (unless --force). Unlike the markdown steering files above, these
        # have no END marker bounding "our" content, so a partial merge isn't
        # possible -- MARKER presence alone can't tell a still-pristine
        # contextlake-managed file from one the user has since modified. The safe
        # default is to only refresh a file whose on-disk content is byte-identical
        # to what we're about to write (a true no-op) or that doesn't exist yet;
        # anything else -- foreign or locally edited -- is kept.
        skills = skill_files()
        skipped = 0
        for rel, content in skills.items():
            p = out / rel
            if p.exists() and not force and p.read_text(errors="ignore") != content:
                skipped += 1
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        log(f"  {style.ok(f'steering enhanced + {len(skills) // 2} skills')} "
            + (f"({skipped} skill file(s) kept -- foreign or locally edited, "
               "use --force to overwrite)" if skipped else "written"))

        def _merge_mcp_entry(rel: str, wrapper_key: str) -> None:
            """Merge our server entry into an MCP config file under `wrapper_key`,
            preserving any other servers the user already has configured there."""
            path = out / rel
            data = {}
            if path.exists():
                try:
                    parsed = _json.loads(path.read_text())
                    data = parsed if isinstance(parsed, dict) else {}
                except _json.JSONDecodeError:
                    data = {}
            if not isinstance(data.get(wrapper_key), dict):
                data[wrapper_key] = {}
            data[wrapper_key]["contextlake-kb"] = mcp_server_entry(config_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_json.dumps(data, indent=2) + "\n", encoding="utf-8")
            log(f"  {style.ok(rel)} (contextlake-kb MCP server, other servers kept)")

        # .mcp.json (Claude Code, Windsurf, Cursor, …) and VS Code's own
        # .vscode/mcp.json — same per-server shape, different wrapper key
        # (`mcpServers` vs `servers`), so both are merged the same way.
        _merge_mcp_entry(".mcp.json", "mcpServers")
        _merge_mcp_entry(".vscode/mcp.json", "servers")

        log(f"{style.ok()} Steering written to {out} (existing files enhanced, not replaced)")
        return 0
    finally:
        store.close()
