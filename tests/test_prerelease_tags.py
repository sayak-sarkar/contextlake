"""A pre-release tag must not move `latest` (10.0.0rc1, 2026-10-15).

`release.yml` pushed the container image with the raw tags `latest`, `latest-slim` and `slim`
on every tag, and both release workflows created a normal GitHub Release. An rc tag would have
replaced the last full release on ghcr.io and on the repository's release page.

Both workflows decide with one shell test: a tag of three numbers is a full release, and any
other tag is a pre-release. The workflows run only on a tag push, so these checks read the files.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
FULL_RELEASE = r"^[0-9]+\.[0-9]+\.[0-9]+$"
SHELL_TEST = f"[[ $v =~ {FULL_RELEASE} ]]"


def _text(name: str) -> str:
    # One logical line per shell command: a `gh release create` continues over `\` lines.
    return re.sub(r"\\\n\s*", " ", (WORKFLOWS / name).read_text(encoding="utf-8"))


def _job(text: str, name: str) -> str:
    start = text.index(f"\n  {name}:\n")
    nxt = re.search(r"\n  [a-z][a-z0-9-]*:\n", text[start + 1:])
    return text[start:start + 1 + nxt.start()] if nxt else text[start:]


@pytest.mark.parametrize("name", ["release.yml", "binaries.yml"])
def test_both_workflows_use_the_same_shell_test(name):
    assert SHELL_TEST in _text(name)


@pytest.mark.parametrize("version,prerelease", [
    ("10.0.0", False), ("9.8.3", False), ("10.0.0rc1", True), ("10.0.0a1", True),
    ("10.0.0.dev1", True), ("10.0.0.post1", True), ("10.0", True), ("10.0.0\nx", True),
])
def test_only_three_numbers_is_a_full_release(version, prerelease):
    assert (re.fullmatch(FULL_RELEASE[1:-1], version) is None) is prerelease


@pytest.mark.parametrize("name", ["release.yml", "binaries.yml"])
def test_every_github_release_call_passes_the_prerelease_flag(name):
    calls = [ln.strip() for ln in _text(name).splitlines()
             if re.search(r"\bgh release (create|edit)\b", ln)]
    assert calls, f"{name} creates no GitHub Release"
    for call in calls:
        assert "--prerelease=" in call, f"{name}: {call}"


def test_a_prerelease_publishes_no_container_image():
    text = _text("release.yml")
    assert "prerelease: ${{ steps.kind.outputs.prerelease }}" in _job(text, "build")
    assert "if: needs.build.outputs.prerelease == 'false'" in _job(text, "docker")
