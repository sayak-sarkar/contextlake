"""Internal linkage: `static` at namespace scope, and anonymous namespaces.

Two translation units may each define their own `static int helper()`, or their own
`namespace { int helper(); }`, and those are genuinely different symbols. Two things
follow, and both were wrong before:

1. **Identity.** The file has to stay in their key, or the two merge into one node and
   the second file's definition disappears -- taking its members with it.
2. **Resolution.** A reference from a different file cannot mean an internal-linkage
   symbol, so offering it as a candidate produces an edge that cannot exist.

Fixing only the first would have been worse than fixing neither: it splits the node and
then hands both copies to every caller as ambiguous candidates.

`static` is deliberately gated on namespace scope, because the keyword means something
different inside a class -- a static member has EXTERNAL linkage and must keep matching
across the header/source split.
"""

from contextlake.kb.parse import index_repo_dir

_ALPHA = """\
namespace {
int tally(int v) { return v; }
struct Holder { int slot; };
}
static int gated(int v) { return v; }
int alpha_entry(int v) { return tally(v) + gated(v); }
"""

_BETA = """\
namespace {
int tally(int v) { return v * 2; }
struct Holder { int slot; };
}
static int gated(int v) { return v * 2; }
int beta_entry(int v) { return tally(v) + gated(v); }
"""


def _index(tmp_path, files):
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    return index_repo_dir(str(tmp_path), "r")


def _by_name(shard, name):
    return [n for n in shard.nodes if n.name == name]


def test_internal_linkage_symbols_do_not_merge_across_files(tmp_path):
    shard = _index(tmp_path, {"alpha.cpp": _ALPHA, "beta.cpp": _BETA})

    for name in ("tally", "gated", "Holder"):
        nodes = _by_name(shard, name)
        assert len(nodes) == 2, f"{name}: each file defines its own"
        assert {n.file for n in nodes} == {"alpha.cpp", "beta.cpp"}

    # The member matters as much as the container: when the two `Holder` structs merged,
    # one file's data member vanished with it, so this is what proves the fix reaches
    # member symbols and not just top-level definitions.
    assert len(_by_name(shard, "slot")) == 2


def test_internal_linkage_is_recorded_on_the_node(tmp_path):
    shard = _index(tmp_path, {"alpha.cpp": _ALPHA, "beta.cpp": _BETA})
    linkage = {(n.name, n.file): (n.attrs or {}).get("linkage") for n in shard.nodes}

    assert linkage[("tally", "alpha.cpp")] == "internal"   # anonymous namespace
    assert linkage[("gated", "alpha.cpp")] == "internal"   # namespace-scope static
    assert linkage[("slot", "alpha.cpp")] == "internal"    # member reached through one
    assert linkage[("alpha_entry", "alpha.cpp")] is None   # ordinary external linkage


def test_a_caller_cannot_reach_another_units_internal_symbol(tmp_path):
    shard = _index(tmp_path, {"alpha.cpp": _ALPHA, "beta.cpp": _BETA})
    by_id = {n.id: n for n in shard.nodes}

    for e in shard.edges:
        if e.relation != "calls":
            continue
        src, dst = by_id[e.src], by_id[e.dst]
        if (dst.attrs or {}).get("linkage") == "internal":
            assert src.file == dst.file, (
                f"{src.name} in {src.file} cannot call {dst.name} in {dst.file}")


def test_a_header_defined_internal_symbol_still_resolves(tmp_path):
    """Prefer same-file, but never DROP the only candidate.

    A `static` (or anonymous-namespace) definition living in a header is included into
    the caller's translation unit, so the files legitimately differ. Measured on a large
    legacy tree: 196 of 1,967 internal-linkage functions are defined in a header, so
    requiring an exact file match would silently delete those callers -- and losing a
    real edge is the worse of the two errors.
    """
    shard = _index(tmp_path, {
        "util.h": "namespace {\nint shared_helper(int v) { return v; }\n}\n",
        "only.cpp": '#include "util.h"\nint entry(int v) { return shared_helper(v); }\n',
    })
    by_id = {n.id: n for n in shard.nodes}
    reached = {by_id[e.dst].name for e in shard.edges
               if e.relation == "calls" and by_id[e.src].name == "entry"}
    assert "shared_helper" in reached, "the only candidate must survive the preference"


def test_static_class_member_keeps_external_linkage(tmp_path):
    """`static` inside a class declares a member, which has external linkage.

    Treating it as internal would file-scope it, so a class defined in a header could
    never match its out-of-line definition -- reintroducing the header/source split this
    project already fixed once.
    """
    shard = _index(tmp_path, {"widget.h": (
        "class Widget {\n"
        "public:\n"
        "  static int counter;\n"
        "  static int fetch(int v) { return v; }\n"
        "};\n"
    )})
    for name in ("counter", "fetch"):
        nodes = _by_name(shard, name)
        assert nodes, f"{name} should be emitted"
        for n in nodes:
            assert (n.attrs or {}).get("linkage") is None, (
                f"{name}: a static class member is not internal linkage")


def test_no_non_cpp_node_is_marked_internal(tmp_path):
    """The resolver's internal-linkage filter is not language-gated, which is only safe
    because nothing outside C/C++ carries the flag. Pin that, rather than assume it."""
    shard = _index(tmp_path, {
        "alpha.cpp": _ALPHA,
        "svc.py": "def helper(v):\n    return v\n",
        "schema.sql": "CREATE TABLE orders (id INT);\n",
        "main.tf": 'resource "aws_s3_bucket" "b" {\n  bucket = "x"\n}\n',
    })
    for n in shard.nodes:
        if (n.attrs or {}).get("linkage") == "internal":
            assert n.lang in ("c", "cpp"), f"{n.name} ({n.lang}) must not be internal"
