"""The AWS and Azure adapters.

NOT RUNNABLE HERE. There is no AWS account and no Azure subscription, so
nothing below calls either API. Tests assert the rendered request document and
the exact argv.

One assertion style is deliberate throughout, and it comes from a defect the
k8s adapter shipped and had corrected: **an argv assertion is not an assertion
that the payload was sent.** Where a command carries a document, the document
is asserted separately and checked against what render produced.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from contextlake.schedule import jobs as jobstore
from contextlake.schedule.platform import cloud


def _job(name="default", platform="aws"):
    return jobstore.new_job(name, ["bootstrap"], "auto", platform)


def _argv():
    return ["/venv/bin/python", "-m", "contextlake", "schedule", "run", "--job", "default"]


def _ok(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


# ---- shared rules --------------------------------------------------------

def test_both_adapters_say_they_cannot_forbid_overlapping_runs():
    """Neither service has a CronJob's concurrencyPolicy: Forbid, so
    single-writer protection rests on the store's advisory lock: the second
    run takes it, fails, and skips. That is weaker than what the cluster
    gives, so it is stated rather than implied."""
    aws = cloud.AwsAdapter().render(_job(), 3600, _argv())
    azure = cloud.AzureAdapter().render(_job(platform="azure"), 3600, _argv())

    for rendered in (aws, azure):
        joined = " ".join(rendered["notes"])
        assert "advisory lock" in joined
        assert "Forbid" in joined


def test_both_adapters_say_the_store_must_be_persistent():
    """An ephemeral store re-indexes the whole fleet every run, which
    `schedule run` already refuses without --allow-ephemeral. A cloud install
    that says nothing about storage ships a schedule whose every run refuses."""
    aws = " ".join(cloud.AwsAdapter().render(_job(), 3600, _argv())["notes"])
    azure = " ".join(cloud.AzureAdapter().render(
        _job(platform="azure"), 3600, _argv())["notes"])

    assert "EFS" in aws
    assert "Azure Files" in azure


def test_the_whole_minute_rounding_matches_the_other_backends():
    """Three backends rounding three ways would give one request three
    intervals. Rounds down above a minute, up below it, never to zero."""
    assert cloud.nearest_expressible(4259) == (4200.0, 70)
    assert cloud.nearest_expressible(30) == (60.0, 1)
    assert cloud.nearest_expressible(0) == (60.0, 1)


# ---- AWS -----------------------------------------------------------------

def test_the_eventbridge_rate_expression_uses_a_unit_word_not_seconds():
    """rate() takes an integer and a UNIT WORD. "rate(4200 seconds)" is not
    valid: seconds is not a rate() unit, and the schedule is rejected when it
    is created rather than when it would have fired."""
    rendered = cloud.AwsAdapter().render(_job(), 4200, _argv())
    assert rendered["rate"] == "rate(70 minutes)"

    document = json.loads(rendered["schedule.json"])
    assert document["ScheduleExpression"] == "rate(70 minutes)"
    assert "seconds" not in document["ScheduleExpression"]


def test_the_rate_expression_is_singular_at_one_minute():
    """rate(1 minutes) is rejected. The unit word is singular at 1."""
    assert cloud.AwsAdapter().render(_job(), 60, _argv())["rate"] == "rate(1 minute)"


def test_the_aws_document_is_valid_json_and_carries_the_container_command():
    """Parsed, not string-matched: a document containing the right words can
    still be malformed, and the API is somewhere this suite cannot reach."""
    document = json.loads(cloud.AwsAdapter().render(_job(), 3600, _argv())["schedule.json"])

    overrides = json.loads(document["Target"]["Input"])["containerOverrides"]
    assert overrides[0]["command"] == ["schedule", "run", "--job", "default"]
    # No interpreter path from the installing machine survives into the task.
    assert not any("/venv" in a for a in overrides[0]["command"])


def test_aws_install_sends_the_document_on_stdin_not_just_the_right_argv():
    """The k8s adapter shipped an install that named a stdin file and never
    wrote to it, so the argv was right and the document was empty. The same
    assertion shape is used here so that cannot recur silently."""
    calls = []

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(cloud, "_cli",
                            lambda b, *a, **kw: calls.append((b, a, kw)) or _ok())
        written = cloud.AwsAdapter().install(_job(), 3600, _argv())
    finally:
        monkeypatch.undo()

    assert written == ["schedule/contextlake-default"]
    binary, argv, kwargs = calls[0]
    assert binary == "aws"
    assert argv[:3] == ("scheduler", "create-schedule", "--cli-input-json")

    sent = kwargs.get("input")
    assert sent, "the schedule document was never sent"
    assert json.loads(sent)["Name"] == "contextlake-default"
    assert sent == cloud.AwsAdapter().render(_job(), 3600, _argv())["schedule.json"]


def test_aws_install_raises_when_the_api_refuses(monkeypatch):
    monkeypatch.setattr(cloud, "_cli",
                        lambda *a, **kw: _ok(returncode=1, stderr="AccessDenied"))
    with pytest.raises(OSError, match="AccessDenied"):
        cloud.AwsAdapter().install(_job(), 3600, _argv())


def test_aws_uninstall_reports_a_removal_only_when_something_was_removed(monkeypatch):
    monkeypatch.setattr(cloud, "_cli", lambda *a, **kw: _ok())
    assert cloud.AwsAdapter().uninstall(_job()) == ["schedule/contextlake-default"]

    monkeypatch.setattr(cloud, "_cli", lambda *a, **kw: _ok(returncode=1))
    assert cloud.AwsAdapter().uninstall(_job()) == []


def test_aws_installed_names_strips_the_prefix_and_claims_nothing_else(monkeypatch):
    monkeypatch.setattr(cloud, "_cli", lambda *a, **kw: _ok(
        stdout="contextlake-default contextlake-nightly somebody-elses-schedule\n"))
    assert cloud.AwsAdapter().installed_names() == ["default", "nightly"]


def test_aws_installed_names_is_none_when_the_cli_is_absent_or_fails(monkeypatch):
    def _boom(*a, **kw):
        raise OSError("aws is not on PATH")

    monkeypatch.setattr(cloud, "_cli", _boom)
    assert cloud.AwsAdapter().installed_names() is None

    monkeypatch.setattr(cloud, "_cli", lambda *a, **kw: _ok(returncode=1))
    assert cloud.AwsAdapter().installed_names() is None


def test_aws_state_reports_the_expression_without_inventing_a_next_run(monkeypatch):
    monkeypatch.setattr(cloud, "_cli", lambda *a, **kw: _ok(stdout="rate(60 minutes)\n"))
    state = cloud.AwsAdapter().state(_job())

    assert state["installed"] is True
    assert state["next_run"] is None
    assert state["interval_s"] is None
    assert any("rate(60 minutes)" in n for n in state["notes"])


# ---- Azure ---------------------------------------------------------------

def test_the_azure_trigger_is_a_cron_expression_and_rounds_like_cron():
    """A Container Apps Job schedule trigger IS a cron expression, so it
    rounds through the cron function rather than the whole-minute one. 70
    minutes is the case that separates them: valid for EventBridge, not
    expressible in cron."""
    from contextlake.schedule.platform import cron

    rendered = cloud.AzureAdapter().render(_job(platform="azure"), 4259, _argv())
    expected_s, expected_spec = cron.nearest_expressible(4259)

    assert rendered["cron"] == expected_spec
    assert rendered["interval_s"] == expected_s


def test_azure_install_passes_the_cron_expression_and_the_container_args(monkeypatch):
    calls = []
    monkeypatch.setattr(cloud, "_cli", lambda b, *a, **kw: calls.append((b, a)) or _ok())

    written = cloud.AzureAdapter().install(_job(platform="azure"), 3600, _argv())

    assert written == ["containerapp-job/contextlake-default"]
    binary, argv = calls[0]
    assert binary == "az"
    argv = list(argv)
    assert argv[argv.index("--trigger-type") + 1] == "Schedule"
    assert argv[argv.index("--cron-expression") + 1] == "0 * * * *"
    # The container runs the contextlake subcommand, not the installing
    # machine's interpreter path.
    assert argv[argv.index("--args") + 1] == "schedule run --job default"


def test_azure_install_raises_when_the_api_refuses(monkeypatch):
    monkeypatch.setattr(cloud, "_cli",
                        lambda *a, **kw: _ok(returncode=1, stderr="Forbidden"))
    with pytest.raises(OSError, match="Forbidden"):
        cloud.AzureAdapter().install(_job(platform="azure"), 3600, _argv())


def test_azure_uninstall_reports_a_removal_only_when_something_was_removed(monkeypatch):
    monkeypatch.setattr(cloud, "_cli", lambda *a, **kw: _ok())
    assert cloud.AzureAdapter().uninstall(_job(platform="azure")) == [
        "containerapp-job/contextlake-default"]

    monkeypatch.setattr(cloud, "_cli", lambda *a, **kw: _ok(returncode=1))
    assert cloud.AzureAdapter().uninstall(_job(platform="azure")) == []


def test_azure_installed_names_is_none_when_the_cli_is_absent(monkeypatch):
    def _boom(*a, **kw):
        raise OSError("az is not on PATH")

    monkeypatch.setattr(cloud, "_cli", _boom)
    assert cloud.AzureAdapter().installed_names() is None


def test_azure_state_reports_the_expression_without_inventing_a_next_run(monkeypatch):
    monkeypatch.setattr(cloud, "_cli", lambda *a, **kw: _ok(stdout="0 * * * *\n"))
    state = cloud.AzureAdapter().state(_job(platform="azure"))

    assert state["installed"] is True
    assert state["next_run"] is None
    assert any("0 * * * *" in n for n in state["notes"])


# ---- neither is usable here ---------------------------------------------

def test_neither_adapter_is_usable_without_its_cli(monkeypatch):
    monkeypatch.setattr(cloud.shutil, "which", lambda _c: None)
    assert cloud.AwsAdapter().usable() is False
    assert cloud.AzureAdapter().usable() is False


def test_the_module_says_to_use_the_k8s_adapter_on_eks_and_aks():
    """EKS and AKS are Kubernetes, so the CronJob adapter serves them and
    brings Forbid with it. Reaching for `aws` on EKS is the obvious wrong
    move, and this module is the only place that says otherwise."""
    assert "EKS" in cloud.__doc__ and "AKS" in cloud.__doc__
    assert "--platform k8s" in cloud.__doc__
