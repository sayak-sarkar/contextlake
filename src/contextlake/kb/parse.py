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

import fnmatch
import logging
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import tree_sitter as ts

from ..logging_setup import log
from .adr import is_adr_path, parse_adr
from .flow.data import extract_data_refs
from .flow.events import extract_event_flow
from .flow.http import extract_http_flow
from .flow.state import extract_state_flow
from .flow.web import extract_web_flow
from .hcl import parse_hcl
from .ids import make_id
from .manifest import is_manifest, parse_manifest
from .model import SHARED_REPO, Confidence, Edge, Node, Provenance
from .sql import parse_sql
from .store.shards import GraphShard

# Directories never worth walking.
_SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", "dist", "build",
              ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".idea",
              # frontend build output: never source of truth, and (e.g. .next)
              # mirrors app routes as built bundles that pollute the graph. Only
              # the dotted, unambiguous dirs — an undotted "out"/"coverage" could
              # be a legitimate source directory in a non-frontend repo.
              ".next", ".nuxt", ".svelte-kit", ".angular", ".turbo", ".output"}

# Path segments that mark a VENDORED nested repo (an upstream clone carried inside
# the mirror, e.g. the webpack Module Federation examples). A repo whose path
# contains one of these is skipped in discovery: it is not the org's source, and
# it floods the global graph with thousands of upstream-demo nodes. Kept to exact,
# unambiguous segments so a real product dir is never caught. (node_modules is
# already in _SKIP_DIRS, so os.walk never reaches a repo nested under it; this set
# is for markers that are NOT dir-pruned, like module-federation.)
_VENDORED_REPO_MARKERS = {"module-federation"}

# Optional per-repo ignore file (gitignore-flavoured *subset*): one glob per line,
# blank lines and `#` comments skipped. Matched with fnmatch against the POSIX path
# relative to the repo root and against the basename, so `*.lock` ignores by name and
# `vendor/` (or `vendor`) ignores a directory and everything under it. No negation /
# `**` / anchoring semantics — a deliberately small, dependency-free subset.
_IGNORE_FILE = ".contextlakeignore"


def load_ignore_patterns(root: Path) -> list[str]:
    f = root / _IGNORE_FILE
    if not f.is_file():
        return []
    try:
        lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def match_ignore(rel: str, patterns: list[str]) -> bool:
    """True if the POSIX-relative path ``rel`` is covered by any ignore pattern."""
    rel = rel.replace(os.sep, "/")
    base = rel.rsplit("/", 1)[-1]
    for raw in patterns:
        p = raw.rstrip("/")
        if p and (fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(base, p)
                  or fnmatch.fnmatch(rel, f"{p}/*")):
            return True
    return False

LANG_BY_EXT = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
    ".cs": "csharp",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    # ".h" is ambiguous between C and C++ headers, but real-world ".h" files
    # overwhelmingly use the common C++ header/.cpp split (see
    # test_index_repo_dir_resolves_out_of_line_method_across_files) -- the
    # tree-sitter-cpp grammar is a near-superset of C, so parsing ".h" as "cpp"
    # matches-or-improves extraction on plain C headers too in 196/200 real
    # ".h" files sampled from the repo fleet (e.g. jni.h's `#ifdef __cplusplus`
    # wrapper classes are only recognized under the cpp grammar; genuinely
    # C-only headers like jvmdi.h extract identically either way). The
    # remaining 4/200 traced to a separate, pre-existing gap in cpp's handling
    # of `template<>` full-specialization structs, not a regression on plain-C
    # content.
    ".h": "cpp",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".c++": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".scala": "scala", ".sc": "scala",
    ".kt": "kotlin", ".kts": "kotlin",
}

# HCL/Terraform files use a bespoke extraction path (kb/hcl.py), not the OO
# capture model, so they are matched separately from LANG_BY_EXT.
HCL_EXTS = {".tf"}

# SQL DDL uses a regex extractor (kb/sql.py), matched separately from LANG_BY_EXT.
SQL_EXTS = {".sql"}

# A code file larger than this is skipped (and logged). Hand-written source is
# essentially never this big — anything that large is a data blob or vendored
# bundle. Raise [kb] max_file_bytes to index them anyway.
DEFAULT_MAX_FILE_BYTES = 5 * 1024 * 1024

# Generated/derived code (machine-emitted from real sources): graph noise and a
# real time sink in legacy repos. Skipped by default; the source it is generated
# FROM is still indexed, so this is not a knowledge gap. Set [kb] skip_generated
# = false to index it anyway.
_GENERATED_NAME_SUFFIXES = (
    ".designer.cs", ".generated.cs", ".g.cs", ".g.i.cs",
    ".min.js", ".min.mjs", ".min.cjs", ".min.css", ".bundle.js",
)
_GENERATED_BASENAMES = {"assemblyinfo.cs"}
_GENERATED_HEADER_MARKERS = (
    b"<auto-generated", b"@generated", b"do not edit", b"code generated by",
)


def _is_generated_name(filename: str) -> bool:
    low = filename.lower()
    return low.endswith(_GENERATED_NAME_SUFFIXES) or low in _GENERATED_BASENAMES


def _has_generated_header(source: bytes) -> bool:
    """True if a file's first bytes carry a recognised 'this is generated' marker."""
    return any(m in source[:2048].lower() for m in _GENERATED_HEADER_MARKERS)

# Bumped whenever a change to def/containment/resolution logic changes graph
# output for existing repos -- doctor's stale-shard check compares a shard's
# stamp against this to know when a re-index is worth recommending.
#
# "1" was the first version stamped. "2" is _sorted_captures: node and edge
# CONTENT is byte-identical to "1", but their ORDER in the shard changed, so a
# "1" shard's bytes will never again be reproduced by re-indexing. A bump is
# language-agnostic to everything that consumes it: doctor flags any shard at an
# older version, and `kb index` re-indexes it rather than calling it unchanged.
# "3" is language-aware name resolution (see _LANG_FAMILY): a call/inherits
# reference no longer resolves to a same-named definition in an unrelated
# language, and AMBIGUOUS edges record how many candidates they were one of.
PARSER_VERSION = "3"

# tree-sitter node types that introduce a named definition, per language.
_DEF_TYPES = {
    "python": {"class_definition": "class", "function_definition": "function"},
    "javascript": {
        "class_declaration": "class", "function_declaration": "function",
        "method_definition": "method",
    },
    "typescript": {
        "class_declaration": "class", "function_declaration": "function",
        "method_definition": "method", "interface_declaration": "interface",
        "enum_declaration": "enum",
    },
    "csharp": {
        "class_declaration": "class", "interface_declaration": "interface",
        "struct_declaration": "struct", "method_declaration": "method",
    },
    # Go has no classes; a `type_spec` names a struct/interface/alias — all indexed
    # as a "struct"-kind type node (the field that distinguishes them is not the node
    # type the kind is keyed on). Methods are top-level with a receiver.
    "go": {
        "function_declaration": "function", "method_declaration": "method",
        "type_spec": "struct",
    },
    "java": {
        "class_declaration": "class", "interface_declaration": "interface",
        "enum_declaration": "enum", "record_declaration": "class",
        "method_declaration": "method", "constructor_declaration": "method",
    },
    # C/C++: a function's name sits under a *_declarator, so parse_source normalizes
    # the def node up to the enclosing function_definition (see _def_node) — that is
    # the type keyed here, so call attribution and containment resolve correctly.
    "c": {
        "function_definition": "function", "struct_specifier": "struct",
        "enum_specifier": "enum", "union_specifier": "struct",
    },
    "cpp": {
        "function_definition": "function", "class_specifier": "class",
        "struct_specifier": "struct", "enum_specifier": "enum",
        "union_specifier": "struct", "namespace_definition": "namespace",
    },
    "rust": {
        "function_item": "function", "struct_item": "struct",
        "enum_item": "enum", "trait_item": "interface",
    },
    "ruby": {   # Ruby has no free functions — every `def` is a method
        "class": "class", "module": "class",
        "method": "method", "singleton_method": "method",
    },
    "php": {
        "class_declaration": "class", "interface_declaration": "interface",
        "trait_declaration": "class", "enum_declaration": "enum",
        "function_definition": "function", "method_declaration": "method",
    },
    "scala": {
        "class_definition": "class", "object_definition": "class",
        "trait_definition": "interface", "function_definition": "function",
    },
    "kotlin": {
        "class_declaration": "class", "object_declaration": "class",
        "function_declaration": "function",
    },
}
_DEF_TYPES["tsx"] = _DEF_TYPES["typescript"]

