"""What the install docs say about attestations must match what the project publishes.

`docs/installing.md` said the wheel and sdist on PyPI carry PEP 740 attestations "which pip
verifies". pip has no attestation verifier and uv has no install-time option for one (pip 26.1.2
and uv 0.12.3 were read), so a reader believed an install had checked who built the wheel. PyPI
does serve the attestation. Nothing at install time reads it.

These checks pin the parts that live in this repository:

- no page says pip or uv verifies or checks an attestation;
- the repository named in the user-facing verify command is the one the release workflow
  publishes from, and the workflow file the docs name exists and publishes through Trusted
  Publishing.

Later checks hold other repository text to the workflows it describes: SECURITY.md's list of
extras no audit job covers, and the lint paths and kb install that the Makefile, CONTRIBUTING.md
and the PR template give a contributor.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _pages() -> list[Path]:
    return [*sorted((REPO / "docs").rglob("*.md")), REPO / "README.md", REPO / "SECURITY.md"]


def _flat(path: Path) -> str:
    return re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))


_PIP_OR_UV_CHECKS = re.compile(
    r"(?:attestations?|PEP 740)[^.]{0,120}\b(?:pip|uv) (?:verif|check|validat)"
    r"|\b(?:pip|uv) (?:verif|check|validat)\w* "
    r"(?:the |an? |these |its )?(?:PEP 740 |PyPI )?attestation",
    re.I)


def test_no_page_says_pip_or_uv_verifies_an_attestation() -> None:
    bad = []
    for page in _pages():
        for sentence in re.split(r"(?<=[.!?]) ", _flat(page)):
            if _PIP_OR_UV_CHECKS.search(sentence) and not re.search(
                    r"\b(?:do|does|did) not\b|\bnot\b.{0,20}\b(?:check|verif)", sentence, re.I):
                bad.append(f"{page.relative_to(REPO)}: {sentence[:160]}")
    assert not bad, "a page says an installer verifies an attestation:\n  " + "\n  ".join(bad)


def test_the_scanner_catches_the_original_sentence() -> None:
    original = ("the wheel and sdist on PyPI carry their own PEP 740 attestations, "
                "which pip verifies, and each release also publishes a CycloneDX SBOM")
    assert _PIP_OR_UV_CHECKS.search(original)
    assert not re.search(r"\b(?:do|does|did) not\b", original)


def test_the_verify_command_names_the_repository_that_publishes() -> None:
    installing = _flat(REPO / "docs" / "installing.md")
    m = re.search(
        r"pypi-attestations verify pypi --repository https://github\.com/([\w.-]+)/([\w.-]+) ",
        installing)
    assert m, "docs/installing.md no longer shows a `pypi-attestations verify pypi` command"
    owner, repo = m.groups()

    releasing = _flat(REPO / "docs" / "releasing.md")
    assert f"**Owner:** `{owner}`" in releasing, (
        "installing.md and releasing.md name different owners")
    assert f"**Repository name:** `{repo}`" in releasing, (
        "installing.md and releasing.md name different repositories")

    workflow = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "pypa/gh-action-pypi-publish" in workflow, (
        "release.yml no longer publishes through the PyPI action")
    # The attestation is only made for a Trusted Publisher. A token upload would carry none.
    assert re.search(r"^\s*id-token: write", workflow, re.M)
    assert "attestations: false" not in workflow, "release.yml switches attestations off"


@pytest.mark.parametrize("workflow", ["release.yml", "binaries.yml"])
def test_every_workflow_the_docs_name_exists(workflow: str) -> None:
    assert (REPO / ".github" / "workflows" / workflow).is_file()


def _extras() -> dict[str, str]:
    """Each extra in pyproject.toml, with its list body (comment lines removed)."""
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    section = text.split("[project.optional-dependencies]", 1)[1].split("\n[", 1)[0]
    section = re.sub(r"(?m)^\s*#.*$", "", section)
    return dict(re.findall(r"(?ms)^([a-z][a-z0-9-]*) = \[(.*?)\]$", section))


def _unaudited_extras() -> set[str]:
    """Extras that no `pip install '.[...]'` line in security.yml reaches.

    An extra made only of `contextlake[other]` references is covered when each one is.
    """
    workflow = (REPO / ".github" / "workflows" / "security.yml").read_text(encoding="utf-8")
    listed = {name for group in re.findall(r"\.\[([a-z0-9,-]+)\]", workflow)
              for name in group.split(",")}
    extras = _extras()

    def covered(name: str) -> bool:
        if name in listed:
            return True
        refs = re.findall(r"contextlake\[([a-z0-9-]+)\]", extras[name])
        own = [dep for dep in re.findall(r'"([^"]+)"', extras[name])
               if not dep.startswith("contextlake[")]
        return bool(refs) and not own and all(covered(ref) for ref in refs)

    return {name for name in extras if not covered(name)}


def test_the_extras_parser_sees_the_extras() -> None:
    # A parse that returns nothing makes the audit-coverage test below compare empty sets.
    extras = _extras()
    assert {"dev", "kb", "kb-full", "kb-video", "llm-local"} <= set(extras)
    assert "contextlake[kb-ocr]" in extras["kb-video"]


def test_security_md_names_exactly_the_extras_no_audit_job_covers() -> None:
    """SECURITY.md said pip-audit covered "every shipped extra". kb-ocr, kb-video,
    kb-transcribe and kb-dockerfile were in no audit job, and a CVE in them turned nothing red."""
    security = _flat(REPO / "SECURITY.md")
    m = re.search(r"Not audited by any job: ((?:`[a-z0-9-]+`(?:, )?)+)\.", security)
    assert m, "SECURITY.md has no `Not audited by any job: ...` line"
    named = set(re.findall(r"`([a-z0-9-]+)`", m.group(1)))
    assert named == _unaudited_extras(), (
        "SECURITY.md and security.yml disagree about which extras are audited")
    assert "every shipped extra" not in security


_WORKFLOWS = REPO / ".github" / "workflows"


def _ci_lint_paths() -> str:
    """The path list after `ruff check` in the workflow steps that lint (ci.yml, release.yml)."""
    found = {m.strip() for name in ("ci.yml", "release.yml")
             for m in re.findall(r"ruff check (src[^\n]*)",
                                 (_WORKFLOWS / name).read_text(encoding="utf-8"))}
    assert len(found) == 1, f"ci.yml and release.yml lint different paths: {sorted(found)}"
    return found.pop()


def test_the_makefile_lints_what_ci_lints() -> None:
    """`make lint` ran `src tests`; CI also lints `scripts` and `benchmarks`, so a finding there
    passed locally and went red in every CI lint step."""
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    assert f"LINT_PATHS := {_ci_lint_paths()}\n" in makefile
    assert "$(RUFF) check $(LINT_PATHS)\n" in makefile
    assert "$(RUFF) check --fix $(LINT_PATHS)\n" in makefile


@pytest.mark.parametrize("page,must_show", [
    ("CONTRIBUTING.md", True),
    (".github/pull_request_template.md", True),
    ("docs/adding-a-language.md", False),
    (".pre-commit-config.yaml", False),
    ("pyproject.toml", False),
])
def test_every_lint_command_a_contributor_is_shown_is_the_one_ci_runs(
        page: str, must_show: bool) -> None:
    shown = [m.strip() for m in re.findall(
        r"ruff check (?:--fix )?(src tests[^\n`#]*)", (REPO / page).read_text(encoding="utf-8"))]
    assert shown or not must_show, f"{page} no longer shows the lint command"
    assert all(paths == _ci_lint_paths() for paths in shown), (
        f"{page} shows a lint command narrower than CI's: {shown}")


def test_make_install_ci_is_the_kb_job_install() -> None:
    """The kb job also installs kb-dockerfile and kb-vec. Without them the tests that need them
    skip, so a local green was not the green CI gives."""
    ci = (_WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    kb_job = re.search(r"\n  kb:\n(.*?)\n  [a-z-]+:\n", ci, re.S).group(1)
    in_ci = re.findall(r'pip install -e "(\.\[[^\]]+\])"', kb_job)
    assert len(in_ci) == 1, "ci.yml's kb job no longer has one `pip install -e` line"
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    recipe = re.search(r"\ninstall-ci:[^\n]*\n\t(.*)\n", makefile).group(1)
    assert recipe == f'$(PIP) install -e "{in_ci[0]}"'
