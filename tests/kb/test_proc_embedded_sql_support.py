"""Pro*C support: C source with `EXEC SQL` written into it.

Two measurements decided the shape of this, and both are pinned here.

The first: handed straight to the C grammar, an `EXEC SQL` statement parses as a
declaration. `EXEC SQL INCLUDE SQLCA;` and `EXEC SQL BEGIN DECLARE SECTION;` produced
`global_variable` nodes named `SQLCA`, `SQL` and `SECTION` -- names of things that do not
exist, in a kind a bare identifier elsewhere in the repository resolves `uses` edges onto.

The second: nothing new was needed to reach the tables. The dataflow pass already matches
literal SQL in any language and already normalises table names through `sql._norm_name`, so
an `EXEC SQL` reference and the `CREATE TABLE` that defines the table in another file land
on one node by construction. The trap named for this work was avoided by not writing a
second copy of the rule, and the test below is what proves it rather than asserts it.
"""

from __future__ import annotations

from contextlake.kb.parse import (
    _PARSERS,
    PROC_EXTS,
    RefCollector,
    SourceFile,
    _file_kind,
    _source_filter,
)
from contextlake.kb.proc import mask_embedded_sql
from contextlake.kb.sql import parse_sql

PROC = rb"""
#include <stdio.h>
EXEC SQL INCLUDE SQLCA;

EXEC SQL BEGIN DECLARE SECTION;
    char  cust_name[41];
    int   cust_id;
EXEC SQL END DECLARE SECTION;

static void load_customer(int id)
{
    cust_id = id;
    /* Wrapped across lines, and the SQL upper-cased, because both are how Pro*C is
       actually written -- and each one is what makes a guard below reachable. */
    EXEC SQL SELECT name
             INTO :cust_name
             FROM CUSTOMERS
             WHERE id = :cust_id;
    printf("%s\n", cust_name);
}

int record_order(int oid)
{
    load_customer(oid);
    EXEC SQL INSERT INTO orders (id, cust_id) VALUES (:oid, :cust_id);
    EXEC SQL UPDATE inventory SET qty = qty - 1 WHERE id = :oid;
    EXEC SQL DELETE FROM staging_orders WHERE id = :oid;
    /* EXEC SQL DELETE FROM audit_log WHERE 1 = 1; */
    return 0;
}
"""


def _run(source=PROC, rel="src/order.pc"):
    refs = RefCollector()
    nodes, edges = _PARSERS["proc"]("demo", SourceFile(rel, source, "proc", ""), refs)
    return nodes, edges, refs


def _defs(nodes):
    return {(n.kind, n.name) for n in nodes if n.kind != "file"}


# --- the C side ------------------------------------------------------------------------

def test_the_c_functions_are_extracted():
    nodes, _edges, _refs = _run()
    assert ("function", "load_customer") in _defs(nodes)
    assert ("function", "record_order") in _defs(nodes)


def test_the_embedded_sql_does_not_become_c_declarations():
    """The measurement this module exists for. Without the mask these are real nodes with
    real-looking names, and `global_variable` is a kind bare identifiers resolve onto."""
    names = {name for _kind, name in _defs(_run()[0])}
    assert names.isdisjoint({"SQLCA", "SQL", "SECTION", "EXEC"}), sorted(names)


def test_the_host_variables_between_the_declare_markers_survive():
    """Only the `EXEC SQL` statements are masked, not the C between them. `cust_name` and
    `cust_id` are ordinary C variables that the precompiler keeps, and a mask that swallowed
    the whole declare section would delete them from the graph."""
    assert {("global_variable", "cust_name"), ("global_variable", "cust_id")} <= _defs(
        _run()[0])


def test_a_call_between_the_files_own_functions_still_resolves():
    _nodes, _edges, refs = _run()
    assert any(target == "load_customer" for _src, target, _rel, _line in refs.calls)


def test_the_mask_preserves_length_and_every_line():
    """Line numbers are the provenance every node cites. A delete rather than a blank would
    shift every function below the first `EXEC SQL` in the file."""
    masked = mask_embedded_sql(PROC)
    assert len(masked) == len(PROC)
    assert masked.count(b"\n") == PROC.count(b"\n")
    assert b"EXEC SQL" not in masked


