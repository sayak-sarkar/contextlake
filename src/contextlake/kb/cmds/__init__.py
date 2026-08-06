"""CLI command dispatch table -- one module per command, see plan-kb-module-split.md."""

from __future__ import annotations

from .connect import cmd_connect
from .dashboard import cmd_dashboard
from .doctor import cmd_doctor
from .embed import cmd_embed
from .enrich import cmd_enrich
from .eval import cmd_eval
from .forget import cmd_forget
from .graph import cmd_graph
from .hook import cmd_hook
from .impact import cmd_impact
from .index import cmd_index
from .ingest import cmd_ingest
from .lint import cmd_lint
from .owners import cmd_owners
from .query import cmd_query
from .serve import cmd_serve
from .steer import cmd_steer
from .wiki import cmd_wiki


def dispatch(command: str, args) -> int:
    if command == "source":
        # Lazy: source_cmd -> config_edit -> tomlkit, kept off every other kb
        # command's import path (see config_edit's module docstring).
        from ..source_cmd import cmd_source

        return cmd_source(args)
    return {
        "index": cmd_index, "connect": cmd_connect, "embed": cmd_embed,
        "forget": cmd_forget,
        "lint": cmd_lint, "wiki": cmd_wiki, "steer": cmd_steer, "query": cmd_query,
        "serve": cmd_serve, "graph": cmd_graph, "doctor": cmd_doctor, "eval": cmd_eval,
        "owners": cmd_owners, "impact": cmd_impact, "ingest": cmd_ingest,
        "enrich": cmd_enrich, "dashboard": cmd_dashboard, "hook": cmd_hook,
    }[command](args)
