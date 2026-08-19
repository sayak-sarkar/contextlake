"""PL/SQL objects and the shell dialects, both of which produced nothing before.

Oracle tooling splits one object per file by convention, so a package spec lives in `.pks`
and its body in `.pkb`. Neither extension was routed anywhere, and inside `.sql` only
`CREATE PROCEDURE` was matched, and only in its T-SQL spelling: an Oracle tree writes
`CREATE OR REPLACE`, which the pattern did not accept, so those files contributed no nodes
at all while appearing to be indexed.
"""

from __future__ import annotations

import pytest

from contextlake.kb.parse import LANG_BY_EXT, SQL_EXTS
from contextlake.kb.sql import parse_sql

PLSQL = b"""
CREATE OR REPLACE PACKAGE billing_pkg AS
  PROCEDURE charge(p_id NUMBER);
END billing_pkg;
/
CREATE OR REPLACE PACKAGE BODY billing_pkg AS
  PROCEDURE charge(p_id NUMBER) IS BEGIN NULL; END;
END billing_pkg;
/
CREATE OR REPLACE FUNCTION calc_total(p NUMBER) RETURN NUMBER IS
BEGIN RETURN p; END;
/
CREATE OR REPLACE TYPE addr_t AS OBJECT (street VARCHAR2(80));
/
CREATE TABLE orders (id NUMBER PRIMARY KEY, cust_id NUMBER REFERENCES customers(id));
CREATE OR REPLACE TRIGGER orders_audit
  AFTER INSERT ON orders
  FOR EACH ROW
BEGIN NULL; END;
/
CREATE OR REPLACE PROCEDURE standalone_proc IS BEGIN NULL; END;
/
"""


def _kinds(nodes):
    return {(n.kind, n.name) for n in nodes}


def test_every_plsql_object_type_is_extracted():
    nodes, _ = parse_sql("demo", "billing.pkb", PLSQL)
    found = _kinds(nodes)
    for kind, name in [("db_package", "billing_pkg"), ("function", "calc_total"),
                       ("typedef", "addr_t"), ("trigger", "orders_audit"),
                       ("procedure", "standalone_proc"), ("table", "orders")]:
        assert (kind, name) in found, f"{kind} {name} was not extracted: {sorted(found)}"


def test_a_package_and_its_body_are_both_recorded():
    """Two definitions of one package, and a reader looking for either should find it."""
    nodes, _ = parse_sql("demo", "billing.pkb", PLSQL)
    pkgs = sorted(n.line_start for n in nodes
                  if n.kind == "db_package" and n.name == "billing_pkg")
    assert len(pkgs) == 2, f"expected the spec and the body, got lines {pkgs}"


def test_package_body_is_not_recorded_as_a_package_named_body():
    """`PACKAGE\\s+<name>` reads "BODY" as the name, and then the real package is lost."""
    nodes, _ = parse_sql("demo", "b.pkb", PLSQL)
    assert not [n for n in nodes if n.name == "body"], (
        "BODY was taken as an object name")


def test_the_oracle_redefinition_spelling_is_accepted():
    """T-SQL writes `CREATE OR ALTER`, Oracle writes `CREATE OR REPLACE`. Only the first
    was matched, so an entire Oracle codebase produced no procedures."""
    nodes, _ = parse_sql("demo", "p.sql",
                         b"CREATE OR REPLACE PROCEDURE only_one IS BEGIN NULL; END;")
    assert ("procedure", "only_one") in _kinds(nodes)


def test_a_trigger_records_the_table_it_fires_on():
    nodes, refs = parse_sql("demo", "t.sql", PLSQL)
    trigger = next(n for n in nodes if n.kind == "trigger")
    targets = [t for src, t, _p, _l in refs if src == trigger.id]
    assert targets == ["orders"], f"trigger references {targets}"


