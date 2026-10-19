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
and the PR template give a contributor. The last ones hold the release runbook to the workflows
and to the verifier script it describes, and run two workflow steps in bash: the release.yml
step that creates the GitHub Release and the binaries.yml launcher check.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import textwrap
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


def _ci_gate(name: str) -> list[str]:
    """The shell lines of one workflow's `verify-ci` step, without comments or the refusal.

    The refusal line names what is being refused ("to release", "to build binaries"), so it
    differs on purpose.
    """
    text = (_WORKFLOWS / name).read_text(encoding="utf-8")
    start = text.index("\n  verify-ci:\n")
    nxt = re.search(r"\n  [a-z][a-z0-9-]*:\n", text[start + 1:])
    job = text[start:start + 1 + nxt.start()]
    body = job.split("        run: |\n", 1)[1]
    lines = [ln.strip() for ln in body.splitlines()]
    return [ln for ln in lines if ln and not ln.startswith(("#", 'echo "::error::'))]


def test_both_workflows_open_with_the_same_ci_gate() -> None:
    """`releasing.md` says both open with one gate. `release.yml` asks again when the answer is
    `missing`, because the commit-filtered listing lags a finished run (measured on v7.24.0).
    `binaries.yml` kept a single call and refused the same green commit."""
    release, binaries = _ci_gate("release.yml"), _ci_gate("binaries.yml")
    assert any(ln.startswith("for attempt in") for ln in release), (
        "release.yml no longer retries a `missing` answer, so this test compares nothing")
    assert binaries == release, "binaries.yml and release.yml gate on different shell"


def test_the_runbook_says_how_long_the_ci_gate_waits() -> None:
    gate = " ".join(_ci_gate("release.yml"))
    loop, nap = re.search(r"for attempt in ([\d ]+);", gate), re.search(r"sleep (\d+)", gate)
    assert loop and nap, "release.yml's gate has no retry loop with a pause"
    looks, pause = len(loop.group(1).split()), int(nap.group(1))
    releasing = _flat(REPO / "docs" / "releasing.md")
    assert f"{(looks - 1) * pause} s" in releasing, "releasing.md omits the gate's wait"
    assert "fail immediately" not in releasing


def _releasing() -> str:
    return _flat(REPO / "docs" / "releasing.md")


def test_the_runbook_does_not_call_the_tag_check_offline() -> None:
    """`--tag` reads git, but the script queries the index first, so it was never offline."""
    spec = importlib.util.spec_from_file_location(
        "verify_published_release", REPO / "scripts" / "verify-published-release.py")
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    wait = (verifier._DOWNLOAD_ATTEMPTS - 1) * verifier._DOWNLOAD_PAUSE_S
    releasing = _releasing()
    assert "needs no network" not in releasing
    assert f"retries the wheel download for {wait} s" in releasing


def test_the_runbook_marks_the_manual_publish_step_as_manual() -> None:
    """The tag push in step 4 already publishes. An unqualified `twine upload` after it can
    land first, and `skip-existing` then keeps a wheel the workflow did not build."""
    step = re.search(r"\b6\. \*\*(.*?)\*\*", _releasing())
    assert step, "releasing.md no longer has a numbered step 6"
    assert "by hand" in step.group(1)
    assert "twine upload" in _releasing()


def test_no_page_or_test_names_the_maintainers_workspace() -> None:
    """A reader of the public runbook cannot open a maintainer's home directory or a private
    note. The needles are built from pieces so this file does not match itself."""
    needles = ("Work/" "ContextLake", "measurement-" "baselines")
    files = [*_pages(), *sorted((REPO / "tests").rglob("*.py"))]
    bad = [f"{f.relative_to(REPO)}: {needle}" for f in files
           for needle in needles if needle in f.read_text(encoding="utf-8")]
    assert not bad, "a tracked file names a private path or note:\n  " + "\n  ".join(bad)