# Queries capture definition *name* identifiers (@def) and import targets (@import).
_QUERIES = {
    "python": """
        (class_definition name: (identifier) @def)
        (function_definition name: (identifier) @def)
        (import_statement (dotted_name) @import)
        (import_from_statement module_name: (dotted_name) @import)
        (call function: (identifier) @call)
        (call function: (attribute attribute: (identifier) @call))
        (class_definition superclasses: (argument_list [(identifier) (attribute)] @base))
    """,
    "javascript": """
        (class_declaration name: (identifier) @def)
        (function_declaration name: (identifier) @def)
        (method_definition name: (property_identifier) @def)
        (import_statement source: (string) @import)
        (call_expression function: (identifier) @call)
        (call_expression function: (member_expression property: (property_identifier) @call))
        (class_heritage (identifier) @base)
    """,
    "typescript": """
        (class_declaration name: (type_identifier) @def)
        (function_declaration name: (identifier) @def)
        (method_definition name: (property_identifier) @def)
        (interface_declaration name: (type_identifier) @def)
        (enum_declaration name: (identifier) @def)
        (import_statement source: (string) @import)
        (call_expression function: (identifier) @call)
        (call_expression function: (member_expression property: (property_identifier) @call))
        (extends_clause [(identifier) (type_identifier)] @base)
        (implements_clause [(type_identifier) (identifier)] @base)
        (extends_type_clause [(type_identifier) (identifier)] @base)
    """,
    "csharp": """
        (class_declaration name: (identifier) @def)
        (interface_declaration name: (identifier) @def)
        (struct_declaration name: (identifier) @def)
        (method_declaration name: (identifier) @def)
        (using_directive (identifier) @import)
        (using_directive (qualified_name) @import)
        (invocation_expression function: (identifier) @call)
        (invocation_expression function: (member_access_expression name: (identifier) @call))
        (base_list [(identifier) (generic_name)] @base)
    """,
    "go": """
        (function_declaration name: (identifier) @def)
        (method_declaration name: (field_identifier) @def)
        (type_spec name: (type_identifier) @def)
        (import_spec path: (interpreted_string_literal) @import)
        (call_expression function: (identifier) @call)
        (call_expression function: (selector_expression field: (field_identifier) @call))
    """,
    "java": """
        (class_declaration name: (identifier) @def)
        (interface_declaration name: (identifier) @def)
        (enum_declaration name: (identifier) @def)
        (record_declaration name: (identifier) @def)
        (method_declaration name: (identifier) @def)
        (constructor_declaration name: (identifier) @def)
        (import_declaration (scoped_identifier) @import)
        (method_invocation name: (identifier) @call)
        (superclass (type_identifier) @base)
        (super_interfaces (type_list (type_identifier) @base))
        (extends_interfaces (type_list (type_identifier) @base))
    """,
    "c": """
        (function_definition declarator: (function_declarator declarator: (identifier) @def))
        (function_definition declarator: (pointer_declarator declarator:
            (function_declarator declarator: (identifier) @def)))
        (struct_specifier name: (type_identifier) @def)
        (enum_specifier name: (type_identifier) @def)
        (union_specifier name: (type_identifier) @def)
        (preproc_include path: (string_literal) @import)
        (preproc_include path: (system_lib_string) @import)
        (call_expression function: (identifier) @call)
    """,
    "cpp": """
        (class_specifier name: (type_identifier) @def)
        (struct_specifier name: (type_identifier) @def)
        (enum_specifier name: (type_identifier) @def)
        (function_definition declarator: (function_declarator declarator: (identifier) @def))
        (function_definition declarator: (function_declarator declarator: (field_identifier) @def))
        (function_definition declarator: (function_declarator declarator:
            (qualified_identifier) @def_qi))
        (namespace_definition name: (namespace_identifier) @def)
        (preproc_include path: (string_literal) @import)
        (preproc_include path: (system_lib_string) @import)
        (call_expression function: (identifier) @call)
        (call_expression function: (field_expression field: (field_identifier) @call))
        (base_class_clause (type_identifier) @base)
    """,
    "rust": """
        (function_item name: (identifier) @def)
        (struct_item name: (type_identifier) @def)
        (enum_item name: (type_identifier) @def)
        (trait_item name: (type_identifier) @def)
        (use_declaration argument: (scoped_identifier) @import)
        (use_declaration argument: (identifier) @import)
        (call_expression function: (identifier) @call)
        (call_expression function: (field_expression field: (field_identifier) @call))
        (call_expression function: (scoped_identifier name: (identifier) @call))
    """,
    "ruby": """
        (class name: (constant) @def)
        (module name: (constant) @def)
        (method name: (identifier) @def)
        (singleton_method name: (identifier) @def)
        (call method: (identifier) @call)
        (superclass (constant) @base)
    """,
    "php": """
        (class_declaration name: (name) @def)
        (interface_declaration name: (name) @def)
        (trait_declaration name: (name) @def)
        (enum_declaration name: (name) @def)
        (function_definition name: (name) @def)
        (method_declaration name: (name) @def)
        (namespace_use_clause (qualified_name) @import)
        (function_call_expression function: (name) @call)
        (member_call_expression name: (name) @call)
        (scoped_call_expression name: (name) @call)
        (base_clause (name) @base)
        (class_interface_clause (name) @base)
    """,
    "scala": """
        (class_definition name: (identifier) @def)
        (object_definition name: (identifier) @def)
        (trait_definition name: (identifier) @def)
        (function_definition name: (identifier) @def)
        (call_expression (identifier) @call)
        (extends_clause (type_identifier) @base)
    """,
    "kotlin": """
        (class_declaration name: (identifier) @def)
        (object_declaration name: (identifier) @def)
        (function_declaration name: (identifier) @def)
        (import (qualified_identifier) @import)
        (call_expression (identifier) @call)
        (delegation_specifier (constructor_invocation (user_type) @base))
        (delegation_specifier (user_type) @base)
    """,
}
_QUERIES["tsx"] = _QUERIES["typescript"]

_LANGS: dict[str, ts.Language] = {}
_PARSERS: dict[str, ts.Parser] = {}
_COMPILED: dict[str, ts.Query] = {}


def _language(lang: str) -> ts.Language:
    if lang not in _LANGS:
        if lang == "python":
            import tree_sitter_python as g
            fn = g.language
        elif lang == "javascript":
            import tree_sitter_javascript as g
            fn = g.language
        elif lang == "typescript":
            import tree_sitter_typescript as g
            fn = g.language_typescript
        elif lang == "tsx":
            import tree_sitter_typescript as g
            fn = g.language_tsx
        elif lang == "csharp":
            import tree_sitter_c_sharp as g
            fn = g.language
        elif lang == "go":
            import tree_sitter_go as g
            fn = g.language
        elif lang == "java":
            import tree_sitter_java as g
            fn = g.language
        elif lang == "c":
            import tree_sitter_c as g
            fn = g.language
        elif lang == "cpp":
            import tree_sitter_cpp as g
            fn = g.language
        elif lang == "rust":
            import tree_sitter_rust as g
            fn = g.language
        elif lang == "ruby":
            import tree_sitter_ruby as g
            fn = g.language
        elif lang == "php":
            import tree_sitter_php as g
            fn = g.language_php
        elif lang == "scala":
            import tree_sitter_scala as g
            fn = g.language
        elif lang == "kotlin":
            import tree_sitter_kotlin as g
            fn = g.language
        else:
            raise ValueError(f"unsupported language: {lang}")
        _LANGS[lang] = ts.Language(fn())
    return _LANGS[lang]


