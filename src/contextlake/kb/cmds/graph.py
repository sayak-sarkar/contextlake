"""`contextlake kb graph` -- render/serve the knowledge graph."""

from __future__ import annotations

from pathlib import Path

from ... import style
from ...logging_setup import log
from ._common import (
    _open_store,
    _unknown_repo_msg,
)


def _has_seed(args) -> bool:
    return bool(getattr(args, "node", None) or getattr(args, "name", None)
                or getattr(args, "search", None)
                or " ".join(getattr(args, "args", []) or []).strip())


def cmd_graph(args) -> int:
    from .. import visualize as viz

    # When a text format is streamed to stdout (no --output), every log line would
    # otherwise land on stdout and corrupt the payload (a truncation note, or a
    # config warning). Redirect logs to stderr BEFORE opening the store, since
    # _open_store loads config and may warn on an unknown key.
    fmt = getattr(args, "format", None) or "html"
    if (fmt in ("json", "dot", "mermaid", "classdiagram", "sequencediagram", "statediagram",
               "erdiagram", "deploymentdiagram", "graphml", "cypher")
            and not getattr(args, "output", None) and not getattr(args, "serve", False)):
        from ...logging_setup import use_stderr
        use_stderr()

    if getattr(args, "c1", False) and not getattr(args, "c4", False):
        from ...logging_setup import use_stderr
        use_stderr()
        log("--c1 only applies to --c4 (it adds the C1 external-system layer "
            "on top of that view); pass --c4 --c1 together.")
        return 2

    store, store_dir = _open_store(args)
    # Generated artifacts live in a dedicated dir next to the store, never the cwd
    # or the user's home — keep generated content close to the knowledge base.
    graphs_dir = store_dir / "graphs"
    try:
        # --site: build the cross-linked offline folder and stop
        site = getattr(args, "site", None)
        if site is not None:
            out_dir = Path(site) if site else (graphs_dir / "site")
            repos_arg = getattr(args, "repos", None)
            patterns = [p.strip() for p in repos_arg.split(",") if p.strip()] if repos_arg else None
            if patterns:
                log(f"Building cross-linked graph site (repos matching {patterns})…")
            else:
                log("Building cross-linked graph site…")
            viz.build_site(store, out_dir, repos=patterns,
                           cdn=getattr(args, "cdn", False), log=log)
            log(style.ok(f"Wrote site -> {out_dir}  (open {out_dir / 'index.html'})"))
            return 0

        # --c4: composed namespace (C4-style) diagram -- repos bucketed into
        # namespace boundaries with aggregated cross-repo edges, instead of the
        # node-level overview/repo/neighborhood modes below.
        if getattr(args, "c4", False):
            from .. import c4 as c4mod

            if fmt in ("mermaid", "classdiagram", "sequencediagram", "statediagram",
                      "erdiagram", "deploymentdiagram"):
                from ...logging_setup import use_stderr
                use_stderr()
                log("mermaid/classdiagram/sequencediagram/statediagram/erdiagram/"
                    "deploymentdiagram output is not supported for --c4; use --format dot or html.")
                return 1

            group_depth = getattr(args, "group_depth", None) or 1
            repos_arg = getattr(args, "repos", None)
            repos_filter = None
            if repos_arg:
                patterns = [p.strip() for p in repos_arg.split(",") if p.strip()]
                if patterns:
                    repos_filter = [r.id for r in store.list_repos()
                                     if viz._match_repo(r.id, patterns)]

            c1 = getattr(args, "c1", False)
            model = c4mod.c4_model(store, group_depth=group_depth, repos=repos_filter, c1=c1)

            if fmt == "dot":
                text = c4mod.to_c4_dot(model)
            elif fmt == "json":
                text = viz.to_json(c4mod.c4_payload(model))
            else:
                title = f"C4 - {len(model.boundaries)} namespaces"
                if c1:
                    title += f", {len(model.systems)} external system(s)"
                text = viz.to_html(c4mod.c4_payload(model), cdn=getattr(args, "cdn", False),
                                    layout="cose", title=title)

            out = getattr(args, "output", None)
            if fmt == "html" and not out:
                graphs_dir.mkdir(parents=True, exist_ok=True)
                out = str(graphs_dir / "c4.html")
            if out:
                Path(out).parent.mkdir(parents=True, exist_ok=True)
                Path(out).write_text(text, encoding="utf-8")
                container_count = sum(len(b.containers) for b in model.boundaries)
                log(style.ok(f"Wrote {fmt} c4 diagram ({len(model.boundaries)} namespaces, "
                             f"{container_count} repos) -> {out}"))
                if fmt == "html" and getattr(args, "open", False):
                    import webbrowser
                    webbrowser.open("file://" + str(Path(out).resolve()))
            else:
                from ...logging_setup import use_stderr
                use_stderr()
                print(text)
            return 0

        max_fanout = getattr(args, "max_fanout", None) or 50
        hops = getattr(args, "hops", None) or 2
        overview = getattr(args, "overview", False)
        # The overview is a fleet inventory — default to loading every repo (so any
        # is findable); neighbourhood/repo views stay bounded at 500.
        max_nodes = getattr(args, "max_nodes", None) or (5000 if overview else 500)
        # Only the Mermaid-rendered formats have a hard edge-count limit of their
        # own to protect against (see repo_subgraph's docstring) -- html (cytoscape)
        # and dot have no such limit and must keep showing every edge among the
        # capped nodes exactly as before, unless the user explicitly asks otherwise.
        max_edges = getattr(args, "max_edges", None)
        if max_edges is None and fmt in ("mermaid", "classdiagram", "statediagram",
                                        "erdiagram", "deploymentdiagram"):
            max_edges = 400

        meta: dict = {}
        if overview:
            nodes, edges = viz.overview_subgraph(store, max_nodes=max_nodes, meta=meta)
            meta["mode"] = "overview"
        elif getattr(args, "repo", None) and not _has_seed(args):
            nodes, edges = viz.repo_subgraph(store, args.repo, max_nodes=max_nodes,
                                             max_edges=max_edges, meta=meta)
            # empty AND not a known repo -> the id is wrong; suggest close ones
            # (a real repo with nodes renders even without a repos-table row).
            if not nodes and store.get_repo(args.repo) is None:
                log(_unknown_repo_msg(store, args.repo))
                return 1
            meta.update(mode="repo", repo=args.repo)
        else:
            seeds = viz.seed_ids_from_args(store, args)
            if not seeds:
                log("usage: contextlake kb graph (--node ID | --name NAME | --search TEXT | "
                    "--repo R | --overview) [--hops N] [--format html|dot|mermaid|json]")
                return 2
            nodes, edges = viz.extract_subgraph(
                store, seeds, hops=hops, max_nodes=max_nodes, max_fanout=max_fanout,
                relation=getattr(args, "relation", None),
                direction=getattr(args, "direction", None) or "both", meta=meta)
            meta.update(mode="neighborhood", seed_ids=seeds, hops=hops)

        payload = viz.to_payload(nodes, edges, meta)
        cdn = getattr(args, "cdn", False)
        # cose (organic clusters) suits small neighbourhoods; for the fleet-scale
        # overview default to the instant, uniform concentric rings (hubs centred).
        layout = getattr(args, "layout", None) or ("concentric" if overview else "cose")

        if getattr(args, "serve", False):
            host = getattr(args, "host", None) or "127.0.0.1"
            port = getattr(args, "port", None) or 8765
            if overview:
                # serve the whole cross-linked site, rendering repo pages on demand
                viz.serve_site(store, host=host, port=port, max_nodes=max_nodes,
                               overview_layout=layout, max_fanout=max_fanout)
            else:
                viz.serve_graph(store, payload, host=host, port=port,
                                cdn=cdn, layout=layout, max_fanout=max_fanout)
            return 0

        # Every Mermaid format draws only a SLICE of the view (classes only, tables
        # only, Terraform only...), so reporting the view's node/edge counts over an
        # empty diagram claimed "900 nodes, 400 edges" for a file whose whole content
        # was a "nothing to draw" comment. Each renderer reports what it drew instead.
        drawn: dict = {}
        if fmt == "json":
            text = viz.to_json(payload)
        elif fmt == "dot":
            text = viz.to_dot(payload)
        elif fmt == "mermaid":
            text = viz.to_mermaid(payload, stats=drawn)
        elif fmt == "classdiagram":
            text = viz.to_class_diagram(payload, stats=drawn)
        elif fmt == "sequencediagram":
            text = viz.to_sequence_diagram(payload, stats=drawn)
        elif fmt == "statediagram":
            text = viz.to_state_diagram(payload, stats=drawn)
        elif fmt == "erdiagram":
            text = viz.to_er_diagram(payload, stats=drawn)
        elif fmt == "deploymentdiagram":
            text = viz.to_deployment_diagram(payload, stats=drawn)
        elif fmt == "graphml":
            text = viz.to_graphml(payload)
        elif fmt == "cypher":
            text = viz.to_cypher(payload)
        else:
            text = viz.to_html(payload, cdn=cdn, layout=layout)

        out = getattr(args, "output", None)
        if fmt == "html" and not out:
            # default into the dedicated graphs dir, not the cwd
            name = "overview.html" if overview else "graph.html"
            graphs_dir.mkdir(parents=True, exist_ok=True)
            out = str(graphs_dir / name)
        n_nodes = drawn.get("nodes", len(payload["nodes"]))
        n_edges = drawn.get("edges", len(payload["edges"]))
        if out:
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_text(text, encoding="utf-8")
            if overview and not payload["nodes"]:
                # An empty overview is a graph with nothing in it -- reporting that
                # as a plain "Wrote" success would hide the real problem (nothing
                # indexed yet), the same trap cmd_index's empty-workspace guard avoids.
                log(style.warn(f"Wrote {fmt} (0 nodes, 0 edges) -> {out}: the store is empty."))
                log("  Run `contextlake kb index` first, then re-run this command.")
            elif not n_nodes:
                # Same trap one level down: this view held nodes, but none of the kind
                # THIS diagram draws. The file explains why in a comment; the console
                # line must not contradict it with the query's counts.
                log(style.warn(f"Wrote {fmt} (0 nodes, 0 edges) -> {out}: "
                               f"nothing in this view for {fmt} to draw."))
                log(f"  The file says why -- see the %% comment in {out}.")
            else:
                log(style.ok(f"Wrote {fmt} ({n_nodes} nodes, {n_edges} edges) -> {out}"))
            if fmt == "html" and getattr(args, "open", False):
                import webbrowser
                webbrowser.open("file://" + str(Path(out).resolve()))
        else:
            from ...logging_setup import use_stderr
            use_stderr()
            print(text)
        return 0
    finally:
        store.close()
