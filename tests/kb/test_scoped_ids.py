"""`scoped_id` and `file_node_id`: exact ids for files and names inside them (D05).

The pinned values are the contract: a change to the hash input changes every stored id, so it
must fail here first and be made on purpose (with a PARSER_VERSION bump).
"""

from __future__ import annotations

import pytest

from contextlake.kb.ids import canonical_path, file_node_id, make_id, scoped_id


def test_pinned_ids():
    assert file_node_id("grp/app", "src/a-b.py") == "grp_app_src_a_b_py_ae369e8e"
    assert file_node_id("grp/app", "src/A_B.py") == "grp_app_src_a_b_py_f363f8ad"
    assert scoped_id("grp/app", "db/schema.sql", "table", "Orders") == \
        "grp_app_db_schema_sql_table_orders_8cd47088"


def test_spellings_that_make_id_folds_are_different_files():
    ids = {file_node_id("grp/app", p) for p in ("src/a-b.py", "src/a_b.py", "src/A_B.py")}
    assert len(ids) == 3
    assert len({make_id("grp/app", p) for p in ("src/a-b.py", "src/a_b.py", "src/A_B.py")}) == 1


def test_repos_that_make_id_folds_share_no_file_id():
    assert file_node_id("grp/a-b", "x.py") != file_node_id("grp/a/b", "x.py")


@pytest.mark.parametrize("spelling", ["src\\a-b.py", "./src/a-b.py", "src//a-b.py",
                                      "/src/a-b.py", "./src\\\\a-b.py"])
def test_one_file_has_one_id_however_its_path_is_spelled(spelling):
    """Windows separators and stray `./` or `//` must not split one file into two nodes."""
    assert file_node_id("grp/app", spelling) == file_node_id("grp/app", "src/a-b.py")


def test_unicode_forms_agree():
    """A path read as NFD (macOS) and as NFC (Linux) is one file."""
    nfc, nfd = "docs/café.md", "docs/café.md"
    assert canonical_path(nfd) == nfc
    assert file_node_id("grp/app", nfd) == file_node_id("grp/app", nfc)


def test_the_prefix_stays_readable():
    nid = scoped_id("grp/app", "db/schema.sql", "table", "Orders")
    assert nid.startswith(make_id("grp/app", "db/schema.sql", "table", "Orders") + "_")


def test_names_inside_a_file_keep_their_case_apart():
    assert scoped_id("r", "s.sql", "table", "Orders") != scoped_id("r", "s.sql", "table", "orders")


# --- package ids (F1) and routes ---------------------------------------------------------

@pytest.mark.parametrize("eco,same,different", [
    ("pypi", ["Acme_Ledger.Client", "acme-ledger-client", "ACME--ledger__client"], []),
    ("nuget", ["Newtonsoft.Json", "newtonsoft.json"], ["Newtonsoft-Json"]),
    ("npm", ["@acme/billing-js"], ["acme-billing-js", "@acme/billing.js", "acme/billing-js"]),
    ("maven", ["org.acme:billing"], ["org.acme:Billing", "org-acme:billing"]),
])
def test_package_names_fold_by_their_ecosystems_rule(eco, same, different):
    from contextlake.kb.manifest import package_id

    ids = {package_id(eco, n) for n in same}
    assert len(ids) == 1, (eco, same)
    for other in different:
        assert package_id(eco, other) not in ids, (eco, other)


def test_routes_differing_in_case_or_punctuation_are_two_routes():
    from contextlake.kb.flow.web import _route_id

    assert _route_id("r", "/orders") != _route_id("r", "/Orders")
    assert _route_id("r", "/a-b") != _route_id("r", "/a_b")
    assert _route_id("r", "/orders/{}") != _route_id("r", "/orders")
