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
import hashlib
import importlib
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
from .config import DEFAULT_MAX_FILE_BYTES  # noqa: F401 -- re-exported, see below
from .flow.data import extract_data_refs
from .flow.events import extract_event_flow
from .flow.http import extract_http_flow
from .flow.state import extract_state_flow
from .flow.web import extract_web_flow
from .hcl import parse_hcl
from .ids import make_id
from .kinds import KIND_REGISTRY
from .manifest import is_manifest, parse_manifest
from .model import (
    PER_SITE_RELATIONS,
    SHARED_REPO,
    Confidence,
    Edge,
    Node,
    Provenance,
)
from .proc import mask_embedded_sql
from .sql import parse_sql
from .store.shards import GraphShard
from .xml_cfg import parse_xml_config
from .xsd import parse_xsd
from .xsl import parse_xsl

# DEFAULT_MAX_FILE_BYTES (imported above) stays importable FROM kb.parse, not just
# kb.config, because kb/cmds/index.py already imports it from here (lazily, to
# avoid an eager tree-sitter import). It used to be its own literal here
# (5 * 1024 * 1024 = 5,242,880 bytes, "5 MiB") disagreeing with KbConfig's
# separately hardcoded 5,000,000 ("5 MB"). docs/indexing-the-code-graph.md documents the
# knob as "5 MB" (decimal), so kb/config.py's value is the one that survived.

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
    ".swift": "swift",
    ".dart": "dart",
    ".zig": "zig",
    # Perl: ".pl" is scripts, ".pm" modules, ".t" its test files.
    ".pl": "perl", ".pm": "perl", ".t": "perl",
    # Every shell dialect the bash grammar reads well enough to be worth indexing. `.ksh`
    # and `.zsh` are near-supersets for the constructs that matter here (functions and the
    # commands they run); `.bats` is bash with a test harness on top. A shell script that
    # went unindexed because of its extension is the same file with a different suffix.
    ".sh": "bash", ".bash": "bash", ".ksh": "bash", ".zsh": "bash",
    ".bats": "bash", ".command": "bash",
    ".ex": "elixir", ".exs": "elixir",
    ".css": "css", ".html": "html", ".htm": "html",
    ".nix": "nix",
    ".svelte": "svelte", ".vue": "vue",
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
    # CUDA is a C++ superset, so the cpp grammar reads it: verified on a real kernel
    # file, it extracts `__global__`/`__device__` functions, classes, methods and
    # out-of-line definitions, and captures ordinary calls.
    #
    # What it does NOT capture, stated because a partial extraction must not be
    # mistaken for a complete one: the host-side launch `kernel<<<grid, block>>>(...)`
    # is not C++ syntax, so it lands in a local ERROR region and that launch is missed
    # as a call edge, while a plain call in the same file resolves normally.
    # tree-sitter degrades locally rather than failing the file, so the rest still
    # extracts. Before this, these files produced ZERO nodes and a comparator that
    # reads them gave a better answer to "who calls this" than we did.
    ".cu": "cpp", ".cuh": "cpp",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".scala": "scala", ".sc": "scala",
    ".kt": "kotlin", ".kts": "kotlin",
    # Included makefile fragments carry an extension; the build file itself does not, and
    # takes the name route below. Two routes, one language: a project whose build system
    # is split across `Makefile` and `common.mk` must not appear as two languages.
    ".mk": "make", ".mak": "make",
}

# Languages whose files are identified by NAME rather than by extension. A build file
# is the common case: `Makefile` and `Dockerfile` have no extension at all, so
# LANG_BY_EXT -- the only route to a grammar before this -- could never reach them, and
# `_select_file` dropped them without even counting the skip.
#
# Keys are the name STEM, lowercased: everything before the first dot. That is an EXACT
# match on a derived key, deliberately not a prefix test. `Makefile.am` must hit and
# `MyMakefile` must not, and a prefix test gets the second one wrong in exactly the way
# an unanchored match always does. A spelling like `prod.Makefile` is an extension and
# belongs in LANG_BY_EXT, not here.
LANG_BY_NAME = {
    "makefile": "make",
    "gnumakefile": "make",
    "dockerfile": "dockerfile",
    "containerfile": "dockerfile",
}

# Every language contextlake can parse, however a file reaches it. Union, never either
# table alone: a count or a coverage check written against LANG_BY_EXT silently omits
# every name-routed language, and reads as complete while doing so.
ALL_LANGS = set(LANG_BY_EXT.values()) | set(LANG_BY_NAME.values())

# Grammars the `kb` extra does NOT install, mapped to the extra that does. A grammar
# belongs here when it has no wheel for a platform contextlake installs on: making it a
# hard dependency would not degrade that platform's indexing, it would make
# `pip install contextlake[kb]` FAIL outright there. tree-sitter-dockerfile publishes two
# wheels and no sdist, so Windows, aarch64 Linux and musl all have nothing to install.
#
# The cost of the extra is that a file in that language is skipped, and the skip must
# say which of the two things it is. "unsupported extension" would be a lie: the language
# IS supported and the package simply is not here, and those have different fixes.
OPTIONAL_GRAMMAR_EXTRA = {
    "dockerfile": "kb-dockerfile",
}


class GrammarNotInstalled(ImportError):
    """An OPTIONAL grammar package is absent. Distinct from a broken install.

    Carries the language and the extra so the caller can count the skip and name the fix,
    rather than folding it into the blanket per-file parse-error handler, where it would
    read as "this file is malformed" and be counted as nothing at all.
    """

    def __init__(self, lang: str, module: str, extra: str) -> None:
        super().__init__(
            f"the {lang} grammar needs {module}, which is an optional dependency. "
            f"Install it with: pip install 'contextlake[{extra}]'")
        self.lang = lang
        self.module = module
        self.extra = extra

    def __reduce__(self):
        """Rebuild from the three values ``__init__`` needs.

        Raised on the ``index_repo_dir`` path the worker pool runs, so the
        default ``cls(*self.args)`` rebuild breaks the whole executor rather
        than failing one repository. See ``RepoTooLarge.__reduce__``.

        ``module`` is stored for this: it was formatted into the message and
        then discarded, so there was nothing to rebuild it from.
        """
        return (self.__class__, (self.lang, self.module, self.extra))

# HCL/Terraform files use a bespoke extraction path (kb/hcl.py), not the OO
# capture model, so they are matched separately from LANG_BY_EXT.
HCL_EXTS = {".tf"}

# SQL DDL uses a regex extractor (kb/sql.py), matched separately from LANG_BY_EXT.
# `.sql` plus the PL/SQL source extensions. Oracle tooling splits one object per file by
# convention: a package spec in `.pks`, its body in `.pkb`, a standalone procedure in
# `.prc`. Those files were previously routed to nothing and contributed no nodes.
SQL_EXTS = {".sql", ".pks", ".pkb", ".plb", ".prc", ".fnc", ".trg", ".pls"}

# XML configuration uses a line scanner (kb/xml_cfg.py), matched separately from
# LANG_BY_EXT. NOT gated by `languages`, for the reason `_file_kind` gives about
# manifests and ADRs: a setting is not written in a language anyone filters on, so
# `--languages python` must not hide a repo's configuration.
# `.config` is the canonical .NET settings file (`web.config`, `app.config`) and was
# routed to nothing: measured across 660 real repositories, 1,023 such files
# contributed ZERO nodes while answering "where is this setting defined" is the
# question this extractor exists for. `.props`, `.targets` and `.settings` are the
# MSBuild and Visual Studio equivalents; `.plist` is Apple's.
#
# THREE FAMILIES ARE DELIBERATELY OUT, each for a measured reason rather than an
# oversight:
#
# * `.resx` -- 3,963 files yielding ~22.9 nodes each, about 91,000 fleet-wide. They
#   are localisation resource strings, not settings: "where is this setting
#   defined" is not a question anyone asks of a translated label, and the roadmap's
#   XML decision warns specifically against a scope that "could add more nodes than
#   all five C++ kinds combined and would dominate the per-kind diagram budgets".
# * `.csproj` / `.vbproj` / `.nuspec` -- build definitions, which is manifest
#   territory and already has its own extractor and its own `depends_on` relation.
# * `.svg` -- 4,140 files and 284 MB of it in this fleet. XML-shaped, and graphics.
#
# A file with one of these extensions that is not actually XML costs nothing: the
# scanner is a regex over markup and simply finds no settings.
XML_EXTS = {".xml", ".config", ".props", ".targets", ".settings", ".plist"}

# XML Schema uses its own scanner (kb/xsd.py), NOT the config scanner: `name` is one of
# `xml_cfg._KEY_ATTRS`, so a schema sent there files every `<xs:element name="Order">` as
# a setting. `.xsd` is therefore matched BEFORE XML_EXTS and never appears in it.
XSD_EXTS = {".xsd"}

# XSLT uses its own scanner (kb/xsl.py). A stylesheet is a program with a call graph,
# not a settings file and not a schema, so it gets neither of the other two extractors.
XSL_EXTS = {".xsl", ".xslt"}

# Pro*C: C with `EXEC SQL` written into the source. Its own dispatch kind rather than a
# LANG_BY_EXT row, because the C parse and the dataflow pass need DIFFERENT bytes -- the
# statements masked for one, intact for the other. See kb/proc.py.
PROC_EXTS = {".pc"}
# The grammar the masked file is read with. C rather than C++ because Pro*C is a C
# precompiler; the two share a language family, so a call either way still resolves.
_PROC_LANG = "c"

# A code file larger than DEFAULT_MAX_FILE_BYTES (imported above from kb/config.py,
# the single source of truth) is skipped and logged. Hand-written source is
# essentially never this big -- anything that large is a data blob or vendored
# bundle. Raise [kb] max_file_bytes to index them anyway.

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
# "4" is the largest output change this stamp has ever covered, and every part of
# it is invisible to a commit-keyed check, which is why the bump matters more here
# than in any previous one:
#   - node ids became file- and line-independent, so EVERY id changed;
#   - config keys, SQL and XML gained file nodes and `contains` edges;
#   - five symbol kinds are emitted that never existed before (data members,
#     macros, typedefs, enum constants, file-scope variables) -- for C and C++;
#     see "5" below, which is where the other language families got them;
#   - `calls` edges are stored per call site rather than per caller/callee pair;
#   - C++ internal linkage is honoured in both identity and resolution.
# A user upgrading without re-indexing keeps a graph whose ids no longer match
# anything this build produces, and nothing about their commit would say so.
#
# "5" is smaller but has the same invisibility: the extra-symbol pass described above
# was C/C++ only, and now also runs for JavaScript, TypeScript, TSX and Python, so every
# repository in those languages gains `global_variable` and `field` nodes plus their
# `contains` edges. A commit-keyed check cannot see it, because no commit moved.
# "6" is `entry_point`: how a program is STARTED became a kind, refined from the
# function or method that something else makes special, plus its own node where nothing
# exists to refine (Python's `__main__` guard, a console script the packaging declares).
# Every language also gained a second condition -- Go's `main` package, Java and C#
# `static`, top-level position for Rust, Kotlin, C and C++ -- without which every helper
# named `main` anywhere in a tree was advertised as a way to run the project.
#
# The three paragraphs below were each labelled ONE LOWER than the version they produced,
# and this paragraph did not exist at all, so the block claimed "6" was the constants work
# when "6" was this. Corrected against `git log -S`. Each label names the version the
# change PRODUCED, which is what a reader deciding whether to re-index needs.
# "7" is the same shape again, twice over. Every constant now carries the declaration it was
# written with (`attrs["declaration"]`), and every place a constant's value is READ is now an
# edge (`uses`), where before the graph recorded that a constant existed and nothing about
# what it was or where it mattered. Neither is visible to a commit-keyed check, and both are
# the point of re-indexing: measured on two public trees, the read edges alone came to +11%
# of all edges on a small Python package and +63% on a macro-heavy C++ one.
# "8" is the manifest side of the same idea. A `depends_on` edge recorded a package name,
# cited line 1 of the manifest whatever the manifest said, and folded runtime, dev, peer and
# optional groups into one relation, so an extra a user opts into was indistinguishable from
# a dependency the package cannot start without. Each edge now carries the constraint as
# written, its group, and the line it was declared on. This bump follows "7" closely, which
# is not ideal for anyone who just re-indexed; the alternative is worse, because a manifest
# that has not changed since the last index would keep the thinner edges forever and no
# commit-keyed check would ever say so.
# "9" adds language coverage rather than changing how existing languages are read:
# PL/SQL objects (package, package body, function, type, trigger) inside `.sql`, the
# Oracle `CREATE OR REPLACE` spelling that the previous patterns rejected outright, the
# per-object PL/SQL extensions (.pks/.pkb/.plb/.prc/.fnc/.trg/.pls) which were routed
# nowhere, and the shell dialects (.ksh/.zsh/.bats/.command) which the bash grammar
# already reads. A repository containing any of those carries strictly more than it did,
# and no commit-keyed check would ever say so, which is what this bump exists for.
# "10" is the same kind of bump for XML Schema. `.xsd` was routed nowhere, so a repository
# whose data contracts live in schemas carried none of them; its global components and the
# names they reference are extracted now. One bump covers the whole language batch rather
# than one per language, because the cost of this constant is a re-index and there is no
# reason to charge it three times for work that lands in one release.
# "11" is that same shape a third time, for XML CONFIGURATION rather than schemas.
# `.config` was routed nowhere, and it is the canonical .NET settings file: measured
# across 660 real repositories, 1,023 of them carried no nodes at all while being
# exactly the files "where is this setting defined" is asked about. `.props`,
# `.targets`, `.settings` and `.plist` join it. A repository holding any of those
# carries strictly more than it did, and NO COMMIT MOVED, so without this bump every
# already-indexed repository reports "unchanged" and never gains a single setting.
# That is the whole reason this constant exists, and it was nearly missed: the
# extraction change and the bump are one piece of work, not two.
PARSER_VERSION = "12"

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
# Swift has no distinct struct node: `struct Box` parses as `class_declaration` too,
# so both arrive as "class" rather than being guessed apart by source text.
_DEF_TYPES["swift"] = {"class_declaration": "class", "protocol_declaration": "interface",
                       "function_declaration": "function"}
# Dart splits a top-level function into `function_signature` + a sibling `function_body`,
# so the signature is the definition node.
_DEF_TYPES["dart"] = {"class_definition": "class", "mixin_declaration": "interface",
                      "function_signature": "function"}
# Zig declares a struct as `const Engine = struct {...}`, which is a `variable_declaration`
# and not a definition node, so only functions are captured here. Struct extraction needs
# to read the initializer and is deliberately left out rather than half-done.
_DEF_TYPES["zig"] = {"function_declaration": "function"}
_DEF_TYPES["perl"] = {"subroutine_declaration_statement": "function",
                      "package_statement": "module"}
