"""Tier E fixture: four small git repos in three code languages plus SQL, ADRs and manifests.

Why it exists: v1's oracles ran on one Python repo with no SQL, ADR or manifest, so the
wrong-output defects W01 (SQL `IF NOT EXISTS`), D05 (file ids that normalise the same) and
W03 (MCP fields the instructions promise) could not show. Every file here exists to make one
named oracle able to fail. `EXPECTED` holds the values the oracles compare against.

Portability (tier F, Windows):

- `build(dest, portable=...)` writes files with `pathlib` only: no symlinks, no shell, no
  POSIX-only calls. Bytes are written as-is, so line numbers match on every OS.
- `--portable` drops the one pair that cannot exist on a case-insensitive file system
  (`Helpers.ts` and `helpers.ts`). No file uses a Windows reserved stem.
- `git_init(dest)` is separate. It calls `git` with an argv list (no shell) and fixed
  author and commit dates, so commit SHAs are the same on every run. It needs a `git` on PATH.
- Expected paths are posix strings. A backslash in a node's `file` on Windows is then a W02
  hit, not a harness bug.

This module must not import `runner`: the runner needs symlinks and a POSIX PATH.

Usage::

    python fixture_e.py DEST [--portable] [--no-git]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

#: Recorded in every finding as "the seed". The fixture has no randomness; the dates fix
#: the commit SHAs, and the version names the file set.
FIXTURE_VERSION = "e-1"
GIT_DATE = "2026-01-15T12:00:00+00:00"
GIT_AUTHOR = ("Fixture Author", "fixture@example.test")
REMOTE_HOST = "git.example.test"

# --------------------------------------------------------------------------------------
# Repo A: acme/billing-core. Python, SQL in five dialect shapes, nested ADRs, two manifests.
# --------------------------------------------------------------------------------------
BILLING = {
    "README.md": "# Billing core\n\nIssues invoices and computes tax for customer orders.\n",
    "pyproject.toml": (
        '[project]\n'
        'name = "acme-billing-core"\n'
        'version = "1.4.0"\n'
        'dependencies = [\n'
        '    "acme-ledger-client>=0.3",\n'
        '    "requests>=2.31",\n'
        ']\n'
    ),
    "src/billing/__init__.py": '"""Billing package."""\n',
    # invoice.py: the cross-file caller (O5), two call sites from one caller (find_callers
    # `note`), and SQL written in application code (reads `orders`, writes `audit_log`).
    "src/billing/invoice.py": (
        '"""Invoices for customer orders."""\n'
        "from .tax import compute_tax\n"
        "from .utils import format_money\n"
        "\n"
        'ORDERS_SQL = "SELECT id, total FROM orders WHERE id = %s"\n'
        "\n"
        "\n"
        "class InvoiceService:\n"
        '    """Builds and issues invoices for customer orders."""\n'
        "\n"
        "    def issue_invoice(self, order_id, amount):\n"
        "        tax = compute_tax(amount)\n"
        "        self.record_audit(order_id)\n"
        "        return format_money(amount + tax)\n"
        "\n"
        "    def reissue_invoice(self, order_id, amount):\n"
        "        first = compute_tax(amount)\n"
        "        second = compute_tax(amount * 2)\n"
        "        return first + second\n"
        "\n"
        "    def record_audit(self, order_id):\n"
        '        return "INSERT INTO audit_log (order_id) VALUES (%s)"\n'
    ),
    "src/billing/tax.py": (
        '"""Tax rules."""\n'
        "\n"
        "\n"
        "def compute_tax(amount):\n"
        '    """Return the sales tax owed on an amount."""\n'
        "    return round(amount * 0.2, 2)\n"
    ),
    # D05 pair: `_utils.py` and `utils.py` normalise to one file id. Both hold a definition,
    # so `kb lint` (which needs `contains` edges) has the evidence it documents needing.
    "src/billing/utils.py": "def format_money(value):\n    return f\"{value:.2f}\"\n",
    "src/billing/_utils.py": "def parse_money(text):\n    return float(text)\n",
    # W01, the bare shape: the only row the W01 oracle reads.
    "db/01_if_not_exists.sql": (
        "CREATE TABLE IF NOT EXISTS orders (\n"
        "    id BIGINT PRIMARY KEY,\n"
        "    customer_id BIGINT,\n"
        "    total NUMERIC(12, 2)\n"
        ");\n"
    ),
    # PostgreSQL quoted, schema-qualified name.
    "db/02_pg_quoted.sql": (
        'CREATE TABLE "public"."customers" (\n'
        "    id BIGINT PRIMARY KEY,\n"
        "    name TEXT\n"
        ");\n"
    ),
    # MySQL backticks, with a foreign key into the IF NOT EXISTS table.
    "db/03_mysql_backtick.sql": (
        "CREATE TABLE `shipments` (\n"
        "    id BIGINT PRIMARY KEY,\n"
        "    order_id BIGINT,\n"
        "    FOREIGN KEY (order_id) REFERENCES `orders` (id)\n"
        ");\n"
    ),
    # T-SQL brackets and schema, with a GO batch separator.
    "db/04_tsql_bracket.sql": (
        "CREATE TABLE [dbo].[refunds] (\n"
        "    id INT PRIMARY KEY,\n"
        "    order_id INT REFERENCES [dbo].[orders](id)\n"
        ");\n"
        "GO\n"
    ),
    # Views: plain, and CREATE OR REPLACE.
    "db/05_views.sql": (
        "CREATE VIEW order_totals AS\n"
        "    SELECT customer_id, SUM(total) AS total FROM orders GROUP BY customer_id;\n"
        "\n"
        "CREATE OR REPLACE VIEW recent_orders AS\n"
        "    SELECT id, total FROM orders WHERE id > 100;\n"
    ),
    # A plain table whose foreign key targets the IF NOT EXISTS table.
    "db/06_audit.sql": (
        "CREATE TABLE audit_log (\n"
        "    id BIGINT PRIMARY KEY,\n"
        "    order_id BIGINT REFERENCES orders(id)\n"
        ");\n"
    ),
    # IF NOT EXISTS combined with a quoted, schema-qualified name.
    "db/07_if_not_exists_quoted.sql": (
        'CREATE TABLE IF NOT EXISTS "billing"."invoices" (\n'
        "    id BIGINT PRIMARY KEY,\n"
        "    order_id BIGINT\n"
        ");\n"
    ),
    # D05 pair in SQL: two files whose paths normalise the same, each with its own table.
    "db/legacy-ledger.sql": "CREATE TABLE ledger_entries (\n    id BIGINT PRIMARY KEY\n);\n",
    "db/legacy_ledger.sql": "CREATE TABLE ledger_accounts (\n    id BIGINT PRIMARY KEY\n);\n",
    # Nested ADRs at two depths.
    "docs/adr/0001-use-postgres.md": (
        "# ADR 0001: Use PostgreSQL for billing data\n\n"
        "Status: accepted.\n\nOrders, refunds and invoices live in one PostgreSQL schema.\n"
    ),
    "docs/adr/archive/0002-drop-mongodb.md": (
        "# ADR 0002: Drop MongoDB\n\nStatus: superseded.\n\nThe document store was retired.\n"
    ),
    # A nested manifest: the repo also publishes an npm client.
    "clients/js/package.json": json.dumps(
        {"name": "@acme/billing-js", "version": "1.0.0",
         "dependencies": {"left-pad": "^1.3.0"}}, indent=2) + "\n",
    "clients/js/index.js": "export function billingUrl(id) {\n  return `/billing/${id}`;\n}\n",
}

# --------------------------------------------------------------------------------------
# Repo B: acme/web-app. A TypeScript and JavaScript monorepo with nested manifests.
# --------------------------------------------------------------------------------------
WEBAPP = {
    "README.md": "# Web app\n\nThe customer-facing order screens.\n",
    "package.json": json.dumps(
        {"name": "@acme/web-root", "private": True, "workspaces": ["packages/*"],
         "devDependencies": {"typescript": "^5.4.0"}}, indent=2) + "\n",
    "packages/api-client/package.json": json.dumps(
        {"name": "@acme/api-client", "version": "2.0.0",
         "dependencies": {"@acme/billing-js": "^1.0.0"}}, indent=2) + "\n",
    "packages/api-client/src/client.ts": (
        "export class ApiClient {\n"
        "  constructor(private base: string) {}\n"
        "\n"
        "  fetchOrders(): string {\n"
        '    return this.base + "/orders";\n'
        "  }\n"
        "}\n"
        "\n"
        "export function createClient(base: string): ApiClient {\n"
        "  return new ApiClient(base);\n"
        "}\n"
    ),
    "packages/api-client/src/sessions.ts": (
        'const END_SESSION_SQL = "DELETE FROM sessions WHERE id = $1";\n'
        "\n"
        "export function endSession(id: string): string {\n"
        "  return END_SESSION_SQL + id;\n"
        "}\n"
    ),
    "packages/ui/package.json": json.dumps(
        {"name": "@acme/ui", "version": "0.5.0",
         "dependencies": {"@acme/api-client": "^2.0.0", "react": "^18.2.0"}}, indent=2) + "\n",
    # JavaScript calling a TypeScript function in another package: a cross-file caller.
    "packages/ui/src/app.js": (
        'import { createClient } from "../../api-client/src/client";\n'
        "\n"
        "export function renderOrders(base) {\n"
        "  const client = createClient(base);\n"
        "  return client.fetchOrders();\n"
        "}\n"
    ),
    # D05 pair that differs by case AND by `-`/`_`. Both names can exist on Windows.
    "packages/ui/src/My-File.ts": "export function myFileWidget(): number {\n  return 1;\n}\n",
    "packages/ui/src/my_file.ts": "export function myFileHelper(): number {\n  return 2;\n}\n",
    # JSDoc capture control: one exported and one module-private function, same file, same
    # comment shape. Isolates whether `export` hides the leading doc comment.
    "packages/api-client/src/jsdoc_control.ts": (
        "/** Seal a batch with its checksum. */\n"
        "export function zk7(p: number): number {\n  return p;\n}\n"
        "\n"
        "/** Open a sealed batch for audit. */\n"
        "function zk8(p: number): number {\n  return p;\n}\n"
    ),
    # Nested ADRs inside a monorepo package, one with no H1 (title from the file name).
    "packages/ui/docs/adr/0001-use-react.md": (
        "# Use React for the UI\n\nStatus: accepted.\n"
    ),
    "packages/ui/docs/adr/proposals/0002-adopt-signals.md": (
        "Status: proposed.\n\nAdopt signals for client state.\n"
    ),
    "db/migrations/001_sessions.sql": (
        "CREATE TABLE IF NOT EXISTS sessions (\n"
        "    id TEXT PRIMARY KEY,\n"
        "    user_id BIGINT\n"
        ");\n"
    ),
}
#: The pair that differs only by case. Cannot coexist on a case-insensitive file system.
WEBAPP_CASE_ONLY = {
    "packages/ui/src/Helpers.ts": "export function upperHelper(): number {\n  return 3;\n}\n",
    "packages/ui/src/helpers.ts": "export function lowerHelper(): number {\n  return 4;\n}\n",
}

# --------------------------------------------------------------------------------------
# Repo C: acme/ledger. Go, a go.mod, a nested Python client manifest, an npm tools manifest.
# --------------------------------------------------------------------------------------
LEDGER = {
    "README.md": "# Ledger\n\nDouble-entry postings.\n",
    # go.mod is not a manifest contextlake parses (kb/manifest.py lists none). It is here so
    # the fixture holds one; no oracle expects a node from it. Its one requirement is OFF the
    # fleet, so ledger declares no dependency on any fixture repo anywhere: an edge from ledger
    # to billing-core can only be invented.
    "go.mod": (
        "module git.example.test/acme/ledger\n\n"
        "go 1.22\n\n"
        "require golang.org/x/text v0.14.0\n"
    ),
    "ledger/ledger.go": (
        "package ledger\n"
        "\n"
        "// PostEntry records one double-entry posting.\n"
        "func PostEntry(account string, amount int64) error {\n"
        "\treturn nil\n"
        "}\n"
    ),
    "ledger/api.go": (
        "package ledger\n"
        "\n"
        "func HandlePost(account string) error {\n"
        "\treturn PostEntry(account, 100)\n"
        "}\n"
    ),
    # PyPI spelling with underscores. billing-core depends on "acme-ledger-client"; PEP 503
    # makes those one package, so this cross-repo link is correct.
    "clients/python/pyproject.toml": (
        '[project]\nname = "acme_ledger_client"\nversion = "0.3.1"\ndependencies = []\n'
    ),
    "clients/python/ledger_client.py": "def post(account):\n    return account\n",
    # npm: the UNSCOPED package `acme-billing-js` is a different package from the scoped
    # `@acme/billing-js` that billing-core publishes. Expected: no ledger -> billing-core link.
    "tools/package.json": json.dumps(
        {"name": "@acme/ledger-tools", "private": True,
         "dependencies": {"acme-billing-js": "^0.1.0"}}, indent=2) + "\n",
    "decisions/0001-use-go.md": "# Use Go for the ledger service\n\nStatus: accepted.\n",
}

# --------------------------------------------------------------------------------------
# Repo D: acme/billing_core. Its repo id normalises the same as acme/billing-core.
# --------------------------------------------------------------------------------------
BILLING_FORK = {
    "README.md": "# Billing core (legacy fork)\n",
    "src/billing/tax.py": "def legacy_tax(amount):\n    return amount * 0.18\n",
}

# --------------------------------------------------------------------------------------
# O3 (docstring rank delta). Each target's name, parameters AND file path share no token
# with its query, so only the docstring can move it (the embedded text includes the path).
# `build(docstrings=False)` writes the second column instead; the oracle compares the
# target's semantic rank between the two stores.
# --------------------------------------------------------------------------------------
DOC_TARGETS = {
    ("billing-core", "src/misc/q7.py"): (
        'def zq7(p, q):\n    """Refund a payment to the original card."""\n    return p - q\n',
        "def zq7(p, q):\n    return p - q\n",
    ),
    ("web-app", "packages/api-client/src/q9.ts"): (
        "/** Reverse a payout back to the source wallet. */\n"
        "export function zk9(p: number, q: number): number {\n  return p - q;\n}\n",
        "export function zk9(p: number, q: number): number {\n  return p - q;\n}\n",
    ),
}
#: (query, target name, target file, repo dir)
DOC_QUERIES = [
    ("refund a payment to the original card", "zq7", "src/misc/q7.py", "billing-core"),
    ("reverse a payout back to the source wallet", "zk9", "packages/api-client/src/q9.ts",
     "web-app"),
]

REPOS = {
    # dir name -> (remote path, files)
    "billing-core": ("acme/billing-core", BILLING),
    "web-app": ("acme/web-app", WEBAPP),
    "ledger": ("acme/ledger", LEDGER),
    "billing_core": ("acme/billing_core", BILLING_FORK),
}


def repo_id(dirname: str) -> str:
    """The id contextlake derives from the origin remote: ``host/path``, lowercased."""
    return f"{REMOTE_HOST}/{REPOS[dirname][0]}".lower()


def files_for(dirname: str, portable: bool, docstrings: bool = True) -> dict[str, str]:
    files = dict(REPOS[dirname][1])
    if dirname == "web-app" and not portable:
        files.update(WEBAPP_CASE_ONLY)
    for (d, rel), (with_doc, without_doc) in DOC_TARGETS.items():
        if d == dirname:
            files[rel] = with_doc if docstrings else without_doc
    return files


def build(dest: Path, portable: bool = False, docstrings: bool = True) -> dict:
    """Write the four repos under ``dest`` (one directory each). Returns ``expected(portable)``.

    pathlib only. ``dest`` must not exist yet or must be empty: a rebuild never merges.
    ``docstrings=False`` writes the O3 baseline: the same files minus the two docstrings.
    """
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise FileExistsError(f"fixture destination is not empty: {dest}")
    for dirname in REPOS:
        for rel, text in files_for(dirname, portable, docstrings).items():
            p = dest / dirname / Path(*rel.split("/"))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(text.encode("utf-8"))
    return expected(portable)


def git_init(dest: Path, git: str = "git", timeout: float = 60.0) -> dict[str, str]:
    """Make each repo a git repo with one commit and an origin remote. Not pathlib-only.

    argv lists, no shell. The env is built from nothing plus the fixed author and dates,
    so the commit SHAs depend on the file bytes alone. Returns ``{dirname: head sha}``.
    """
    env = {
        "GIT_AUTHOR_NAME": GIT_AUTHOR[0], "GIT_AUTHOR_EMAIL": GIT_AUTHOR[1],
        "GIT_COMMITTER_NAME": GIT_AUTHOR[0], "GIT_COMMITTER_EMAIL": GIT_AUTHOR[1],
        "GIT_AUTHOR_DATE": GIT_DATE, "GIT_COMMITTER_DATE": GIT_DATE,
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": str(Path(dest).resolve()),
        "PATH": os.environ.get("PATH", ""),
    }
    if os.name == "nt":  # git for Windows needs these to start
        for k in ("SYSTEMROOT", "COMSPEC", "PATHEXT"):
            if k in os.environ:
                env[k] = os.environ[k]
    heads = {}
    for dirname, (remote, _files) in REPOS.items():
        d = str(Path(dest) / dirname)
        for argv in (["init", "-q", "-b", "main"],
                     # Git for Windows turns autocrlf on system-wide. Off here, plus
                     # eol=lf, so the files commit and check out as the LF bytes written.
                     ["config", "core.autocrlf", "false"],
                     ["config", "core.eol", "lf"],
                     ["config", "core.ignorecase", "false"],
                     ["remote", "add", "origin", f"https://{REMOTE_HOST}/{remote}.git"],
                     ["add", "-A"],
                     ["commit", "-q", "-m", "fixture"]):
            subprocess.run([git, "-C", d, *argv], env=env, check=True, timeout=timeout,
                           capture_output=True)
        out = subprocess.run([git, "-C", d, "rev-parse", "HEAD"], env=env, check=True,
                             timeout=timeout, capture_output=True, text=True)
        heads[dirname] = out.stdout.strip()
    return heads


def expected(portable: bool = False) -> dict:
    """Named expected values, one block per oracle family. Paths are posix."""
    bc, wa, lg, bf = (repo_id(d) for d in ("billing-core", "web-app", "ledger", "billing_core"))
    collisions = [
        # (label, repo, path a, path b, search token) - one file node expected per path
        ("py-underscore-prefix", bc, "src/billing/utils.py", "src/billing/_utils.py", "utils"),
        ("sql-dash-underscore", bc, "db/legacy-ledger.sql", "db/legacy_ledger.sql", "ledger"),
        ("ts-case-and-dash", wa, "packages/ui/src/My-File.ts", "packages/ui/src/my_file.ts",
         "file"),
    ]
    if not portable:
        collisions.append(("ts-case-only", wa, "packages/ui/src/Helpers.ts",
                           "packages/ui/src/helpers.ts", "helpers"))
    return {
        "fixture_version": FIXTURE_VERSION,
        "git_date": GIT_DATE,
        "portable": portable,
        "repos": {"billing-core": bc, "web-app": wa, "ledger": lg, "billing_core": bf},
        "repo_count": 4,
        # W01 and the dialect rows. One row per SQL file: (file, kind, expected name).
        "sql_defs": [
            ("db/01_if_not_exists.sql", "table", "orders"),
            ("db/02_pg_quoted.sql", "table", "customers"),
            ("db/03_mysql_backtick.sql", "table", "shipments"),
            ("db/04_tsql_bracket.sql", "table", "refunds"),
            ("db/05_views.sql", "view", "order_totals"),
            ("db/05_views.sql", "view", "recent_orders"),
            ("db/06_audit.sql", "table", "audit_log"),
            ("db/07_if_not_exists_quoted.sql", "table", "invoices"),
        ],
        "sql_keyword_names": ["if", "not", "exists", "public", "dbo", "billing"],
        # Foreign keys into `orders` (src table -> dst table), in billing-core.
        "sql_fks": [("audit_log", "orders"), ("shipments", "orders"), ("refunds", "orders")],
        # Application code -> table, inside one repo (resolution is repo-local by design).
        "data_reads": [(bc, "src/billing/invoice.py", "orders")],
        "data_writes": [(bc, "src/billing/invoice.py", "audit_log"),
                        (wa, "packages/api-client/src/sessions.ts", "sessions")],
        "webapp_tables": [("db/migrations/001_sessions.sql", "table", "sessions")],
        # D05: each path must own its own file node in its own repo.
        "collisions": collisions,
        "cross_repo_collision": ("tax", "src/billing/tax.py", bc, bf),
        # ADR nodes: (repo, file, title, a search token from the title)
        "adrs": [
            (bc, "docs/adr/0001-use-postgres.md", "ADR 0001: Use PostgreSQL for billing data",
             "PostgreSQL"),
            (bc, "docs/adr/archive/0002-drop-mongodb.md", "ADR 0002: Drop MongoDB", "MongoDB"),
            (wa, "packages/ui/docs/adr/0001-use-react.md", "Use React for the UI", "React"),
            (wa, "packages/ui/docs/adr/proposals/0002-adopt-signals.md", "Adopt signals",
             "signals"),
            (lg, "decisions/0001-use-go.md", "Use Go for the ledger service", "ledger"),
        ],
        # Manifests: (repo, manifest file, published package or None, [runtime deps])
        "manifests": [
            (bc, "pyproject.toml", "acme-billing-core", ["acme-ledger-client", "requests"]),
            (bc, "clients/js/package.json", "@acme/billing-js", ["left-pad"]),
            (wa, "package.json", "@acme/web-root", []),
            (wa, "packages/api-client/package.json", "@acme/api-client", ["@acme/billing-js"]),
            (wa, "packages/ui/package.json", "@acme/ui", ["@acme/api-client", "react"]),
            (lg, "clients/python/pyproject.toml", "acme_ledger_client", []),
            (lg, "tools/package.json", "@acme/ledger-tools", ["acme-billing-js"]),
        ],
        # Repo -> repo package dependencies (out direction). PEP 503 makes the PyPI link real.
        "repo_deps_out": {bc: {lg}, wa: {bc}, lg: set(), bf: set()},
        # Cross-file callers: (callee, caller file, {caller names}, repo)
        "callers": [
            ("compute_tax", "src/billing/invoice.py", {"issue_invoice", "reissue_invoice"}, bc),
            ("createClient", "packages/ui/src/app.js", {"renderOrders"}, wa),
            ("PostEntry", "ledger/api.go", {"HandlePost"}, lg),
        ],
        # compute_tax is called from 3 lines by 2 callers: find_callers must say so in `note`.
        "compute_tax_sites": 3,
        "compute_tax_distinct_callers": 2,
        # Leading doc comments that must reach the node's `doc`: (name, file, doc)
        "jsdoc": [
            ("zk7", "packages/api-client/src/jsdoc_control.ts", "Seal a batch with its checksum."),
            ("zk8", "packages/api-client/src/jsdoc_control.ts", "Open a sealed batch for audit."),
        ],
        # Top hit per language for a name query: (query, file, kind)
        "top_hits": [
            ("InvoiceService", "src/billing/invoice.py", "class"),
            ("ApiClient", "packages/api-client/src/client.ts", "class"),
            ("renderOrders", "packages/ui/src/app.js", "function"),
            ("PostEntry", "ledger/ledger.go", "function"),
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dest")
    ap.add_argument("--portable", action="store_true",
                    help="skip files that cannot exist on Windows (names differing only by case)")
    ap.add_argument("--no-git", action="store_true", help="write files only, no git init")
    a = ap.parse_args()
    exp = build(Path(a.dest), portable=a.portable)
    if not a.no_git:
        exp["heads"] = git_init(Path(a.dest))
    print(json.dumps({k: v for k, v in exp.items() if k in ("fixture_version", "portable",
                                                             "repos", "heads")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
