from contextlake.kb.flow.state import extract_state_flow
from contextlake.kb.model import Confidence


def _transitions(edges):
    return [e for e in edges if e.relation == "transitions_to"]


def _contains(edges):
    return [e for e in edges if e.relation == "contains"]


def test_python_guarded_transition_is_extracted():
    src = b'''
class Order:
    def pay(self):
        if self.status == "Created":
            self.status = "Paid"
'''
    nodes, edges = extract_state_flow("r", "order.py", src, "python")
    assert {n.name for n in nodes if n.kind == "state"} == {"Created", "Paid"}
    trans = _transitions(edges)
    assert len(trans) == 1
    e = trans[0]
    assert e.context == "pay" and e.confidence == Confidence.INFERRED
    created = next(n for n in nodes if n.name == "Created")
    paid = next(n for n in nodes if n.name == "Paid")
    assert (e.src, e.dst) == (created.id, paid.id)
    assert created.attrs["entity"] == "Order" and paid.qualified_name == "Order.Paid"
    # each state node is reachable from the file (not an island only a --repo
    # view can see) -- 2-hop from a class-name seed: class <-file-> state
    assert {c.dst for c in _contains(edges)} == {created.id, paid.id}


def test_csharp_guarded_transition_with_enum_values():
    src = b'''
public class Order {
    public void Pay() {
        if (this.Status == OrderStatus.Created) {
            this.Status = OrderStatus.Paid;
        }
    }
}
'''
    nodes, edges = extract_state_flow("r", "Order.cs", src, "csharp")
    # enum-qualified values collapse to the member name, not the qualified enum path
    assert {n.name for n in nodes} == {"Created", "Paid"}
    assert _transitions(edges)[0].context == "Pay"


def test_js_class_method_shorthand_guarded_transition():
    src = b'''
class Order {
  pay() {
    if (this.status === "created") {
      this.status = "paid";
    }
  }
}
'''
    nodes, edges = extract_state_flow("r", "order.ts", src, "typescript")
    assert {n.name for n in nodes} == {"created", "paid"}
    assert _transitions(edges)[0].context == "pay"


def test_unguarded_assignment_is_not_a_transition():
    """No preceding comparison on the same field -- option (a): fewer, honest
    edges over guessing a source state the code never establishes."""
    src = b'''
class Order:
    def force_ship(self):
        self.status = "Shipped"
'''
    nodes, edges = extract_state_flow("r", "order.py", src, "python")
    assert nodes == [] and edges == []


def test_guard_reasserting_the_same_value_is_not_a_transition():
    src = b'''
class Order:
    def touch(self):
        if self.status == "Paid":
            self.status = "Paid"
'''
    nodes, edges = extract_state_flow("r", "order.py", src, "python")
    assert nodes == [] and edges == []


def test_transition_with_no_enclosing_class_is_dropped():
    src = b'''
def free_function():
    if order.status == "Created":
        order.status = "Paid"
'''
    nodes, edges = extract_state_flow("r", "funcs.py", src, "python")
    assert nodes == [] and edges == []


def test_two_entities_in_one_file_are_kept_separate():
    src = b'''
class Order:
    def pay(self):
        if self.status == "Created":
            self.status = "Paid"

class Invoice:
    def issue(self):
        if self.state == "Draft":
            self.state = "Issued"
'''
    nodes, edges = extract_state_flow("r", "both.py", src, "python")
    entities = {n.attrs["entity"] for n in nodes}
    assert entities == {"Order", "Invoice"}
    assert len(_transitions(edges)) == 2


def test_different_receivers_are_not_cross_wired():
    """A guard on `order` must not license an assignment on a different `invoice`
    receiver, even if both use the same field name."""
    src = b'''
class Mixed:
    def weird(self):
        if order.status == "Created":
            invoice.status = "Paid"
'''
    nodes, edges = extract_state_flow("r", "mixed.py", src, "python")
    assert nodes == [] and edges == []


def test_unsupported_language_is_noop():
    assert extract_state_flow("r", "a.go", b'if x.status == "y" { x.status = "z" }', "go") == (
        [], [])


def test_assignment_in_an_else_branch_is_not_the_if_branchs_transition():
    """The assignment is reached when the guard is FALSE, not true -- emitting
    it as a transition from the guarded state would be a false transition,
    which this module's docstring promises never to produce."""
    src = b'''
class Order:
    def foo(self):
        if self.status == "Created":
            notify()
        else:
            self.status = "Cancelled"
'''
    nodes, edges = extract_state_flow("r", "order.py", src, "python")
    assert nodes == [] and edges == []


def test_assignment_in_an_elif_branch_is_not_the_ifs_transition():
    """Must not cross-wire to a DIFFERENT elif branch's own (unmatched) guard,
    and must not swallow the elif's real transition via the same match."""
    src = b'''
class Order:
    def foo(self):
        if self.status == "Created":
            log()
        elif self.status == "Shipped":
            self.status = "Delivered"
'''
    nodes, edges = extract_state_flow("r", "order.py", src, "python")
    assert nodes == [] and edges == []


def test_assignment_after_a_guard_in_a_different_method_is_dropped():
    src = b'''
class Order:
    def a(self):
        if self.status == "Created":
            log()
    def b(self):
        if self.status == "Shipped":
            self.status = "Delivered"
'''
    nodes, edges = extract_state_flow("r", "order.py", src, "python")
    assert nodes == [] and edges == []


def test_assignment_actually_governed_by_a_later_unrelated_guard_is_dropped():
    """A `!=` guard doesn't match `_GUARD_ASSIGN` on its own, but its presence
    between an unrelated `==` guard and the assignment means the assignment
    isn't reliably reached under the first guard either."""
    src = b'''
class Order:
    def foo(self):
        if self.status == "Created":
            pass
        if self.status != "Paid":
            self.status = "Failed"
'''
    nodes, edges = extract_state_flow("r", "order.py", src, "python")
    assert nodes == [] and edges == []


def test_assignment_from_a_field_read_is_not_a_state_literal():
    """`self.status = other.status` copies a field, it doesn't assign a state
    literal -- must not synthesize a state node named after the field itself."""
    src = b'''
class Order:
    def foo(self, other):
        if self.status == "Created":
            self.status = other.status
'''
    nodes, edges = extract_state_flow("r", "order.py", src, "python")
    assert nodes == [] and edges == []


def test_repeated_identical_transition_is_deduped():
    src = b'''
class Order:
    def pay(self):
        if self.status == "Created":
            self.status = "Paid"
        if self.status == "Created":
            self.status = "Paid"
'''
    nodes, edges = extract_state_flow("r", "order.py", src, "python")
    assert len(_transitions(edges)) == 1
    assert len(_contains(edges)) == len(nodes)  # one contains edge per state node, not per hit