# A bash variable is global unless declared `local`, so capturing assignments anywhere is
# right for this language, and is NOT the same call as the module-scope-only rule that
# applies to JavaScript and Python.
_DEF_TYPES["bash"] = {"function_definition": "function",
                      "variable_assignment": "global_variable"}
# Every Elixir definition is a `call`, so this maps the one node type and `_elixir_kind`
# refines it per macro. Keeping `call` here is also what gives a `def` inside a
# `defmodule` its module scope, since `_enclosing_defs` walks ancestors of this type.
_DEF_TYPES["elixir"] = {"call": "function"}
# The presentation and build tier declares NO definition node types and runs an EMPTY
# query. Its symbols all come from `_member_symbols`, which returns the kind in the tuple
# rather than deriving it from a node type. That is required, not stylistic: in CSS the
# pseudo-class in `a.nav:hover` is the same `class_name` node as the real class in `.nav`,
# so a node type cannot tell a selector from a pseudo-class, and a query that captured
# both would invent a CSS class called `hover`.
_QUERYLESS_LANGS = ("css", "html", "nix", "svelte", "vue", "make", "dockerfile")
for _markup in _QUERYLESS_LANGS:
    _DEF_TYPES[_markup] = {}
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
    # Every pattern below was compiled and run against a real snippet of its language
    # before being written here, because each of these grammars names things differently
    # from what the language's own syntax suggests.
    "swift": """
        (class_declaration name: (type_identifier) @def)
        (protocol_declaration name: (type_identifier) @def)
        (function_declaration name: (simple_identifier) @def)
        (call_expression (simple_identifier) @call)
        (import_declaration (identifier) @import)
    """,
    "dart": """
        (class_definition name: (identifier) @def)
        (mixin_declaration (identifier) @def)
        (function_signature name: (identifier) @def)
    """,
    "zig": """
        (function_declaration name: (identifier) @def)
    """,
    # `package Engine;` puts the keyword and the name under the same node type, so the
    # name is reached positionally rather than by a field.
    "perl": """
        (subroutine_declaration_statement name: (bareword) @def)
        (package_statement (package) @def)
    """,
    "bash": """
        (function_definition name: (word) @def)
        (variable_assignment name: (variable_name) @def)
        (command name: (command_name (word) @call))
    """,
    # Elixir has NO definition node types. `defmodule`, `def` and `defp` are ordinary
    # `call` nodes whose target is an identifier naming the macro, so the shape below is
    # the only one that reaches them, and the KIND cannot come from the node type the way
    # every other language's does. `_elixir_kind` reads the macro name instead.
    "elixir": """
        (call target: (identifier) @kw (arguments (alias) @def))
        (call target: (identifier) @kw (arguments (call target: (identifier) @def)))
    """,
}
# One list, read twice: this was two identical literals, and adding a language to the
# first without the second compiles a missing query into a KeyError at parse time on
# the language's first real file. Deriving the second from the first makes that
# impossible rather than merely unlikely.
for _markup in _QUERYLESS_LANGS:
    _QUERIES[_markup] = ""
_QUERIES["tsx"] = _QUERIES["typescript"]

_LANGS: dict[str, ts.Language] = {}
# The tree-sitter parser cache, keyed by LANGUAGE. Named distinctly from the
# extraction-kind registry further down, which is keyed by FILE KIND: both were
# called _PARSERS, so the later definition rebound the name and `_parser()` was
# inserting ts.Parser objects into the kind registry. Harmless only while no
# language shares a name with a FILE KIND -- and "xml" is a file kind here and the
# obvious name for a future tree-sitter XML grammar, at which point `_parser("xml")`
# would have returned the extraction callable instead of a parser.
#
# Stated exactly, because this comment misled a reader once: "xml" is a *file kind*
# in the extraction registry below, not a NODE kind. The XML extractor emits
# `config_key` nodes, and "xml" appears nowhere in `KIND_REGISTRY`. The collision
# risk above is real; "kind" in it means the file-kind registry, nothing else.
_TS_PARSERS: dict[str, ts.Parser] = {}
_COMPILED: dict[str, ts.Query] = {}