def _parser(lang: str) -> ts.Parser:
    if lang not in _PARSERS:
        _PARSERS[lang] = ts.Parser(_language(lang))
    return _PARSERS[lang]


def _query(lang: str) -> ts.Query:
    if lang not in _COMPILED:
        _COMPILED[lang] = ts.Query(_language(lang), _QUERIES[lang])
    return _COMPILED[lang]


def _leading_doc(def_ts: ts.Node) -> str | None:
    """A definition's leading doc-comment — JSDoc ``/** */`` or C# ``///`` XML docs.

    Restricted to *doc-comment* syntax (not plain ``//`` / ``/*``) so a stray code
    comment above a function isn't mistaken for documentation. Collects the contiguous
    run of doc-comments immediately above the def, strips the comment + XML syntax.
    """
    raws: list[str] = []
    node = def_ts.prev_sibling
    last_line = def_ts.start_point[0]
    while node is not None and node.type == "comment" and last_line - node.end_point[0] <= 1:
        raw = node.text.decode("utf-8", "replace").strip()
        if not (raw.startswith("/**") or raw.startswith("///")):
            break  # a plain comment ends the doc run
        raws.append(raw)
        last_line = node.start_point[0]
        node = node.prev_sibling
    if not raws:
        return None
    parts: list[str] = []
    for raw in reversed(raws):
        if raw.startswith("/**"):
            inner = raw[3:-2] if raw.endswith("*/") else raw[3:]
            parts += [ln.strip().lstrip("*").strip() for ln in inner.splitlines()]
        else:  # /// XML doc line
            parts.append(raw.lstrip("/").strip())
    doc = re.sub(r"<[^>]+>", " ", " ".join(p for p in parts if p))
    doc = " ".join(doc.split())  # collapse whitespace left by tag/comment stripping
    return doc[:1000] or None


def _doc_sig(def_ts: ts.Node, lang: str) -> dict:
    """Capture a definition's signature (parameters) and docstring as node attrs.

    Additive, best-effort — richer graph facts (shown in the UI, wiki, and
    ``get_repo_brief``) and the groundwork for body-aware embeddings. Signature is
    captured across languages (py/js/ts/c#); the docstring comes from Python's
    first-statement string or, elsewhere, a JSDoc/C#-XML leading comment. Never raises.
    """
    out: dict = {}
    try:
        # signature: the parameter list — generalizes across languages (field name
        # is "parameters" in py/js/ts, "parameter_list" in c#); graceful if absent.
        params = (def_ts.child_by_field_name("parameters")
                  or def_ts.child_by_field_name("parameter_list"))
        if params is not None:
            sig = params.text.decode("utf-8", "replace").strip()
            if sig:
                out["signature"] = sig[:300]
        if lang == "python":
            body = def_ts.child_by_field_name("body")
            if body is not None and body.named_child_count:
                first = body.named_child(0)
                if first.type == "expression_statement" and first.named_child_count:
                    lit = first.named_child(0)
                    if lit.type == "string":
                        doc = lit.text.decode("utf-8", "replace").lstrip("rRbBuUfF")
                        doc = doc.strip().strip("\"'").strip()
                        if doc:
                            out["doc"] = doc[:1000]
        else:
            doc = _leading_doc(def_ts)
            if doc:
                out["doc"] = doc
    except Exception:  # noqa: BLE001 - capture is best-effort, never blocks indexing
        return out
    return out


def _def_node(name_node: ts.Node, def_types: set[str]) -> ts.Node | None:
    """The definition node a captured ``@def`` name belongs to.

    Usually ``name_node.parent`` (the name is a direct child of the def node). In
    C/C++ the name sits under one or more ``*_declarator`` wrappers, so climb to the
    enclosing def-typed node (the ``function_definition``) — that is what encloses the
    body, so call attribution and containment resolve to it.
    """
    node = name_node.parent
    while node is not None and node.type not in def_types:
        node = node.parent
    return node


def _enclosing_defs(name_node: ts.Node, def_types: set[str]) -> list[ts.Node]:
    """Definition nodes enclosing this name's definition, innermost first (the name's
    own definition node is excluded)."""
    own = _def_node(name_node, def_types)
    out = []
    node = own.parent if own is not None else (
        name_node.parent.parent if name_node.parent else None)
    while node is not None:
        if node.type in def_types:
            out.append(node)
        node = node.parent
    return out


def _conditional_root(node: ts.Node) -> int | None:
    """The ``.id`` of the nearest enclosing ``#if``/``#ifdef`` construct, or ``None``.

    Two definitions sharing the same conditional root are candidates for being
    branch-twins of the same preprocessor conditional (tree-sitter parses both
    branches unconditionally, since it never evaluates the preprocessor) --
    matching root is a necessary, but NOT sufficient, cheap pre-filter: an
    ``#ifndef FOO_H`` header guard is *itself* a ``preproc_ifdef`` with no
    ``#else`` at all, so every definition in a guarded header shares one root.
    ``_conditional_branch`` below is what actually confirms two defs sit in
    different branches of that root, which the root check alone cannot.
    """
    n = node.parent
    while n is not None:
        if n.type in ("preproc_if", "preproc_ifdef"):
            return n.id
        n = n.parent
    return None


def _conditional_branch(node: ts.Node) -> int | None:
    """Which branch of its enclosing conditional ``node`` sits in.

    Returns the ``.id`` of the nearest ``preproc_else``/``preproc_elif``
    ancestor encountered while climbing from ``node`` up to (but not including)
    its ``_conditional_root`` -- or ``None`` if ``node`` sits directly in the
    primary ``#if``/``#ifdef`` branch, with no intervening ``preproc_else``/
    ``preproc_elif``. Two definitions are only genuine ``#ifdef``/``#else`` (or
    ``#elif``) twins if they share a root AND have DIFFERENT branch markers --
    two defs directly under an ``#ifndef`` guard (no ``#else``) both return
    ``None`` here, correctly refusing to be treated as twins just because they
    share that guard's root.
    """
    n = node.parent
    while n is not None and n.type not in ("preproc_if", "preproc_ifdef"):
        if n.type in ("preproc_else", "preproc_elif"):
            return n.id
        n = n.parent
    return None


def _signature_text(def_ts: ts.Node) -> str:
    """A signature discriminator read directly off the definition node.

    Deliberately does NOT reuse ``_doc_sig``'s ``attrs["signature"]``: that field
    only looks at a ``parameters``/``parameter_list`` *field on def_ts itself*,
    which a C/C++ ``function_definition`` never has -- its parameter list is
    nested inside a ``function_declarator``, reached via the ``declarator``
    field, itself possibly wrapped in one or more pointer/reference declarators
    (e.g. ``int* Foo(int x)``). Relying on the attrs field left ``sig`` empty for
    every C/C++ definition, silently disabling overload discrimination below.

    For C/C++, this returns the *entire* ``function_declarator``'s text, not
    just its parameter list: tree-sitter attaches trailing cv-qualifiers
    (``const``) and ref-qualifiers (``&``/``&&``) as siblings of the parameter
    list INSIDE the same declarator, not inside the parameter list itself --
    keying on the parameter list alone let ``int at(int) const`` and
    non-const ``int at(int)`` collapse together as if they were one signature.
    For py/js/ts/c#, which expose ``parameters``/``parameter_list`` as a direct
    field on the def node, that field's text is used as before.
    """
    direct = def_ts.child_by_field_name("parameters") or def_ts.child_by_field_name(
        "parameter_list")
    if direct is not None:
        return direct.text.decode("utf-8", "replace").strip()
    node = def_ts.child_by_field_name("declarator")
    seen = 0
    while node is not None and seen < 10:
        if node.type == "function_declarator":
            return node.text.decode("utf-8", "replace").strip()
        node = node.child_by_field_name("declarator")
        seen += 1
    return ""


