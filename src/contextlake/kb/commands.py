"""Back-compat shim: every name this module used to define directly now lives
under kb/cmds/ (see plan-kb-module-split.md). Re-exported here so
`contextlake.kb.commands` keeps working exactly as before for every caller
(cli.py, server.py, git_hook.py) and every test that imports from it."""

from __future__ import annotations

import importlib.util  # noqa: F401 -- tests patch commands.importlib.util.find_spec

from .. import style  # noqa: F401 -- tests patch commands.style.Progress
from .cmds import dispatch  # noqa: F401
from .cmds._common import (  # noqa: F401
    _connect_targets,
    _git_head,
    _guard_store,
    _open_store,
    _repo_id_suggestions,
    _unknown_repo_msg,
    _watch_loop,
)
from .cmds.connect import (  # noqa: F401
    _build_enrichers,
    _rule_patterns,
    cmd_connect,
)
from .cmds.dashboard import (  # noqa: F401
    cmd_dashboard,
)
from .cmds.doctor import (  # noqa: F401
    _builtin_model_present,
    _check,
    cmd_doctor,
)
from .cmds.embed import (  # noqa: F401
    _embed_unavailable_hint,
    cmd_embed,
)
from .cmds.enrich import (  # noqa: F401
    cmd_enrich,
)
from .cmds.eval import (  # noqa: F401
    cmd_eval,
)
from .cmds.graph import (  # noqa: F401
    _has_seed,
    cmd_graph,
)
from .cmds.hook import (  # noqa: F401
    _canonical_repo_id,
    _git_root,
    cmd_hook,
)
from .cmds.impact import (  # noqa: F401
    cmd_impact,
)
from .cmds.index import (  # noqa: F401
    _default_index_workers,
    _index_workspace,
    _store_and_index,
    cmd_index,
)
from .cmds.ingest import (  # noqa: F401
    _embed_documents,
    cmd_ingest,
)
from .cmds.lint import (  # noqa: F401
    cmd_lint,
    lint_result,
)
from .cmds.owners import (  # noqa: F401
    cmd_owners,
)
from .cmds.query import (  # noqa: F401
    _QUERY_USAGE,
    _hit_json,
    _print_hit,
    _query_as_of,
    cmd_query,
)
from .cmds.serve import (  # noqa: F401
    cmd_serve,
)
from .cmds.steer import (  # noqa: F401
    cmd_steer,
)
from .cmds.wiki import (  # noqa: F401
    _store_wiki_partition,
    _wiki_partition,
    _wiki_section_nodes,
    cmd_wiki,
)