# Which pip package provides each grammar, and which factory to call on it.
#
# A table rather than the if/elif chain this replaces. The chain was 14 branches of
# `import tree_sitter_X as g; fn = g.language`, and the three that differ
# (`tree_sitter_c_sharp`, and typescript's two entry points, and php's) were the only
# reason it could not already be data. Adding a language is now one row, and the
# import stays lazy: `importlib.import_module` is called on first use of that language,
# never at module import, so installing the knowledge layer does not load 25 grammars.
#
# Verified equivalent to the chain it replaced before the swap: every language then
# known produced the same `ts.Language`.
_GRAMMARS: dict[str, tuple[str, str]] = {
    "python": ("tree_sitter_python", "language"),
    "javascript": ("tree_sitter_javascript", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
    "tsx": ("tree_sitter_typescript", "language_tsx"),
    "csharp": ("tree_sitter_c_sharp", "language"),
    "go": ("tree_sitter_go", "language"),
    "java": ("tree_sitter_java", "language"),
    "c": ("tree_sitter_c", "language"),
    "cpp": ("tree_sitter_cpp", "language"),
    "rust": ("tree_sitter_rust", "language"),
    "ruby": ("tree_sitter_ruby", "language"),
    "php": ("tree_sitter_php", "language_php"),
    "scala": ("tree_sitter_scala", "language"),
    "kotlin": ("tree_sitter_kotlin", "language"),
    "swift": ("tree_sitter_swift", "language"),
    "dart": ("tree_sitter_dart", "language"),
    "zig": ("tree_sitter_zig", "language"),
    "perl": ("tree_sitter_perl", "language"),
    "bash": ("tree_sitter_bash", "language"),
    "elixir": ("tree_sitter_elixir", "language"),
    "css": ("tree_sitter_css", "language"),
    "html": ("tree_sitter_html", "language"),
    "nix": ("tree_sitter_nix", "language"),
    "make": ("tree_sitter_make", "language"),
    # Optional: see OPTIONAL_GRAMMAR_EXTRA. A row here does not imply an installed package.
    "dockerfile": ("tree_sitter_dockerfile", "language"),
    "svelte": ("tree_sitter_svelte", "language"),
    # `.vue` has no tree-sitter package anywhere, and its template/script/style structure
    # is what the HTML grammar already parses. Same shape as typescript and tsx sharing
    # one package: a row, not a special case.
    "vue": ("tree_sitter_html", "language"),
}


def _language(lang: str) -> ts.Language:
    if lang not in _LANGS:
        spec = _GRAMMARS.get(lang)
        if spec is None:
            raise ValueError(f"unsupported language: {lang}")
        module, factory = spec
        try:
            mod = importlib.import_module(module)
        except ImportError as exc:
            # Names the package, because the alternative is a bare ImportError from a
            # module the reader never asked for by name. Two different situations, and
            # they have different fixes: an OPTIONAL grammar was never installed and the
            # user opts in, while a hard dependency being absent means a partial install.
            if (extra := OPTIONAL_GRAMMAR_EXTRA.get(lang)):
                raise GrammarNotInstalled(lang, module, extra) from exc
            raise ImportError(
                f"the {lang} grammar needs {module}, which is not installed. "
                f"Reinstall the knowledge layer: pip install 'contextlake[kb]'") from exc
        _LANGS[lang] = ts.Language(getattr(mod, factory)())
    return _LANGS[lang]


def _parser(lang: str) -> ts.Parser:
    if lang not in _TS_PARSERS:
        _TS_PARSERS[lang] = ts.Parser(_language(lang))
    return _TS_PARSERS[lang]


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
    captured across py/js/ts/c# (parameters sit on the definition) and c/c++ (they sit
    on the declarator, one level in); the docstring comes from Python's
    first-statement string or, elsewhere, a JSDoc/C#-XML leading comment. Never raises.
    """
    out: dict = {}
    try:
        # signature: the parameter list — generalizes across languages (field name
        # is "parameters" in py/js/ts, "parameter_list" in c#); graceful if absent.
        params = (def_ts.child_by_field_name("parameters")
                  or def_ts.child_by_field_name("parameter_list"))
        if params is None:
            # C and C++ hang the parameter list off the DECLARATOR, not off the
            # definition, so the two lookups above always missed and every C/C++ node
            # carried signature=None. That is a real coverage gap in the DISPLAYED field,
            # which appears in the UI, the wiki and get_repo_brief -- so walk one level in.
            #
            # This is not the overload discriminator: `_signature_text` already handles
            # that, and deliberately returns the whole declarator so trailing `const` and
            # `&`/`&&` are included. Do not substitute this field for it.
            dec = def_ts.child_by_field_name("declarator")
            while dec is not None and params is None:
                params = dec.child_by_field_name("parameters")
                dec = dec.child_by_field_name("declarator")
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
            # Guard against dropping the keeper itself. Node ids no longer contain the
            # line, so two `#ifdef`/`#else` twins -- identical qualified name, identical
            # signature -- now produce the SAME id, making `nid == keeper_id`. Adding it
            # to drop_ids then deleted BOTH branches, because the filter below removes
            # every node carrying a dropped id. The remap above is still needed: call
            # attribution is keyed by tree-sitter node id, and both branches' bodies must
            # attribute to the one surviving graph node.
            if nid != keeper_id:
                drop_ids.add(nid)

    if drop_ids:
        nodes[:] = [n for n in nodes if n.id not in drop_ids]
        pending[:] = [p for p in pending if p[3] not in drop_ids]

    # Two definitions that produce the same id ARE the same symbol -- that is what the
    # key asserts (repo, kind, qualified name, signature, and the file only for
    # internal-linkage symbols). So collapse duplicates, keeping the first occurrence.
    # This is what makes preprocessor twins fold together without the branch analysis
    # above having to fire at all. If two genuinely distinct symbols ever land here, the
    # bug is in the KEY and must be fixed there; preserving duplicate ids would only
    # hide it behind two nodes that cannot be told apart anyway.
    seen: set[str] = set()
    nodes[:] = [n for n in nodes if not (n.id in seen or seen.add(n.id))]
    seen_p: set[str] = set()
    pending[:] = [p for p in pending if not (p[3] in seen_p or seen_p.add(p[3]))]


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
        if scope is not None:
            seg = _scope_segment(scope)
            if seg:
                segments.append(seg)
        if name is None:
            return segments, node  # defensive; not expected from valid C++
        if name.type == "qualified_identifier":
            node = name
            continue
        return segments, name
    return segments, node


# Scope-segment node types that are already a plain name.
_PLAIN_SCOPE_TYPES = frozenset({"namespace_identifier", "type_identifier", "identifier"})


def _scope_segment(scope: ts.Node) -> str:
    """One qualifier segment's name, or "" when the node carries no usable name.

    This used to be an `in (...)` test with **no else**, so any scope type outside the
    plain three was dropped without a trace. `template_type` is the common one: in
    `NS::Box<T>::put` the `Box<T>` segment vanished, leaving `NS` as the last
    qualifier, and the resolver then attached `put` to whatever `NS` matched -- a
    FABRICATED parent, which is worse than a missing edge because it reads as fact.

    So every unrecognised type now falls through to its own text rather than
    disappearing. A segment that is merely ugly still resolves or fails loudly; a
    segment that is absent silently changes which class a method belongs to.
    """
    if scope.type in _PLAIN_SCOPE_TYPES:
        return scope.text.decode("utf-8", "replace")
    if scope.type == "template_type":
        # `Box<T>` -> `Box`. The arguments belong to the specialisation, not to the
        # class's identity, and the class node is named `Box`.
        base = scope.child_by_field_name("name")
        if base is not None:
            return base.text.decode("utf-8", "replace")
    # Unknown shape: keep the raw text, minus any template argument list so it still
    # has a chance of matching a class node. Never return "" here -- that is the
    # silent-drop behaviour this function exists to remove.
    raw = scope.text.decode("utf-8", "replace").strip()
    return raw.split("<", 1)[0].strip() or raw


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

    # The same rule the id key uses, hoisted to file level for the member pass: only
    # C/C++ external-linkage symbols drop the file from their identity.
    file_scope_default = lang not in ("c", "cpp")
    tree = _parser(lang).parse(source)
    captures = _sorted_captures(ts.QueryCursor(_query(lang)).captures(tree.root_node))

    # First pass: a Node for every definition, keyed by its tree-sitter def node id.
    def_node_to_id: dict[int, str] = {}
    pending: list[tuple[ts.Node, str, int, str]] = []  # (def_ts_node, qualified_name, line, nid)
    def_worklist: list[tuple[ts.Node, ts.Node, list[str]]] = []  # (def_ts, name_node, extra_scope)
    for name_node in captures.get("def", []):
        # Elixir only: the query's shape necessarily also matches `use`/`import`/`alias`
        # directives, which are not definitions. See `_elixir_defines`.
        if lang == "elixir" and not _elixir_defines(name_node):
            continue
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
        scope = [nm for nm in (_def_name_text(n, lang) for n in reversed(enclosing))
                 if nm]
        full_scope = scope + extra_scope if extra_scope else scope
        # A test macro with a body parses as a function_definition whose name is the
        # MACRO, so the case's real name is thrown away and every case in the repo
        # collapses onto one node. Recover the name from the macro's arguments.
        macro_case = _test_macro_case(def_ts, name, lang)
        if macro_case is not None:
            suite, case = macro_case
            name = case
            if suite:
                full_scope = [*full_scope, suite]
        qualified = ".".join([*full_scope, name])
        line = name_node.start_point[0] + 1
        kind = ("test" if macro_case is not None
                else _lang_kind(lang, def_ts) or _DEF_TYPES[lang][def_ts.type])
        # An entry point is re-kinded rather than duplicated, the same call `test`
        # already makes one line up: a symbol has one kind, and the more specific one is
        # the answer to the question somebody is actually asking. "How do I run this"
        # cannot be answered by a list of functions.
        if kind in ("function", "method") and _is_entry_point(lang, def_ts, name):
            kind = "entry_point"
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
        node_attrs = _doc_sig(def_ts, lang)
        # ONE rule, two consumers: the node id's key and the qualified name below.
        #
        # A file belongs to a symbol's identity everywhere EXCEPT the C/C++ header/source
        # split, which is the single case where one symbol legitimately lives in two
        # files -- and the reason C1 exists. Python, JS/TS, Go, Java and the rest put one
        # module per file, so `class Widget` in two modules are two different classes and
        # the file is genuinely part of both their identity and their qualified name.
        # Measured the hard way: three cross-language tests failed the moment the file
        # came out of the key unconditionally.
        #
        # `static` free functions and anonymous-namespace members keep their file too,
        # because internal linkage is file-scoped by language rule.
        internal = lang in ("c", "cpp") and _is_internal_linkage(def_ts)
        file_scope = None if lang in ("c", "cpp") and not internal else rel_path
        if internal:
            # Recorded rather than re-derived downstream. Resolution has to know this to
            # avoid offering one translation unit's internal symbol to another's caller,
            # and the only other signal available there is the file prefix on
            # `qualified_name` -- which is ALSO set for every non-C/C++ language, so a
            # Python symbol would read as internal linkage. Same one rule, one flag.
            node_attrs["linkage"] = "internal"
        # Computed before the id, because the parameter signature is part of the key.
        nid = symbol_id(
            repo_id, kind, qualified,
            # `_signature_text`, NOT attrs["signature"]: only the former carries
            # trailing cv- and ref-qualifiers, which tree-sitter attaches as siblings of
            # the parameter list. Keying on the parameter list alone collapses
            # `at(int) const` into `at(int)`, and the suite already had tests forbidding
            # exactly that.
            signature=_signature_text(def_ts),
            file_scope=file_scope,
            name=name,
            enclosing_name=(scope[-1] if scope else None),
            lang=lang,
        )
        def_node_to_id[def_ts.id] = nid
        if extra_scope:
            node_attrs["_pending_method_of"] = extra_scope
        nodes.append(Node(
            id=nid, repo=repo_id, kind=kind, name=name,
            # Unprefixed for C/C++ external-linkage symbols: `NS.Box.put` IS the fully
            # qualified name there, and prefixing it with a path was exactly what stopped
            # a header and its .cpp matching on it (C1). Still prefixed elsewhere, where
            # the module the file represents is part of the qualification.
            qualified_name=(f"{file_scope}::{qualified}" if file_scope else qualified),
            file=rel_path, line_start=line, line_end=def_ts.end_point[0] + 1, lang=lang,
            attrs=node_attrs,
        ))
        pending.append((def_ts, qualified, line, nid))

    # Members, macros, typedefs, enum constants and file-scope variables. Emitted after
    # the definition pass so `def_node_to_id` is populated and each one can be contained
    # by the class or namespace it actually sits in rather than by the file.
    for m_kind, m_name_node, m_container in _member_symbols(tree, lang):
        m_name = m_name_node.text.decode("utf-8", "replace")
        if not m_name:
            continue
        m_line = m_name_node.start_point[0] + 1
        # NOT `_enclosing_defs`: that excludes the name's OWN definition node, which is
        # right for a class or function name and wrong here. A data member's nearest
        # def-typed ancestor IS its class -- the scope we want -- so it must be included,
        # or every member lands on the file and loses its qualifier.
        m_enclosing = []
        m_anc = _def_node(m_name_node, def_types)
        while m_anc is not None:
            if m_anc.type in def_types:
                m_enclosing.append(m_anc)
            m_anc = m_anc.parent
        m_scope = [nm for nm in (_def_name_text(n, lang) for n in reversed(m_enclosing))
                   if nm]
        m_qualified = ".".join([*m_scope, m_name])
        # A macro is not scoped by anything -- the preprocessor runs before C++ scope
        # exists -- so it keeps the file in its key even in C/C++, where other symbols
        # drop it. Two headers defining the same macro name really are two macros.
        #
        # Internal linkage applies here for the same reason it does to a function: a
        # `static` file-scope variable, and ANY member reached through an anonymous
        # namespace (including a data member of a class declared inside one), belongs to
        # a single translation unit. Without this a second file's copy merged into the
        # first and its members disappeared with it -- measured on a two-file fixture,
        # where a struct in an anonymous namespace lost one of its two data members.
        m_internal = lang in ("c", "cpp") and _is_internal_linkage(m_container)
        m_file_scope = (rel_path if m_kind == "macro" or file_scope_default or m_internal
                        else None)
        m_id = symbol_id(repo_id, m_kind, m_qualified, file_scope=m_file_scope,
                         name=m_name, lang=lang)
        if m_id in {n.id for n in nodes}:
            continue
        m_attrs: dict = {"linkage": "internal"} if m_internal else {}
        # The declaration as written, for the kinds where it carries the information: a
        # constant's whole meaning is its value, and until now the graph recorded that a
        # constant EXISTED and nothing about what it was set to. A method or a class is
        # described by its signature instead, which `_doc_sig` already supplies.
        if m_kind in DECLARED_VALUE_KINDS:
            decl = _declaration_text(m_name_node, m_container)
            if decl:
                m_attrs["declaration"] = decl
        nodes.append(Node(
            id=m_id, repo=repo_id, kind=m_kind, name=m_name,
            qualified_name=(f"{m_file_scope}::{m_qualified}" if m_file_scope
                            else m_qualified),
            file=rel_path, line_start=m_line, line_end=m_container.end_point[0] + 1,
            lang=lang, attrs=m_attrs,
        ))
        # Contained by the nearest enclosing definition (a class for a data member, a
        # namespace for a file-scope variable), falling back to the file.
        m_parent = m_enclosing[0] if m_enclosing else None
        m_parent_id = def_node_to_id.get(m_parent.id, file_id) if m_parent else file_id
        edges.append(Edge(
            src=m_parent_id, dst=m_id, relation="contains",
            confidence=Confidence.EXTRACTED,
            provenance=Provenance(source_file=rel_path, source_line=m_line,
                                  verified_at=verified_at),
        ))

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

    # Imports: file -> module node. `_extra_imports` is for languages whose dependency
    # is not expressible as an `@import` capture: a Dockerfile's base image is a
    # dependency in exactly this sense, and routing it here rather than through the
    # member pass is what earns it an `imports` edge. Sent through the member pass it
    # became `contains`, so the graph said a Dockerfile CONTAINED nginx, which is a
    # wrong relation dressed as a real one.
    for imp in list(captures.get("import", [])) + _extra_imports(tree, lang):
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
_XSD = "xsd"
_XSL = "xsl"
_PROC = "proc"
_XML = "xml"


#: Estimated peak RSS per byte of source, by extraction kind, MEASURED rather
#: than guessed. Each figure comes from indexing a bounded sample of one kind in
#: an isolated, RSS-monitored child on 2026-08-31
#: (`.superpowers/sdd/kb-index-memory-investigation.md`):
#:
#:     code  19.6x   95.0 MB of C# -> 1,865 MB peak, 82k nodes / 467k edges
#:     sql    5.0x   12.5 MB       ->    62 MB peak
#:     xsd    4.3x   60.2 MB       ->   258 MB peak
#:     xml    3.5x  201.1 MB       ->   704 MB peak
#:
#: Rounded UP, because a budget that under-estimates does not bound anything.
#: Code is six times worse per byte than XML and the reason is edge count, not
#: node count: a 5.7:1 edge-to-node ratio on a codebase with heavy name
#: collision. Unmeasured kinds take the SQL figure rather than the code one, so
#: an unmeasured kind cannot silently inherit the worst case and refuse a repo
#: on a number nobody measured.
KIND_COST = {
    _CODE: 20.0,
    _SQL: 5.0,
    _PROC: 5.0,
    _XSD: 5.0,
    _XSL: 5.0,
    _XML: 4.0,
    _HCL: 5.0,
    _ADR: 4.0,
    _MANIFEST: 4.0,
}

#: The cost charged for a kind with no measured weight.
DEFAULT_KIND_COST = 5.0


class RepoTooLarge(Exception):
    """A repository whose estimated peak memory exceeds the caller's budget.

    Raised BEFORE any file is parsed. That timing is the whole point: the
    existing guards cannot help here. ``max_file_bytes`` is per-FILE and never
    fires on a repository that is wide rather than deep, and the shard-size
    guard in ``kb/cmds/index.py`` checks node and edge counts AFTER the shard
    is built, which is after the memory has already been spent.
    """

    def __init__(self, repo_id, estimate_bytes, budget_bytes, breakdown):
        self.repo_id = repo_id
        self.estimate_bytes = estimate_bytes
        self.budget_bytes = budget_bytes
        self.breakdown = breakdown
        top = ", ".join(
            f"{k} {v / 1048576:.0f} MB" for k, v in
            sorted(breakdown.items(), key=lambda kv: -kv[1])[:3])
        super().__init__(
            f"{repo_id}: estimated {estimate_bytes / 1073741824:.1f} GB peak "
            f"exceeds the {budget_bytes / 1073741824:.1f} GB per-repository "
            f"budget ({top}). Raise kb.max_repo_memory to index it anyway, or "
            # `kb.languages`, not `--languages`: there has never been such a flag.
            # This advice named one from the day it shipped, so a reader who took it
            # got "unrecognized arguments" and no way to act on the error.
            f"narrow it with kb.languages.")

    def __reduce__(self):
        """Rebuild from the four values ``__init__`` needs, not from ``args``.

        Python reconstructs an exception as ``cls(*self.args)``, and ``args``
        holds only the formatted message passed to ``Exception.__init__``, so
        the default rebuild is three arguments short and raises TypeError.

        That TypeError lands in ``ProcessPoolExecutor``'s manager thread, inside
        ``result_reader.recv()``, where there is no future to attribute it to.
        The pool is declared broken, every healthy worker is sent SIGTERM, and
        every pending future raises ``BrokenProcessPool``. One repository
        refused by the memory budget ended a whole workspace run that way.

        ``pickle.dumps`` succeeds without this, so a test must round-trip.
        """
        return (self.__class__,
                (self.repo_id, self.estimate_bytes, self.budget_bytes, self.breakdown))


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
    #: Extension -> count for files no parser claimed. A Swift, Dart or Vue tree used to
    #: index to `0 nodes, 0 edges`, exit 0, and report `skipped 0 generated, 0 oversized,
    #: 0 ignored` -- every counter truthfully zero, and the reason invisible. Counting
    #: BY EXTENSION rather than as one number is deliberate: `kind is None` also fires
    #: for READMEs, lockfiles and images, so a bare total would be noise. The extensions
    #: let the reader see at a glance whether the miss is source code.
    unsupported_exts: dict = field(default_factory=dict)
    # language -> files skipped because its OPTIONAL grammar is not installed. Kept apart
    # from `unsupported_exts` because the two have different fixes, and reporting one as
    # the other tells the user the wrong thing to do about it.
    missing_grammars: dict = field(default_factory=dict)


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
    # A template's use of a name a stylesheet defines: `class="btn-primary"` and the
    # element's own tag. Cross-domain by design, exactly like the SQL stream: markup
    # refers to a selector the same way code refers to a table.
    styles: list[tuple[str, str, str, int]] = field(default_factory=list)
    # An XML-Schema component naming another one: `type=`, `base=`, `ref=`. Cross-domain
    # like the SQL stream, and kept apart from it on purpose -- a schema name and a table
    # name must never resolve to each other.
    schema: list[tuple[str, str, str, int]] = field(default_factory=list)
    # Every bare name a file reads. Only those matching a real constant node survive
    # resolution, so this list is large and its resolved output is small -- the same shape
    # as `calls`, which also emits every candidate name and filters by target kind.
    constant_uses: list[tuple[str, str, str, int]] = field(default_factory=list)

    def resolved_edges(self, by_id: dict[str, Node],
                       *, stats: dict | None = None) -> list[Edge]:
        # Target-kind sets are module-level names defined further down the file,
        # so they are read here at call time rather than bound at class creation.
        # The first flag is same-language resolution: a call/inheritance must
        # stay inside one language family, while the HCL/SQL streams are
        # cross-domain by design (code reads a table) -- see _resolve_name_refs.
        #
        # Per-site retention is NOT a flag here -- it is derived from PER_SITE_RELATIONS
        # so that this file never names the per-site relation itself. "Where is this
        # called" is a question about invocations, so every call site earns its own edge
        # citing its own line; the other streams stay one-edge-per-pair, because
        # retaining every mention of a base class or every reference to a table is a
        # different question that has not been asked. The degree consumers read the same
        # constant, which is what stops the producer and the consumers disagreeing.
        streams = (
            (self.calls, "calls", _CALLABLE_KINDS, True),
            (self.inherits, "inherits", _INHERITABLE_KINDS, True),
            (self.hcl, "depends_on", _HCL_KINDS, False),
            (self.sql, "references", _SQL_KINDS, False),
            (self.data_reads, "reads", _SQL_KINDS, False),
            (self.data_writes, "writes", _SQL_KINDS, False),
            # `references`, not a new relation: it already means "this thing names that
            # thing" for SQL, it is already in impact's default traversal, and the target
            # kind distinguishes a stylesheet selector from a table without a second word
            # for the same idea.
            (self.styles, "references", _STYLE_KINDS, False),
            # LAST on purpose. This class's docstring notes that shard edge order depends on
            # this sequence, so a new stream goes on the end, where it cannot renumber any
            # edge that already existed. `uses` is its own relation rather than a reuse of
            # `references` because it needs per-site retention (see PER_SITE_RELATIONS) and
            # that set is keyed by relation -- widening `references` would silently turn
            # every SQL and stylesheet reference into one edge per mention too.
            (self.constant_uses, "uses", _CONSTANT_KINDS, True),
            # After `uses` for the reason given above: shard edge order follows this
            # sequence, so a new stream goes on the end where it renumbers nothing that
            # already existed. `references` rather than a new relation, because it already
            # means "this thing names that thing" and the target kind is what distinguishes
            # a schema component from a table or a stylesheet selector.
            (self.schema, "references", _SCHEMA_KINDS, False),
        )
        edges: list[Edge] = []
        for refs, relation, target_kinds, same_language in streams:
            edges.extend(_resolve_name_refs(
                refs, by_id, relation=relation, target_kinds=target_kinds,
                same_language=same_language,
                per_site=relation in PER_SITE_RELATIONS,
                stats=stats if relation == "calls" else None))
        return edges


# Test macros that take a body, so tree-sitter reports them as a function_definition
# named after the macro. Measured on a large legacy C++ tree: TEST_F 1,855, TEST 962,
# TEST_P 3 -- 2,820 nodes, 6.8% of all function+method nodes, every one of them called
# `TEST`/`TEST_F` instead of the case it defines. The googletest family plus the two
# obvious siblings; deliberately a closed list rather than "any ALL_CAPS name", because
# a constructor also has no return type and a guess there would mangle real symbols.
# Verified 2026-08-11 that each of these actually parses as a `function_definition`,
# which is the only shape this code can reach. Catch2's `TEST_CASE("a name")` does NOT:
# with a string-literal argument the grammar produces `expression_statement` +
# `compound_statement`, so no definition node exists to rename and Catch2 needs a
# separate mechanism. It is left out rather than listed and silently unsupported.
_TEST_MACROS = frozenset({
    "TEST", "TEST_F", "TEST_P", "TYPED_TEST", "TYPED_TEST_P",
})

# `MACRO(argument, argument)` -- the declarator's own text, so it needs no assumption
# about how the grammar shaped the arguments (they are not real parameters, and the
# grammar's reading of them varies with what the names happen to look like).
_MACRO_CALL = re.compile(r"^[A-Za-z_]\w*\s*\((.*)\)\s*$", re.DOTALL)


def _test_macro_case(def_ts: ts.Node, name: str, lang: str) -> tuple[str, str] | None:
    """``(suite, case)`` when this "definition" is really a test-macro invocation.

    Returns None for everything else, including a constructor or destructor, which
    also has no return type. That is why the macro name is matched against a closed
    set first: absence of a return type alone cannot tell `TEST(A, B)` from `C::C()`.
    """
    if lang not in ("c", "cpp") or name not in _TEST_MACROS:
        return None
    if def_ts.child_by_field_name("type") is not None:
        return None                      # a real definition declares a return type
    dec = def_ts.child_by_field_name("declarator")
    if dec is None:
        return None
    m = _MACRO_CALL.match(dec.text.decode("utf-8", "replace").strip())
    if not m:
        return None
    args = [a.strip().strip('"').strip() for a in m.group(1).split(",")]
    args = [a for a in args if a]
    if not args:
        return None
    # Two arguments is the googletest shape (suite, case). One is Catch2's TEST_CASE.
    return (args[0], args[1]) if len(args) >= 2 else ("", args[0])


# Depth cap on the two linkage walks below. Generous compared to the 8/10 used by the
# qualifier walks, because a member can sit inside a class inside a class inside an
# anonymous namespace and each level contributes intermediate list nodes -- but still
# bounded, so a pathological tree cannot turn this into an unbounded climb.
_MAX_LINKAGE_WALK = 32

# Scopes that make a declaration a CLASS MEMBER rather than a namespace-scope entity.
# `static` means two unrelated things in C++ and this set is what tells them apart:
# at namespace scope it means internal linkage, inside a class it declares a static
# member, which has EXTERNAL linkage.
_CLASS_SCOPES = frozenset({"class_specifier", "struct_specifier", "union_specifier",
                           "field_declaration_list"})


def _in_anonymous_namespace(node: ts.Node) -> bool:
    """Whether ``node`` sits anywhere inside an unnamed ``namespace { ... }``.

    An ancestor test, not a child test: the namespace encloses the definition rather
    than decorating it. Verified against the grammar -- an anonymous namespace is a
    ``namespace_definition`` whose ``name`` field is absent, while a named, an ``inline``
    and a C++17 nested ``namespace A::B`` all carry one, and ``extern "C"`` is a
    different node type (``linkage_specification``) which does NOT confer internal
    linkage and is correctly not matched here.
    """
    cur, seen = node.parent, 0
    while cur is not None and seen < _MAX_LINKAGE_WALK:
        if cur.type == "namespace_definition" and cur.child_by_field_name("name") is None:
            return True
        cur = cur.parent
        seen += 1
    return False


def _at_namespace_scope(node: ts.Node) -> bool:
    """Whether ``node``'s nearest enclosing scope is a namespace or the file, not a class.

    Deliberately separate from :func:`_is_file_scope`, which answers "not inside a
    function body" and stops at no class boundary. Only this question decides what a
    ``static`` keyword means.
    """
    cur, seen = node.parent, 0
    while cur is not None and seen < _MAX_LINKAGE_WALK:
        if cur.type in _CLASS_SCOPES:
            return False
        if cur.type in _FILE_SCOPES:
            return True
        cur = cur.parent
        seen += 1
    return False


def _is_internal_linkage(def_ts: ts.Node) -> bool:
    """Whether this definition is file-scoped BY LANGUAGE RULE rather than by accident.

    A `static` free function has internal linkage: two translation units may each
    define their own, with the same name and signature, and they are genuinely
    different symbols. So the file has to stay in their key, or the two merge into one
    node and every caller of either appears to call a single shared function.

    **Anonymous-namespace members have exactly the same property**, and measurement
    showed the consequence plainly: two files each declaring `namespace { int tally(int); }`
    produced ONE node, so the second file's definition vanished and its caller resolved
    to the first file's function -- an edge that cannot exist.

    The `static` half is gated on namespace scope because the keyword means two
    unrelated things. At namespace scope it means internal linkage; inside a class it
    declares a static member, which has **external** linkage and must keep matching
    across the header/source split like any other member.
    """
    if _in_anonymous_namespace(def_ts):
        return True
    return _at_namespace_scope(def_ts) and any(
        ch.type == "storage_class_specifier" and ch.text == b"static"
        for ch in def_ts.children)


def _symbol_slug(kind: str, qualified: str, name: str, enclosing_name: str | None) -> str:
    """The readable half of a node id.

    `normalize_id` folds every non-word character to `_`, so a constructor and its
    destructor both reduce to the class name (C2 in the gap register): `C::C` and
    `C::~C` are indistinguishable after normalisation. The digest keeps them apart
    regardless, but an id a person cannot tell apart is barely better than an opaque
    one, so the marker is spelled out here rather than left to the hash.
    """
    marker = ""
    if name.startswith("~"):
        marker = "dtor"
    elif enclosing_name and name == enclosing_name:
        marker = "ctor"
    return make_id(qualified, marker) or make_id(kind, name) or "sym"


def symbol_id(repo_id: str, kind: str, qualified: str, *, signature: str | None = None,
              file_scope: str | None = None, name: str = "",
              enclosing_name: str | None = None, lang: str = "") -> str:
    """A node id that survives an edit and matches across a header/source split.

    Ids used to be ``make_id(repo, rel_path, qualified, line)``, which made two things
    impossible. The path meant a declaration and its out-of-line definition could never
    be the same symbol (C1). The line meant editing a file above a symbol changed its
    id, so every edge, vector and wiki reference to it churned for no semantic reason.

    Shape is ``<readable-slug>_<8 hex>``. The slug keeps ids legible in answers,
    dashboards and MCP arguments, which is a real property of this project's output.
    The digest is what makes them CORRECT: it covers the exact key below, so two symbols
    the slug cannot separate still get different ids.

    ``signature`` is part of the key because overloads share everything else -- measured
    on a large legacy C++ tree, 1,038 qualified names occur more than once in a single
    file, and until now only the line number told them apart. ``file_scope`` is passed
    only for internal-linkage symbols, which are file-scoped by language rule.
    """
    key = "\0".join([repo_id, lang, kind, qualified, signature or "", file_scope or ""])
    digest = hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:8]
    return f"{_symbol_slug(kind, qualified, name, enclosing_name)}_{digest}"


# Node types that wrap a declarator without changing what is being declared.
# `int* p`, `int a[4]`, `int& r` and `int x = 0` all still declare ONE name; the name
# sits at the bottom of a chain of these.
_DECL_WRAPPERS = frozenset({
    "init_declarator", "pointer_declarator", "array_declarator",
    "reference_declarator", "parenthesized_declarator",
})

# Scopes at which a `declaration` is a genuine file-level variable. Anything else --
# overwhelmingly a function body -- is a local, and there are 235,010 of those in a
# single large tree against 5,965 real globals. Counting `declaration` without this
# check emits 4.5x the intended node count for this kind alone.
_FILE_SCOPES = frozenset({"translation_unit", "namespace_definition", "linkage_specification"})


def _declared_name(node: ts.Node) -> ts.Node | None:
    """The identifier a declaration declares, or None if it declares no plain name.

    Walks down through wrapper declarators. Returns None on hitting a
    `function_declarator`, which is how a member FUNCTION declaration is told apart
    from a data member: `void draw(int);` inside a class body is a `field_declaration`
    to tree-sitter, exactly like `int width_;` is. Measured on a large legacy tree,
    8,970 member-function declarations take that shape, and treating them as data
    members would invent 8,970 fields that are really methods.
    """
    cur = node.child_by_field_name("declarator")
    seen = 0
    while cur is not None and seen < 8:
        if cur.type == "function_declarator":
            return None
        if cur.type in ("field_identifier", "identifier"):
            return cur
        if cur.type in _DECL_WRAPPERS:
            nxt = cur.child_by_field_name("declarator")
            if nxt is None:
                # `reference_declarator` does not expose its child through the
                # `declarator` field, so fall back to the first named child.
                nxt = next((c for c in cur.named_children
                            if c.type in _DECL_WRAPPERS
                            or c.type in ("field_identifier", "identifier")), None)
            cur = nxt
        else:
            return None
        seen += 1
    return None


def _is_file_scope(node: ts.Node) -> bool:
    """Whether a declaration sits at file or namespace scope rather than inside a body."""
    parent = node.parent
    while parent is not None:
        if parent.type in _FILE_SCOPES:
            return True
        if parent.type in ("function_definition", "compound_statement",
                           "for_statement", "while_statement", "if_statement"):
            return False
        parent = parent.parent
    return False


_JS_LANGS = ("javascript", "typescript", "tsx")
# A `const`/`let`/`var` binding, and the `var` spelling, at whatever scope we reached.
_JS_BINDINGS = ("lexical_declaration", "variable_declaration")


def _js_member_symbols(tree: ts.Tree) -> list[tuple[str, ts.Node, ts.Node]]:
    """Module-scope bindings and class fields for the JavaScript family.

    Descends deliberately (module children, then class bodies) instead of walking the
    whole tree and asking "is this file scope?". The C/C++ predicate `_is_file_scope`
    cannot be reused here and could not simply be widened: it tests for
    `function_definition`, `compound_statement` and `for_statement`, none of which the
    JavaScript grammar produces. Descending says which scope we are in by construction,
    so a `const` inside a function body is never reached rather than reached and then
    rejected by a test that has to be right about every enclosing form.

    Verified against the grammar rather than written from memory: `export const NAME`
    arrives as an `export_statement` WRAPPING a `lexical_declaration`, so the export
    keyword has to be stepped through or every exported binding is missed. Class members
    are `field_definition`, whose name is a `property_identifier` (or a
    `private_property_identifier` for a `#private` field).
    """
    out: list[tuple[str, ts.Node, ts.Node]] = []

    def _bindings(decl: ts.Node, kind: str) -> None:
        for d in decl.children:
            if d.type == "variable_declarator":
                nm = d.child_by_field_name("name")
                # Only a plain identifier. A destructuring pattern binds several names
                # through an `object_pattern`/`array_pattern`, and inventing one node
                # named after the whole pattern would be a symbol nobody wrote.
                if nm is not None and nm.type == "identifier":
                    out.append((kind, nm, decl))

    def _class_fields(cls: ts.Node) -> None:
        body = cls.child_by_field_name("body")
        if body is None:
            return
        for member in body.children:
            if member.type == "field_definition":
                nm = member.child_by_field_name("property")
                if nm is not None and nm.type in ("property_identifier",
                                                  "private_property_identifier"):
                    out.append(("field", nm, member))

    def _scan_module_level(node: ts.Node) -> None:
        for ch in node.children:
            if ch.type == "export_statement":
                _scan_module_level(ch)          # step through `export`
            elif ch.type in _JS_BINDINGS:
                _bindings(ch, "global_variable")
            elif ch.type in ("class_declaration", "class"):
                _class_fields(ch)

    _scan_module_level(tree.root_node)
    # Classes nested anywhere (inside a function, an IIFE, an export default) still have
    # fields worth emitting, and their containment is resolved by the caller from the
    # class ancestor, so reaching them here is enough.
    stack = list(tree.root_node.children)
    seen: set[int] = set()
    while stack:
        n = stack.pop()
        if n.type in ("class_declaration", "class") and n.id not in seen:
            seen.add(n.id)
            _class_fields(n)
        stack.extend(n.children)
    return out


def _dunder_main_name_node(if_stmt: ts.Node) -> ts.Node | None:
    """The `__main__` literal of an `if __name__ == "__main__":` guard, or None.

    Returns the string's CONTENT node so the graph node is named `__main__` rather than
    `"__main__"` with its quotes, and so its line is the guard's own line.

    Both operands are checked, because `if "__main__" == __name__:` is legal Python that
    a linter will not rewrite. Checking one side is the kind of half-match that reads as
    complete until somebody writes the other order.
    """
    for cmp_node in if_stmt.children:
        if cmp_node.type != "comparison_operator":
            continue
        parts = [c for c in cmp_node.children if c.type in ("identifier", "string")]
        if len(parts) != 2:
            continue
        names = {p.text for p in parts if p.type == "identifier"}
        if b"__name__" not in names:
            continue
        for p in parts:
            if p.type != "string":
                continue
            for piece in p.children:
                if piece.type == "string_content" and piece.text == b"__main__":
                    return piece
    return None


def _py_member_symbols(tree: ts.Tree) -> list[tuple[str, ts.Node, ts.Node]]:
    """Module-level names and class attributes for Python.

    The shape here is the one most easily got wrong from memory: a module-level
    assignment is NOT an `assignment` child of the module. It is an
    `expression_statement` WRAPPING an `assignment`, and the same is true inside a class
    body. Checked against the grammar before this was written.

    Annotated assignments (`x: int = 3`) arrive the same way, so both are covered by
    reading the assignment's `left` field.
    """
    out: list[tuple[str, ts.Node, ts.Node]] = []

    def _assigned_names(stmt: ts.Node, kind: str) -> None:
        for a in stmt.children:
            if a.type != "assignment":
                continue
            lhs = a.child_by_field_name("left")
            # A tuple target (`a, b = ...`) binds several names through a
            # `pattern_list`; emitting one node for the pattern would name nothing.
            if lhs is not None and lhs.type == "identifier":
                out.append((kind, lhs, stmt))

    for ch in tree.root_node.children:
        if ch.type == "expression_statement":
            _assigned_names(ch, "global_variable")
        # Python's entry point is not a definition at all, so unlike every other
        # language's it cannot be re-kinded from one: `if __name__ == "__main__":` is an
        # `if_statement`, and the thing that makes it special lives in its condition.
        # This is the same shape as the CSS pseudo-class problem, where a node type
        # cannot express the distinction and the extraction has to be code.
        #
        # The node is named `__main__`, which is what the file is called when it runs
        # that way. Naming it after the module would collide with the module node, and
        # naming it after whatever the block calls would be a guess about which of
        # several statements is "the" entry.
        elif ch.type == "if_statement":
            named = _dunder_main_name_node(ch)
            if named is not None:
                out.append(("entry_point", named, ch))

    stack = list(tree.root_node.children)
    while stack:
        n = stack.pop()
        if n.type == "class_definition":
            body = n.child_by_field_name("body")
            if body is not None:
                for stmt in body.children:
                    if stmt.type == "expression_statement":
                        _assigned_names(stmt, "field")
        stack.extend(n.children)
    return out


# Which Elixir macro introduces which kind. Anything not listed is left to
# `_DEF_TYPES`, so an unrecognised `defsomething` still becomes a function rather than
# vanishing: a name in the graph under a slightly wrong kind beats no name at all.
_ELIXIR_KINDS = {
    "defmodule": "module", "defprotocol": "interface", "defimpl": "class",
    "defstruct": "struct", "def": "function", "defp": "function",
    "defmacro": "function", "defmacrop": "function", "defdelegate": "function",
    "defguard": "function", "defguardp": "function",
}


def _elixir_defines(name_node: ts.Node) -> bool:
    """Whether an Elixir `@def` capture really sits under a DEFINITION macro.

    The query has to match `(call target: (identifier) (arguments (alias)))` to reach
    `defmodule Engine`, and that shape also matches `use ExUnit.Case` and `import Engine`,
    which are directives. Measured before this existed: a test module produced function
    nodes named `EngineTest.ExUnit.Case` and `EngineTest.Engine`, symbols nobody wrote.

    tree-sitter query predicates would express this, but the rest of this module filters in
    Python and one mechanism is easier to follow than two.
    """
    node = name_node.parent
    # alias -> arguments -> call, or identifier -> call -> arguments -> call.
    for _ in range(3):
        if node is None:
            return False
        if node.type == "call":
            target = node.child_by_field_name("target")
            if target is not None and target.type == "identifier":
                if target.text.decode("utf-8", "replace") in _ELIXIR_KINDS:
                    return True
        node = node.parent
    return False


def _def_name_text(node: ts.Node, lang: str) -> str | None:
    """The declared name of a definition node, for building a qualified name.

    Almost every grammar puts it in a `name` field. Elixir does not: its definitions are
    `call` nodes with a `target` and an `arguments` list, so a `defmodule Engine` has no
    `name` field at all and the scope walk silently produced nothing. Functions came out
    as `start` rather than `Engine.start`, which is the form Elixir code is actually
    written and searched in.
    """
    if lang == "elixir" and node.type == "call":
        # By child TYPE, not `child_by_field_name("arguments")`: Elixir exposes the
        # argument list as a named child and not as a field, so the field lookup returns
        # None and the whole scope walk silently produced nothing. Measured, after the
        # first version of this function did exactly that.
        # ONLY module-like macros contribute scope. In Elixir a function is scoped by its
        # module and never by another function -- there is no nested `def` -- so counting
        # a `def` as a scope produced `Engine.start.start`, the function's own name twice.
        # This is the whole reason the check is by macro rather than by node type: `def`
        # and `defmodule` are the same node type and only one of them is a scope.
        target = node.child_by_field_name("target")
        macro = target.text.decode("utf-8", "replace") if target is not None else ""
        if _ELIXIR_KINDS.get(macro) not in ("module", "interface", "class"):
            return None
        args = next((c for c in node.children if c.type == "arguments"), None)
        if args is not None:
            for child in args.children:
                if child.type in ("alias", "identifier"):
                    return child.text.decode("utf-8", "replace")
        return None
    nm = node.child_by_field_name("name")
    return nm.text.decode("utf-8", "replace") if nm is not None else None


# The name a language spells its process entry point with. Case matters: C# is `Main`
# and everything else here is `main`, which is exactly the sort of detail that silently
# halves a feature's coverage if it is assumed rather than checked.
_ENTRY_NAMES = {
    "go": "main", "rust": "main", "c": "main", "cpp": "main", "kotlin": "main",
    "java": "main", "csharp": "Main",
}

# Languages where being the entry point means being at the TOP LEVEL of the file. A
# `main` nested inside another function is a local helper, not the way in.
_ENTRY_TOPLEVEL_LANGS = frozenset({"go", "rust", "c", "cpp", "kotlin"})

# ...and the two where it is a METHOD, so the signal is a modifier rather than depth.
# Java spells the modifier list `modifiers` and C# repeats singular `modifier` nodes.
_ENTRY_MODIFIER_NODES = {"java": "modifiers", "csharp": "modifier"}


def _is_entry_point(lang: str, def_ts: ts.Node, name: str) -> bool:
    """Whether this definition is how the program is STARTED, not merely called `main`.

    Each language needs a second condition, and the second condition is the whole point.
    Without it every helper named `main` anywhere in a repository becomes an advertised
    way to run the project, which is a name nobody wrote as an entry point appearing in
    the graph as though somebody had.

    - **Go**: the package must BE `main`. A `func main()` in a helper package is an
      ordinary function and Go will not build it as a command. This is the case most
      likely to be got wrong, because the function looks identical.
    - **Rust, Kotlin, C, C++**: the definition must sit at the top level of the file.
    - **Java, C#**: the method must be `static`. An instance method named `main` is not
      an entry point in either language.
    """
    if _ENTRY_NAMES.get(lang) != name:
        return False
    if lang in _ENTRY_TOPLEVEL_LANGS:
        parent = def_ts.parent
        if parent is not None and parent.parent is not None:
            return False
        if lang == "go":
            return _go_package_name(def_ts) == "main"
        return True
    holder = _ENTRY_MODIFIER_NODES.get(lang)
    if holder is None:
        return False
    return any(c.type == holder and b"static" in c.text for c in def_ts.children)


def _go_package_name(node: ts.Node) -> str | None:
    """The `package X` name of the file ``node`` belongs to, or None.

    Read from the tree rather than threaded in, because the entry-point decision is the
    only thing that needs it and a parameter for one language's one question would reach
    every call site of the kind computation.
    """
    root = node
    while root.parent is not None:
        root = root.parent
    for child in root.children:
        if child.type == "package_clause":
            for part in child.children:
                if part.type == "package_identifier":
                    return part.text.decode("utf-8", "replace")
    return None


def _lang_kind(lang: str, def_ts: ts.Node) -> str | None:
    """A kind the node TYPE cannot express, or None to use `_DEF_TYPES`.

    Only Elixir needs this today. Its `defmodule` / `def` / `defp` are all `call` nodes,
    so the node type says "call" for a module, a function and a private function alike.
    The macro name is the first child of the same call.
    """
    if lang != "elixir" or def_ts.type != "call":
        return None
    target = def_ts.child_by_field_name("target")
    if target is None or target.type != "identifier":
        return None
    return _ELIXIR_KINDS.get(target.text.decode("utf-8", "replace"))


def _css_symbols(tree: ts.Tree) -> list[tuple[str, ts.Node, ts.Node]]:
    """Selectors a stylesheet defines: classes, ids and element types.

    THE TRAP, and the reason this is code rather than a query: in `a.nav:hover` the
    pseudo-class `hover` is the SAME `class_name` node type as the real class `nav`. A
    query matching `class_name` invents a CSS class called `hover` on every hover rule in
    the codebase. The two are told apart only by their parent, so the walk below descends
    from `class_selector` and `id_selector` deliberately and never matches a bare name.
    """
    out: list[tuple[str, ts.Node, ts.Node]] = []
    seen: set[int] = set()
    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        if n.type == "class_selector":
            # The LAST class_name child, since `a.nav` nests tag_name and class_name as
            # siblings and only the class_name is this selector's own name.
            for ch in n.children:
                if ch.type == "class_name" and ch.id not in seen:
                    seen.add(ch.id)
                    out.append(("css_class", ch, n))
        elif n.type == "id_selector":
            for ch in n.children:
                if ch.type == "id_name" and ch.id not in seen:
                    seen.add(ch.id)
                    out.append(("css_id", ch, n))
        elif n.type == "tag_name" and n.id not in seen:
            # A type selector styles EVERY element of that name, so it is the highest
            # fanout thing in this tier by construction.
            parent = n.parent
            if parent is not None and parent.type in ("selectors", "class_selector",
                                                      "pseudo_class_selector"):
                seen.add(n.id)
                out.append(("css_element", n, parent))
        stack.extend(n.children)
    return out


def _html_symbols(tree: ts.Tree) -> list[tuple[str, ts.Node, ts.Node]]:
    """What an HTML file DEFINES: the `id` of each element.

    A page's `class=` attributes and its tag names are REFERENCES to what a stylesheet
    defines, not definitions, so they are not returned here. They become edges instead,
    resolved repo-wide like any other unresolved name.
    """
    out: list[tuple[str, ts.Node, ts.Node]] = []
    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        if n.type == "attribute":
            name = next((c for c in n.children if c.type == "attribute_name"), None)
            value = next((c for c in n.children
                          if c.type in ("quoted_attribute_value", "attribute_value")), None)
            if (name is not None and value is not None
                    and name.text.decode("utf-8", "replace").lower() == "id"):
                inner = next((c for c in value.children if c.type == "attribute_value"),
                             value)
                out.append(("html_id", inner, n))
        stack.extend(n.children)
    return out


def _make_symbols(tree: ts.Tree) -> list[tuple[str, ts.Node, ts.Node]]:
    """Make targets: the names another rule, or a person at a shell, invokes by name.

    A rule's `targets` node holds one or more `word` children, because `build test:`
    declares two targets sharing one recipe. Each becomes its own node rather than one
    node named "build test", which is what a naive read of the node's text would give.

    Reached by NAME routing, not by extension: a bare `Makefile` has none. That routing
    is what this extractor waited for, and `LANG_BY_NAME` is what supplies it.

    A target whose name starts with a dot is skipped. Those are make's own special
    targets (`.PHONY`, `.SUFFIXES`, `.DEFAULT`) and legacy suffix rules (`.c.o`), never
    names a person or a CI job invokes, and emitting them puts a symbol in the graph
    that nobody wrote -- the same defect Elixir's `use`/`import` directives produced.

    Variables (`CC = gcc`) are NOT extracted. `$(CC)` is a real reference and capturing
    the definitions would be worth doing; it is left out rather than half-done, and
    stated here so the gap stays visible instead of being assumed closed.
    """
    out: list[tuple[str, ts.Node, ts.Node]] = []
    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        if n.type == "targets":
            for ch in n.children:
                if ch.type == "word" and not ch.text.startswith(b"."):
                    out.append(("make_target", ch, n.parent or n))
        stack.extend(n.children)
    return out


def _dockerfile_stages(tree: ts.Tree) -> list[ts.Node]:
    """The `image_alias` node of every `FROM ... AS name`, in file order."""
    return [c
            for n in _descendants(tree.root_node) if n.type == "from_instruction"
            for c in n.children if c.type == "image_alias"]


def _dockerfile_symbols(tree: ts.Tree) -> list[tuple[str, ts.Node, ts.Node]]:
    """A Dockerfile's build stages, which are the names its other instructions refer to.

    `FROM node:20 AS builder` declares a stage named `builder`, which `COPY --from=builder`
    and a later `FROM builder` then refer to.

    The base images are NOT here: they go through `_extra_imports`, because a base image
    is something the file depends on rather than something it contains, and the member
    pass produces `contains` edges.

    NOT extracted, and named so the gap stays visible: the `COPY --from=` reference back
    to a stage. Stage names are FILE-LOCAL, and the cross-file name resolver would happily
    link a `builder` stage in one Dockerfile to a `builder` stage in another, which is a
    wrong edge rather than a missing one.
    """
    return [("dockerfile_stage", s, s.parent or s) for s in _dockerfile_stages(tree)]


def _dockerfile_base_images(tree: ts.Tree) -> list[ts.Node]:
    """The EXTERNAL images a Dockerfile builds on, which is a decision the grammar does
    not make for us.

    In `FROM builder AS test` the `image_name` is a stage declared earlier in the same
    file; in `FROM nginx:alpine` it is an image somebody pulls. Both are the same node
    type. So the stages are collected first and an `image_name` matching one is dropped,
    rather than emitted as a dependency on a container image that does not exist. That
    ordering is the only reason this walks the tree twice.

    The tag is left off the name: `node:20` and `node:22` are one image at two versions,
    and the question worth answering across a fleet is which repositories build on node.
    """
    stage_names = {s.text for s in _dockerfile_stages(tree)}
    return [name
            for n in _descendants(tree.root_node) if n.type == "from_instruction"
            for spec in n.children if spec.type == "image_spec"
            for name in spec.children
            if name.type == "image_name" and name.text not in stage_names]


def _extra_imports(tree: ts.Tree, lang: str) -> list[ts.Node]:
    """Dependency-name nodes for languages whose imports no `@import` capture can express.

    Empty for every language whose query already captures its imports, which is all of
    them but one.
    """
    return _dockerfile_base_images(tree) if lang == "dockerfile" else []


def _descendants(root: ts.Node) -> Iterator[ts.Node]:
    """Every node under ``root``, root included. Order is unspecified."""
    stack = [root]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.children)


def _nix_symbols(tree: ts.Tree) -> list[tuple[str, ts.Node, ts.Node]]:
    """Nix attribute names, which are what another expression refers to by name."""
    out: list[tuple[str, ts.Node, ts.Node]] = []
    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        if n.type == "binding":
            attr = next((c for c in n.children if c.type == "attrpath"), None)
            if attr is not None:
                out.append(("nix_attr", attr, n))
        stack.extend(n.children)
    return out


def extract_style_refs(repo_id: str, rel_path: str, source: bytes,
                       lang: str) -> list[tuple[str, str, str, int]]:
    """What an HTML file REFERENCES: every class name it uses, and every element tag.

    A stylesheet defines `.btn-primary`; a page uses it. Emitting the use as a definition
    would give one name two definitions and make "where is this defined" ambiguous, so the
    use is a reference resolved repo-wide, exactly like a call to a function in another
    file.

    Element tags are references too: a CSS rule keyed on `button` styles every button on
    the site, so the tag is a use of that rule's name. They are high fanout by nature, and
    the same caps that protect dense symbols apply.

    Returns the unresolved `(src_id, target_name, file, line)` shape every other reference
    stream uses. The source is the FILE: the question this answers is "which stylesheet
    defines the class this page uses", and the page is the thing asking.
    """
    if lang != "html":
        return []
    out: list[tuple[str, str, str, int]] = []
    file_id = make_id(repo_id, rel_path)
    tree = _parser("html").parse(source)
    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        if n.type in ("start_tag", "self_closing_tag"):
            tag = next((c for c in n.children if c.type == "tag_name"), None)
            if tag is not None:
                out.append((file_id, tag.text.decode("utf-8", "replace"),
                            rel_path, tag.start_point[0] + 1))
            for attr in (c for c in n.children if c.type == "attribute"):
                name = next((c for c in attr.children if c.type == "attribute_name"), None)
                value = next((c for c in attr.children
                              if c.type in ("quoted_attribute_value", "attribute_value")),
                             None)
                if name is None or value is None:
                    continue
                if name.text.decode("utf-8", "replace").lower() != "class":
                    continue
                inner = next((c for c in value.children if c.type == "attribute_value"),
                             value)
                # `class="wrap grid"` is TWO names. Splitting is the whole point: an
                # unsplit attribute resolves to nothing and the edge is silently lost.
                for word in inner.text.decode("utf-8", "replace").split():
                    out.append((file_id, word, rel_path, inner.start_point[0] + 1))
        stack.extend(n.children)
    return out


# The kinds whose declaration text is worth storing. These are the kinds whose whole meaning
# IS the right-hand side: a constant named `MAX_RETRY` tells a reader nothing without the `3`.
# A function or class is described by its signature instead, which `_doc_sig` already carries,
# so adding a declaration there would duplicate a field and bloat every shard.
DECLARED_VALUE_KINDS = frozenset({
    "global_variable", "enum_constant", "macro", "field",
})

# The node that declares ONE name, per language. Measured against the real grammars rather
# than assumed, because the obvious choice is wrong: the enclosing statement is SHARED by
# every name it declares, so `var a = 1, b = 2;` would report `b` as being declared
# `var a = 1, b = 2` -- true of the statement, and misleading about `b`.
_DECLARATOR_NODES = frozenset({
    "variable_declarator",   # JavaScript, TypeScript, TSX
    "assignment",            # Python
    "init_declarator",       # C, C++
    "enumerator",            # C, C++ enum members: already `FAST = 1`
})

# How much of a declaration to keep. A generated table or a long literal is a declaration
# thousands of characters wide (measured: a 200-element list came to 608), and neither a wiki
# page nor an inferred-decision citation is improved by all of it.
MAX_DECLARATION_CHARS = 200


def _declaration_text(name_node: ts.Node, container: ts.Node) -> str:
    """The declaration of ONE name, collapsed to a single line and capped.

    Walks UP to the nearest declarator rather than checking the immediate parent, because a
    declarator is not always the parent: `const char *NAME = "svc";` puts a
    `pointer_declarator` in between, and a parent check silently misses it and falls back to
    the shared statement.

    Falls back to `container` when no declarator ancestor exists. That is not a failure case:
    a `#define` has no declarator node and names exactly one macro, so the container already
    IS per-name there.

    Named "declaration" wherever it is stored, never "value". This is the text as written; no
    value has been parsed out of it, and calling it a value would claim work nobody did.
    """
    node = name_node
    while node is not None and node is not container:
        if node.type in _DECLARATOR_NODES:
            break
        node = node.parent
    chosen = node if (node is not None and node.type in _DECLARATOR_NODES) else container
    text = " ".join(chosen.text.decode("utf-8", "replace").split())
    if len(text) <= MAX_DECLARATION_CHARS:
        return text
    # Says the DOCUMENT truncated it. A bare ellipsis reads as source that ends that way,
    # which several languages allow.
    return text[:MAX_DECLARATION_CHARS] + " [truncated]"


def _member_symbols(tree: ts.Tree, lang: str) -> list[tuple[str, ts.Node, ts.Node]]:
    """`(kind, name_node, container)` for the symbol kinds the def query cannot express.

    These five -- data members, macros, typedefs, enum constants and file-scope
    variables -- were never emitted at all: measured at 83,052 named symbols on one
    large legacy C/C++ tree, which is more than the entire rest of that graph.

    They live here rather than in the tree-sitter query because two of them need a
    decision the query language cannot make: which declarator chain actually declares
    a name (see `_declared_name`), and whether a declaration is file-scope or a local
    (see `_is_file_scope`).
    """
    if lang == "css":
        return _css_symbols(tree)
    if lang == "html":
        return _html_symbols(tree)
    if lang == "nix":
        return _nix_symbols(tree)
    if lang == "make":
        return _make_symbols(tree)
    if lang == "dockerfile":
        return _dockerfile_symbols(tree)
    if lang in _JS_LANGS:
        return _js_member_symbols(tree)
    if lang == "python":
        return _py_member_symbols(tree)
    if lang not in ("c", "cpp"):
        return []
    out: list[tuple[str, ts.Node, ts.Node]] = []
    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        t = n.type
        if t in ("preproc_def", "preproc_function_def"):
            nm = n.child_by_field_name("name")
            if nm is not None:
                out.append(("macro", nm, n))
        elif t == "type_definition":
            nm = n.child_by_field_name("declarator")
            if nm is not None and nm.type == "type_identifier":
                out.append(("typedef", nm, n))
        elif t == "alias_declaration":
            nm = n.child_by_field_name("name")
            if nm is not None:
                out.append(("typedef", nm, n))
        elif t == "enumerator":
            nm = n.child_by_field_name("name")
            if nm is not None:
                out.append(("enum_constant", nm, n))
        elif t == "field_declaration":
            nm = _declared_name(n)
            if nm is not None:
                out.append(("field", nm, n))
        elif t == "declaration":
            if _is_file_scope(n):
                nm = _declared_name(n)
                if nm is not None:
                    out.append(("global_variable", nm, n))
        stack.extend(n.children)
    return out


def name_key(fn: str) -> str:
    """A file's name-routing key: the lowercased stem before its first dot.

    ``Makefile`` -> ``makefile``; ``Dockerfile.prod`` -> ``dockerfile``; ``MyMakefile``
    -> ``mymakefile``, which matches nothing, which is correct. A dotfile such as
    ``.gitignore`` yields ``""`` and so can never match a table entry.
    """
    return os.path.basename(fn).split(".", 1)[0].lower()


def lang_for(fn: str, ext: str) -> str | None:
    """The language a file parses as, by extension first and then by name.

    The single place the two routing tables are consulted together. Extension wins
    when both could apply, so an explicit ``.mk`` is never overridden by a stem.
    """
    return LANG_BY_EXT.get(ext) or LANG_BY_NAME.get(name_key(fn))


def _file_kind(fn: str, ext: str, rel: str, *, allowed_exts: set[str],
               allowed_names: set[str], index_hcl: bool, index_sql: bool) -> str | None:
    """Which extractor owns this file, or None if nothing indexes it.

    ``languages`` gates code, HCL and SQL only: a manifest, an ADR or an XML
    config file is never language-specific, so filtering to ``--languages python``
    must not hide the repo's package manifests, decision records or settings.

    ``allowed_names`` has NO default on purpose, matching ``allowed_exts``. A default
    would mean every caller that has not been taught about name routing silently
    answers "not indexable" for build files while the walker indexes them, and the two
    would disagree without any test failing.
    """
    if index_hcl and ext in HCL_EXTS:
        return _HCL
    if index_sql and ext in SQL_EXTS:
        return _SQL
    if ext in XSD_EXTS:
        return _XSD
    if ext in XSL_EXTS:
        return _XSL
    # Gated on C rather than on `index_sql`: the file is C source, so `--languages c`
    # must select it and `--languages python` must not.
    if ext in PROC_EXTS and ".c" in allowed_exts:
        return _PROC
    if ext in XML_EXTS:
        return _XML
    if ext == ".md" and is_adr_path(rel):
        return _ADR
    if ext in allowed_exts or name_key(fn) in allowed_names:
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
                      allowed_names=set(LANG_BY_NAME),
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
    fpath: Path, rel: str, fn: str, *, allowed_exts: set[str], allowed_names: set[str],
    index_hcl: bool, index_sql: bool, ignore: list[str], max_file_bytes: int,
    skip_generated: bool, counts: WalkCounts,
) -> SourceFile | None:
    """One file's full accept/skip decision, read included; None means skipped.

    Every skip increments its counter here rather than being dropped silently —
    the summary line ``index_repo_dir`` logs is the only place a user learns that
    a file was passed over.
    """
    ext = os.path.splitext(fn)[1]
    kind = _file_kind(fn, ext, rel, allowed_exts=allowed_exts,
                      allowed_names=allowed_names,
                      index_hcl=index_hcl, index_sql=index_sql)
    if kind is None:
        # Recorded, not dropped. A file with no extension and no name route (LICENSE,
        # AUTHORS) is skipped without a counter: it is overwhelmingly not source, and
        # counting it under the empty string would drown the per-extension signal.
        if ext:
            counts.unsupported_exts[ext] = counts.unsupported_exts.get(ext, 0) + 1
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
    # `lang_for`, not `LANG_BY_EXT[ext]`: a name-routed file has an extension the
    # table has never heard of (`Dockerfile.prod`) or none at all (`Makefile`), and
    # the subscript that used to be here would raise KeyError on both.
    return SourceFile(rel=rel, source=source, kind=kind,
                      lang=(lang_for(fn, ext) or "") if kind == _CODE else "")


def _walk_source_files(
    root: Path, *, allowed_exts: set[str], allowed_names: set[str], index_hcl: bool,
    index_sql: bool, max_file_bytes: int, skip_generated: bool, counts: WalkCounts,
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
                              allowed_names=allowed_names,
                              index_hcl=index_hcl, index_sql=index_sql, ignore=ignore,
                              max_file_bytes=max_file_bytes,
                              skip_generated=skip_generated, counts=counts)
            if sf is not None:
                yield sf


# A single-file component keeps JavaScript and CSS inside markup. Neither the Svelte nor
# the HTML grammar parses those blocks: both hand back the contents as one opaque
# `raw_text` node. So the grammar's job here is finding the block boundaries reliably, and
# the contents still have to go through the JavaScript and CSS grammars.
#
# Svelte uses its own grammar and Vue uses HTML's, which parses a `.vue` file's
# template/script/style structure correctly and means Vue needs no dependency that does not
# exist on PyPI.
_SFC_OUTER = {"svelte": "svelte", "vue": "html"}
_SFC_INNER = {"script_element": "javascript", "style_element": "css"}


def _sfc_blocks(source: bytes, lang: str) -> list[tuple[str, bytes]]:
    """`(inner_lang, masked_source)` for each script/style block of a component file.

    MASKED, not sliced, and that is the whole correctness argument. Everything outside the
    block is replaced byte-for-byte with spaces while newlines are kept, so the block sits
    at its true offset and every line number the inner parser reports is the line in the
    FILE. Slicing the block out and parsing it alone would report line 1 for a function on
    line 40, and a citation that points at the wrong line is the one failure this project
    cannot afford: it looks exactly like a correct answer.
    """
    outer = _SFC_OUTER.get(lang)
    if outer is None:
        return []
    out: list[tuple[str, bytes]] = []
    stack = [_parser(outer).parse(source).root_node]
    while stack:
        n = stack.pop()
        inner = _SFC_INNER.get(n.type)
        if inner is not None:
            raw = next((c for c in n.children if c.type == "raw_text"), None)
            if raw is not None and raw.end_byte > raw.start_byte:
                masked = bytearray(b" " * len(source))
                for i, byte in enumerate(source):
                    if byte == 0x0A:            # keep every newline where it is
                        masked[i] = 0x0A
                masked[raw.start_byte:raw.end_byte] = source[raw.start_byte:raw.end_byte]
                out.append((inner, bytes(masked)))
        stack.extend(n.children)
    return out


def _parse_sfc(repo_id: str, sf: SourceFile, refs: RefCollector,
               ) -> tuple[list[Node], list[Edge]]:
    """A single-file component: parse each embedded block with the grammar that fits it.

    The file node keeps the COMPONENT's language rather than the last block's, because the
    file is a `.svelte` file whatever its script happens to be written in.
    """
    nodes: list[Node] = []
    edges: list[Edge] = []
    seen: set[str] = set()
    for inner_lang, masked in _sfc_blocks(sf.source, sf.lang):
        bn, be, calls, inh = parse_source(repo_id, sf.rel, masked, inner_lang)
        refs.calls.extend(calls)
        refs.inherits.extend(inh)
        for node in bn:
            if node.id in seen:
                continue
            seen.add(node.id)
            nodes.append(node.model_copy(update={"lang": sf.lang})
                         if node.kind == "file" else node)
        edges.extend(be)
    if not nodes:
        # No blocks at all (a template-only component). The file still belongs in the
        # graph, and saying so beats a file that silently is not there.
        file_id = make_id(repo_id, sf.rel)
        nodes.append(Node(id=file_id, repo=repo_id, kind="file", name=sf.rel,
                          file=sf.rel, lang=sf.lang))
    return nodes, edges


def _parse_code(repo_id: str, sf: SourceFile, refs: RefCollector,
                ) -> tuple[list[Node], list[Edge]]:
    if sf.lang in _SFC_OUTER:
        return _parse_sfc(repo_id, sf, refs)
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
    refs.styles.extend(extract_style_refs(repo_id, sf.rel, sf.source, sf.lang))
    refs.constant_uses.extend(
        extract_constant_uses(repo_id, sf.rel, sf.source, sf.lang))
    dr, dw = extract_data_refs(repo_id, sf.rel, sf.source)
    refs.data_reads.extend(dr)
    refs.data_writes.extend(dw)
    nodes += hn + en + wn + sn
    edges += he + ee + we + se
    return nodes, edges


# Identifier node types, per grammar family. Every language this runs over spells a bare
# name reference as one of these; anything else (an attribute, a property, a string) is
# deliberately NOT a bare name and so is not a use of a file-scope constant.
_NAME_NODES = frozenset({"identifier", "type_identifier"})

# Parent types where an identifier is not a bare value read at all: a dotted attribute, an
# import, a parameter list, a keyword-argument label. These exclude the WHOLE subtree because
# no position under them is a read of a file-scope constant.
_NOT_A_READ_PARENT = frozenset({
    "parameter_declaration", "parameter", "parameters", "formal_parameters",
    "import_statement", "import_from_statement", "import_specifier", "aliased_import",
    # Python wraps each imported name in a `dotted_name`, so the import statement is the
    # GRANDparent and excluding only the statement misses it. Measured: `dotted_name` appears
    # under import statements here and nowhere else -- attribute access is `attribute`, which
    # is excluded on its own line -- so excluding it does not cost a real read.
    "dotted_name",
    "attribute", "member_expression", "field_expression", "keyword_argument",
    "function_definition", "class_definition", "class_declaration",
    "field_declaration", "function_declarator",
    # Names being DECLARED by a construct that has no entry in _DECLARED_NAME_FIELD. None of
    # these can resolve to a constant kind, so they were harmless, but every one of them was
    # a row in the unresolved stream for a large repository to carry and then discard.
    "function_declaration", "generator_function_declaration", "method_definition",
    "enum_specifier", "struct_specifier", "union_specifier", "type_definition",
    "namespace_definition", "labeled_statement", "goto_statement",
    # `global TOTAL` / `nonlocal x` name a binding rather than read its value. Counted as a
    # read they put a scope declaration in the middle of a list of uses, on a line where
    # nothing is actually read. Measured on the fixture: `global TOTAL` was reported as a use.
    "global_statement", "nonlocal_statement",
    # A function-like macro's parameter list. `#define CMP(a, b)` names two parameters; they are
    # not reads of anything, and they were being emitted as reads of `a` and `b`.
    "preproc_params",
})

# The FIELD that holds the name being declared, per declaring node. This has to be a field
# check and not a parent-type check, which the first draft got wrong in both directions at
# once: excluding every child of `assignment` and `variable_declarator` dropped the
# right-hand side (`TOTAL = MAX_RETRY` stopped counting as a read of `MAX_RETRY`), while
# excluding none of them counted each constant's own declaration as a use of itself. Both
# were visible on a four-symbol fixture: `MAX_RETRY` was reported as used on the line that
# declares it.
_DECLARED_NAME_FIELD = {
    "assignment": "left",             # Python
    "variable_declarator": "name",    # JavaScript, TypeScript
    "init_declarator": "declarator",  # C, C++
    "enumerator": "name",             # C, C++ enum members
    "preproc_def": "name",            # C, C++ object-like macros: `#define LIMIT 5`
    # And function-like macros, which are a DIFFERENT node type. Missing this counted every
    # `#define NAME(a, b) ...` as a use of itself: on one public C++ tree the impact walk for a
    # test-assertion macro listed the header that defines it, citing the `#define` line.
    "preproc_function_def": "name",
    "augmented_assignment": "left",   # `X += 1` declares nothing but reads X; see below
}


# Levels to climb looking for a declaring node. Three is enough for the deepest measured
# wrapper chain (`identifier < pointer_declarator < array_declarator < init_declarator`) and
# small enough that a read nested inside an unrelated expression cannot reach a declaration
# far above it and be wrongly excluded.
_MAX_DECLARATOR_WALK = 3


def _is_declared_name(node: ts.Node) -> bool:
    """Whether ``node`` is the name its parent DECLARES, rather than a value it reads.

    `X += 1` is deliberately treated as a declaration position and therefore not a read.
    It is really both, and counting it as a read would be defensible -- but a compound
    assignment is a WRITE to the name, and reporting a write as a read in a list headed
    "where this value is used" is the kind of confidently-wrong answer this project keeps
    removing. A write deserves its own relation if it is ever asked for.
    """
    # Walk up to the declaring node rather than reading `node.parent` once. In C and C++ the
    # identifier's parent is a wrapper: `const char *NAME = "x"` nests
    # `identifier < pointer_declarator < init_declarator`, so a single-level lookup finds
    # `pointer_declarator`, which declares nothing, and reports the declaration as a read of
    # itself. Bounded, so a pathological tree cannot turn this into an unbounded climb.
    parent, field, hops = node.parent, None, 0
    while parent is not None and hops < _MAX_DECLARATOR_WALK:
        field = _DECLARED_NAME_FIELD.get(parent.type)
        if field is not None:
            break
        parent, hops = parent.parent, hops + 1
    if parent is None or field is None:
        return False
    named = parent.child_by_field_name(field)
    if named is None:
        return False
    # Compared by `.id`, never by `is`. The tree-sitter bindings hand back a NEW Python
    # object each time a node is reached, so `parent.child_by_field_name("left") is node`
    # is False even when both wrap the same syntax node -- which silently made this
    # function always return False, and every constant was reported as used on the line
    # that declares it. `.id` is the identity the rest of this module already keys on.
    #
    # Position, not text: the same identifier appears on both sides of `TOTAL = TOTAL`,
    # and only the left one is the declaration.
    if named.id == node.id:
        return True
    # C and C++ wrap the declared name in one or more declarators (`*NAME`, `NAME[3]`),
    # so the field points at a wrapper rather than at the identifier itself.
    return any(d.id == node.id for d in _descendants(named))


def _descendants(node: ts.Node):
    stack = list(node.children)
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.children)


def extract_constant_uses(repo_id: str, rel_path: str, source: bytes, lang: str,
                          ) -> list[tuple[str, str, str, int]]:
    """Unresolved ``(file_id, name, file, line)`` for every bare name a file reads.

    The graph recorded that a constant existed and nothing about where it was used, so
    "what breaks if I change this timeout" was unanswerable from the graph even though every
    use is sitting in the AST. This is the read side of that.

    **The source is the FILE, not the enclosing function**, and that is a deliberate choice
    rather than a shortcut. The enclosing definition's node id is computed inside
    `parse_source` while it walks, and reaching it here would mean either widening that
    function's return tuple -- which a dozen tests unpack -- or recomputing the qualifier
    logic, which would then drift from the original. A file and a line is also exactly the
    citation this was asked for: "used at N sites", each site a path and a line a reader can
    open.

    Emits every candidate name and lets `_resolve_name_refs` decide. That is how the calls
    stream works, and it is what keeps the filtering honest: this function does not guess
    which names look like constants (an ALL_CAPS rule would miss `timeout` and invent
    `HTTPError`), it emits bare reads and only names matching a real constant node survive
    resolution, under the same ambiguity cap as every other stream.
    """
    if lang not in ALL_LANGS or lang in _QUERYLESS_LANGS:
        return []
    try:
        tree = _parser(lang).parse(source)
    except Exception:  # noqa: BLE001 - one unparseable file must not stop the walk
        return []
    file_id = make_id(repo_id, rel_path)
    out: list[tuple[str, str, str, int]] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if node.type not in _NAME_NODES:
            continue
        parent = node.parent
        if parent is not None and parent.type in _NOT_A_READ_PARENT:
            continue
        # The name being declared is not a use of itself. Without this every constant is
        # reported as used on the line that declares it, which inflates every count by one
        # and puts the declaration at the top of its own list of uses.
        if _is_declared_name(node):
            continue
        # A call's function position is a call, which the `calls` stream already records.
        # Counting it again here would double-report the same line under two relations.
        if parent is not None and parent.type == "call" and parent.child_by_field_name(
                "function") is node:
            continue
        name = node.text.decode("utf-8", "replace")
        if not name:
            continue
        out.append((file_id, name, rel_path, node.start_point[0] + 1))
    return out


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


def _parse_proc_file(repo_id: str, sf: SourceFile, refs: RefCollector,
                     ) -> tuple[list[Node], list[Edge]]:
    """A Pro*C file: parsed as C with its embedded SQL masked out, and as SQL without.

    The mask is applied to the CODE parse only. The dataflow pass reads `sf.source`
    intact, because that pass is the reason to index the file at all: which tables it
    reads and writes is what an `EXEC SQL` statement is there to say, and masking it
    for both would leave a Pro*C file contributing nothing but its C functions.
    """
    nodes, edges, calls, inh = parse_source(
        repo_id, sf.rel, mask_embedded_sql(sf.source), _PROC_LANG)
    refs.calls.extend(calls)
    refs.inherits.extend(inh)
    dr, dw = extract_data_refs(repo_id, sf.rel, sf.source)
    refs.data_reads.extend(dr)
    refs.data_writes.extend(dw)
    return nodes, edges


def _parse_xsl_file(repo_id: str, sf: SourceFile, refs: RefCollector,
                    ) -> tuple[list[Node], list[Edge]]:
    nodes, calls, var_uses = parse_xsl(repo_id, sf.rel, sf.source)
    refs.calls.extend(calls)
    refs.constant_uses.extend(var_uses)
    return nodes, []


def _parse_xsd_file(repo_id: str, sf: SourceFile, refs: RefCollector,
                    ) -> tuple[list[Node], list[Edge]]:
    nodes, schema_refs = parse_xsd(repo_id, sf.rel, sf.source)
    refs.schema.extend(schema_refs)
    return nodes, []


def _parse_xml_file(repo_id: str, sf: SourceFile, _refs: RefCollector,
                    ) -> tuple[list[Node], list[Edge]]:
    return parse_xml_config(repo_id, sf.rel, sf.source), []


def _parse_adr_file(repo_id: str, sf: SourceFile, _refs: RefCollector,
                    ) -> tuple[list[Node], list[Edge]]:
    return parse_adr(repo_id, sf.rel, sf.source), []


def _parse_manifest_file(repo_id: str, sf: SourceFile, _refs: RefCollector,
                         ) -> tuple[list[Node], list[Edge]]:
    return parse_manifest(repo_id, sf.rel, sf.source)


# Extraction kind -> parser, all sharing one signature so the orchestrator below
# is a lookup rather than a multi-way branch. A new file kind is a table entry
# plus a `_file_kind` clause; nothing in index_repo_dir changes.
# Extraction kinds whose parser returns bare definition nodes and no file node.
# `_CODE` is absent because `parse_source` builds its own file node and parents every
# definition to it; `_MANIFEST` is absent because its nodes are cross-repo package
# nodes that several manifests legitimately share, and the relation that belongs
# between a manifest and a package is `depends_on`, which the manifest parser already
# emits -- `contains` would assert the package lives in that file.
_FILE_CONTAINED_KINDS = frozenset({_XML, _XSD, _XSL, _SQL, _ADR})


def _with_file_containment(repo_id: str, sf: SourceFile, nodes: list[Node],
                           edges: list[Edge]) -> tuple[list[Node], list[Edge]]:
    """Give a bespoke extractor's nodes the file node and containment the code path
    builds for itself.

    Without this, these extractors' output is unreachable: measured 2026-08-11 on a
    large legacy C++ tree, 12,991 of 12,991 `config_key` nodes and 142 of 207 `table`
    nodes had **zero** incident edges, against 0 of 28,274 functions. The files
    themselves were missing too -- 0 `file` nodes for `.xml` and 0 for `.sql`, so a
    file-level view of the repository silently omitted every config file it had.

    A name lookup still found those nodes, which is why the gap survived: the answer
    to "where is this setting defined" looked complete while nothing could reach the
    setting by traversal, and no diagram of a file could show its contents.
    """
    if not nodes:
        return nodes, edges
    file_id = make_id(repo_id, sf.rel)
    file_node = Node(id=file_id, repo=repo_id, kind="file", name=sf.rel, file=sf.rel)
    verified_at = date.today()
    contains = [
        Edge(src=file_id, dst=n.id, relation="contains", confidence=Confidence.EXTRACTED,
             provenance=Provenance(source_file=sf.rel, source_line=n.line_start,
                                   verified_at=verified_at))
        for n in nodes if n.id != file_id
    ]
    return [file_node, *nodes], [*edges, *contains]


_PARSERS: dict[str, Callable[[str, SourceFile, RefCollector],
                             tuple[list[Node], list[Edge]]]] = {
    _CODE: _parse_code,
    _HCL: _parse_hcl_file,
    _SQL: _parse_sql_file,
    _XML: _parse_xml_file,
    _XSD: _parse_xsd_file,
    _XSL: _parse_xsl_file,
    _PROC: _parse_proc_file,
    _ADR: _parse_adr_file,
    _MANIFEST: _parse_manifest_file,
}


def _source_filter(languages: list[str] | None) -> tuple[set[str], set[str], bool, bool]:
    """``languages`` resolved to (code extensions, code names, index HCL?, index SQL?).

    No filter means everything; HCL and SQL are opted in by name because neither
    lives in ``LANG_BY_EXT``.

    The name set is filtered by the SAME language list as the extension set, so
    ``--languages make`` selects Makefiles and nothing else, and ``--languages python``
    excludes them. A name table that ignored the filter would be permanently on, which
    no single-direction test would catch.
    """
    allowed_exts = {ext for ext, lang in LANG_BY_EXT.items()
                    if not languages or lang in languages}
    allowed_names = {name for name, lang in LANG_BY_NAME.items()
                     if not languages or lang in languages}
    # ".h" is classified as "cpp" internally (see LANG_BY_EXT), but C and C++
    # headers are shared infrastructure -- a user who filters to just "c"
    # almost certainly still wants its headers indexed, not silently dropped.
    # So ".h" inclusion is decided by either language being enabled, not by
    # which single language it happens to be parsed as.
    if languages and ("c" in languages or "cpp" in languages):
        allowed_exts.add(".h")
    return (allowed_exts, allowed_names,
            not languages or "hcl" in languages,
            not languages or "sql" in languages)


def estimate_repo_cost(
    root, *, allowed_exts: set[str], allowed_names: set[str], index_hcl: bool,
    index_sql: bool, max_file_bytes: int,
) -> tuple[float, dict]:
    """``(estimated_peak_bytes, bytes_by_kind)`` for a repository, from stat alone.

    A separate pass over the tree, deliberately. Accumulating the estimate
    during the real walk would only notice the problem once part of the memory
    was already committed, and the entire reason this exists is to answer
    "can I afford this?" BEFORE the first file is parsed. The pass costs
    ``os.stat`` per candidate and nothing else: 660 repositories measured in
    about two minutes, against hours to parse them.

    **The skip predicates are the walker's own**, reached through the same
    ``_SKIP_DIRS``, ``_ignored`` and ``_file_kind`` that ``_walk_source_files``
    uses, rather than reimplemented. Two paths that decide "is this file
    indexed?" separately drift, and a divergence here would either refuse a
    repository over files that are never read or wave through one that is.
    ``tests/kb/test_repo_cost_estimate.py`` asserts the two agree on a tree
    built to exercise every skip.

    ``skip_generated`` is deliberately NOT applied: deciding it needs the file's
    first bytes, which would turn a stat pass into a read pass. The estimate is
    therefore an over-estimate on a generated-heavy tree, which is the safe
    direction for a budget.
    """
    root = Path(root)
    ignore = load_ignore_patterns(root)
    by_kind: dict = {}
    for dirpath, dirnames, filenames in os.walk(root):
        relbase = os.path.relpath(dirpath, root)
        relbase = "" if relbase == "." else relbase.replace(os.sep, "/")
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS
                       and not _ignored(f"{relbase}/{d}".lstrip("/"), ignore)]
        for fn in filenames:
            fpath = Path(dirpath) / fn
            rel = str(fpath.relative_to(root))
            if _ignored(rel.replace(os.sep, "/"), ignore):
                continue
            ext = fpath.suffix.lower()
            kind = _file_kind(fn, ext, rel, allowed_exts=allowed_exts,
                              allowed_names=allowed_names,
                              index_hcl=index_hcl, index_sql=index_sql)
            if kind is None:
                continue
            try:
                size = fpath.stat().st_size
            except OSError:
                continue
            if kind == _CODE and size > max_file_bytes:
                continue        # the real walk drops it, so it costs nothing
            by_kind[kind] = by_kind.get(kind, 0) + size
    estimate = sum(size * KIND_COST.get(kind, DEFAULT_KIND_COST)
                   for kind, size in by_kind.items())
    return estimate, by_kind


def index_repo_dir(
    repo_path: str, repo_id: str, head_commit: str | None = None,
    languages: list[str] | None = None, *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES, skip_generated: bool = True,
    max_repo_memory: int | None = None,
) -> GraphShard:
    """Walk a repository directory and parse every supported file into a shard.

    Generated/derived files (see ``_is_generated_name``/``_has_generated_header``)
    and code files larger than ``max_file_bytes`` are skipped — both reported, never
    silent — to keep legacy monorepos from exploding the graph and the index time.

    ``max_repo_memory`` bounds the whole repository rather than any one file. When
    the estimate exceeds it, ``RepoTooLarge`` is raised before the first file is
    read. ``None`` disables the check, which is what every existing caller that
    has not been taught about it gets.
    """
    allowed_exts, allowed_names, index_hcl, index_sql = _source_filter(languages)
    if max_repo_memory is not None:
        estimate, breakdown = estimate_repo_cost(
            repo_path, allowed_exts=allowed_exts, allowed_names=allowed_names,
            index_hcl=index_hcl, index_sql=index_sql, max_file_bytes=max_file_bytes)
        if estimate > max_repo_memory:
            raise RepoTooLarge(repo_id, estimate, max_repo_memory, breakdown)
    shard = GraphShard(repo=repo_id, head_commit=head_commit, parser_version=PARSER_VERSION)
    by_id: dict[str, Node] = {}
    refs = RefCollector()
    counts = WalkCounts()

    for sf in _walk_source_files(
        Path(repo_path), allowed_exts=allowed_exts, allowed_names=allowed_names,
        index_hcl=index_hcl, index_sql=index_sql,
        max_file_bytes=max_file_bytes, skip_generated=skip_generated, counts=counts,
    ):
        try:
            nodes, edges = _PARSERS[sf.kind](repo_id, sf, refs)
            if sf.kind in _FILE_CONTAINED_KINDS:
                nodes, edges = _with_file_containment(repo_id, sf, nodes, edges)
        except GrammarNotInstalled as e:
            # Caught BEFORE the blanket handler below, which would log this once per file
            # as "parse error" and count it as nothing. The file is fine and so is the
            # install; an opt-in package is simply absent, and the summary says so once.
            counts.missing_grammars[e.lang] = counts.missing_grammars.get(e.lang, 0) + 1
            continue
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
    resolve_stats: dict = {}
    shard.edges.extend(refs.resolved_edges(by_id, stats=resolve_stats))
    log(f"  parsed {counts.files} file(s); skipped {counts.generated} generated, "
        f"{counts.oversize} oversized, {counts.ignored} ignored", level=logging.DEBUG)
    # Said at NORMAL verbosity, and loudest when nothing was parsed at all. A tree in a
    # language contextlake has no grammar for produced `0 nodes, 0 edges`, exit 0, and a
    # skip line of all zeros -- which reads as "this repo is empty" rather than "this
    # tool cannot read it". That is the single worst first impression available.
    if counts.unsupported_exts:
        from .. import style

        top = sorted(counts.unsupported_exts.items(), key=lambda kv: (-kv[1], kv[0]))
        shown = ", ".join(f"{ext} x{n}" for ext, n in top[:5])
        more = f" (+{len(top) - 5} more)" if len(top) > 5 else ""
        total = sum(counts.unsupported_exts.values())
        if counts.files == 0:
            log(style.warn(
                f"  no file in this repository has a supported parser — {total} file(s) "
                f"skipped: {shown}{more}"))
            # Counted, never written down: a literal here goes stale the moment a
            # grammar is added, which is the same docs-vs-code drift this release is
            # about. ALL_LANGS is the single source of truth for what parses, and it is
            # the UNION of both routing tables -- counting LANG_BY_EXT alone would omit
            # every language a file reaches by name and still read as a complete total.
            log(f"  contextlake indexes {len(ALL_LANGS)} languages; see "
                f"docs/adding-a-language.md to add one.")
        else:
            log(f"  {total} file(s) had no parser for their type: {shown}{more}")
    # A DIFFERENT sentence from the one above, deliberately. "no parser for their type"
    # means contextlake cannot read that language at all and the user can do nothing;
    # this means it can, and one `pip install` away it will. Silent when zero, so it
    # never becomes boilerplate on the overwhelming majority of runs.
    for lang, n in sorted(counts.missing_grammars.items()):
        log(f"  {n} {lang} file(s) skipped: {lang} is supported but its grammar is an "
            f"optional dependency. Install it with: "
            f"pip install 'contextlake[{OPTIONAL_GRAMMAR_EXTRA[lang]}]'")
    # Said at normal verbosity, unlike the DEBUG resolution summary: this is a KNOWN
    # INCOMPLETENESS in the graph the user is about to query, and "who calls X" will
    # quietly omit these. Silent when zero, so it never becomes boilerplate.
    if (n := resolve_stats.get("ambiguity_dropped", 0)):
        log(f"  {n} call reference(s) name a symbol defined in more than "
            f"{_MAX_AMBIG_FANOUT} places and were left unresolved; callers through "
            f"those names will be missing")
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


def discover_repos(root: str, *, unusable: list[str] | None = None
                   ) -> list[tuple[str, str]]:
    """Find git repositories under ``root``: (repo_id, absolute_path) pairs.

    ``unusable`` is an optional list this APPENDS the relative path of every directory it
    had to skip because git cannot use it -- a dangling gitlink, or a ``.git`` that
    resolves to an ancestor repo. Those were logged and then dropped, so a caller counting
    its own results could not tell they existed: `kb index --workspace` warned about a repo
    git could not open, indexed the rest, and reported "0 failed" with exit 0.

    Deliberately NOT collecting the other two skips. A vendored tree and a duplicate
    checkout are decisions this function makes correctly on purpose; folding them in would
    turn a clean run into a failed one and teach a user to ignore the count.

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
            if unusable is not None:
                unusable.append(rel)
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
# can resolve to. A method can be called but never inherited from. All four sets below are
# projected from the registry (kb/kinds.py); a kind missing from one of them makes the
# corresponding edge silently never exist, which is the quietest failure in the codebase —
# the graph is simply smaller and nothing anywhere reports it.
_CALLABLE_KINDS = {k for k, s in KIND_REGISTRY.items() if s.callable_target}
_INHERITABLE_KINDS = {k for k, s in KIND_REGISTRY.items() if s.inheritable_target}