def _dedupe_preprocessor_twins(
    nodes: list[Node], pending: list[tuple[ts.Node, str, int, str]],
    def_node_to_id: dict[int, str],
) -> None:
    """Collapse same-name, same-signature, DIFFERENT-branch duplicate definitions.

    A genuine overload (``void Setup(); void Setup(int);``) has a different
    signature (see ``_signature_text``) and is never touched. Two defs merge
    only when they share an identical (qualified name, signature) AND the same
    conditional root (``_conditional_root``) AND sit in two DIFFERENT branches
    of that conditional (``_conditional_branch``) -- the exact shape of an
    ``#ifdef``/``#else``/``#elif`` twin. Root alone is not enough: an ``#ifndef``
    header guard is itself a conditional root with no ``#else``, so every
    definition in a guarded header would otherwise share one root, making a
    root-only check a no-op for the entire file (and silently deleting one of
    two genuine overloads that happen to share a root, e.g. ``const``/non-const
    pairs, if their signatures also happened to look alike). Two unrelated
    ``#ifdef`` blocks elsewhere in the file that happen to define the same
    name/signature have different roots and are never conflated either. Within
    one (name, signature, root) group, if any branch holds more than one
    candidate, or fewer than two distinct branches are represented, nothing in
    that group is merged -- leaving distinct nodes uncollapsed is always safe;
    silently merging when the branch shape isn't a clean twin is not.

    The first (lowest source line) surviving candidate is kept; the others'
    tree-sitter ids are remapped to the same graph node id so later containment/
    call attribution (both keyed by ``def_ts.id`` via ``def_node_to_id``) still
    resolves correctly. Because both branches' bodies genuinely called
    something, the surviving node absorbs outgoing calls from EVERY merged
    branch (e.g. both a ``FastInit()`` call from the ``#ifdef`` body and a
    ``SlowInit()`` call from the ``#else`` body attribute to the one kept
    node) -- there is no way to know which branch really compiles, so this is
    the only coherent choice.

    The final filter step matches dropped ``pending`` entries to ``nodes`` by
    node id (``pending``'s 4th element), never by list position -- ``nodes``
    starts with a leading file node before this loop ever runs, so
    ``pending[i]`` and ``nodes[i]`` are never the same definition.
    """
    groups: dict[tuple, list[tuple[ts.Node, str, int, str]]] = {}
    for entry in pending:
        def_ts, qualified, _line, _nid = entry
        root = _conditional_root(def_ts)
        if root is None:
            continue
        sig = _signature_text(def_ts)
        groups.setdefault((qualified, sig, root), []).append(entry)

    drop_ids: set[str] = set()
    for entries in groups.values():
        if len(entries) < 2:
            continue
        by_branch: dict[int | None, list[tuple[ts.Node, str, int, str]]] = {}
        for entry in entries:
            by_branch.setdefault(_conditional_branch(entry[0]), []).append(entry)
        if len(by_branch) < 2 or any(len(v) > 1 for v in by_branch.values()):
            continue  # not a clean one-def-per-branch twin shape -- leave as-is
        representatives = [v[0] for v in by_branch.values()]
        representatives.sort(key=lambda e: e[2])  # by line -- keep the first
        keeper_id = representatives[0][3]
        for def_ts, _qualified, _line, nid in representatives[1:]:
            def_node_to_id[def_ts.id] = keeper_id
            drop_ids.add(nid)

    if drop_ids:
        nodes[:] = [n for n in nodes if n.id not in drop_ids]
        pending[:] = [p for p in pending if p[3] not in drop_ids]


def _qualified_chain(qi_node: ts.Node) -> tuple[list[str], ts.Node]:
    """Segments + innermost name node of a (possibly multi-level) qualified_identifier.

    tree-sitter-cpp right-nests a 3+-segment qualified name *under the ``name:``
    field*: ``A::B::C`` parses as ``qualified_identifier(scope: A, name:
    qualified_identifier(scope: B, name: C))``. A query pattern anchored on a fixed
    field path only ever sees the outermost node, so this walks the chain in Python
    to any depth instead, returning e.g. (["A", "B"], <the C identifier node>).
    """
    segments: list[str] = []
    node = qi_node
    while node.type == "qualified_identifier":
        scope = node.child_by_field_name("scope")
        name = node.child_by_field_name("name")
        if scope is not None and scope.type in (
            "namespace_identifier", "type_identifier", "identifier"
        ):
            segments.append(scope.text.decode("utf-8", "replace"))
        if name is None:
            return segments, node  # defensive; not expected from valid C++
        if name.type == "qualified_identifier":
            node = name
            continue
        return segments, name
    return segments, node


def _sorted_captures(captures: dict[str, list[ts.Node]]) -> dict[str, list[ts.Node]]:
    """Put every capture list into source order, so extraction is reproducible.

    tree-sitter's ``QueryCursor.captures()`` (0.26.0) returns each capture list in
    an order that varies run to run and even within a single process. Nothing
    downstream *depends* on capture order for correctness -- the node and edge sets
    are identical either way -- but ``parse_source`` appends in iteration order, so
    the shard's node/edge sequence moved every run: six consecutive indexes of one
    unchanged fixture produced six distinct shard byte-strings, which silently
    falsified ``archive_shard``'s "re-indexed at the same commit overwrites
    identically" invariant and made content-addressing a shard impossible.

    Sorting here, at the single point captures enter the extractor, fixes all of it
    at once. ``(start_byte, end_byte, type)`` is a total order on real capture lists
    (verified: no collisions across the golden fixture's languages); ``Node.id`` is
    pointer-derived and would reintroduce the very entropy this removes, so it must
    never be used as a tiebreak.
    """
    return {name: sorted(nodes, key=lambda n: (n.start_byte, n.end_byte, n.type))
            for name, nodes in captures.items()}


