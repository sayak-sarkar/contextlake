"""State-flow extraction: the nearest class and method come from one scan of the file.

``extract_state_flow`` used to find a transition's enclosing class and method by running
``pattern.finditer(text, 0, pos)`` once per transition for each of the two patterns. That
reads the file from the top every time: (transitions x file size). A 0.8 MB file with 1,000
transitions took 8 s.

* method: with the two patterns wrapped in a call counter, a file with 200 transitions
  scans each pattern once, not 200 times. This counts calls, so it cannot flake.
* parity: the class and method named on each transition are the nearest ones before its
  guard, in the three languages, with several classes and methods in one file.
"""

import time

import pytest

from contextlake.kb.flow import state
from contextlake.kb.flow.state import extract_state_flow


class _CountingPattern:
    """Delegates to a compiled pattern and counts ``finditer`` calls."""

    def __init__(self, real):
        self._real = real
        self.calls = 0

    def finditer(self, *args, **kwargs):
        self.calls += 1
        return self._real.finditer(*args, **kwargs)


def _py_file(transitions: int, filler_lines: int = 0) -> str:
    return "class Order:\n" + "".join(
        f"    def m{i}(self, o):\n        if o.status == S{i}:\n            o.status = T{i}\n"
        + "    # filler line\n" * filler_lines
        for i in range(transitions))


def test_each_pattern_is_scanned_once_per_file_not_once_per_transition(monkeypatch):
    klass = _CountingPattern(state._CLASS)
    method = _CountingPattern(state._METHOD_NAME["py"])
    monkeypatch.setattr(state, "_CLASS", klass)
    monkeypatch.setitem(state._METHOD_NAME, "py", method)
    _nodes, edges = extract_state_flow("r", "o.py", _py_file(200), "python")
    assert len([e for e in edges if e.relation == "transitions_to"]) == 200
    assert klass.calls == 1
    assert method.calls == 1


def test_a_file_with_no_transition_scans_nothing(monkeypatch):
    klass = _CountingPattern(state._CLASS)
    method = _CountingPattern(state._METHOD_NAME["py"])
    monkeypatch.setattr(state, "_CLASS", klass)
    monkeypatch.setitem(state._METHOD_NAME, "py", method)
    extract_state_flow("r", "o.py", "class Order:\n    def m(self):\n        return 1\n" * 50,
                       "python")
    assert (klass.calls, method.calls) == (0, 0)


def test_1000_transitions_are_extracted_in_linear_time():
    # The unfixed code took 8 s on this file. The count test above decides; this one shows
    # what a user sees, with a bound loose enough for a slow machine.
    text = _py_file(1000, filler_lines=40)
    assert len(text) > 700_000
    start = time.perf_counter()
    _nodes, edges = extract_state_flow("r", "o.py", text, "python")
    elapsed = time.perf_counter() - start
    assert len([e for e in edges if e.relation == "transitions_to"]) == 1000
    assert elapsed < 3.0, f"{elapsed:.1f}s"


_PY = """class Order:
    def pay(self, o):
        if o.status == Created:
            o.status = Paid

class Invoice:
    def settle(self, i):
        if i.state == Open:
            i.state = Closed

    def void(self, i):
        if i.state == Open:
            i.state = Void
"""

_JS = """class Order {
  pay(o) {
    if (o.status === "created") {
      o.status = "paid";
    }
  }
}
class Invoice {
  settle(i) {
    if (i.state === "open") {
      i.state = "closed";
    }
  }
  function cancel(i) {
    if (i.state === "open") {
      i.state = "void";
    }
  }
}
"""

_CS = """public class Order {
    public void Pay(Order o) {
        if (o.Status == Created) {
            o.Status = Paid;
        }
    }
}
public class Invoice {
    public void Settle(Invoice i) {
        if (i.State == Open) {
            i.State = Closed;
        }
    }
    public void Void(Invoice i) {
        if (i.State == Open) {
            i.State = Voided;
        }
    }
}
"""


@pytest.mark.parametrize("src,lang,expected", [
    (_PY, "python", [("Order", "pay", "Created", "Paid"),
                     ("Invoice", "settle", "Open", "Closed"),
                     ("Invoice", "void", "Open", "Void")]),
    (_JS, "javascript", [("Order", "pay", "created", "paid"),
                         ("Invoice", "settle", "open", "closed"),
                         ("Invoice", "cancel", "open", "void")]),
    (_CS, "csharp", [("Order", "Pay", "Created", "Paid"),
                     ("Invoice", "Settle", "Open", "Closed"),
                     ("Invoice", "Void", "Open", "Voided")]),
])
def test_each_transition_names_the_nearest_class_and_method_before_its_guard(
        src, lang, expected):
    nodes, edges = extract_state_flow("r", "f", src, lang)
    by_id = {n.id: n for n in nodes}
    got = [(by_id[e.src].attrs["entity"], e.context, by_id[e.src].name, by_id[e.dst].name)
           for e in edges if e.relation == "transitions_to"]
    assert got == expected


def test_a_guard_glued_to_a_class_name_keeps_the_answer_of_the_cut_scan():
    # No `\b` before the guard's `if`, so it can start inside a word. The match for
    # `class Midif ...` then runs across the guard's start. The one-scan lookup must give
    # the name the old `endpos=pos` scan gave (`Mid`, the name cut at the guard), not fall
    # back to the class before it.
    src = ("class Order:\n    def pay(self, o):\n        pass\n"
           "class Midif o.status == Created:\n    o.status = Paid\n")
    nodes, edges = extract_state_flow("r", "o.py", src, "python")
    entities = {n.attrs["entity"] for n in nodes if n.kind == "state"}
    assert entities == {"Mid"}