def test_a_join_is_not_read_as_a_trigger_target():
    """`ON` introduces a join everywhere else in SQL, so the search is bounded to the
    trigger's own statement.

    The fixture gives the trigger NO table of its own, which is the only shape that can
    tell a bounded search from an unbounded one: with a target present, both find it and
    the bound proves nothing. Unbounded, the join below is attributed to the trigger.
    """
    # A truncated first trigger, which legacy trees do contain, followed by a complete one.
    # This is the only shape that separates a bounded search from an unbounded one: with a
    # target of its own, the first trigger finds it either way and the bound proves nothing.
    src = b"""
CREATE OR REPLACE TRIGGER t_truncated
/
CREATE OR REPLACE TRIGGER t_real
  AFTER INSERT ON orders
  FOR EACH ROW
BEGIN NULL; END;
/
"""
    nodes, refs = parse_sql("demo", "t.sql", src)
    truncated = next(n for n in nodes if n.name == "t_truncated")
    stolen = sorted(t for s, t, _p, _l in refs if s == truncated.id)
    assert stolen == [], (
        f"the truncated trigger claimed a table belonging to a later statement: {stolen}")
    real = next(n for n in nodes if n.name == "t_real")
    assert [t for s, t, _p, _l in refs if s == real.id] == ["orders"]


def test_commented_out_plsql_mints_nothing():
    """The masking that protects table extraction has to cover the new objects too."""
    src = b"""
-- CREATE OR REPLACE PACKAGE ghost_pkg AS END;
/* CREATE OR REPLACE TRIGGER ghost_trg AFTER INSERT ON t FOR EACH ROW BEGIN NULL; END; */
CREATE OR REPLACE PACKAGE real_pkg AS END;
"""
    nodes, _ = parse_sql("demo", "c.sql", src)
    names = {n.name for n in nodes}
    assert "real_pkg" in names
    assert "ghost_pkg" not in names and "ghost_trg" not in names, (
        f"a commented-out definition was recorded: {sorted(names)}")


def test_a_package_body_does_not_swallow_a_later_tables_foreign_keys():
    """Scope: without the PL/SQL keywords in the boundary, a CREATE TABLE's reference scope
    runs on through an unrelated body and attributes its REFERENCES to the wrong table."""
    # The REFERENCES inside the body is LIVE code, not a comment. A commented one is masked
    # before any pattern runs, so it cannot tell a correct scope boundary from a missing
    # one: the first version of this fixture commented it out and the test passed with
    # PACKAGE removed from the boundary entirely.
    src = b"""
CREATE TABLE first_t (id NUMBER);
CREATE OR REPLACE PACKAGE BODY some_pkg AS
  PROCEDURE p IS BEGIN
    INSERT INTO audit_t SELECT * FROM other_t WHERE id IN (
      SELECT id FROM x REFERENCES other_t);
  END;
END some_pkg;
/
CREATE TABLE second_t (id NUMBER, fk NUMBER REFERENCES target_t(id));
"""
    nodes, refs = parse_sql("demo", "s.sql", src)
    first = next(n for n in nodes if n.name == "first_t")
    assert not [t for s, t, _p, _l in refs if s == first.id], (
        "first_t picked up a reference from beyond its own statement")
    second = next(n for n in nodes if n.name == "second_t")
    assert [t for s, t, _p, _l in refs if s == second.id] == ["target_t"]


@pytest.mark.parametrize("ext", [".pks", ".pkb", ".plb", ".prc", ".fnc", ".trg", ".pls"])
def test_every_plsql_extension_is_routed(ext):
    """A file that goes unindexed because of its suffix is the same file with a different
    name. These were routed nowhere."""
    assert ext in SQL_EXTS


@pytest.mark.parametrize("ext", [".sh", ".bash", ".ksh", ".zsh", ".bats", ".command"])
def test_every_shell_dialect_is_routed_to_the_bash_grammar(ext):
    assert LANG_BY_EXT.get(ext) == "bash"


def test_the_new_kinds_are_registered():
    """An unregistered kind has no colour, no group and no embedding decision."""
    from contextlake.kb.kinds import KIND_REGISTRY

    for kind in ("db_package", "trigger"):
        assert kind in KIND_REGISTRY, f"{kind} is emitted but not registered"


def test_a_database_package_is_not_the_dependency_kind():
    """`package` is a shared cross-repo node in the packages partition, built from manifests.
    Reusing it for an Oracle package would put database objects into the fleet's
    shared-dependency count, which is a number the fleet page reports."""
    from contextlake.kb.kinds import KIND_REGISTRY

    assert KIND_REGISTRY["db_package"].group == "Data model"
    assert KIND_REGISTRY["package"].group != KIND_REGISTRY["db_package"].group
    nodes, _ = parse_sql("demo", "p.pks", b"CREATE OR REPLACE PACKAGE p AS END;")
    assert {n.kind for n in nodes} == {"db_package"}