def parse_source(
    repo_id: str, rel_path: str, source: bytes, lang: str, verified_at: date | None = None
) -> tuple[list[Node], list[Edge], list[tuple[str, str, str, int]],
           list[tuple[str, str, str, int]]]:
    """Parse one file into (nodes, edges, calls, inherits).

    ``calls`` and ``inherits`` are unresolved (src_id, target_name, file, line)
    tuples — the target definition (a callee, or a base class/interface) is
    resolved repo-wide by :func:`index_repo_dir`, since it may live in another file.
    """
    verified_at = verified_at or date.today()
    def_types = set(_DEF_TYPES[lang])

    file_id = make_id(repo_id, rel_path)
    file_node = Node(id=file_id, repo=repo_id, kind="file", name=rel_path, file=rel_path, lang=lang)
    nodes: list[Node] = [file_node]
    edges: list[Edge] = []

    tree = _parser(lang).parse(source)
    captures = _sorted_captures(ts.QueryCursor(_query(lang)).captures(tree.root_node))

    # First pass: a Node for every definition, keyed by its tree-sitter def node id.
    def_node_to_id: dict[int, str] = {}
    pending: list[tuple[ts.Node, str, int, str]] = []  # (def_ts_node, qualified_name, line, nid)
    def_worklist: list[tuple[ts.Node, ts.Node, list[str]]] = []  # (def_ts, name_node, extra_scope)
    for name_node in captures.get("def", []):
        def_ts = _def_node(name_node, def_types)
        if def_ts is not None:
            def_worklist.append((def_ts, name_node, []))
    for qi_node in captures.get("def_qi", []):
        extra_scope, name_node = _qualified_chain(qi_node)
        def_ts = _def_node(qi_node, def_types)
        if def_ts is not None:
            def_worklist.append((def_ts, name_node, extra_scope))

    for def_ts, name_node, extra_scope in def_worklist:
        name = name_node.text.decode("utf-8", "replace")
        enclosing = _enclosing_defs(name_node, def_types)
        scope = [n.child_by_field_name("name").text.decode("utf-8", "replace")
                 for n in reversed(enclosing) if n.child_by_field_name("name")]
        full_scope = scope + extra_scope if extra_scope else scope
        qualified = ".".join([*full_scope, name])
        line = name_node.start_point[0] + 1
        kind = _DEF_TYPES[lang][def_ts.type]
        # A forward declaration ("class Widget;", the standard way to break header
        # include cycles in C/C++) is a body-less class_specifier/struct_specifier/
        # enum_specifier/union_specifier -- not a definition in the graph-node sense.
        # Skipping it avoids cluttering the graph with spurious body-less class
        # nodes, and (crucially for _resolve_pending_methods) avoids a repo with
        # both a forward decl and the real definition producing two same-named
        # "class"/"struct" candidates, which silently disables out-of-line-method
        # resolution (len(candidates) != 1 bails out with no error).
        if (lang in ("c", "cpp") and kind in ("class", "struct", "enum")
                and def_ts.child_by_field_name("body") is None):
            continue
        if kind == "function" and enclosing and _DEF_TYPES[lang].get(enclosing[0].type) == "class":
            kind = "method"
        nid = make_id(repo_id, rel_path, qualified, str(line))
        def_node_to_id[def_ts.id] = nid
        node_attrs = _doc_sig(def_ts, lang)
        if extra_scope:
            node_attrs["_pending_method_of"] = extra_scope
        nodes.append(Node(
            id=nid, repo=repo_id, kind=kind, name=name, qualified_name=f"{rel_path}::{qualified}",
            file=rel_path, line_start=line, line_end=def_ts.end_point[0] + 1, lang=lang,
            attrs=node_attrs,
        ))
        pending.append((def_ts, qualified, line, nid))

    _dedupe_preprocessor_twins(nodes, pending, def_node_to_id)

    # Second pass: containment edges (parent definition, else the file). The parent is
    # the nearest enclosing def-typed node above this one — computed from def_ts itself
    # (a C/C++ function_definition has no "name" field to walk from).
    for def_ts, _qualified, line, _nid in pending:
        parent = def_ts.parent
        while parent is not None and parent.type not in def_types:
            parent = parent.parent
        # A structural def-typed ancestor doesn't guarantee it was *registered* in
        # def_node_to_id -- the first pass skips a def whose declarator has no
        # enclosing def node (line ~536), which is common in C/C++ under macros,
        # `extern "C"` blocks, and complex templates. Without this fallback,
        # `.get(parent.id)` silently returns None, producing Edge(src=None) --
        # a pydantic validation error that aborts extraction of the *entire file*,
        # not just this one containment edge.
        parent_id = def_node_to_id.get(parent.id, file_id) if parent else file_id
        edges.append(Edge(
            src=parent_id, dst=def_node_to_id[def_ts.id], relation="contains",
            confidence=Confidence.EXTRACTED,
            provenance=Provenance(source_file=rel_path, source_line=line, verified_at=verified_at),
        ))

    # Imports: file -> module node.
    for imp in captures.get("import", []):
        module = imp.text.decode("utf-8", "replace").strip().strip("'\"")
        mid = make_id("module", module)
        nodes.append(Node(id=mid, repo=SHARED_REPO, kind="module", name=module, lang=lang))
        edges.append(Edge(
            src=file_id, dst=mid, relation="imports", confidence=Confidence.EXTRACTED,
            provenance=Provenance(source_file=rel_path, source_line=imp.start_point[0] + 1,
                                  verified_at=verified_at),
        ))

    # Calls: (caller_node_id, callee_name, file, line) — resolved repo-wide later.
    calls: list[tuple[str, str, str, int]] = []
    for call_node in captures.get("call", []):
        callee = call_node.text.decode("utf-8", "replace")
        caller_id = file_id
        node = call_node.parent
        while node is not None:
            if node.type in def_types:
                caller_id = def_node_to_id.get(node.id, file_id)
                break
            node = node.parent
        calls.append((caller_id, callee, rel_path, call_node.start_point[0] + 1))

    # Inherits: (subclass_id, base_name, file, line) — the base may be defined in
    # another file, so resolve repo-wide later (like calls). A dotted/qualified base
    # (django.views.View) keeps only its last segment, matching how defs are named.
    # A generic base (Comparable<Order> in Kotlin, IComparable<Order> in C#'s
    # generic_name capture) drops the type-argument suffix first, so only the bare
    # base name remains; both strips are no-ops for the other languages, whose @base
    # capture never contains "<" or ".".
    inherits: list[tuple[str, str, str, int]] = []
    for base_node in captures.get("base", []):
        base = base_node.text.decode("utf-8", "replace").split("<")[0].split(".")[-1].strip()
        sub_id = None
        node = base_node.parent
        while node is not None:
            if node.type in def_types and node.id in def_node_to_id:
                sub_id = def_node_to_id[node.id]
                break
            node = node.parent
        if sub_id and base:
            inherits.append((sub_id, base, rel_path, base_node.start_point[0] + 1))

    return nodes, edges, calls, inherits


# Extraction "kinds" a walked file can be dispatched as, in the precedence the
# dispatch has always used: HCL, SQL and ADR are matched by extension/path before
# the code table, and a manifest is the fallback for a file that matched nothing
# else. The sets are disjoint in practice (no manifest filename carries a code
# extension), so precedence only ever settles hypotheticals -- but it is written
# out rather than left implicit because the oversize and generated-file checks
# below key off it.
_HCL, _SQL, _ADR, _CODE, _MANIFEST = "hcl", "sql", "adr", "code", "manifest"


@dataclass
class WalkCounts:
    """Per-repo tallies reported in the one summary line ``index_repo_dir`` logs.

    Held by the caller rather than returned by the walker so the summary can be
    logged *after* reference resolution, exactly where it always was, without
    depending on generator return values.
    """

    files: int = 0
    generated: int = 0
    oversize: int = 0
    ignored: int = 0


@dataclass(frozen=True)
class SourceFile:
    """One file the walk selected, already read, with its dispatch kind resolved."""

    rel: str
    source: bytes
    kind: str
    lang: str  # only meaningful for kind == _CODE


@dataclass
class RefCollector:
    """Repo-wide unresolved ``(src_id, target_name, file, line)`` references.

    Every extractor emits references whose target may live in another file, so
    they can only be resolved once the whole repo is walked. One structure keeps
    the six reference streams -- and, crucially, the fixed order they are resolved
    in -- in a single place; edge order in the shard depends on that order.
    """

    calls: list[tuple[str, str, str, int]] = field(default_factory=list)
    inherits: list[tuple[str, str, str, int]] = field(default_factory=list)
    hcl: list[tuple[str, str, str, int]] = field(default_factory=list)
    sql: list[tuple[str, str, str, int]] = field(default_factory=list)
    data_reads: list[tuple[str, str, str, int]] = field(default_factory=list)
    data_writes: list[tuple[str, str, str, int]] = field(default_factory=list)

    def resolved_edges(self, by_id: dict[str, Node]) -> list[Edge]:
        # Target-kind sets are module-level names defined further down the file,
        # so they are read here at call time rather than bound at class creation.
        # The trailing flag is same-language resolution: a call/inheritance must
        # stay inside one language family, while the HCL/SQL streams are
        # cross-domain by design (code reads a table) -- see _resolve_name_refs.
        streams = (
            (self.calls, "calls", _CALLABLE_KINDS, True),
            (self.inherits, "inherits", _INHERITABLE_KINDS, True),
            (self.hcl, "depends_on", _HCL_KINDS, False),
            (self.sql, "references", _SQL_KINDS, False),
            (self.data_reads, "reads", _SQL_KINDS, False),
            (self.data_writes, "writes", _SQL_KINDS, False),
        )
        edges: list[Edge] = []
        for refs, relation, target_kinds, same_language in streams:
            edges.extend(_resolve_name_refs(
                refs, by_id, relation=relation, target_kinds=target_kinds,
                same_language=same_language))
        return edges


def _file_kind(fn: str, ext: str, rel: str, *, allowed_exts: set[str],
               index_hcl: bool, index_sql: bool) -> str | None:
    """Which extractor owns this file, or None if nothing indexes it.

    ``languages`` gates code, HCL and SQL only: a manifest or an ADR is never
    language-specific, so filtering to ``--languages python`` must not hide the
    repo's package manifests or decision records.
    """
    if index_hcl and ext in HCL_EXTS:
        return _HCL
    if index_sql and ext in SQL_EXTS:
        return _SQL
    if ext == ".md" and is_adr_path(rel):
        return _ADR
    if ext in allowed_exts:
        return _CODE
    return _MANIFEST if is_manifest(fn) else None


