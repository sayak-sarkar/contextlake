"""Structural leaves are folded into a count on their container, in the graph VIEW only.

Measured on a 663k-node store: five kinds are the source of no edge and are reached only by
`contains` -- `config_key`, `macro`, `field`, `enum_constant`, `global_variable` -- and they
are 74.1% of all nodes. Drawing them adds a dot and a label and answers nothing the container
does not already answer.

**The rule is structural and that kind list is deliberately not in the code.** A hardcoded list
would encode one fleet's shape (`config_key` alone is 55.6% of that store, is 100% XML, and
three repositories hold 71.8% of it), would keep folding a kind the day a parser gives it a real
edge, and would need `typedef` remembered as an exception. Asking the graph gets all three right.
"""

from contextlake.kb.visualize.payload import fold_contained_leaves, to_payload


def _n(i, kind):
    return {"id": i, "kind": kind}


def _e(s, d, rel="contains"):
    return {"src": s, "dst": d, "relation": rel}


# --- what folds -------------------------------------------------------------------------

def test_a_contained_leaf_folds_into_a_count_on_its_container():
    nd, ed, tally = fold_contained_leaves(
        [_n("f", "file"), _n("k1", "config_key"), _n("k2", "config_key")],
        [_e("f", "k1"), _e("f", "k2")])
    assert [n["id"] for n in nd] == ["f"]
    assert nd[0]["folded"] == 2 and nd[0]["folded_kinds"] == {"config_key": 2}
    assert tally == {"config_key": 2}
    assert ed == [], "the contains edges to folded nodes go with them"


def test_a_node_that_is_a_source_is_never_folded():
    """It calls something, so it answers a question its container does not."""
    nd, _, _ = fold_contained_leaves(
        [_n("c", "class"), _n("m", "method"), _n("x", "class")],
        [_e("c", "m"), _e("m", "x", "calls")])
    assert {n["id"] for n in nd} == {"c", "m", "x"}


def test_a_node_reached_by_more_than_contains_is_never_folded():
    """`typedef` is the real case: 22k nodes, never a source, but `inherits` edges reach
    them. Folding those would hide a type hierarchy. A hardcoded kind list has to remember
    this exception; the structural rule never learns it in the first place."""
    nd, _, _ = fold_contained_leaves(
        [_n("f", "file"), _n("c", "class"), _n("t", "typedef")],
        [_e("f", "t"), _e("c", "t", "inherits")])
    assert {n["id"] for n in nd} == {"f", "c", "t"}


def test_a_leaf_whose_container_is_absent_is_kept():
    """Folding into something not in this payload would drop it from the picture."""
    nd, _, _ = fold_contained_leaves([_n("k", "config_key")], [_e("missing", "k")])
    assert [n["id"] for n in nd] == ["k"]


def test_the_rule_names_no_kinds():
    """The guard against encoding one fleet's shape: an invented kind with leaf STRUCTURE
    folds, and a known-foldable kind with a real edge does not."""
    nd, _, tally = fold_contained_leaves(
        [_n("f", "file"), _n("z", "some_future_kind")], [_e("f", "z")])
    assert tally == {"some_future_kind": 1} and [n["id"] for n in nd] == ["f"]

    nd2, _, _ = fold_contained_leaves(
        [_n("f", "file"), _n("k", "config_key"), _n("o", "file")],
        [_e("f", "k"), _e("k", "o", "calls")])
    assert {n["id"] for n in nd2} == {"f", "k", "o"}, "config_key with a real edge stays"


# --- the payload contract -----------------------------------------------------------------

def test_counts_describe_what_was_handed_in_not_what_is_drawn():
    """A reader told only the post-fold number would think the graph smaller than it is."""
    p = to_payload([_n("f", "file"), _n("k", "config_key")], [_e("f", "k")], fold_leaves=True)
    assert p["meta"]["node_count"] == 2          # as handed in
    assert p["meta"]["drawn_node_count"] == 1    # after folding
    assert p["meta"]["folded_leaves"] == 1


def test_folding_is_off_by_default():
    """Off is the correct default: several exports read this payload whole. Defaulting it
    on emptied the UML class diagram, because a `method` reached only by `contains` is a
    structural leaf and in that view the methods ARE the content."""
    p = to_payload([_n("f", "file"), _n("k", "config_key")], [_e("f", "k")])
    assert len(p["nodes"]) == 2
    assert "folded_leaves" not in p["meta"]


def test_nothing_is_hidden_silently():
    """A folded container carries the tally, so the picture can say what it left out."""
    p = to_payload([_n("f", "file"), _n("a", "field"), _n("b", "macro")],
                   [_e("f", "a"), _e("f", "b")], fold_leaves=True)
    assert p["meta"]["folded_leaf_kinds"] == {"field": 1, "macro": 1}
    assert p["nodes"][0]["folded_kinds"] == {"field": 1, "macro": 1}


def test_a_graph_with_nothing_foldable_is_returned_untouched():
    nodes = [_n("a", "class"), _n("b", "class")]
    edges = [_e("a", "b", "calls")]
    nd, ed, tally = fold_contained_leaves(list(nodes), list(edges))
    assert nd == nodes and ed == edges and tally == {}
