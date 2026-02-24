"""A built-in, generic library of agent skills/workflows.

Distilled from a strategize -> plan -> implement -> review -> ship -> learn
lifecycle and anti-hallucination / surgical / memory-first principles, written
generically (no organization specifics). ``steer`` installs them into a workspace
in the formats local AI tools read: Claude Code skills (``.claude/skills``) and
Windsurf workflows (``.windsurf/workflows``).
"""

from __future__ import annotations

from .generate import MARKER

SKILLS = [
    {
        "name": "use-knowledge-graph",
        "desc": "Get grounded context from the local knowledge graph before searching by hand.",
        "body": """\
This workspace ships a knowledge graph reachable over MCP (see `.mcp.json` and
AGENTS.md). Before grepping or guessing:

1. `search_code "<symbol or phrase>"` to locate definitions across all repos.
2. `find_definition` / `find_callers` / `find_dependents` to understand impact.
3. `semantic_search` / `hybrid_search` for natural-language questions (if embeddings
   are enabled).
4. Open the cited files and read them — never describe code you have not read.

Treat the graph as the starting point and the source files as the source of truth.""",
    },
    {
        "name": "investigate-root-cause",
        "desc": "Find the root cause before proposing any fix — no fix without a root cause.",
        "body": """\
1. Reproduce the problem and capture the exact error/output — never work from a summary.
2. Trace it to the specific line, commit, or config that causes it (use the knowledge
   graph to follow callers and dependents).
3. State the root cause in one sentence and how you confirmed it.
4. Only then design the smallest fix that addresses the cause, not the symptom.
5. Add a test that fails before the fix and passes after.""",
    },
    {
        "name": "plan-before-coding",
        "desc": "Write a short plan before any non-trivial change; pin down ambiguity first.",
        "body": """\
1. Restate the goal and the acceptance check in one or two lines.
2. List the files you will touch and why; note anything you are unsure about.
3. If a requirement is genuinely ambiguous, ask ONE focused question before coding.
4. A precise spec collapses many possible implementations into one — get the spec
   right, then write the code.
5. Verify each step against real output; do not assume success.""",
    },
    {
        "name": "surgical-change",
        "desc": "Make the smallest change that does the job; match the code around it.",
        "body": """\
1. Touch only what the task requires — no drive-by refactors or speculative abstractions.
2. Match the surrounding style, naming, and patterns even if you would choose differently.
3. Prefer extending an existing code path over adding a new one.
4. Keep diffs reviewable; one logical change per commit.
5. Never reformat or rewrite unrelated code.""",
    },
    {
        "name": "review-before-landing",
        "desc": "Review like a staff engineer before landing a change.",
        "body": """\
Before opening a PR or marking work done, check:

1. **Correctness** — does it do exactly what was asked, with edge cases handled?
2. **Tests** — is there a test that would fail without the change? Do all tests pass?
3. **Security & data** — no secrets in code or logs; inputs validated; no PII leakage.
4. **Performance** — no obvious N+1, unbounded loops, or blocking calls on hot paths.
5. **Surface** — public APIs, migrations, and config changes are intentional and documented.""",
    },
    {
        "name": "ship-safely",
        "desc": "Land work through a deliberate flow; protect shared and in-progress branches.",
        "body": """\
1. Sync the base branch and run the full test suite locally first.
2. Update the changelog / version if the project tracks them.
3. Work on a feature branch and open a PR — do not push directly to a protected branch.
4. Never force-push a shared branch; never discard someone else's uncommitted work.
5. After landing, record what changed and why for the next person.""",
    },
]


def _title(name: str) -> str:
    return name.replace("-", " ").capitalize()


def skill_md(skill: dict) -> str:
    """Claude Code skill format: YAML frontmatter + instructions."""
    return (
        f"---\nname: {skill['name']}\ndescription: {skill['desc']}\n---\n\n"
        f"{MARKER}\n\n# {_title(skill['name'])}\n\n{skill['body']}\n"
    )


def workflow_md(skill: dict) -> str:
    """Windsurf workflow format: description frontmatter + steps."""
    return (
        f"---\ndescription: {skill['desc']}\n---\n\n"
        f"{MARKER}\n\n# {_title(skill['name'])}\n\n{skill['body']}\n"
    )


def skill_files() -> dict:
    """Map of relative path -> content for the whole skills library."""
    files = {}
    for skill in SKILLS:
        files[f".claude/skills/{skill['name']}/SKILL.md"] = skill_md(skill)
        files[f".windsurf/workflows/{skill['name']}.md"] = workflow_md(skill)
    return files
