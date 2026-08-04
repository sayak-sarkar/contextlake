"""Observability for unattended runs: JSON logs, run correlation, redaction,
Prometheus metrics, and the traceback --verbose owes a crash report.

contextlake ships a systemd service + timer, so "nobody is watching" is a
supported mode. These tests pin the four things that make such a run
inspectable afterwards, plus the one guarantee that keeps the interactive
experience untouched: the human log format is unchanged, byte for byte.
"""

import json
import logging
import time

import pytest

from contextlake import cli, core, observability
from contextlake.logging_setup import log, setup_logging

_FAKE_CFG = {"work_dir": "/tmp/x", "gitlab_group": "g"}


@pytest.fixture(autouse=True)
def clean_observability_state():
    """Redaction rules, the run id and the access-log switch are process-wide
    (one CLI process = one run), so each test starts from a blank slate."""
    observability.reset_redactions()
    observability.set_run_id("")
    observability.set_command("")
    observability.set_access_log(False)
    yield
    observability.reset_redactions()
    observability.set_run_id("")
    observability.set_command("")
    observability.set_access_log(False)


@pytest.fixture
def patched_config(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda path=None: dict(_FAKE_CFG))
    monkeypatch.setattr(cli, "run_audit", lambda *a, **k: None)


def _json_lines(text):
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


# --- structured logging -----------------------------------------------------

def test_json_format_carries_ts_level_msg_run_id_and_command(capsys):
    observability.set_run_id("abc123")
    observability.set_command("mirror sync")
    setup_logging(log_format="json")

    log("Cloned", repo="team/api", duration_ms=812)

    record = _json_lines(capsys.readouterr().out)[0]
    assert record["msg"] == "Cloned"
    assert record["level"] == "INFO"
    assert record["run_id"] == "abc123"
    assert record["command"] == "mirror sync"
    assert record["repo"] == "team/api"
    assert record["duration_ms"] == 812


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs POSIX time.tzset")
def test_json_timestamp_is_really_utc_not_local_time_wearing_a_z(capsys, monkeypatch):
    """The line claims UTC, so it had better be UTC.

    ``logging.Formatter`` converts through ``time.localtime`` by default, which
    would shift every timestamp a collector ingests by the host's offset while
    still looking perfectly well-formed. Asserted against a fixed
    ``record.created`` under a non-whole-hour zone, so a formatter that
    regressed to local time cannot pass by coincidence.
    """
    monkeypatch.setenv("TZ", "Asia/Kolkata")  # UTC+05:30
    time.tzset()
    try:
        setup_logging(log_format="json")
        record = logging.LogRecord("contextlake", logging.INFO, "", 0, "x", None, None)
        record.created = 1_700_000_000  # 2023-11-14T22:13:20Z / 03:43:20 in Kolkata
        logging.getLogger("contextlake").handlers[0].emit(record)
    finally:
        monkeypatch.undo()
        time.tzset()

    assert _json_lines(capsys.readouterr().out)[0]["ts"] == "2023-11-14T22:13:20Z"


def test_every_line_of_one_run_shares_the_run_id(capsys):
    observability.set_run_id("run-1")
    setup_logging(log_format="json")

    log("index")
    log("connect")
    log("embed")

    assert {r["run_id"] for r in _json_lines(capsys.readouterr().out)} == {"run-1"}


def test_a_failure_line_carries_the_error_type_and_message(capsys):
    setup_logging(log_format="json")
    log("failed", repo="team/api", error_type="dns", error="could not resolve host")
    record = _json_lines(capsys.readouterr().out)[0]
    assert record["error_type"] == "dns"
    assert record["error"] == "could not resolve host"


def test_run_id_can_be_pinned_by_the_calling_job(monkeypatch):
    monkeypatch.setenv(observability.RUN_ID_ENV, "systemd-job-42")
    assert observability.new_run_id() == "systemd-job-42"


def test_a_hostile_pinned_run_id_is_sanitised(monkeypatch):
    # It is echoed into every JSON record, so quotes/newlines must never survive.
    monkeypatch.setenv(observability.RUN_ID_ENV, 'a"b\nc d')
    assert observability.new_run_id() == "abcd"


def test_the_run_id_reaches_a_worker_thread():
    """PEP 567 gives each thread a fresh Context, and the per-repo lines that
    matter most are emitted from ThreadPoolExecutor workers -- so the fallback
    behind the ContextVar is load-bearing, not belt-and-braces."""
    from concurrent.futures import ThreadPoolExecutor

    observability.set_run_id("threaded")
    with ThreadPoolExecutor(max_workers=1) as ex:
        assert ex.submit(observability.run_id).result() == "threaded"


