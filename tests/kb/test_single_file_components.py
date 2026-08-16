"""Svelte and Vue: JavaScript and CSS living inside markup.

Neither grammar parses the embedded blocks. tree-sitter-svelte and tree-sitter-html both
hand back a `<script>` or `<style>` body as one opaque `raw_text` node, so the grammar's
job is finding block boundaries reliably and the contents still go through the JavaScript
and CSS grammars. That is why Svelte having its own grammar and Vue borrowing HTML's does
not produce two different mechanisms: the outer step differs, the inner step is shared.

**The line numbers are the point of the whole design.** Blocks are MASKED rather than
sliced: everything outside the block becomes spaces, newlines are kept where they are, so a
function on line 40 of the file is reported on line 40. Slicing would report line 1, and a
citation pointing at the wrong line is indistinguishable from a correct answer, which is
the one failure this project cannot afford.
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
