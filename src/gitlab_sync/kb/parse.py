"""Tree-sitter code parsing → knowledge-graph nodes and edges.

Parses source files into structural facts (files, classes, functions/methods,
and their containment + imports) using tree-sitter. Everything extracted here is
``EXTRACTED`` confidence — it comes straight from the AST. Call-graph edges
(which need name resolution and are inherently less certain) come in a later
ticket.

Adding a language = registering its grammar loader, file extensions, and a query
in the tables below; the rest of the pipeline is language-agnostic.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import tree_sitter as ts

from ..logging_setup import log
from .ids import make_id
from .model import Confidence, Edge, Node, Provenance
from .store.shards import GraphShard

# Directories never worth walking.
_SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", "dist", "build",
              ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".idea"}

LANG_BY_EXT = {".py": "python"}

# tree-sitter node types that introduce a named definition, per language.
_DEF_TYPES = {"python": {"class_definition": "class", "function_definition": "function"}}

# Queries capture definition *name* identifiers and import module names.
_QUERIES = {
    "python": """
        (class_definition name: (identifier) @def)
        (function_definition name: (identifier) @def)
        (import_statement (dotted_name) @import)
        (import_from_statement module_name: (dotted_name) @import)
    """,
}

_LANGS: dict[str, ts.Language] = {}
_PARSERS: dict[str, ts.Parser] = {}
_COMPILED: dict[str, ts.Query] = {}


def _language(lang: str) -> ts.Language:
    if lang not in _LANGS:
        if lang == "python":
            import tree_sitter_python as grammar
        else:
            raise ValueError(f"unsupported language: {lang}")
        _LANGS[lang] = ts.Language(grammar.language())
    return _LANGS[lang]


def _parser(lang: str) -> ts.Parser:
    if lang not in _PARSERS:
        _PARSERS[lang] = ts.Parser(_language(lang))
    return _PARSERS[lang]


def _query(lang: str) -> ts.Query:
    if lang not in _COMPILED:
        _COMPILED[lang] = ts.Query(_language(lang), _QUERIES[lang])
    return _COMPILED[lang]


def _enclosing_defs(name_node: ts.Node, def_types: set[str]) -> list[ts.Node]:
    """Definition nodes enclosing this name's definition, innermost first."""
    out = []
    node = name_node.parent.parent if name_node.parent else None
    while node is not None:
        if node.type in def_types:
            out.append(node)
        node = node.parent
    return out


def parse_source(
    repo_id: str, rel_path: str, source: bytes, lang: str, verified_at: date | None = None
) -> tuple[list[Node], list[Edge]]:
    """Parse one file into nodes + edges."""
    verified_at = verified_at or date.today()
    def_types = set(_DEF_TYPES[lang])

    file_id = make_id(repo_id, rel_path)
    file_node = Node(id=file_id, repo=repo_id, kind="file", name=rel_path, file=rel_path, lang=lang)
    nodes: list[Node] = [file_node]
    edges: list[Edge] = []

    tree = _parser(lang).parse(source)
    captures = ts.QueryCursor(_query(lang)).captures(tree.root_node)

    # First pass: a Node for every definition, keyed by its tree-sitter def node id.
    def_node_to_id: dict[int, str] = {}
    pending: list[tuple[ts.Node, str, int]] = []  # (def_ts_node, qualified_name, line)
    for name_node in captures.get("def", []):
        def_ts = name_node.parent
        name = name_node.text.decode("utf-8", "replace")
        enclosing = _enclosing_defs(name_node, def_types)
        scope = [n.child_by_field_name("name").text.decode("utf-8", "replace")
                 for n in reversed(enclosing) if n.child_by_field_name("name")]
        qualified = ".".join([*scope, name])
        line = name_node.start_point[0] + 1
        kind = _DEF_TYPES[lang][def_ts.type]
        if kind == "function" and enclosing and enclosing[0].type == "class_definition":
            kind = "method"
        nid = make_id(repo_id, rel_path, qualified, str(line))
        def_node_to_id[def_ts.id] = nid
        nodes.append(Node(
            id=nid, repo=repo_id, kind=kind, name=name, qualified_name=f"{rel_path}::{qualified}",
            file=rel_path, line_start=line, line_end=def_ts.end_point[0] + 1, lang=lang,
        ))
        pending.append((def_ts, qualified, line))

    # Second pass: containment edges (parent definition, else the file).
    for def_ts, _qualified, line in pending:
        parent = next((p for p in _enclosing_defs(def_ts.child_by_field_name("name"), def_types)),
                      None)
        parent_id = def_node_to_id.get(parent.id) if parent else file_id
        edges.append(Edge(
            src=parent_id, dst=def_node_to_id[def_ts.id], relation="contains",
            confidence=Confidence.EXTRACTED,
            provenance=Provenance(source_file=rel_path, source_line=line, verified_at=verified_at),
        ))

    # Imports: file -> module node.
    for imp in captures.get("import", []):
        module = imp.text.decode("utf-8", "replace")
        mid = make_id("module", module)
        nodes.append(Node(id=mid, repo=repo_id, kind="module", name=module, lang=lang))
        edges.append(Edge(
            src=file_id, dst=mid, relation="imports", confidence=Confidence.EXTRACTED,
            provenance=Provenance(source_file=rel_path, source_line=imp.start_point[0] + 1,
                                  verified_at=verified_at),
        ))

    return nodes, edges


def index_repo_dir(
    repo_path: str, repo_id: str, head_commit: str | None = None, languages: list[str] | None = None
) -> GraphShard:
    """Walk a repository directory and parse every supported file into a shard."""
    root = Path(repo_path)
    allowed_exts = {ext for ext, lang in LANG_BY_EXT.items()
                    if not languages or lang in languages}
    shard = GraphShard(repo=repo_id, head_commit=head_commit)
    by_id: dict[str, Node] = {}
    n_files = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            ext = os.path.splitext(fn)[1]
            if ext not in allowed_exts:
                continue
            fpath = Path(dirpath) / fn
            rel = str(fpath.relative_to(root))
            try:
                source = fpath.read_bytes()
            except OSError as e:
                log(f"  skip {rel}: {e}")
                continue
            try:
                nodes, edges = parse_source(repo_id, rel, source, LANG_BY_EXT[ext])
            except Exception as e:  # noqa: BLE001 - one bad file must not abort the repo
                log(f"  skip {rel}: parse error: {e}")
                continue
            n_files += 1
            for node in nodes:
                by_id[node.id] = node  # dedupe shared nodes (e.g. modules)
            shard.edges.extend(edges)

    shard.nodes.extend(by_id.values())
    log(f"  parsed {n_files} file(s)")
    return shard