# --- the human format is unchanged -----------------------------------------

def test_human_format_is_unchanged_and_ignores_structured_fields(capsys):
    setup_logging()
    log("Cloned team/api", repo="team/api", duration_ms=812)
    out = capsys.readouterr().out
    assert out.startswith("[") and out.rstrip().endswith("] Cloned team/api")
    assert "duration_ms" not in out and "repo" not in out


# --- redaction --------------------------------------------------------------

def test_redaction_replaces_workspace_group_and_repo_names():
    observability.add_redactions(paths=[("/home/dev/mirror", "<workspace>")],
                                 literals=[("acme-private", "<group>")])
    observability.add_repo_names(["team/api"])

    out = observability.redact(
        "cloned /home/dev/mirror/team/api for acme-private")

    assert "/home/dev/mirror" not in out
    assert "acme-private" not in out
    assert "team/api" not in out
    assert "<workspace>" in out and "<group>" in out


def test_redaction_is_stable_so_a_scrubbed_log_still_correlates():
    observability.add_repo_names(["team/api", "team/billing"])
    first = observability.redact("team/api failed")
    second = observability.redact("team/api failed again")
    assert first.split()[0] == second.split()[0]
    assert observability.redact("team/billing") != first.split()[0]


def test_redaction_keeps_the_file_path_inside_a_repo_readable():
    """The repo's identity is the secret; which file broke is the useful part."""
    observability.add_redactions(paths=[("/home/dev/mirror", "<workspace>")])
    observability.add_repo_names(["team/api"])
    out = observability.redact("/home/dev/mirror/team/api/src/main.py:42")
    assert out.endswith("/src/main.py:42")
    assert "team/api" not in out


def test_redaction_does_not_match_a_longer_surrounding_name():
    observability.add_repo_names(["team/api"])
    assert observability.redact("myteam/api") == "myteam/api"


def test_a_path_under_the_workspace_is_scrubbed_even_if_unregistered():
    # No add_repo_names() call: a repo we never learned the name of must still
    # not leak its namespace just because it was not in the fleet list.
    observability.add_redactions(paths=[("/home/dev/mirror", "<workspace>")])
    out = observability.redact("/home/dev/mirror/secret-team/thing")
    assert "secret-team" not in out and out.startswith("<workspace>/repo-")


def test_redaction_is_a_no_op_until_something_is_registered():
    assert not observability.redaction_configured()
    assert observability.redact("/home/dev/mirror/team/api") == "/home/dev/mirror/team/api"


def test_log_file_is_redacted_by_default_but_the_console_is_not(tmp_path, capsys):
    """The console is yours and you need the real paths to act on it; the file is
    what gets attached to a bug report."""
    observability.add_redactions(literals=[("acme-private", "<group>")])
    log_file = tmp_path / "run.log"
    setup_logging(log_file=str(log_file))

    log("group: acme-private")

    assert "acme-private" in capsys.readouterr().out
    assert "acme-private" not in log_file.read_text()
    assert "<group>" in log_file.read_text()


def test_redact_true_scrubs_the_console_too(tmp_path, capsys):
    observability.add_redactions(literals=[("acme-private", "<group>")])
    setup_logging(redact=True)
    log("group: acme-private")
    assert "acme-private" not in capsys.readouterr().out


def test_redact_false_scrubs_nothing_including_the_file(tmp_path, capsys):
    observability.add_redactions(literals=[("acme-private", "<group>")])
    log_file = tmp_path / "run.log"
    setup_logging(log_file=str(log_file), redact=False)
    log("group: acme-private")
    assert "acme-private" in capsys.readouterr().out
    assert "acme-private" in log_file.read_text()


def test_json_fields_are_redacted_too(capsys):
    observability.add_repo_names(["team/api"])
    setup_logging(log_format="json", redact=True)
    log("cloned", repo="team/api")
    assert _json_lines(capsys.readouterr().out)[0]["repo"].startswith("repo-")


def test_the_fleet_listing_functions_teach_redaction_the_repo_names(tmp_path):
    from conftest import make_local_repo

    make_local_repo(tmp_path, "team/api")
    core.get_local_repos(str(tmp_path))

    assert "team/api" not in observability.redact("failed: team/api")