def is_indexable_name(fn: str, rel: str) -> bool:
    """Whether the indexer would pick this file up at all, from its name alone.

    The same :func:`_file_kind` decision :func:`_walk_source_files` makes, minus
    every gate that needs I/O (the per-repo ignore file, the generated-header
    probe, the size limit). Name-only is the point: it costs one ``splitext`` per
    file and never a ``stat`` or a read, so a caller can afford to walk a whole
    tree with it. ``kb index``'s bundling diagnosis does exactly that, and it
    needs the right order of magnitude rather than an exact parse count.
    """
    ext = os.path.splitext(fn)[1]
    return _file_kind(fn, ext, rel, allowed_exts=set(LANG_BY_EXT),
                      index_hcl=True, index_sql=True) is not None


def _ignored(rel: str, patterns: list[str]) -> bool:
    return bool(patterns) and match_ignore(rel, patterns)


def _oversize(fpath: Path, kind: str, max_file_bytes: int) -> bool:
    """Whether a file exceeds the size limit, decided by stat alone (never a read).

    Manifests are exempt: the limit exists to keep data blobs and vendored
    bundles out of the *code* graph, and a manifest is small by construction.
    An unstattable path is not reported as oversize — the read below produces
    the one, accurate skip message for it instead.
    """
    if kind == _MANIFEST:
        return False
    try:
        return fpath.stat().st_size > max_file_bytes
    except OSError:
        return False


def _generated(kind: str, skip_generated: bool, probe: Callable[[], bool]) -> bool:
    """Whether generated/derived code should be skipped, deferring the (possibly
    expensive) ``probe`` until the cheap guards have passed."""
    return skip_generated and kind == _CODE and probe()


def _select_file(
    fpath: Path, rel: str, fn: str, *, allowed_exts: set[str], index_hcl: bool,
    index_sql: bool, ignore: list[str], max_file_bytes: int, skip_generated: bool,
    counts: WalkCounts,
) -> SourceFile | None:
    """One file's full accept/skip decision, read included; None means skipped.

    Every skip increments its counter here rather than being dropped silently —
    the summary line ``index_repo_dir`` logs is the only place a user learns that
    a file was passed over.
    """
    ext = os.path.splitext(fn)[1]
    kind = _file_kind(fn, ext, rel, allowed_exts=allowed_exts,
                      index_hcl=index_hcl, index_sql=index_sql)
    if kind is None:
        return None
    if _ignored(rel, ignore):
        counts.ignored += 1
        return None
    # Generated code by name, and oversized blobs by stat — both decided without
    # reading the file.
    if _generated(kind, skip_generated, lambda: _is_generated_name(fn)):
        counts.generated += 1
        return None
    if _oversize(fpath, kind, max_file_bytes):
        counts.oversize += 1
        return None
    try:
        source = fpath.read_bytes()
    except OSError as e:
        log(f"  skip {rel}: {e}")
        return None
    if _generated(kind, skip_generated, lambda: _has_generated_header(source)):
        counts.generated += 1
        return None
    return SourceFile(rel=rel, source=source, kind=kind,
                      lang=LANG_BY_EXT[ext] if kind == _CODE else "")


def _walk_source_files(
    root: Path, *, allowed_exts: set[str], index_hcl: bool, index_sql: bool,
    max_file_bytes: int, skip_generated: bool, counts: WalkCounts,
) -> Iterator[SourceFile]:
    """Yield every indexable file under ``root``, already read into memory.

    Owns all of the "should this file be looked at at all" policy: pruned
    directories, the per-repo ignore file, generated/derived code, and the
    oversize limit.
    """
    ignore = load_ignore_patterns(root)
    for dirpath, dirnames, filenames in os.walk(root):
        relbase = os.path.relpath(dirpath, root)
        relbase = "" if relbase == "." else relbase.replace(os.sep, "/")
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS
                       and not _ignored(f"{relbase}/{d}".lstrip("/"), ignore)]
        for fn in filenames:
            fpath = Path(dirpath) / fn
            # ADR/decision-record markdown (docs/adr/, decisions/, ...) is
            # recognised by relative path, not by name, so rel is built for every
            # candidate before anything is classified or skipped.
            rel = str(fpath.relative_to(root))
            sf = _select_file(fpath, rel, fn, allowed_exts=allowed_exts,
                              index_hcl=index_hcl, index_sql=index_sql, ignore=ignore,
                              max_file_bytes=max_file_bytes,
                              skip_generated=skip_generated, counts=counts)
            if sf is not None:
                yield sf


def _parse_code(repo_id: str, sf: SourceFile, refs: RefCollector,
                ) -> tuple[list[Node], list[Edge]]:
    nodes, edges, calls, inh = parse_source(repo_id, sf.rel, sf.source, sf.lang)
    refs.calls.extend(calls)
    refs.inherits.extend(inh)
    # cross-repo flow surfaces: HTTP endpoints + message topics; repo-local
    # frontend routes (web-topology), entity state machines (state-diagram source
    # data), and intra-repo dataflow (which tables/views this file reads/writes)
    hn, he = extract_http_flow(repo_id, sf.rel, sf.source, sf.lang)
    en, ee = extract_event_flow(repo_id, sf.rel, sf.source, sf.lang)
    wn, we = extract_web_flow(repo_id, sf.rel, sf.source, sf.lang)
    sn, se = extract_state_flow(repo_id, sf.rel, sf.source, sf.lang)
    dr, dw = extract_data_refs(repo_id, sf.rel, sf.source)
    refs.data_reads.extend(dr)
    refs.data_writes.extend(dw)
    nodes += hn + en + wn + sn
    edges += he + ee + we + se
    return nodes, edges


def _parse_hcl_file(repo_id: str, sf: SourceFile, refs: RefCollector,
                    ) -> tuple[list[Node], list[Edge]]:
    nodes, hcl_refs = parse_hcl(repo_id, sf.rel, sf.source)
    refs.hcl.extend(hcl_refs)
    return nodes, []


def _parse_sql_file(repo_id: str, sf: SourceFile, refs: RefCollector,
                    ) -> tuple[list[Node], list[Edge]]:
    nodes, sql_refs = parse_sql(repo_id, sf.rel, sf.source)
    refs.sql.extend(sql_refs)
    return nodes, []


def _parse_adr_file(repo_id: str, sf: SourceFile, _refs: RefCollector,
                    ) -> tuple[list[Node], list[Edge]]:
    return parse_adr(repo_id, sf.rel, sf.source), []


def _parse_manifest_file(repo_id: str, sf: SourceFile, _refs: RefCollector,
                         ) -> tuple[list[Node], list[Edge]]:
    return parse_manifest(repo_id, sf.rel, sf.source)


# Extraction kind -> parser, all sharing one signature so the orchestrator below
# is a lookup rather than a multi-way branch. A new file kind is a table entry
# plus a `_file_kind` clause; nothing in index_repo_dir changes.
_PARSERS: dict[str, Callable[[str, SourceFile, RefCollector],
                             tuple[list[Node], list[Edge]]]] = {
    _CODE: _parse_code,
    _HCL: _parse_hcl_file,
    _SQL: _parse_sql_file,
    _ADR: _parse_adr_file,
    _MANIFEST: _parse_manifest_file,
}


def _extension_filter(languages: list[str] | None) -> tuple[set[str], bool, bool]:
    """The ``languages`` filter resolved to (code extensions, index HCL?, index SQL?).

    No filter means everything; HCL and SQL are opted in by name because neither
    lives in ``LANG_BY_EXT``.
    """
    allowed_exts = {ext for ext, lang in LANG_BY_EXT.items()
                    if not languages or lang in languages}
    # ".h" is classified as "cpp" internally (see LANG_BY_EXT), but C and C++
    # headers are shared infrastructure -- a user who filters to just "c"
    # almost certainly still wants its headers indexed, not silently dropped.
    # So ".h" inclusion is decided by either language being enabled, not by
    # which single language it happens to be parsed as.
    if languages and ("c" in languages or "cpp" in languages):
        allowed_exts.add(".h")
    return (allowed_exts,
            not languages or "hcl" in languages,
            not languages or "sql" in languages)


