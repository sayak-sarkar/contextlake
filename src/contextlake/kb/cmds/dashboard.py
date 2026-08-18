"""`contextlake dashboard` -- build/serve the fleet dashboard."""

from __future__ import annotations

from pathlib import Path

from ... import style
from ...logging_setup import log
from .._util import _or_default
from ..config import load_kb_config
from ..http_base import LOOPBACK_HOSTS


def _refuse_non_loopback(flag: str, host: str, exposure: str) -> str | None:
    """The refusal message for a token-gated flag on a non-loopback bind, or None.

    Every flag that turns on a privileged dashboard route is gated by the same
    per-process token, and that token is inlined into ``/dashboard.js``, which
    is served over a plain GET. Host-header pinning (see
    :meth:`kb.http_base.LocalHttpHandler.reject_bad_host`) stops a *browser*
    being tricked into fetching that script cross-origin, but it is only a
    browser control: a direct client on the LAN can send the pinned Host header
    by hand, read the token out of the script, and use it. So on a non-loopback
    bind the token is effectively public and refusing the bind is the only
    control left.

    One helper rather than a check per flag, because that is exactly how F-3
    happened: the identical reasoning was written inline for
    ``--allow-mutations`` only, and ``--llm-chat`` -- same token, same GET, but
    fronting a *paid* provider -- was added beside it without one.
    """
    if host in LOOPBACK_HOSTS:
        return None
    return (f"{flag} refused with --host {host!r}: {exposure} It is loopback-only "
            f"because the per-launch token that gates it is served inside "
            f"/dashboard.js, so anyone who can reach this bind can read that token "
            f"and use it. Re-run with --host 127.0.0.1 (the default), or drop {flag}.")


def cmd_dashboard(args) -> int:
    """The knowledge-system dashboard — fleet / repo / relationships / impact / health
    / search UI over the local graph. ``--serve`` runs a local server (default);
    ``--site DIR`` materializes a static offline export.
    """
    from ..dashboard.server import serve_dashboard
    from ..dashboard.site import build_dashboard_site

    cfg = load_kb_config(getattr(args, "config", None))
    store_dir = cfg.store_path
    dash_dir = store_dir / "dashboard"

    # The flag says "this invocation", the setting says "this machine", and the STRICTER
    # of the two wins. There is deliberately no way to turn anonymising off from the
    # command line: `--anonymize` can only ever raise it, so somebody who set
    # `[kb] anonymize = "always"` cannot lose it to a flag they half-remember. To show
    # identities again they change the setting, which is a decision with a file behind it
    # rather than a keystroke in a shell they are screen-sharing.
    anonymize_default = cfg.anonymize == "always"

    sample = getattr(args, "sample", False)
    site = getattr(args, "site", None)
    if site is not None:
        # --site and --serve ask for different things: one writes a static export
        # and exits, the other holds a socket open. --site won silently, so
        # `--serve --site out/` built a snapshot, exited 0, and never served --
        # the flag the user typed did nothing and nothing said so.
        if getattr(args, "serve", False):
            log(style.fail(
                "--serve and --site ask for different things: --serve runs a local "
                "server, --site writes a static export and exits.\n"
                "  Pick one -- drop --site to serve, or drop --serve to build."))
            return 1
        out_dir = Path(site) if site else (dash_dir / "site")
        anonymize = bool(getattr(args, "anonymize", False)) or anonymize_default
        repos = getattr(args, "repos", None)
        group_depth = _or_default(getattr(args, "group_depth", None), 1)
        # The same refusal `kb graph --site` makes, on the same flag, over the same store.
        # Without it a filter matching nothing wrote an overview and zero repo pages, logged
        # that zero, and printed a success line over it -- a sibling surface left behind
        # when the graph side was fixed, which is how a read/write pair or a pair of
        # neighbouring commands usually goes wrong.
        if repos and not sample:
            from ..store.sqlite_store import SqliteStore
            from ._common import register_store_for_observability
            from .graph import _no_repo_matches, _repos_matching
            patterns = [p.strip() for p in repos.split(",") if p.strip()]
            if patterns:
                # Opened only for this check and closed straight away: the builder below
                # opens its own, and holding two write-capable handles to one sqlite file
                # for the length of a site build is a lock waiting to happen.
                # Registered like every other store this process opens, not merely
                # constructed: the refusal below names real repository ids as suggestions,
                # and `--redact` has to be able to hide them. The repo's own parity gate
                # caught this the moment the raw construction was added, which is what that
                # gate is for.
                db_path = store_dir / "index.sqlite"
                probe = SqliteStore(db_path)
                register_store_for_observability(probe, db_path)
                try:
                    matched = _repos_matching(probe, patterns)
                    # Built while the handle is open, because the message names real repo
                    # ids to try and reading those needs the store.
                    refusal = (None if matched
                               else _no_repo_matches(probe, patterns, "dashboard"))
                finally:
                    probe.close()
                if refusal:
                    log(style.fail(refusal))
                    return 1
        src = "the bundled demo fleet" if sample else "the local store"
        log(f"Building dashboard site from {src}…")
        build_dashboard_site(store_dir, out_dir, repos=repos, anonymize=anonymize,
                             sample=sample, group_depth=group_depth)
        log(style.ok(f"Wrote dashboard -> {out_dir}  (open {out_dir / 'index.html'})"))
        return 0

    host = getattr(args, "host", None) or "127.0.0.1"
    port = getattr(args, "port", None) or 8765
    allow_mutations = getattr(args, "allow_mutations", False)
    llm_chat = getattr(args, "llm_chat", False)
    if allow_mutations and sample:
        log(style.fail("--allow-mutations refused with --sample: the demo fleet "
                      "is fictional, there's nothing on disk for it to sync/clone."))
        return 1
    # Checked in a fixed order so a run that trips both flags names one reason.
    for flag, enabled, exposure in (
        ("--allow-mutations", allow_mutations,
         "it opens routes that sync/clone repos and start servers on this machine."),
        ("--llm-chat", llm_chat,
         "it opens a route that spends real time and money on the configured "
         "[llm] provider."),
    ):
        refusal = _refuse_non_loopback(flag, host, exposure) if enabled else None
        if refusal:
            log(style.fail(refusal))
            return 1
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
            return serve_dashboard(materialize_sample_store(tmp), host=host, port=port,
                                   open_browser=getattr(args, "open", False), sample=True,
                                   llm_chat=llm_chat)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return serve_dashboard(store_dir, host=host, port=port,
                    open_browser=getattr(args, "open", False),
                    config_path=getattr(args, "config", None),
                    allow_mutations=allow_mutations,
                    workspace=getattr(args, "workspace", None),
                    anonymize=(bool(getattr(args, "anonymize", False))
                               or anonymize_default),
                    llm_chat=llm_chat)