# --- Prometheus textfile ----------------------------------------------------

def _parse(text):
    """A minimal textfile parser: {metric_name: [series lines]} plus HELP counts."""
    series, helps = {}, {}
    for line in text.splitlines():
        if line.startswith("# HELP "):
            helps[line.split()[2]] = helps.get(line.split()[2], 0) + 1
        elif line.startswith("# TYPE ") or not line:
            continue
        else:
            name = line.split("{")[0].split(" ")[0]
            series.setdefault(name, []).append(line)
    return series, helps


def test_textfile_has_the_run_metrics_and_one_help_per_metric(tmp_path):
    path = tmp_path / "contextlake.prom"
    text = observability.write_textfile(
        path, command_name="mirror sync", duration_seconds=12.5, exit_code=0,
        repos={"ok": 480, "failed": 0, "skipped": 3}, nodes=1200, edges=3400,
        now=1_700_000_000)

    series, helps = _parse(text)
    assert set(series) == {
        "contextlake_run_duration_seconds", "contextlake_run_exit_code",
        "contextlake_repos", "contextlake_graph_nodes", "contextlake_graph_edges",
        "contextlake_last_success_timestamp_seconds",
    }
    assert all(count == 1 for count in helps.values()), helps
    assert 'contextlake_repos{command="mirror sync",status="ok"} 480' in series[
        "contextlake_repos"]
    assert series["contextlake_graph_nodes"] == ["contextlake_graph_nodes 1200"]
    assert path.read_text() == text


def test_unmeasured_graph_size_is_omitted_never_written_as_zero(tmp_path):
    """A `mirror sync` publishing `contextlake_graph_nodes 0` reads as "the graph
    was wiped" -- the sort of gauge that wakes someone up for nothing."""
    text = observability.write_textfile(
        tmp_path / "m.prom", command_name="mirror sync", duration_seconds=1,
        exit_code=0, repos={"ok": 1, "failed": 0, "skipped": 0})
    assert "contextlake_graph_nodes" not in text
    assert "contextlake_graph_edges" not in text


def test_a_failing_run_keeps_the_previous_last_success(tmp_path):
    path = tmp_path / "m.prom"
    observability.write_textfile(path, command_name="mirror sync",
                                 duration_seconds=1, exit_code=0, now=1_700_000_000)
    text = observability.write_textfile(path, command_name="mirror sync",
                                        duration_seconds=1, exit_code=1,
                                        now=1_700_009_999)

    series, helps = _parse(text)
    assert series["contextlake_last_success_timestamp_seconds"] == [
        'contextlake_last_success_timestamp_seconds{command="mirror sync"} 1700000000']
    assert helps["contextlake_last_success_timestamp_seconds"] == 1
    assert 'contextlake_run_exit_code{command="mirror sync"} 1' in text


def test_a_successful_run_replaces_its_own_last_success(tmp_path):
    path = tmp_path / "m.prom"
    observability.write_textfile(path, command_name="mirror sync",
                                 duration_seconds=1, exit_code=0, now=1_700_000_000)
    text = observability.write_textfile(path, command_name="mirror sync",
                                        duration_seconds=1, exit_code=0,
                                        now=1_700_009_999)
    series, _ = _parse(text)
    assert series["contextlake_last_success_timestamp_seconds"] == [
        'contextlake_last_success_timestamp_seconds{command="mirror sync"} 1700009999']


def test_another_commands_last_success_is_carried_over_untouched(tmp_path):
    path = tmp_path / "m.prom"
    observability.write_textfile(path, command_name="kb index", duration_seconds=1,
                                 exit_code=0, now=1_700_000_000)
    text = observability.write_textfile(path, command_name="mirror sync",
                                        duration_seconds=1, exit_code=0,
                                        now=1_700_009_999)
    series, helps = _parse(text)
    assert len(series["contextlake_last_success_timestamp_seconds"]) == 2
    assert helps["contextlake_last_success_timestamp_seconds"] == 1


def test_label_values_are_escaped(tmp_path):
    text = observability.write_textfile(tmp_path / "m.prom",
                                        command_name='we"ird\\', duration_seconds=1,
                                        exit_code=0)
    assert r'command="we\"ird\\"' in text


