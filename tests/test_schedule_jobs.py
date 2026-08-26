"""The job record: create, read, update, delete, and survive a bad file."""
from __future__ import annotations

from contextlake.schedule import jobs


def test_a_new_job_round_trips(tmp_path):
    path = str(tmp_path / "jobs.json")
    job = jobs.new_job("default", ["bootstrap"], "auto", "systemd")
    jobs.write_job(path, job)
    loaded = jobs.read_jobs(path)
    assert list(loaded) == ["default"]
    assert loaded["default"].argv == ["bootstrap"]
    assert loaded["default"].interval == "auto"
    assert loaded["default"].platform == "systemd"
    assert loaded["default"].failures == 0


def test_writing_the_same_name_replaces_rather_than_duplicates(tmp_path):
    path = str(tmp_path / "jobs.json")
    jobs.write_job(path, jobs.new_job("default", ["bootstrap"], "auto", "systemd"))
    jobs.write_job(path, jobs.new_job("default", ["mirror", "sync"], "2h", "cron"))
    loaded = jobs.read_jobs(path)
    assert len(loaded) == 1
    assert loaded["default"].argv == ["mirror", "sync"]
    assert loaded["default"].interval == "2h"


def test_two_named_jobs_coexist(tmp_path):
    path = str(tmp_path / "jobs.json")
    jobs.write_job(path, jobs.new_job("default", ["bootstrap"], "auto", "systemd"))
    jobs.write_job(path, jobs.new_job("nightly", ["kb", "wiki"], "24h", "systemd"))
    assert sorted(jobs.read_jobs(path)) == ["default", "nightly"]


def test_delete_reports_whether_it_removed_anything(tmp_path):
    path = str(tmp_path / "jobs.json")
    jobs.write_job(path, jobs.new_job("default", ["bootstrap"], "auto", "systemd"))
    assert jobs.delete_job(path, "default") is True
    assert jobs.delete_job(path, "default") is False
    assert jobs.read_jobs(path) == {}


def test_reading_a_missing_file_is_empty(tmp_path):
    assert jobs.read_jobs(str(tmp_path / "nope.json")) == {}


def test_a_corrupt_file_reads_as_empty_rather_than_crashing(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text("{not json", encoding="utf-8")
    assert jobs.read_jobs(str(path)) == {}


def test_a_record_missing_required_fields_is_skipped(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text('{"jobs": {"broken": {"interval": "2h"}, '
                    '"ok": {"argv": ["bootstrap"], "interval": "auto"}}}',
                    encoding="utf-8")
    assert list(jobs.read_jobs(str(path))) == ["ok"]


def test_a_failure_increments_the_counter(tmp_path):
    path = str(tmp_path / "jobs.json")
    jobs.write_job(path, jobs.new_job("default", ["bootstrap"], "auto", "systemd"))
    jobs.record_outcome(path, "default", 1, "2026-08-26T01:00:00Z")
    jobs.record_outcome(path, "default", 1, "2026-08-26T02:00:00Z")
    job = jobs.read_jobs(path)["default"]
    assert job.failures == 2
    assert job.last_exit == 1
    assert job.last_run == "2026-08-26T02:00:00Z"


def test_a_success_resets_the_counter(tmp_path):
    path = str(tmp_path / "jobs.json")
    jobs.write_job(path, jobs.new_job("default", ["bootstrap"], "auto", "systemd"))
    jobs.record_outcome(path, "default", 1, "2026-08-26T01:00:00Z")
    jobs.record_outcome(path, "default", 0, "2026-08-26T02:00:00Z")
    assert jobs.read_jobs(path)["default"].failures == 0


def test_recording_an_outcome_for_an_unknown_job_is_a_no_op(tmp_path):
    """A no-op is a claim about the FILE, so the file is what gets asserted.
    Returning None while rewriting the document would satisfy a return-value
    check alone."""
    path = str(tmp_path / "jobs.json")
    jobs.write_job(path, jobs.new_job("default", ["bootstrap"], "auto", "systemd"))
    before = open(path, "rb").read()
    assert jobs.record_outcome(path, "ghost", 0, "2026-08-26T00:00:00Z") is None
    assert open(path, "rb").read() == before


def test_the_default_job_runs_bootstrap_incrementally_and_forced_on_the_full_cycle():
    job = jobs.new_job(jobs.DEFAULT_JOB, jobs.DEFAULT_ARGV, "auto", "systemd")
    assert job.argv == ["bootstrap"]
    assert job.full_argv == ["bootstrap", "--force"]


def test_an_ad_hoc_job_with_no_full_variant_reuses_its_own_argv():
    """A job the user wrote has one command. There is no forced version of
    `kb wiki`, so the full cycle runs what the incremental one does."""
    job = jobs.new_job("nightly", ["kb", "wiki"], "24h", "cron")
    assert job.full_argv == ["kb", "wiki"]


def test_full_argv_is_keyed_on_the_command_not_the_job_name():
    """A job that runs bootstrap gets the forced full cycle whatever it is
    named, and a job named `default` that runs something else does not."""
    named_other = jobs.new_job("weekly", list(jobs.DEFAULT_ARGV), "7d", "systemd")
    assert named_other.full_argv == ["bootstrap", "--force"]

    default_name_other_cmd = jobs.new_job(jobs.DEFAULT_JOB, ["kb", "wiki"], "auto", "cron")
    assert default_name_other_cmd.full_argv == ["kb", "wiki"]


def test_argv_is_stored_as_a_list_never_a_shell_string(tmp_path):
    """These land in unit files that run unattended. A shell string would be an
    injection surface, so the store cannot even represent one."""
    path = tmp_path / "jobs.json"
    path.write_text('{"jobs": {"bad": {"argv": "kb wiki; rm -rf /", "interval": "1h"}}}',
                    encoding="utf-8")
    assert jobs.read_jobs(str(path)) == {}


def test_argv_with_a_non_string_element_is_dropped(tmp_path):
    """A list is not enough on its own: every element must be a string too.

    ``full_argv`` is given here as a separately valid list, so this record can
    be dropped only by the element check on ``argv`` itself, not by the
    matching check that also runs on ``full_argv``.
    """
    path = tmp_path / "jobs.json"
    path.write_text(
        '{"jobs": {"mixed": {"argv": ["kb", 123], '
        '"full_argv": ["kb", "wiki"], "interval": "1h"}}}',
        encoding="utf-8")
    assert jobs.read_jobs(str(path)) == {}
