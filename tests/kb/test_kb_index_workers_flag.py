"""`kb index --workers` -- the CLI never wired a flag to the parallelism
`_index_workspace` already accepts and honours.

Verified before this fix: `build_parser().parse_args(["kb", "index", "--workers",
"2", "--workspace", "/tmp"])` was rejected by the parser, and neither `"--workers"`
nor `args.workers` appeared anywhere in `cli.py` or `index.py`. A user with a large
fleet or a small machine had no way to cap indexing parallelism.

The wiring test is the one that matters: a flag that parses and is then ignored
reads exactly like one that works, and this project has shipped that shape before.
"""

from __future__ import annotations

import argparse
import os
import subprocess

import pytest

from contextlake import cli

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _args(*argv: str):
    return cli.build_parser().parse_args(argv)


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, env=_ENV, check=True,
                          capture_output=True, text=True).stdout.strip()


def _git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / "m.py").write_text("def foo():\n    return 1\n")
    _git(["init", "-q", "-b", "main"], path)
    _git(["add", "-A"], path)
    _git(["commit", "-q", "-m", "c"], path)


def _kb(tmp_path):
    """A config naming this test's own store, never the user's real one."""
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir}"\n')
    return cfg


# --- parsing --------------------------------------------------------------

def test_workers_flag_parses_as_int():
    args = _args("kb", "index", "--workers", "2", "--workspace", "/tmp")
    assert args.workers == 2
    assert isinstance(args.workers, int)


def test_workers_omitted_means_use_the_default_not_the_suppress_sentinel():
    """A SUPPRESS-default flag with no _DEFAULTS entry leaves getattr(args,
    "workers", None) returning the SUPPRESS sentinel, which is truthy -- this
    project has been bitten by exactly that shape before."""
    args = _args("kb", "index", "--workspace", "/tmp")
    assert args.workers is None
    assert args.workers is not argparse.SUPPRESS


@pytest.mark.parametrize("value", ["0", "-1"])
def test_workers_zero_and_negative_are_refused_at_parse_time(value, capsys):
    """Matches this file's _COUNT convention: a count that can only produce a
    nonsense result (zero or negative workers) is refused where it is typed."""
    with pytest.raises(SystemExit) as exc:
        _args("kb", "index", "--workers", value, "--workspace", "/tmp")
    assert exc.value.code == 2
    assert "must be between" in capsys.readouterr().err


def test_workers_also_parses_before_the_command_name():
    """Every bounded numeric flag is also accepted on the pre-subparser spelling
    (`contextlake --limit -1 kb query x`), so a bound checked only on the leaf
    parser would leave that spelling wide open. --workers must follow suit."""
    args = _args("--workers", "3", "kb", "index", "--workspace", "/tmp")
    assert args.workers == 3

    with pytest.raises(SystemExit) as exc:
        _args("--workers", "0", "kb", "index", "--workspace", "/tmp")
    assert exc.value.code == 2


# --- wiring: the value must actually reach _index_workspace ----------------

def test_workers_reaches_index_workspace(tmp_path, gls_logs):
    """The observable is the log line _index_workspace itself writes: "indexing
    N with {workers} worker(s)". Asserting on that, rather than on the parsed
    namespace, is what catches a flag that parses and is then ignored."""
    from contextlake.kb.commands import cmd_index

    ws = tmp_path / "ws"
    _git_repo(ws / "one")
    _git_repo(ws / "two")
    cfg = _kb(tmp_path)

    rc = cmd_index(_args("kb", "index", "--config", str(cfg),
                         "--workspace", str(ws), "--workers", "1"))
    assert rc == 0
    assert "with 1 worker(s)" in gls_logs.text, gls_logs.text


def test_omitting_the_flag_still_honours_kb_toml_index_workers(tmp_path, gls_logs):
    """A plausible wrong wiring -- ``getattr(args, "workers", None) or
    _default_index_workers()`` -- passes every test above while silently
    dropping kb.toml's own index_workers setting whenever the flag is not
    given. Assert the config value still reaches _index_workspace on its own."""
    from contextlake.kb.commands import cmd_index

    ws = tmp_path / "ws"
    _git_repo(ws / "one")
    _git_repo(ws / "two")
    store_dir = tmp_path / "kb"
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{store_dir}"\nindex_workers = 2\n')

    rc = cmd_index(_args("kb", "index", "--config", str(cfg), "--workspace", str(ws)))
    assert rc == 0
    assert "with 2 worker(s)" in gls_logs.text, gls_logs.text