# --- the release-create step and the launcher check -----------------------------------
#
# A step with no `shell:` key (release.yml's) runs under `bash -e`. A step with `shell: bash`
# (binaries.yml's) runs under `bash --noprofile --norc -eo pipefail`. These tests run each body
# with the flags its step gets. Both have `-e`, and `-e` decides whether a failing command
# inside `|| { ...; }` fails the job. A text search cannot show that.

_NEEDS_BASH = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _step_script(workflow: str, step: str) -> str:
    """The `run: |` body of the step whose name starts with `step`, dedented."""
    lines = (_WORKFLOWS / workflow).read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.lstrip().startswith(f"- name: {step}"))
    indent = len(lines[start]) - len(lines[start].lstrip())
    i = next(i for i in range(start + 1, len(lines)) if lines[i] == " " * (indent + 2) + "run: |")
    body = []
    for ln in lines[i + 1:]:
        if ln.strip() and len(ln) - len(ln.lstrip()) < indent + 4:
            break
        body.append(ln)
    return textwrap.dedent("\n".join(body)) + "\n"


_BASH_E = ["bash", "-e"]                                      # a step with no `shell:` key
_BASH_PIPEFAIL = ["bash", "--noprofile", "--norc", "-eo", "pipefail"]   # `shell: bash`


def _run_step(script: str, cwd: Path, env: dict[str, str],
              shell: list[str] = _BASH_E) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*shell, "-c", script],
        cwd=cwd, env={**os.environ, **env}, capture_output=True, text=True, timeout=60,
        check=False)


# A `gh` that never leaves the machine. `release view` always says "no release", which is what
# the step saw before the other workflow created it. `release create` then answers 422 when the
# marker file exists, which is the other workflow having won the race, and succeeds otherwise.
_FAKE_GH = """#!/bin/bash
echo "$*" >> "$GH_LOG"
case "$1 $2" in
  "release view") exit 1 ;;
  "release create")
    if [ -e "$OTHER_WORKFLOW_WON" ]; then
      echo "HTTP 422: Validation Failed (tag_name already_exists)" >&2; exit 1
    fi ;;
  "release edit") [ -z "$EDIT_FAILS" ] || { echo "HTTP 500" >&2; exit 1; } ;;
  "release upload") ;;
  *) echo "unexpected gh call: $*" >&2; exit 99 ;;
esac
"""


def _release_env(tmp_path: Path, *, other_workflow_won: bool, edit_fails: bool = False) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(_FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    marker = tmp_path / "other-workflow-won"
    if other_workflow_won:
        marker.touch()
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "GH_LOG": str(tmp_path / "gh.log"),
           "OTHER_WORKFLOW_WON": str(marker), "GITHUB_REF_NAME": "v10.0.0", "PRERELEASE": "false"}
    if edit_fails:
        env["EDIT_FAILS"] = "1"
    (tmp_path / "CHANGELOG.md").write_text("## [10.0.0]\n\n- the notes\n\n## [9.9.9]\n",
                                           encoding="utf-8")
    for name in ("dist/a.whl", "sbom/b.json"):
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_text("x", encoding="utf-8")
    return env


def _gh_calls(tmp_path: Path) -> list[str]:
    return (tmp_path / "gh.log").read_text(encoding="utf-8").splitlines()


@_NEEDS_BASH
def test_release_yml_attaches_the_files_when_binaries_yml_created_the_release_first(
        tmp_path: Path) -> None:
    """`releasing.md` says `binaries.yml` uploads onto the release "whichever finishes first
    creating it". `binaries.yml` falls back to an upload on a 422. `release.yml` did not: it
    exited 1 and attached no wheel, sdist or SBOM, and the notes stayed "See CHANGELOG.md."."""
    script = _step_script("release.yml", "Create GitHub Release from the CHANGELOG")
    done = _run_step(script, tmp_path, _release_env(tmp_path, other_workflow_won=True))
    assert done.returncode == 0, done.stderr
    calls = _gh_calls(tmp_path)
    assert [c.split()[1] for c in calls] == ["view", "create", "edit", "upload"], calls
    assert "--notes-file notes.md" in calls[2]
    assert "--prerelease=false" in calls[2]
    assert "dist/a.whl" in calls[3] and "sbom/b.json" in calls[3] and "--clobber" in calls[3]
    assert "the notes" in (tmp_path / "notes.md").read_text(encoding="utf-8")


