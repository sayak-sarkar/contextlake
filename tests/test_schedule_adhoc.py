"""Ad-hoc jobs: `schedule interval <dur|auto> run <command...>`."""
from __future__ import annotations

import argparse

import pytest

from contextlake.schedule import cmds, jobs


def _config(tmp_path):
    return {"cache_dir": str(tmp_path), "cache_file": "p.txt"}


def _args(rest, **kw):
    base = dict(action="interval", rest=list(rest), job=None, json=False,
                platform=None, quiet=True, verbose=False, interval=None, yes=True)
    base.update(kw)
    return argparse.Namespace(**base)


# ---- parsing the spec ---------------------------------------------------

def test_a_duration_and_a_command_are_split_at_run():
    setting, argv = cmds.parse_interval_spec(["6h", "run", "kb", "wiki", "--force"])
    assert setting == "6h"
    assert argv == ["kb", "wiki", "--force"]


def test_auto_is_a_valid_interval_and_opts_back_into_adjusting():
    setting, argv = cmds.parse_interval_spec(["auto", "run", "mirror", "sync"])
    assert setting == "auto"
    assert argv == ["mirror", "sync"]


def test_a_double_dash_separator_is_accepted_and_dropped():
    """Defensive only. argparse strips the separator before parse_interval_spec
    sees the words, so this never fires through the CLI."""
    _, argv = cmds.parse_interval_spec(["1h", "run", "--", "kb", "index", "--force"])
    assert argv == ["kb", "index", "--force"]


def test_the_words_argparse_delivers_parse():
    """What the CLI hands over for `schedule interval 6h run -- kb wiki --force`:
    no separator, because argparse consumed it."""
    setting, argv = cmds.parse_interval_spec(["6h", "run", "kb", "wiki", "--force"])
    assert setting == "6h"
    assert argv == ["kb", "wiki", "--force"]


def test_the_captured_command_keeps_a_quoted_glob():
    _, argv = cmds.parse_interval_spec(["2h", "run", "mirror", "sync", "--repos", "acme/*"])
    assert argv == ["mirror", "sync", "--repos", "acme/*"]


@pytest.mark.parametrize("rest,fragment", [
    ([], "interval"),
    (["6h"], "run"),
    (["6h", "run"], "command"),
    (["run", "kb", "wiki"], "duration"),
    (["banana", "run", "kb", "wiki"], "duration"),
    (["6h", "kb", "wiki"], "run"),
])
def test_a_malformed_spec_fails_with_a_message_naming_the_problem(rest, fragment):
    with pytest.raises(ValueError) as excinfo:
        cmds.parse_interval_spec(rest)
    assert fragment in str(excinfo.value).lower()


def test_a_shell_string_is_refused_rather_than_split():
    """A single argument containing a shell metacharacter is not a command
    line, it is somebody expecting a shell. These land in unit files."""
    with pytest.raises(ValueError) as excinfo:
        cmds.parse_interval_spec(["1h", "run", "kb wiki && rm -rf /"])
    assert "shell" in str(excinfo.value).lower()


@pytest.mark.parametrize("bad", ["kb wiki; ls", "kb wiki | tee x", "$(id)", "`id`",
                                 "kb wiki > /etc/passwd", "kb\nwiki"])
def test_every_shell_metacharacter_is_refused(bad):
    with pytest.raises(ValueError):
        cmds.parse_interval_spec(["1h", "run", bad])


# ---- validation ---------------------------------------------------------

def test_a_real_command_validates():
    assert isinstance(cmds.validate_job_argv(["mirror", "sync"]), list)


def test_a_real_command_with_its_own_flags_validates():
    cmds.validate_job_argv(["kb", "index", "--workspace", "/tmp/x"])


def test_a_typo_fails_when_it_is_typed_not_at_3am():
    """The `kb graph --serve` lesson: an example advertised for months that had
    never once been run."""
    with pytest.raises(ValueError):
        cmds.validate_job_argv(["kb", "wikki"])


def test_an_unknown_flag_fails_too():
    with pytest.raises(ValueError):
        cmds.validate_job_argv(["mirror", "sync", "--no-such-flag"])


def test_a_flat_verb_that_needs_its_namespace_fails():
    """`sync` alone has not parsed at the root since the namespacing cutover."""
    with pytest.raises(ValueError):
        cmds.validate_job_argv(["sync"])


def test_scheduling_schedule_itself_is_refused():
    """A job that runs `schedule run` would recurse forever."""
    with pytest.raises(ValueError) as excinfo:
        cmds.validate_job_argv(["schedule", "run"])
    assert "itself" in str(excinfo.value).lower()