# HCL block kinds a depends_on reference can resolve to. Note `module` is also
# emitted for code imports, so kinds are not disjoint; safety comes from address
# namespacing instead: HCL addresses are prefixed (`var.`/`module.`/`data.`/
# `local.`) or resource-typed (`type.name`), while code module nodes carry raw
# import paths (`os`, `requests`), so the name indices never overlap. A
# pathological collision would surface as an AMBIGUOUS edge, never a wrong
# INFERRED one.
_HCL_KINDS = {k for k, s in KIND_REGISTRY.items() if s.hcl_ref_target}

# SQL FK references resolve to table/view defs (both non-colliding with code and
# HCL kinds, so their name index stays isolated).
_SQL_KINDS = {k for k, s in KIND_REGISTRY.items() if s.sql_ref_target}
# What an XML-Schema `type=`/`base=`/`ref=` may resolve to: the schema's own global
# components and nothing else. Derived from the registry rather than listed here, so a third
# schema kind cannot be added without answering whether a schema name can refer to it.
_SCHEMA_KINDS = {k for k, s in KIND_REGISTRY.items() if s.schema_ref_target}
# The kinds a BARE identifier can actually refer to. Narrower than DECLARED_VALUE_KINDS, and
# the difference was measured rather than reasoned: `field` had to come out.
#
# A data member is reached as `self.x`, `this->x` or `obj.x`, every one of which is an
# attribute or field expression that this extractor already skips. So a bare `x` matching a
# field name is almost never that field -- it is a local. On one public C++ tree, including
# `field` attributed 588 reads of a loop counter `i` to a class member named `i`, and
# comparable counts for `os` and `string`. Those were not ambiguous-and-flagged, they were
# confident and wrong, which is the one outcome this project does not ship.
#
# The three that stay are file- or namespace-scope names, which a bare identifier genuinely
# does refer to. This is why the set is its own constant and not an alias of the declaration
# set: "has a value worth recording" and "a bare name can mean this" are different questions
# that happened to look like one.
_CONSTANT_KINDS = {"global_variable", "enum_constant", "macro"}
# What an HTML `class=`/tag reference may resolve to: the three things a stylesheet
# defines. Derived from the registry rather than listed here, so a fourth presentation
# kind cannot be added without answering whether markup can refer to it.
_STYLE_KINDS = {k for k, s in KIND_REGISTRY.items() if s.style_ref_target}


