"""Svelte and Vue: JavaScript and CSS living inside markup.

Neither grammar parses the embedded blocks. tree-sitter-svelte and tree-sitter-html both
hand back a `<script>` or `<style>` body as one opaque `raw_text` node, so the grammar's
job is finding block boundaries reliably and the contents still go through the JavaScript
and CSS grammars. That is why Svelte having its own grammar and Vue borrowing HTML's does
not produce two different mechanisms: the outer step differs, the inner step is shared.

**The line numbers are the point of the whole design.** A block is parsed from its own text,
and every line it reports is then moved down by the row the block starts on, so a function on
line 40 of the file is reported on line 40. Left unmoved, a block would report line 1, and a
citation pointing at the wrong line is indistinguishable from a correct answer, which is the
one failure this project cannot afford. The cost of a block is its own length. It used to be a
copy of the whole file for every block, and then a run of newlines for every row above it.
"""

from __future__ import annotations

import pytest

from contextlake.kb.parse import index_repo_dir

SVELTE = b"""<script>
  export const NAME = "widget";
  function greet() { return NAME; }
</script>

<div class="wrap">hi</div>

<style>
.wrap { color: red; }
</style>
"""

VUE = b"""<template>
  <div class="wrap">hi</div>
</template>

<script>
function mounted() { return 1; }
</script>
"""

TEMPLATE_ONLY = b"""<template>
  <p>no script, no style</p>
</template>
"""


@pytest.fixture(scope="module")
def shard(tmp_path_factory):
    d = tmp_path_factory.mktemp("sfc")
    (d / "Widget.svelte").write_bytes(SVELTE)
    (d / "Demo.vue").write_bytes(VUE)
    (d / "Bare.vue").write_bytes(TEMPLATE_ONLY)
    return index_repo_dir(str(d), "demo", head_commit="h")


def _at(shard, file, name):
    return next((n for n in shard.nodes if n.file == file and n.name == name), None)


@pytest.mark.parametrize("name,kind", [("NAME", "global_variable"), ("greet", "function")])
def test_svelte_script_symbols(shard, name, kind):
    node = _at(shard, "Widget.svelte", name)
    assert node is not None, f"{name} was not extracted from the script block"
    assert node.kind == kind


@pytest.mark.parametrize("file,name,line", [
    ("Widget.svelte", "NAME", 2),
    ("Widget.svelte", "greet", 3),
    ("Widget.svelte", "wrap", 9),
    ("Demo.vue", "mounted", 6),
])
def test_line_numbers_are_relative_to_the_file(shard, file, name, line):
    """THE LOAD-BEARING ASSERTION. Each of these lines was counted in the fixture above.
    A sliced block would report 1, 2, 1 and 2 instead, which still looks like a citation."""
    node = _at(shard, file, name)
    assert node is not None, f"{name} missing from {file}"
    assert node.line_start == line, (
        f"{file}:{name} cites line {node.line_start}, but it is on line {line} of the "
        f"file; the block offset was lost")


def test_the_style_block_is_parsed_as_css(shard):
    node = _at(shard, "Widget.svelte", "wrap")
    assert node is not None and node.kind == "css_class"


def test_the_file_node_keeps_the_component_language(shard):
    """A `.svelte` file is a Svelte file whatever its script is written in, and the language
    a reader filters by is the one on the file."""
    for name, lang in (("Widget.svelte", "svelte"), ("Demo.vue", "vue")):
        node = _at(shard, name, name)
        assert node is not None and node.kind == "file"
        assert node.lang == lang, f"{name} file node claims to be {node.lang}"


def test_a_template_only_component_still_appears(shard):
    """No script and no style is a real component, and a file that silently is not in the
    graph is worse than one with nothing under it."""
    node = _at(shard, "Bare.vue", "Bare.vue")
    assert node is not None and node.kind == "file" and node.lang == "vue"


def test_a_top_level_script_call_resolves_into_a_javascript_file(tmp_path):
    """A call at the top of a `<script>` is made by the FILE node, and that node keeps the
    component's language, `vue` or `svelte`. Neither was in a language family, so every
    JavaScript candidate was rejected as a cross-language match and the call left no edge.
    A call inside a function worked, because the function node carries `javascript`."""
    (tmp_path / "composables.js").write_text("export function useThing() { return 1; }\n")
    (tmp_path / "thing.py").write_text("def useThing():\n    return 2\n")
    (tmp_path / "App.vue").write_bytes(b"<script setup>\nconst thing = useThing();\n</script>\n")
    (tmp_path / "Widget.svelte").write_bytes(b"<script>\nconst thing = useThing();\n</script>\n")
    shard = index_repo_dir(str(tmp_path), "demo", head_commit="h")
    by_id = {n.id: n for n in shard.nodes}
    calls = {(by_id[e.src].name, by_id[e.dst].file) for e in shard.edges
             if e.relation == "calls"}
    # The JavaScript definition and nothing else: the Python `useThing` is another family.
    assert calls == {("App.vue", "composables.js"), ("Widget.svelte", "composables.js")}