def test_a_kb_job_on_a_core_only_install_warns_rather_than_passing_silently(monkeypatch):
    """Parsing proves the verb exists, not that it can RUN. A kb job on a
    core-only install parses clean and fails every night."""
    import builtins

    real_import = builtins.__import__

    def _no_kb(name, *a, **k):
        if name.startswith("contextlake.kb") or name == "contextlake.kb":
            raise ImportError("No module named 'mcp'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_kb)
    warnings = cmds.validate_job_argv(["kb", "wiki"])
    assert any("extra" in w.lower() or "kb" in w.lower() for w in warnings)


def test_a_mirror_job_never_warns_about_the_kb_extra():
    assert cmds.validate_job_argv(["mirror", "sync"]) == []


# ---- the command --------------------------------------------------------

def test_creating_an_ad_hoc_job_records_it(tmp_path, monkeypatch):
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: _FakeAdapter())
    rc = cmds.cmd_interval(_args(["6h", "run", "kb", "wiki", "--force"], job="nightly"),
                           _config(tmp_path))
    assert rc == 0
    job = jobs.read_jobs(jobs.jobs_path(_config(tmp_path)))["nightly"]
    assert job.argv == ["kb", "wiki", "--force"]
    assert job.interval == "6h"
    assert job.full_argv == ["kb", "wiki", "--force"], \
        "an ad-hoc job has one command; there is no forced variant to invent"


def test_an_unnamed_ad_hoc_job_gets_a_name_from_its_command(tmp_path, monkeypatch):
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: _FakeAdapter())
    cmds.cmd_interval(_args(["6h", "run", "kb", "wiki"]), _config(tmp_path))
    assert "kb-wiki" in jobs.read_jobs(jobs.jobs_path(_config(tmp_path)))


def test_an_ad_hoc_job_does_not_replace_the_default_one(tmp_path, monkeypatch):
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: _FakeAdapter())
    path = jobs.jobs_path(_config(tmp_path))
    jobs.write_job(path, jobs.new_job(jobs.DEFAULT_JOB, ["bootstrap"], "auto", "fake"))
    cmds.cmd_interval(_args(["6h", "run", "kb", "wiki"]), _config(tmp_path))
    assert sorted(jobs.read_jobs(path)) == ["default", "kb-wiki"]


def test_a_bad_command_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: _FakeAdapter())
    assert cmds.cmd_interval(_args(["6h", "run", "kb", "wikki"]), _config(tmp_path)) != 0
    assert jobs.read_jobs(jobs.jobs_path(_config(tmp_path))) == {}


def test_a_bad_command_installs_no_unit(tmp_path, monkeypatch):
    adapter = _FakeAdapter()
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: adapter)
    cmds.cmd_interval(_args(["6h", "run", "kb", "wikki"]), _config(tmp_path))
    assert adapter.installed_with is None, "validate BEFORE installing, not after"


def test_the_ad_hoc_job_is_installed_at_its_own_interval(tmp_path, monkeypatch):
    adapter = _FakeAdapter()
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: adapter)
    cmds.cmd_interval(_args(["6h", "run", "kb", "wiki"]), _config(tmp_path))
    assert adapter.installed_with[1] == 6 * 3600.0


def test_a_failing_install_leaves_no_job_record(tmp_path, monkeypatch):
    """Write the job record only after the unit installs. A failed install
    must not leave a record claiming a schedule that does not exist."""
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: _RefusingAdapter())
    rc = cmds.cmd_interval(_args(["6h", "run", "kb", "wiki"]), _config(tmp_path))
    assert rc != 0
    assert jobs.read_jobs(jobs.jobs_path(_config(tmp_path))) == {}


def test_the_interval_action_routes_through_dispatch(tmp_path, monkeypatch):
    """cmd_interval unreachable from `dispatch` would leave the CLI printing
    'not implemented yet' for every `schedule interval` invocation."""
    monkeypatch.setattr(cmds, "_adapter_for", lambda *a, **k: _FakeAdapter())
    rc = cmds.dispatch(_args(["6h", "run", "kb", "wiki"]), _config(tmp_path))
    assert rc == 0
    assert "kb-wiki" in jobs.read_jobs(jobs.jobs_path(_config(tmp_path)))


class _FakeAdapter:
    def __init__(self):
        self.id = "fake"
        self.catches_up_after_sleep = True
        self.installed_with = None

    def install(self, job, interval_s, exec_argv, **options):
        self.installed_with = (job.name, interval_s)
        return ["/tmp/fake.unit"]

    def render(self, job, interval_s, exec_argv, **options):
        return {"fake.unit": ""}

    def state(self, job):
        return {"installed": True, "interval_s": 3600.0,
                "next_run": None, "notes": []}


class _RefusingAdapter(_FakeAdapter):
    """An adapter whose install always fails, for the ordering break-test."""

    def install(self, job, interval_s, exec_argv, **options):
        raise OSError("no permission to write the unit")