def test_an_embedded_plsql_block_is_masked_to_its_end_exec():
    """`EXEC SQL EXECUTE` opens a PL/SQL block whose body has semicolons of its own.
    Terminating it at the first one leaves the rest of the block in the C parse."""
    src = b"""
EXEC SQL EXECUTE
  BEGIN
     UPDATE parts SET qty = 0;
     COMMIT;
  END;
END-EXEC;
int after(void) { return 1; }
"""
    masked = mask_embedded_sql(src)
    assert b"COMMIT" not in masked
    assert b"END-EXEC" not in masked
    assert b"int after(void)" in masked


def test_an_unterminated_statement_is_masked_to_end_of_file():
    """Malformed input, and blanking the remainder is the safe reading: the alternative
    hands a partial SQL statement to the C grammar."""
    masked = mask_embedded_sql(b"int f(void) { return 0; }\nEXEC SQL SELECT * FROM t\n")
    assert b"int f(void)" in masked
    assert b"SELECT" not in masked


# --- the SQL side ----------------------------------------------------------------------

def test_the_tables_the_file_reads_and_writes_are_recorded():
    _nodes, _edges, refs = _run()
    assert {t for _s, t, _r, _l in refs.data_reads} == {"customers"}
    assert {t for _s, t, _r, _l in refs.data_writes} == {
        "orders", "inventory", "staging_orders"}


def test_a_commented_out_statement_is_not_a_write():
    _nodes, _edges, refs = _run()
    assert "audit_log" not in {t for _s, t, _r, _l in refs.data_writes}


def test_the_table_reference_and_its_definition_in_another_file_reach_one_node():
    """The trap this work was warned about, tested the way the ledger prescribed: the
    `CREATE TABLE` and the `EXEC SQL` that names it are in DIFFERENT files, and the edge
    must land on exactly one node."""
    # The DDL spells the table in mixed case and the `EXEC SQL` in upper. Matching them
    # is what `sql._norm_name` does for both sides; a reference that kept its own casing
    # would resolve to nothing while looking entirely correct.
    ddl = b"CREATE TABLE dbo.[Customers] (id INT PRIMARY KEY, name VARCHAR(40));\n"
    table_nodes, _ = parse_sql("demo", "schema/tables.sql", ddl)
    proc_nodes, _edges, refs = _run()

    by_id = {n.id: n for n in [*proc_nodes, *table_nodes]}
    edges = refs.resolved_edges(by_id)
    reads = [e for e in edges if e.relation == "reads"]
    assert len(reads) == 1, reads
    assert by_id[reads[0].dst].kind == "table"
    assert by_id[reads[0].dst].file == "schema/tables.sql"


# --- routing ---------------------------------------------------------------------------

def test_pc_routes_to_the_proc_extractor_when_c_is_selected():
    allowed_exts, allowed_names, hcl, sql = _source_filter(None)
    assert _file_kind("order.pc", ".pc", "src/order.pc", allowed_exts=allowed_exts,
                      allowed_names=allowed_names, index_hcl=hcl, index_sql=sql) == "proc"
    assert PROC_EXTS == {".pc"}


def test_pc_is_not_selected_when_the_language_filter_excludes_c():
    """It is C source, so it follows C rather than a flag of its own: `--languages c`
    selects it and `--languages python` does not."""
    allowed_exts, allowed_names, hcl, sql = _source_filter(["python"])
    assert _file_kind("order.pc", ".pc", "src/order.pc", allowed_exts=allowed_exts,
                      allowed_names=allowed_names, index_hcl=hcl, index_sql=sql) != "proc"

    allowed_exts, allowed_names, hcl, sql = _source_filter(["c"])
    assert _file_kind("order.pc", ".pc", "src/order.pc", allowed_exts=allowed_exts,
                      allowed_names=allowed_names, index_hcl=hcl, index_sql=sql) == "proc"