def index_repo_dir(
    repo_path: str, repo_id: str, head_commit: str | None = None,
    languages: list[str] | None = None, *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES, skip_generated: bool = True,
) -> GraphShard:
    """Walk a repository directory and parse every supported file into a shard.

    Generated/derived files (see ``_is_generated_name``/``_has_generated_header``)
    and code files larger than ``max_file_bytes`` are skipped — both reported, never
    silent — to keep legacy monorepos from exploding the graph and the index time.
    """
    allowed_exts, index_hcl, index_sql = _extension_filter(languages)
    shard = GraphShard(repo=repo_id, head_commit=head_commit, parser_version=PARSER_VERSION)
    by_id: dict[str, Node] = {}
    refs = RefCollector()
    counts = WalkCounts()

    for sf in _walk_source_files(
        Path(repo_path), allowed_exts=allowed_exts,
        index_hcl=index_hcl, index_sql=index_sql,
        max_file_bytes=max_file_bytes, skip_generated=skip_generated, counts=counts,
    ):
        try:
            nodes, edges = _PARSERS[sf.kind](repo_id, sf, refs)
        except Exception as e:  # noqa: BLE001 - one bad file must not abort the repo
            log(f"  skip {sf.rel}: parse error: {e}")
            continue
        counts.files += 1
        for node in nodes:
            by_id[node.id] = node  # dedupe shared nodes (e.g. packages, modules)
        shard.edges.extend(edges)

    # Order matters and is load-bearing: `shard.nodes` is populated *before*
    # _resolve_pending_methods, which then re-kinds those same Node objects in
    # place through by_id's references.
    shard.nodes.extend(by_id.values())
    _resolve_pending_methods(by_id, shard.edges)
    shard.edges.extend(refs.resolved_edges(by_id))
    log(f"  parsed {counts.files} file(s); skipped {counts.generated} generated, "
        f"{counts.oversize} oversized, {counts.ignored} ignored", level=logging.DEBUG)
    return shard


def _repo_commit_epoch(path: str) -> int:
    """HEAD's commit time (unix epoch), or -1 if unavailable -- used only to break
    a tie between two local checkouts of the same canonical repo."""
    from .repo_identity import run_git

    out = run_git(path, "log", "-1", "--format=%ct")
    return int(out) if out.isdigit() else -1


def iter_repo_dirs(root: str | Path) -> Iterator[Path]:
    """Yield every git working-tree root at or under ``root``, at any depth.

    The single definition of "there is a repository here", shared by
    :func:`discover_repos` (which then resolves each one's canonical identity)
    and by ``kb index``'s bundling check. Sharing it is the point: that check
    tells a reader to use ``--workspace``, and a count that disagreed with what
    ``--workspace`` then walked would be the same wrong-denominator defect the
    message exists to prevent.

    Bounded two ways. A repository is never descended into once found -- a
    submodule is that repository's business, not a separate workspace member --
    and ``_SKIP_DIRS`` is pruned everywhere else, which includes ``.git``, so
    the walk never enters a git directory either.
    """
    base = Path(root)
    for dirpath, dirnames, _filenames in os.walk(base):
        here = Path(dirpath)
        if (here / ".git").exists():
            dirnames[:] = []  # a repo: never descend past it
            yield here
            continue
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]


def is_vendored_repo(root: Path, repo_dir: Path) -> bool:
    """Whether ``repo_dir`` sits under a segment marking a vendored upstream
    clone (see :data:`_VENDORED_REPO_MARKERS`). A pure path test, no git calls,
    so every consumer of :func:`iter_repo_dirs` can afford to apply it."""
    return bool(_VENDORED_REPO_MARKERS & set(repo_dir.relative_to(root).parts))


def count_indexable_files(root: str | Path, *, limit: int | None = None) -> int:
    """How many files under ``root`` the indexer would pick up, by name.

    Stops as soon as ``limit`` is reached, because every caller so far wants a
    comparison rather than a number: answering "is there at least this much
    content here" on a large tree must not cost a full walk of it.
    """
    return _count_files(Path(root), stop_at_repos=False, limit=limit)


def count_files_outside_repos(root: str | Path, *, limit: int | None = None) -> int:
    """The same count, restricted to files that lie outside **every** git working
    tree at or under ``root`` -- the exact complement of :func:`iter_repo_dirs`.

    This is what tells ``kb index`` apart a workspace mirroring repositories
    (nothing of the user's own outside them, so each should be indexed under its
    own identity) from a project that merely carries a dependency with its own
    ``.git`` (the user's own sources are right there in the open, and indexing
    only the nested repositories would drop them).

    A working tree bounds the walk whether or not ``discover_repos`` would index
    it, which is deliberately *not* how :func:`is_vendored_repo` is applied to the
    repository count next to it: the count means "repositories ``--workspace``
    would walk", while this means "content that is the user's own". An upstream
    clone carried inside the tree is neither, so it must not inflate either
    number.
    """
    return _count_files(Path(root), stop_at_repos=True, limit=limit)


def _count_files(base: Path, *, stop_at_repos: bool, limit: int | None) -> int:
    n = 0
    for dirpath, dirnames, filenames in os.walk(base):
        if stop_at_repos and (Path(dirpath) / ".git").exists():
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if is_indexable_name(fn, os.path.relpath(os.path.join(dirpath, fn), base)):
                n += 1
                if limit is not None and n >= limit:
                    return n
    return n


def discover_repos(root: str) -> list[tuple[str, str]]:
    """Find git repositories under ``root``: (repo_id, absolute_path) pairs.

    ``repo_id`` is canonical (see :mod:`repo_identity`) -- derived from the repo's
    ``origin`` remote, not its path relative to ``root`` -- so the same physical
    repo gets the same id regardless of where it's checked out or indexed from.
    Discovery does not descend into a repo once found.

    Two local checkouts of the *same* remote (a stale pre-reorg clone left behind
    alongside its replacement, a real pattern found in this project's own fleet)
    would otherwise collide on one canonical id; the more recently committed
    checkout wins and the other is skipped with a logged reason, never silently.
    """
    from .. import style
    from .repo_identity import describe_gitdir_mismatch, is_own_gitdir, resolve_repo_id

    base = Path(root)
    by_id: dict[str, str] = {}
    for here in iter_repo_dirs(base):
        rel = here.relative_to(base).as_posix()
        if is_vendored_repo(base, here):
            log(f"  skip vendored repo {rel}")
            continue
        if not is_own_gitdir(str(here)):
            # Two genuinely different git-level situations collapse to the same
            # skip decision here (see describe_gitdir_mismatch's docstring for
            # why they're worth telling apart in the message): a dangling
            # gitlink git can't resolve at all, or -- the dangerous one -- git
            # resolving fine but to an ANCESTOR repo, which would silently
            # misattribute this directory's identity and history if not skipped.
            log(style.warn(f"  skip {rel}: {describe_gitdir_mismatch(str(here))}"))
            continue
        rid = resolve_repo_id(str(here))
        prior = by_id.get(rid)
        if prior is not None:
            # same canonical repo checked out twice -- keep whichever HEAD is
            # more recently committed, log the one dropped.
            winner, loser = ((prior, str(here)) if _repo_commit_epoch(prior)
                              >= _repo_commit_epoch(str(here)) else (str(here), prior))
            log(f"  skip duplicate checkout of {rid}: {loser} "
                f"(keeping the more recently committed {winner})")
            by_id[rid] = winner
            continue
        by_id[rid] = str(here)
    return [(rid, path) for rid, path in by_id.items()]


# Node kinds a call can resolve to, and the (narrower) kinds a base class/interface
# can resolve to. A method can be called but never inherited from.
_CALLABLE_KINDS = {"class", "function", "method", "interface", "struct"}
_INHERITABLE_KINDS = {"class", "interface", "struct"}