def test_graph_counts_reads_a_real_store_read_only(tmp_path):
    import sqlite3

    db = tmp_path / "index.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript("CREATE TABLE nodes(id TEXT); CREATE TABLE edges(id TEXT);"
                       "INSERT INTO nodes VALUES ('a'),('b'); INSERT INTO edges VALUES ('e');")
    conn.commit()
    conn.close()

    observability.note_store_path(db)
    assert observability.graph_counts() == (2, 1)


def test_graph_counts_is_none_when_there_is_no_store(tmp_path):
    observability.note_store_path(tmp_path / "nope.sqlite")
    assert observability.graph_counts() == (None, None)


# --- the CLI wiring ---------------------------------------------------------

def _stub_clean_pipeline(monkeypatch):
    monkeypatch.setattr(cli, "update_repositories",
                        lambda *a, **k: core.StageResult(ok=2, failed=0, skipped=1))


def test_metrics_file_is_written_after_a_mirror_run(monkeypatch, tmp_path, patched_config):
    _stub_clean_pipeline(monkeypatch)
    path = tmp_path / "contextlake.prom"

    assert cli.main(["mirror", "update", "--metrics-file", str(path)]) is None

    text = path.read_text()
    assert 'contextlake_repos{command="mirror update",status="ok"} 2' in text
    assert 'contextlake_repos{command="mirror update",status="skipped"} 1' in text
    assert 'contextlake_run_exit_code{command="mirror update"} 0' in text


def test_metrics_record_the_real_exit_code_of_a_failed_run(monkeypatch, tmp_path,
                                                           patched_config):
    monkeypatch.setattr(cli, "update_repositories",
                        lambda *a, **k: core.StageResult(ok=1, failed=3))
    path = tmp_path / "contextlake.prom"

    with pytest.raises(SystemExit) as exc:
        cli.main(["mirror", "update", "--metrics-file", str(path)])

    assert exc.value.code == 1
    text = path.read_text()
    assert 'contextlake_run_exit_code{command="mirror update"} 1' in text
    assert "contextlake_last_success_timestamp_seconds" not in text


def test_an_unwritable_metrics_path_never_replaces_the_real_outcome(
        monkeypatch, tmp_path, patched_config, capsys):
    """The write happens in main()'s finally, where raising would swap the run's
    actual failure for a traceback about a gauge."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setattr(cli, "update_repositories",
                        lambda *a, **k: core.StageResult(failed=1))

    with pytest.raises(SystemExit) as exc:
        cli.main(["mirror", "update", "--metrics-file", str(blocker / "m.prom")])

    assert exc.value.code == 1
    assert "Could not write metrics" in capsys.readouterr().out


def test_json_logs_end_to_end_through_the_cli(monkeypatch, patched_config, capsys):
    _stub_clean_pipeline(monkeypatch)
    cli.main(["mirror", "update", "--log-format", "json"])
    records = _json_lines(capsys.readouterr().out)
    assert records, "expected JSON lines on stdout"
    assert {r["command"] for r in records} == {"mirror update"}
    assert len({r["run_id"] for r in records}) == 1


def test_verbose_surfaces_the_traceback(monkeypatch, patched_config, capsys):
    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(cli, "update_repositories", boom)

    # Without --verbose: the one-line summary and exit 1, as always.
    with pytest.raises(SystemExit) as exc:
        cli.main(["mirror", "update"])
    assert exc.value.code == 1
    assert "Error: kaboom" in capsys.readouterr().out

    # With --verbose: the exception reaches the caller, so a crash report has a
    # traceback in it without asking the reporter to reproduce under a debugger.
    with pytest.raises(RuntimeError, match="kaboom"):
        cli.main(["mirror", "update", "--verbose"])


def test_bootstrap_stage_traceback_is_available_at_debug(monkeypatch, patched_config,
                                                        gls_logs):
    """A bootstrap stage must not abort the run, so its exception cannot be
    re-raised -- but --verbose should still be able to see why it failed."""
    kb = pytest.importorskip("contextlake.kb.commands")
    gls_logs.set_level(logging.DEBUG)
    args = cli.build_parser().parse_args(["bootstrap", "--no-sync", "--no-audit"])

    def boom(_args):
        raise RuntimeError("stage exploded")

    monkeypatch.setattr(kb, "cmd_index", boom)

    with pytest.raises(SystemExit):  # a failed index aborts bootstrap by design
        cli._bootstrap(args, dict(_FAKE_CFG), "/tmp/x", "g")

    assert any("RuntimeError: stage exploded" in (record.exc_text or "")
               for record in gls_logs.records)
