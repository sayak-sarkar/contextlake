# contextlake — tests

*This page covers only the `tests` module/subsystem of `contextlake`, not the repository as a whole.*

---
Overview
This module focuses on integration tests and utilities within the contextlake repository's `tests` sub-system, covering functionalities related to metrics, safety protocols, logging, database queries, and more. Indexed at commit 0f01c748883f8f1d7e497adf6683a9fff4fa2ac2 with a total of 3243 symbols and 6097 relations.

Setup & Run
The module includes entry-point files such as `tests/conftest.py`, which serves as a configuration file for setting up common fixtures. Notable setup files include fuzz tests like `tests/kb/fixtures/fuzz/hcl_repeated_resource_prefix.tf` and others like `tests/kb/fixtures/sql/orders.sql`.

Architecture
No specific architecture details were provided by the facts, but it operates within the scope of test utilities and integration testing for various functionalities.

Dependencies
The module has a variety of language dependencies including Python (2426 symbols), SQL (412), HCL (402), C# (1), Manifest (1), and JavaScript (1). The main files include `tests/conftest.py`, `tests/kb/fixtures/fuzz/hcl_repeated_resource_prefix.tf`, and others like `tests/test_metrics.py` and `tests/kb/fixtures/sql/orders.sql`.

Gotchas
- Method run in tests/conftest.py has 71 callers.
- Function run in tests/test_safety.py has 71 callers.
- Class _Tty in tests/test_logging.py has 42 callers.
- Function run in tests/test_metrics.py has 71 callers.
- Method run in tests/kb/test_kb_server.py has 71 callers.
- Function _kb_config in tests/kb/test_serve_matrix.py and tests/kb/test_kb_doctor.py both have 60 callers.

---
*Generated from the knowledge graph of the `tests` module of `contextlake` at commit `0f01c748883f8f1d7e497adf6683a9fff4fa2ac2` on 2026-08-04. Sources: `tests/conftest.py`, `tests/kb/fixtures/fuzz/hcl_deep_nested_braces.tf`, `tests/kb/fixtures/fuzz/hcl_repeated_resource_prefix.tf`, `tests/kb/fixtures/fuzz/hcl_unbalanced_quotes.tf`, `tests/kb/fixtures/fuzz/http_cs_deep_nested.cs`, `tests/kb/fixtures/fuzz/http_js_unbalanced_quotes.js`, `tests/kb/fixtures/fuzz/manifest_csproj_repeated_prefix.csproj`, `tests/kb/fixtures/fuzz/sql_repeated_prefix.sql`, `tests/kb/fixtures/fuzz/sql_unbalanced_quotes.sql`, `tests/kb/fixtures/sql/addresses.sql`. Grounded in 30/3243 file-backed symbols (0.9%).*