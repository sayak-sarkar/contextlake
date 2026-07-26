"""`contextlake dashboard` -- build/serve the fleet dashboard."""

from __future__ import annotations

from pathlib import Path

from ... import style
from ...logging_setup import log
from ..config import load_kb_config


def cmd_dashboard(args) -> int:
    """The knowledge-system dashboard — fleet / repo / relationships / impact / health
    / search UI over the local graph. ``--serve`` runs a local server (default);
    ``--site DIR`` materializes a static offline export.
    """
    from ..dashboard.server import serve_dashboard
    from ..dashboard.site import build_dashboard_site

    store_dir = load_kb_config(getattr(args, "config", None)).store_path
    dash_dir = store_dir / "dashboard"

    sample = getattr(args, "sample", False)
    site = getattr(args, "site", None)
    if site is not None:
        out_dir = Path(site) if site else (dash_dir / "site")
        anonymize = getattr(args, "anonymize", False)
        repos = getattr(args, "repos", None)
        group_depth = getattr(args, "group_depth", None) or 1
        src = "the bundled demo fleet" if sample else "the local store"
        log(f"Building dashboard site from {src}…")
        build_dashboard_site(store_dir, out_dir, repos=repos, anonymize=anonymize,
                             sample=sample, group_depth=group_depth)
        log(style.ok(f"Wrote dashboard -> {out_dir}  (open {out_dir / 'index.html'})"))
        return 0

    host = getattr(args, "host", None) or "127.0.0.1"
    port = getattr(args, "port", None) or 8765
    if sample:
        # The advertised zero-setup preview: serve the bundled demo fleet from an
        # ephemeral store, never the user's real data.
        import shutil
        import tempfile

        from ..dashboard.site import materialize_sample_store
        tmp = Path(tempfile.mkdtemp(prefix="contextlake-dash-sample-"))
        try:
            log("Serving the bundled demo fleet (fictional data, nothing local is read)…")
            # sample=True: load_kb_config(None) still merges the user's real
            # ~/.contextlake/kb.toml regardless of config_path, so the
            # Settings/MCP routes must be told explicitly to use bare config
            # defaults instead -- otherwise real embedder/connector config
            # would leak into a surface billed as "nothing local is read".
            serve_dashboard(materialize_sample_store(tmp), host=host, port=port,
                            open_browser=getattr(args, "open", False), sample=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return 0
    serve_dashboard(store_dir, host=host, port=port,
                    open_browser=getattr(args, "open", False),
                    config_path=getattr(args, "config", None))
    return 0