# A name that resolves to 2..N definitions is emitted as AMBIGUOUS edges to each
# candidate (so blast-radius doesn't miss hot symbols); a name matching more than
# this is too generic (e.g. `get`/`handle`) to be signal and is skipped.
# Raised from 6 to 10 on 2026-09-02, from the measurement recorded below. A reference
# naming a symbol defined in more than this many places produces NO edge, so the cap is
# the line between a caller that is reported and one that silently is not.
_MAX_AMBIG_FANOUT = 10

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


def _scope_chain_of(node: Node) -> str:
    """A definition's own scope chain, with the file prefix removed.

    C/C++ external-linkage symbols now store an unprefixed chain (``NS.Box``), so for
    them this is a pass-through. The strip is still needed for the cases that keep their
    file: internal-linkage symbols (``static``), and every language where one module per
    file makes the file part of the qualification.
    """
    q = node.qualified_name or node.name or ""
    return q.split("::", 1)[1] if "::" in q else q


def _resolve_pending_methods(by_id: dict[str, Node], edges: list[Edge]) -> None:
    """Repo-wide second pass: link an out-of-line qualified method to its class.

    ``Node.attrs["_pending_method_of"]`` (set by ``parse_source`` when a definition's
    declarator was a qualified name, e.g. ``Widget::Draw``) names the qualifier chain;
    the class may live in any file (the common header/source split), so this can only
    resolve once every file in the repo has been parsed and ``by_id`` is complete.
    """
    # Two indexes, both built in ONE pass over the nodes:
    #   qual_index -- the class's own scope chain ("NS.Box") -> ids. An exact hit on the
    #                 whole qualifier is the unambiguous case and needs no filtering.
    #   by_bare    -- last segment ("Box") -> [(chain, id)]. The fallback list is short
    #                 (the classes sharing one bare name), so filtering it per pending
    #                 method stays linear overall. This is what keeps the pass off the
    #                 O(pending x classes) path, the same hazard the contains_edge_idx
    #                 pre-index below exists for.
    qual_index: dict[str, list[str]] = {}
    by_bare: dict[str, list[tuple[str, str]]] = {}
    for node in by_id.values():
        if node.kind in ("class", "struct"):
            chain = _scope_chain_of(node)
            qual_index.setdefault(chain, []).append(node.id)
            by_bare.setdefault(node.name, []).append((chain, node.id))

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
        # Match the WHOLE qualifier, not just its last segment. Keying on the last
        # segment alone let `NS::Box::put` attach to an unrelated `Other::Box`, which
        # is a fabricated parent rather than a missing edge; and bailing whenever the
        # bare name was ambiguous threw away ties the qualifier already settles.
        want = ".".join(pending)
        candidates = qual_index.get(want) or []
        if len(candidates) != 1:
            # The qualifier can be relative to an enclosing scope: inside
            # `namespace NS`, `void Box::put()` carries pending ["Box"] while the class
            # node's chain is "NS.Box". A suffix match accepts that and still rejects
            # `Other.Box`, because that does not end with ".Box" preceded by the rest of
            # the chain the definition named.
            candidates = [cid for chain, cid in by_bare.get(pending[-1], ())
                          if chain == want or chain.endswith("." + want)]
        if len(candidates) != 1:
            continue  # unresolved, or genuinely ambiguous -- leave file-contained
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
    per_site: bool = False, stats: dict | None = None,
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
    to be signal and are skipped. Self-references are always dropped.

    ``per_site`` selects what a duplicate ``(src, dst)`` pair means:

    - ``False`` (the default, and every stream except calls): one edge per distinct
      pair, keeping the lowest source line. ``helper()`` invoked twice from the same
      caller is one edge citing the first invocation.
    - ``True`` (the ``calls`` stream only): **one edge per call site.** The same pair
      appears once per invocation, each citing its own line, so "where is this called"
      can be answered exhaustively rather than with one representative site.

    It is deliberately per-stream rather than global. Retaining every mention of a base
    class, or every reference to a SQL table, is a separate question nobody has decided,
    and it must not change as a side effect of a shared helper gaining a parameter.

    **Consumers that rank by degree must count DISTINCT pairs, not rows** — under
    ``per_site`` a raw row count answers "how many call sites", which silently reads as
    "how many callers". See ``wiki.generate.repo_brief`` and ``visualize.payload``.

    The sort is a total order rather than by line alone. Line ties used to be harmless
    because the pair de-duplication discarded all but one of them; retaining every site
    makes tie order observable in the output, and tree-sitter's capture order is not
    guaranteed, so sorting by line alone would make shard output non-deterministic.
    """
    name_index: dict[str, set[str]] = {}
    for node in nodes_by_id.values():
        if node.kind in target_kinds:
            name_index.setdefault(node.name, set()).add(node.id)

    edges: list[Edge] = []
    seen: set[tuple[str, str]] = set()
    resolved = ambiguous = dropped = cross_lang = internal_rejected = 0
    for src_id, name, rel, line in sorted(refs, key=lambda r: (r[3], r[2], r[1], r[0])):
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
        # An internal-linkage symbol belongs to ONE translation unit, so a reference from
        # a different file cannot mean it. Measured: two files each defining `static int
        # gated(int)` produced four `calls` edges where only two are possible.
        #
        # Prefer rather than require, because 196 of 1,967 internal-linkage functions on a
        # large legacy tree are defined in a HEADER, where the definition's file and the
        # caller's file legitimately differ. Requiring equality would silently delete those
        # callers, and dropping real edges is the worse error of the two. So: keep every
        # candidate that is either external or same-file, and fall back to the unfiltered
        # set when that leaves nothing.
        #
        # Safe across streams without a language gate because only C/C++ definitions are
        # ever marked internal -- no HCL or SQL node carries the flag, which a test pins.
        reachable = {t for t in matches
                     if (nodes_by_id[t].attrs or {}).get("linkage") != "internal"
                     or nodes_by_id[t].file == rel}
        if reachable:
            internal_rejected += len(matches) - len(reachable)
            matches = reachable
        if len(matches) == 1:
            conf, targets = Confidence.INFERRED, list(matches)
        elif len(matches) <= _MAX_AMBIG_FANOUT:
            conf, targets = Confidence.AMBIGUOUS, sorted(matches)  # deterministic
            ambiguous += 1
        else:
            dropped += 1  # too many candidates -> noise
            continue
        for target in targets:
            if target == src_id:
                continue
            if not per_site:
                if (src_id, target) in seen:
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
    if stats is not None:
        # Reported to the caller, not just logged at DEBUG. A reference over the fanout
        # cap produces NO edge at all, so a genuine caller is simply absent from the
        # answer -- and a loss nobody is told about is indistinguishable from a repo
        # that really has no such caller.
        #
        # Re-measured 2026-09-01 with the cap removed, on the two largest ambiguity
        # contributors in a 717,381-node store. Both figures this comment used to carry
        # were wrong. At 6 the loss was 29.6% and 37.0% of resolvable call references,
        # not 21.6%. And there IS a knee, which is why the cap moved to 10: it reaches
        # 76.2% and 80.6% of references for 1.22x and 1.52x the ambiguous edges. The old
        # "3.6x for 21.6%" was the cost of admitting EVERYTHING, a different question
        # from where to put the cap.
        #
        # Admitting everything stays wrong, for a sharper reason than "no knee": the
        # uncapped cost is one or two pathological names per repo -- 2,864 sites naming
        # a symbol with 1,432 definitions produced 4.1M edges by themselves, 92% of that
        # repository's uncapped total. A cap is the right shape of control; only its
        # value was in question. Full write-up: planning/private/, 2026-09-01.
        stats["ambiguity_dropped"] = stats.get("ambiguity_dropped", 0) + dropped
    if edges or dropped:
        unit = "call site(s)" if per_site else "edge(s)"
        log(f"  resolved {resolved} {relation} {unit}; {ambiguous} ambiguous "
            f"(<= {_MAX_AMBIG_FANOUT} candidates), {dropped} too-generic skipped, "
            f"{cross_lang} cross-language candidate(s) rejected, "
            f"{internal_rejected} out-of-file internal-linkage candidate(s) rejected",
            level=logging.DEBUG)
    return edges