def test_a_block_is_parsed_from_its_own_text_and_not_from_a_copy_of_the_file(monkeypatch):
    """Every block used to be parsed from a buffer as long as the whole file, kept in a list
    for the length of the run. 200 style blocks in a 968,724-byte component took 19 s and
    held about 194 MB. The input sizes are counted here, not the seconds, so the result does
    not move with the machine: the old code passed 3 x len(file) bytes for this fixture."""
    import contextlake.kb.parse as parse_mod

    src = (b"<style>.a { color: red; }</style>\n<style>.b { color: red; }</style>\n"
           b"<script>function c() { return 1; }</script>\n"
           b"<template><p>" + b"x" * 200_000 + b"</p></template>\n")
    sizes: list[int] = []
    real = parse_mod.parse_source

    def spy(repo_id, rel, source, lang, *a, **k):
        sizes.append(len(source))
        return real(repo_id, rel, source, lang, *a, **k)

    monkeypatch.setattr(parse_mod, "parse_source", spy)
    sf = parse_mod.SourceFile(rel="Big.vue", source=src, kind="code", lang="vue")
    nodes, _edges = parse_mod._parse_sfc("r", sf, parse_mod.RefCollector())
    assert len(sizes) == 3
    assert sum(sizes) < 1_000, f"blocks were parsed from {sizes} bytes of a {len(src)}-byte file"
    assert {n.name for n in nodes} >= {"a", "b", "c"}, "and the blocks still parse"


def test_the_blocks_of_a_component_are_built_one_at_a_time():
    """A list holds every block until the caller is done with all of them. A generator holds
    one, which is what keeps memory flat when a component has thousands."""
    from contextlake.kb.parse import _sfc_blocks

    blocks = _sfc_blocks(b"<style>.a { color: red; }</style>\n<style>.b { color: red; }</style>\n",
                         "vue")
    assert iter(blocks) is blocks
    assert [lang for lang, _text, _row in blocks] == ["css", "css"]


def test_many_blocks_keep_their_rows_and_columns(tmp_path):
    """Several blocks per line and several lines apart. Each block is moved by its own row,
    so a miscount shows as a wrong line only on the later blocks."""
    lines = ["<template><p>x</p></template>"]
    want = {}
    for i in range(40):
        lines.append(f"<style>.s{i} {{ color: red; }}</style> "
                     f"<script>function f{i}() {{}}</script>")
        want[f"f{i}"] = len(lines)
        lines.append("")
    (tmp_path / "Many.vue").write_text("\n".join(lines) + "\n")
    shard = index_repo_dir(str(tmp_path), "demo")
    got = {n.name: n.line_start for n in shard.nodes if n.kind == "function"}
    assert got == want
    assert {n.name: n.line_start for n in shard.nodes if n.kind == "css_class"} == {
        f"s{i}": want[f"f{i}"] for i in range(40)}


def test_a_block_is_its_own_text_with_the_row_it_starts_on():
    """The block is the text between the tags and nothing else. The row says where it sits."""
    from contextlake.kb.parse import _sfc_blocks

    src = b"<template><p>x</p></template>\n\n  <script>function f() {}</script>\n"
    ((lang, text, row),) = _sfc_blocks(src, "vue")
    assert lang == "javascript"
    assert text == b"function f() {}"
    assert row == 2


def test_every_line_a_block_reports_is_a_line_of_the_file():
    """A line lives in a node's two lines, an edge's source line, a call and an inherits.
    Each is checked against a line counted in the fixture, so a field the shift missed
    reads as a block-relative line (7 or less) instead of the file's (21 to 26)."""
    import contextlake.kb.parse as parse_mod

    src = ("\n" * 19 + "<script>\nimport a from './a'\nclass Child extends Base {\n"
           "  go() {\n    return helper();\n  }\n}\n</script>\n").encode()
    # Line 20 is `<script>`, 21 the import, 22 the class, 23 `go`, 24 the call, 27 `</script>`.
    refs = parse_mod.RefCollector()
    sf = parse_mod.SourceFile(rel="Lines.vue", source=src, kind="code", lang="vue")
    nodes, edges = parse_mod._parse_sfc("r", sf, refs)
    by_name = {n.name: n for n in nodes}
    assert (by_name["Child"].line_start, by_name["Child"].line_end) == (22, 26)
    assert (by_name["go"].line_start, by_name["go"].line_end) == (23, 25)
    assert {e.relation: e.provenance.source_line for e in edges
            if e.relation == "imports"} == {"imports": 21}
    assert sorted(e.provenance.source_line for e in edges if e.relation == "contains") == [
        22, 23]
    assert [line for *_rest, line in refs.calls] == [24]
    assert [line for *_rest, line in refs.inherits] == [22]


def _bytes_parsed(monkeypatch, n_blocks: int) -> int:
    """Total bytes handed to `parse_source` for a component of `n_blocks` one-line blocks."""
    import contextlake.kb.parse as parse_mod

    sizes: list[int] = []
    real = parse_mod.parse_source

    def spy(repo_id, rel, source, lang, *a, **k):
        sizes.append(len(source))
        return real(repo_id, rel, source, lang, *a, **k)

    monkeypatch.setattr(parse_mod, "parse_source", spy)
    src = b"".join(b"<style>.s%03d { color: red; }</style>\n" % i for i in range(n_blocks))
    sf = parse_mod.SourceFile(rel="Rows.vue", source=src, kind="code", lang="vue")
    parse_mod._parse_sfc("r", sf, parse_mod.RefCollector())
    assert len(sizes) == n_blocks
    return sum(sizes)


def test_the_bytes_parsed_grow_with_the_block_count_and_not_with_the_rows(monkeypatch):
    """N blocks on N rows. A block padded with the rows above it costs N x N newline bytes,
    so doubling the blocks multiplied the bytes by about 4: 10,000 blocks took 5.2 s and
    40,000 took 77 s. Counted here as bytes, not seconds, so it does not move with the
    machine. Every block has the same text, so linear growth is a ratio of exactly 2."""
    small = _bytes_parsed(monkeypatch, 100)
    double = _bytes_parsed(monkeypatch, 200)
    assert double == 2 * small, f"2x the blocks parsed {double / small:.1f}x the bytes"
    assert small == 100 * len(b".s000 { color: red; }"), "and each block is only its own text"