@_NEEDS_BASH
def test_release_yml_creates_the_release_once_when_nobody_else_did(tmp_path: Path) -> None:
    script = _step_script("release.yml", "Create GitHub Release from the CHANGELOG")
    done = _run_step(script, tmp_path, _release_env(tmp_path, other_workflow_won=False))
    assert done.returncode == 0, done.stderr
    assert [c.split()[1] for c in _gh_calls(tmp_path)] == ["view", "create"]


@_NEEDS_BASH
def test_a_failed_fallback_fails_the_release_job(tmp_path: Path) -> None:
    """The fallback must not hide a real error: if `gh release edit` fails too, the job is red
    and no upload runs."""
    script = _step_script("release.yml", "Create GitHub Release from the CHANGELOG")
    env = _release_env(tmp_path, other_workflow_won=True, edit_fails=True)
    done = _run_step(script, tmp_path, env)
    assert done.returncode != 0
    assert [c.split()[1] for c in _gh_calls(tmp_path)] == ["view", "create", "edit"]


_SANITY = "Sanity check"
_BINARY = "contextlake-linux-x86_64"


def _run_sanity(tmp_path: Path, content: bytes | None) -> subprocess.CompletedProcess:
    script = _step_script("binaries.yml", _SANITY).replace("${{ matrix.asset_name }}", _BINARY)
    (tmp_path / "out").mkdir()
    if content is not None:
        (tmp_path / "out" / _BINARY).write_bytes(content)
    return _run_step(script, tmp_path, {}, _BASH_PIPEFAIL)


@_NEEDS_BASH
@pytest.mark.parametrize("content,passes", [
    (b"\x7fELF-not-empty", True),
    (b"", False),     # a zero-byte launcher was uploaded to the release
    (None, False),    # no file at all
])
def test_the_binary_sanity_check_fails_on_an_empty_or_missing_launcher(
        tmp_path: Path, content: bytes | None, passes: bool) -> None:
    done = _run_sanity(tmp_path, content)
    assert (done.returncode == 0) is passes, done.stdout + done.stderr


def test_the_binary_sanity_check_names_only_what_it_checks() -> None:
    """The label said "binary exists/executable", but both commands ended in `|| true`, so the
    step could not fail. `docs/installing.md` tells the user to `chmod +x` the download, so the
    mode set here is not something the step promises."""
    text = (_WORKFLOWS / "binaries.yml").read_text(encoding="utf-8")
    label = re.search(r"- name: (Sanity check[^\n]*)", text).group(1)
    assert "executable" not in label
    assert "non-empty" in label


_STALE_WORDS = ("gguf", "only-binary", "separate index", "wheel index", "compile c++",
                "pyapp_pip_extra_args")


@pytest.mark.parametrize("name", ["binaries.yml", "release.yml"])
def test_the_release_workflows_do_not_describe_the_llama_cpp_backend(name: str) -> None:
    """The llm-local extra is an ordinary openvino-genai wheel on PyPI. These comments and a
    step label kept describing the per-accelerator wheel index, the `--only-binary` pin and the
    GGUF model that the backend change removed (security.yml names them as history, on purpose)."""
    text = (_WORKFLOWS / name).read_text(encoding="utf-8").lower()
    assert not [w for w in _STALE_WORDS if w in text], f"{name} still describes the old backend"


def test_dependabot_does_not_list_the_removed_backend_as_an_extra_dependency() -> None:
    text = (REPO / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    assert "llama" not in text.lower()


def test_binaries_yml_says_what_the_launcher_fetches_at_first_run() -> None:
    """Removing the llama-cpp-python clause left "fetches contextlake and its" cut off in the
    middle of the sentence."""
    comments = " ".join(ln.strip().lstrip("#").strip()
                        for ln in (_WORKFLOWS / "binaries.yml").read_text(encoding="utf-8")
                        .splitlines() if ln.strip().startswith("#"))
    assert "fetches contextlake and its dependencies from PyPI" in comments