# HCL block kinds a depends_on reference can resolve to. Note `module` is also
# emitted for code imports, so kinds are not disjoint; safety comes from address
# namespacing instead: HCL addresses are prefixed (`var.`/`module.`/`data.`/
# `local.`) or resource-typed (`type.name`), while code module nodes carry raw
# import paths (`os`, `requests`), so the name indices never overlap. A
# pathological collision would surface as an AMBIGUOUS edge, never a wrong
# INFERRED one.
_HCL_KINDS = {"resource", "data", "variable", "output", "module", "local"}

# SQL FK references resolve to table/view defs (both non-colliding with code and
# HCL kinds, so their name index stays isolated).
_SQL_KINDS = {"table", "view"}


# A name that resolves to 2..N definitions is emitted as AMBIGUOUS edges to each
# candidate (so blast-radius doesn't miss hot symbols); a name matching more than
# this is too generic (e.g. `get`/`handle`) to be signal and is skipped.
_MAX_AMBIG_FANOUT = 6

# Languages that can call into each other's definitions, keyed by the language a
# candidate's file is written in. Name resolution is repo-wide and name-based, so
# without this a Python `conn.close()` matched a `close()` in a JavaScript file
# and every Python caller of any close() became a "dependent" of a browser event
# handler -- 282 false positives on one real repo, precision 1/282.
#
# Grouped, not compared for equality, because the interop is real inside each
# group and would otherwise be lost: TS/TSX/JS compile to one runtime and import
# each other freely, a C++ .cpp routinely calls what a .h declares (and ".h" is
# classified "cpp" -- see LANG_BY_EXT), and JVM languages call each other's
# classes directly. Across groups a same-name match is coincidence, never a call.
# A language absent here is its own group (Python, Go, Rust, C#, Ruby, PHP).
_LANG_FAMILY = {
    "javascript": "js", "typescript": "js", "tsx": "js",
    "c": "c", "cpp": "c",
    "java": "jvm", "kotlin": "jvm", "scala": "jvm",
}


def _family(lang: str | None) -> str | None:
    """A language's interop group, or None when the language is unknown.

    None disables the filter for that node rather than isolating it: HCL, SQL and
    manifest nodes carry no lang, and their reference streams (depends_on /
    references / reads / writes) are namespaced by kind instead.
    """
    return _LANG_FAMILY.get(lang, lang) if lang else None


def _resolve_pending_methods(by_id: dict[str, Node], edges: list[Edge]) -> None:
    """Repo-wide second pass: link an out-of-line qualified method to its class.

    ``Node.attrs["_pending_method_of"]`` (set by ``parse_source`` when a definition's
    declarator was a qualified name, e.g. ``Widget::Draw``) names the qualifier chain;
    the class may live in any file (the common header/source split), so this can only
    resolve once every file in the repo has been parsed and ``by_id`` is complete.
    """
    class_index: dict[str, list[str]] = {}
    for node in by_id.values():
        if node.kind in ("class", "struct"):
            class_index.setdefault(node.name, []).append(node.id)

    # Index each node's *first* "contains" edge once, up front. A linear scan of
    # `edges` per pending method makes this pass O(pending x edges) -- measured
    # at ~192s for a 20k-pending/150k-edge C++ repo, exactly the legacy-scale
    # case this plan targets. "First occurrence wins" (only set if not already
    # present) mirrors the previous `for i, e in enumerate(edges): ... break`
    # scan's first-match semantics exactly.
    contains_edge_idx: dict[str, int] = {}
    for i, e in enumerate(edges):
        if e.relation == "contains" and e.dst not in contains_edge_idx:
            contains_edge_idx[e.dst] = i

    for node in by_id.values():
        pending = node.attrs.pop("_pending_method_of", None)
        if not pending:
            continue
        candidates = class_index.get(pending[-1])
        if not candidates or len(candidates) != 1:
            continue  # unresolved or ambiguous -- leave as "function", file-contained
        class_id = candidates[0]
        i = contains_edge_idx.get(node.id)
        if i is None:
            continue  # defensive: no containment edge found for this node
        e = edges[i]
        edges[i] = Edge(
            src=class_id, dst=node.id, relation="contains",
            confidence=e.confidence, provenance=e.provenance,
        )
        node.kind = "method"


def _resolve_name_refs(
    refs: list[tuple[str, str, str, int]], nodes_by_id: dict[str, Node],
    *, relation: str, target_kinds: set[str], same_language: bool = False,
) -> list[Edge]:
    """Resolve ``(src_id, target_name, file, line)`` references to definitions
    repo-wide, emitting ``relation`` edges. Shared by calls and inherits.

    ``same_language`` restricts candidates to the reference's own language family
    (see :data:`_LANG_FAMILY`). It is on for ``calls``/``inherits``, where the
    languages must genuinely match, and off for the HCL/SQL streams, which are
    cross-domain by design (code ``reads`` a SQL table) and are kept apart by
    their disjoint kind sets instead.

    An edge is INFERRED when a name maps to a single definition. A name mapping to
    2..``_MAX_AMBIG_FANOUT`` definitions is genuinely ambiguous — rather than drop it
    (which silently loses the hottest symbols and undercounts blast radius), emit an
    AMBIGUOUS edge to each candidate. Names matching more than the cap are too generic
    to be signal and are skipped; self-references and duplicate (src, dst) pairs are
    de-duplicated, keeping the lowest source line among duplicates (a repeated call
    site, e.g. ``helper()`` invoked twice from the same caller, must not surface an
    arbitrary later line just because tree-sitter's capture order isn't guaranteed to
    match source order -- callers that order edges by line, like a sequence diagram,
    depend on this being deterministic).
    """
    name_index: dict[str, set[str]] = {}
    for node in nodes_by_id.values():
        if node.kind in target_kinds:
            name_index.setdefault(node.name, set()).add(node.id)

    edges: list[Edge] = []
    seen: set[tuple[str, str]] = set()
    resolved = ambiguous = dropped = cross_lang = 0
    for src_id, name, rel, line in sorted(refs, key=lambda r: r[3]):
        matches = name_index.get(name)
        if not matches:
            continue
        # Drop candidates the reference's own language cannot reach BEFORE the
        # fan-out cap, so a name that was only "too generic" because of unrelated
        # same-named definitions in other languages resolves properly instead of
        # being skipped. An unknown language on either side keeps the candidate.
        src_family = _family(getattr(nodes_by_id.get(src_id), "lang", None)) \
            if same_language else None
        if src_family is not None:
            reachable = {t for t in matches
                         if (f := _family(nodes_by_id[t].lang)) is None or f == src_family}
            cross_lang += len(matches) - len(reachable)
            matches = reachable
        if not matches:
            continue
        if len(matches) == 1:
            conf, targets = Confidence.INFERRED, list(matches)
        elif len(matches) <= _MAX_AMBIG_FANOUT:
            conf, targets = Confidence.AMBIGUOUS, sorted(matches)  # deterministic
            ambiguous += 1
        else:
            dropped += 1  # too many candidates -> noise
            continue
        for target in targets:
            if target == src_id or (src_id, target) in seen:
                continue
            seen.add((src_id, target))
            edges.append(Edge(
                src=src_id, dst=target, relation=relation, confidence=conf,
                context="ambiguous" if conf is Confidence.AMBIGUOUS else None,
                # How many definitions this one reference could have meant. Carried
                # on the edge because only this pass knows it: a consumer re-deriving
                # it later would have to guess the kind set and the language filter,
                # and a "confidence" label nobody can quantify is decoration.
                attrs={"name_candidates": len(targets)} if len(targets) > 1 else {},
                provenance=Provenance(source_file=rel, source_line=line,
                                      verified_at=date.today())))
            if conf is Confidence.INFERRED:
                resolved += 1
    if edges or dropped:
        log(f"  resolved {resolved} {relation} edge(s); {ambiguous} ambiguous "
            f"(<= {_MAX_AMBIG_FANOUT} candidates), {dropped} too-generic skipped, "
            f"{cross_lang} cross-language candidate(s) rejected",
            level=logging.DEBUG)
    return edges
