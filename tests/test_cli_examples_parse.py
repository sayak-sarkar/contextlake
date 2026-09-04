"""Every example the CLI advertises in a `--help` epilog must actually run.

Found by the stability campaign, 2026-08-25: `contextlake kb graph --serve` was listed as an
example and exited 2 with a usage banner, because `kb graph` requires one of
`--node/--name/--search/--repo/--overview` and `--serve` is not one of them. That check is
correct and deliberate ("asking for nothing is a usage error"); the *example* was wrong.

Only the usage-error class counts as a failure. An example carrying a placeholder
(`demo/app`, `ForecastService`) legitimately exits 1 "not found" against an empty store -- a
well-formed command with an empty answer is not a broken example.

The seed check lives in the command body, not in argparse, so a parse-only test would not
have caught this. The examples have to be executed.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
# a runnable example: no shell placeholders, no alternation
_EXAMPLE = re.compile(r"^\s+(contextlake [a-z].*?)(?:\s{2,}.*)?$", re.M)
# Every log line carries a `[YYYY-MM-DD HH:MM:SS] ` prefix, so a bare `^usage:` never
# matches and this gate goes silently blind. Caught by break-testing it.
_USAGE = re.compile(r"^(?:\[[^\]]*\]\s*)?usage:", re.M)


def _commands() -> list[tuple[list[str], str]]:
    """Every leaf command that actually exists, paired with its `--help` text.

    The top-level help lists namespaced commands by their bare names (`fetch`, `index`), and
    those flat spellings stopped parsing at v3.0.0. Scraped without a check they became phantom
    entries that contributed zero examples and quietly shrank the surface this gate covers, so
    each candidate has to prove itself by answering `--help` with exit 0.

    The help text is returned with the command rather than fetched again by `_examples`: at
    ~55 candidates that second spawn was half the runtime of this test, and the test has to
    stay well inside the suite's 300s timeout on a cold runner.
    """
    out: list[tuple[list[str], str]] = []
    seen: set[tuple[str, ...]] = set()
    for ns in ([], ["mirror"], ["kb"]):
        h = subprocess.run([sys.executable, "-m", "contextlake", *ns, "--help"],
                           capture_output=True, text=True, timeout=120, cwd=REPO).stdout
        block = h.split("commands:")[-1] if "commands:" in h else h
        for m in re.finditer(r"^\s{2,4}([a-z][a-z-]+)\s{2,}", block, re.M):
            name = m.group(1)
            if name in {"completion", "init"}:      # write real user config
                continue
            cmd = [*ns, name]
            if tuple(cmd) in seen:
                continue
            seen.add(tuple(cmd))
            probe = subprocess.run([sys.executable, "-m", "contextlake", *cmd, "--help"],
                                   capture_output=True, text=True, timeout=120, cwd=REPO)
            if probe.returncode == 0:
                out.append((cmd, probe.stdout))
    return out


def _examples(help_text: str) -> list[str]:
    if "Examples:" not in help_text:
        return []
    return [m.group(1).strip()
            for m in _EXAMPLE.finditer(help_text[help_text.index("Examples:"):])
            if "|" not in m.group(1) and "<" not in m.group(1)]


@pytest.mark.slow
def test_every_help_example_runs(tmp_path: Path) -> None:
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "store"}"\n')
    seen, bad = 0, []
    for _cmd, help_text in _commands():
        for ex in _examples(help_text):
            seen += 1
            proc = subprocess.Popen(
                [sys.executable, "-m", "contextlake", *ex.split()[1:], "--config", str(cfg)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                stdin=subprocess.DEVNULL, cwd=tmp_path)
            # A usage error is printed immediately. An example that is still alive after the
            # grace period started a server, which is a pass -- running it to completion would
            # make this test take minutes and would test the timeout, not the example.
            deadline = time.monotonic() + 3
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.1)
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=10)
                continue
            blob = proc.communicate(timeout=10)[0] or ""
            if proc.returncode == 2 and _USAGE.search(blob):
                bad.append(f"{ex}\n      {blob.strip().splitlines()[0][:150]}")
    # Pinned to the measured surface, not a soft floor. 63 runnable examples across 33
    # commands, 2026-08-25 at 8.6.1, identical in a full dev venv and in a `.[dev]`-only
    # venv matching ci.yml's core job. A `> 20` assert would have shrugged at losing an
    # entire namespace; adding an example is welcome and raises this number.
    assert seen >= 63, (
        f"only {seen} examples found, expected at least 63: the epilog scraper has lost "
        "part of the CLI surface")
    assert not bad, "the CLI advertises examples that do not run:\n  " + "\n  ".join(bad)
