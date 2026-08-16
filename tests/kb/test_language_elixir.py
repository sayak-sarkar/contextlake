"""Elixir, which needed two hooks no other language has.

Elixir has **no definition node types at all**. `defmodule`, `def` and `defp` are ordinary
`call` nodes whose target is an identifier naming the macro, so:

1. **The kind cannot come from the node type.** Every definition is a `call`, so
   `_DEF_TYPES` alone would make a module, a function and a protocol the same kind.
   `_lang_kind` reads the macro name instead.
2. **The scope cannot come from a `name` field.** A `call` has `target` and `arguments`,
   not `name`, so the shared scope walk found nothing and every function came out
   unqualified. `_def_name_text` reads the macro's argument instead.

The second one has a subtlety the first draft got wrong twice, and both are recorded in the
source: `arguments` is a named CHILD and not a FIELD, so `child_by_field_name` returns None;
and only module-like macros may contribute scope, because counting a `def` as a scope
produced `Engine.start.start`, the function's own name twice.
"""

from __future__ import annotations

from contextlake.kb.parse import parse_source

SRC = b'''defmodule Engine do
  @moduledoc "the engine"
  def start(n), do: helper(n)
  defp helper(n), do: n > 0
end

defmodule Other do
  def start(n), do: n
end

defprotocol Drawable do
  def draw(x)
end
'''


def _syms():
    nodes, _e, _c, _i = parse_source("r", "a.ex", SRC, "elixir")
    return {(n.kind, n.qualified_name) for n in nodes if n.kind != "file"}


def test_the_macro_decides_the_kind_not_the_node_type():
    got = _syms()
    assert ("module", "a.ex::Engine") in got
    assert ("interface", "a.ex::Drawable") in got, (
        "defprotocol must not come back as a module or a function; every one of these is "
        "the same `call` node type and only the macro name tells them apart")
    assert ("function", "a.ex::Engine.start") in got


def test_functions_are_qualified_by_their_module():
    """THE LOAD-BEARING ASSERTION. Two modules each define `start`, which is ordinary in
    Elixir. Without module scope both collapse onto one node and the graph claims a single
    function that two callers share, which is worse than not indexing them."""
    got = _syms()
    assert ("function", "a.ex::Engine.start") in got
    assert ("function", "a.ex::Other.start") in got
    assert len({q for k, q in got if k == "function" and q.endswith(".start")}) == 2


def test_a_private_function_is_still_a_function_in_its_module():
    assert ("function", "a.ex::Engine.helper") in _syms()


def test_the_function_name_is_not_repeated_in_its_own_scope():
    """`Engine.start.start` is what came out when a `def` was allowed to contribute scope.
    Elixir has no nested `def`, so a function is scoped by its module and nothing else."""
    bad = [q for _k, q in _syms() if q and ".start.start" in q or ".helper.helper" in q]
    assert not bad, f"a definition contributed its own name as a scope: {bad}"


DIRECTIVES = b'''defmodule EngineTest do
  use ExUnit.Case
  import Engine
  alias Engine.Helper

  def helper(n), do: n
end
'''


def test_directives_are_not_definitions():
    """`use`, `import` and `alias` are NOT definitions, and the query cannot exclude them.

    Reaching `defmodule Engine` requires matching `(call (arguments (alias)))`, and that
    shape also matches every directive taking a module name. Measured before the filter
    existed: this file produced function nodes called `EngineTest.ExUnit.Case` and
    `EngineTest.Engine` -- symbols nobody wrote, in a graph whose whole claim is that its
    contents came out of the source.

    This case is here because the earlier fixture did not contain it, and its absence made
    a break-test pass against a build with the guard removed. A guard nothing exercises
    reads exactly like a guard that works.
    """
    nodes, _e, _c, _i = parse_source("r", "t.ex", DIRECTIVES, "elixir")
    names = {n.qualified_name for n in nodes if n.kind != "file"}
    assert names == {"t.ex::EngineTest", "t.ex::EngineTest.helper"}, (
        f"a directive became a definition: {sorted(names)}")
