"""CLI command dispatch table -- one module per command, see plan-kb-module-split.md."""

from __future__ import annotations

from .connect import cmd_connect
from .dashboard import cmd_dashboard
from .docs import cmd_docs
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
from .keys_cmd import cmd_keys
from .lint import cmd_lint
from .owners import cmd_owners
from .query import cmd_query
from .refresh import cmd_refresh
from .serve import cmd_serve
from .steer import cmd_steer
from .wiki import cmd_wiki

# The table as a module constant rather than a literal inside `dispatch`, so the set of verbs
# has ONE readable authority. A docs test compares the CLI reference against this; while the
# table was inline, nothing outside `dispatch` could name the verbs, so "is every documented
# verb real" was unanswerable except by parsing this file as text.
#
# `source` is deliberately absent: it is dispatched lazily below because its import chain
# reaches tomlkit, which would otherwise sit on every other command's startup path. It is
# listed in HANDLERS' docstring rather than the dict so the lazy path stays the only one.
_EAGER_HANDLERS = {
    "index": cmd_index, "connect": cmd_connect, "embed": cmd_embed,
    "forget": cmd_forget,
    "lint": cmd_lint, "wiki": cmd_wiki, "steer": cmd_steer, "query": cmd_query,
    "serve": cmd_serve, "graph": cmd_graph, "doctor": cmd_doctor, "eval": cmd_eval,
    "owners": cmd_owners, "impact": cmd_impact, "ingest": cmd_ingest,
    "enrich": cmd_enrich, "dashboard": cmd_dashboard, "hook": cmd_hook,
    "refresh": cmd_refresh, "docs": cmd_docs,
    # `keys` is EAGER, not lazy like `source`. The lazy branch exists for one
    # reason: `source_cmd` reaches tomlkit, which would otherwise sit on every
    # other kb command's startup path. `keys_cmd` reaches `kb/keyfile.py` and
    # `kb/keys.py`, whose imports are json, hashlib, zlib, secrets and datetime
    # -- all stdlib, all already loaded. There is nothing to keep off the
    # startup path, so it goes here.
    "keys": cmd_keys,
}

# Every verb `dispatch` accepts, including the lazily-imported one. This is what a consumer
# should ask; `_EAGER_HANDLERS` alone would under-report by exactly one and look complete.
VERBS = frozenset(_EAGER_HANDLERS) | {"source"}


def dispatch(command: str, args) -> int:
    if command == "source":
        # Lazy: source_cmd -> config_edit -> tomlkit, kept off every other kb
        # command's import path (see config_edit's module docstring).
        from ..source_cmd import cmd_source

        return cmd_source(args)
    return _EAGER_HANDLERS[command](args)
