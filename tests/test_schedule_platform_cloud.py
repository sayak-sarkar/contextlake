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

    monkeypatch.setattr(cloud, "_cli", lambda *a, **kw: _ok(
        returncode=254, stderr="An error occurred (ResourceNotFoundException) when calling "
                               "the DeleteSchedule operation: Schedule contextlake-default "
                               "does not exist."))
    assert cloud.AwsAdapter().uninstall(_job()) == []


def test_aws_uninstall_raises_for_a_failure_that_is_not_a_missing_schedule(monkeypatch):
    """Expired credentials and AccessDenied exit non-zero too. Read as "already gone", the
    job record was deleted and the schedule kept firing."""
    for stderr in ("An error occurred (ExpiredTokenException) when calling the "
                   "DeleteSchedule operation: The security token included in the request "
                   "is expired",
                   "An error occurred (AccessDeniedException) when calling the DeleteSchedule "
                   "operation: User is not authorized"):
        monkeypatch.setattr(cloud, "_cli",
                            lambda *a, _e=stderr, **kw: _ok(returncode=254, stderr=_e))
        with pytest.raises(OSError, match="delete-schedule for contextlake-default failed"):
            cloud.AwsAdapter().uninstall(_job())


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


def test_aws_state_reports_not_installed_only_for_a_not_found_answer(monkeypatch):
    monkeypatch.setattr(cloud, "_cli", lambda *a, **kw: _ok(
        returncode=254, stderr="An error occurred (ResourceNotFoundException) when calling "
                               "the GetSchedule operation: Schedule does not exist."))
    state = cloud.AwsAdapter().state(_job())
    assert state["installed"] is False
    assert state["notes"] == []


def test_aws_state_is_none_when_aws_cannot_be_asked(monkeypatch):
    """Expired credentials, a missing permission and a dead network exit non-zero too. Read as
    "not installed", `status` told the user to re-run `schedule install`, which needs the same
    access. None is "cannot tell", with the first line of the error in a note."""
    for stderr, shown in (
            ("An error occurred (ExpiredTokenException) when calling the GetSchedule "
             "operation: The security token included in the request is expired\n",
             "ExpiredTokenException"),
            ("An error occurred (AccessDeniedException) when calling the GetSchedule "
             "operation: User is not authorized\nsecond line\n",
             "AccessDeniedException"),
            ("Could not connect to the endpoint URL: \"https://scheduler.example.invalid/\"",
             "Could not connect"),
            ("", "no output")):
        monkeypatch.setattr(cloud, "_cli",
                            lambda *a, _e=stderr, **kw: _ok(returncode=254, stderr=_e))
        state = cloud.AwsAdapter().state(_job())
        assert state["installed"] is None
        assert len(state["notes"]) == 1
        assert state["notes"][0].startswith("could not read the aws state: ")
        assert shown in state["notes"][0]
        assert "second line" not in state["notes"][0]


def test_aws_state_is_none_when_the_cli_is_absent(monkeypatch):
    def _boom(*a, **kw):
        raise OSError("aws is not on PATH")

    monkeypatch.setattr(cloud, "_cli", _boom)
    state = cloud.AwsAdapter().state(_job())
    assert state["installed"] is None
    assert state["notes"] == ["aws is not on PATH"]


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

    for stderr in ("ERROR: (ResourceNotFound) The Resource 'Microsoft.App/jobs/"
                   "contextlake-default' under resource group 'contextlake' was not found.",
                   "ERROR: (ResourceGroupNotFound) Resource group 'contextlake' could not "
                   "be found."):
        monkeypatch.setattr(cloud, "_cli",
                            lambda *a, _e=stderr, **kw: _ok(returncode=1, stderr=_e))
        assert cloud.AzureAdapter().uninstall(_job(platform="azure")) == []


def test_azure_uninstall_raises_for_a_failure_that_is_not_a_missing_job(monkeypatch):
    """A missing permission, a login that expired and a dead network exit non-zero too. Read
    as "already gone", the job record was deleted and the job kept running. A failure with
    no output at all is not a not-found code either."""
    for stderr in ("ERROR: (AuthorizationFailed) The client does not have authorization to "
                   "perform action 'Microsoft.App/jobs/delete'.",
                   "ERROR: Please run 'az login' to setup account.",
                   "ERROR: HTTPSConnectionPool: Max retries exceeded, Failed to resolve "
                   "'management.azure.com'",
                   ""):
        monkeypatch.setattr(cloud, "_cli",
                            lambda *a, _e=stderr, **kw: _ok(returncode=1, stderr=_e))
        with pytest.raises(OSError,
                           match="az containerapp job delete for contextlake-default failed"):
            cloud.AzureAdapter().uninstall(_job(platform="azure"))


def test_every_azure_call_for_a_job_names_the_resource_group_install_used(monkeypatch):
    """`az containerapp job delete` and `show` take a resource group. Install passes one and
    the CLI has no flag for another, so a call that leaves it out only works for a user with
    a default group set in `az configure`. Anyone else got a missing-argument error, which
    uninstall read as "already gone"."""
    calls = []
    monkeypatch.setattr(cloud, "_cli", lambda *a, **kw: calls.append(a) or _ok())
    adapter = cloud.AzureAdapter()
    job = _job(platform="azure")
    adapter.install(job, 3600, _argv())
    adapter.uninstall(job)
    adapter.state(job)
    assert len(calls) == 3

    def group(argv):
        argv = list(argv)
        return argv[argv.index("--resource-group") + 1]

    assert group(calls[1]) == group(calls[0]) == cloud.DEFAULT_RESOURCE_GROUP
    assert group(calls[2]) == group(calls[0])


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


def test_azure_state_reports_not_installed_only_for_a_not_found_answer(monkeypatch):
    for stderr in ("ERROR: (ResourceNotFound) The Resource 'Microsoft.App/jobs/"
                   "contextlake-default' under resource group 'contextlake' was not found.",
                   "ERROR: (ResourceGroupNotFound) Resource group 'contextlake' could not "
                   "be found."):
        monkeypatch.setattr(cloud, "_cli",
                            lambda *a, _e=stderr, **kw: _ok(returncode=1, stderr=_e))
        state = cloud.AzureAdapter().state(_job(platform="azure"))
        assert state["installed"] is False
        assert state["notes"] == []


def test_azure_state_is_none_when_az_cannot_be_asked(monkeypatch):
    for stderr in ("ERROR: Please run 'az login' to setup account.",
                   "ERROR: (AuthorizationFailed) The client does not have authorization.",
                   ""):
        monkeypatch.setattr(cloud, "_cli",
                            lambda *a, _e=stderr, **kw: _ok(returncode=1, stderr=_e))
        state = cloud.AzureAdapter().state(_job(platform="azure"))
        assert state["installed"] is None
        assert len(state["notes"]) == 1
        assert state["notes"][0].startswith("could not read the azure state: ")


def test_azure_state_is_none_when_the_cli_is_absent(monkeypatch):
    def _boom(*a, **kw):
        raise OSError("az is not on PATH")

    monkeypatch.setattr(cloud, "_cli", _boom)
    state = cloud.AzureAdapter().state(_job(platform="azure"))
    assert state["installed"] is None
    assert state["notes"] == ["az is not on PATH"]


# ---- a job named like an error code --------------------------------------

def test_aws_denied_answer_quoting_a_job_named_like_the_marker_is_not_a_not_found(
        monkeypatch):
    """check_name accepts `ResourceNotFoundException` as a job name, and an AccessDenied answer
    quotes the schedule ARN, which ends in that name. The code only counts when the CLI prints
    it in brackets, `(ResourceNotFoundException)`. As a bare word it read a denied query as "not
    installed" and a denied delete as "already gone", so `schedule uninstall` dropped the job
    record while the schedule kept firing."""
    name = "ResourceNotFoundException"
    arn = f"arn:aws:scheduler:eu-west-1:111122223333:schedule/default/contextlake-{name}"
    job = _job(name)

    def denied(operation):
        return _ok(returncode=254, stderr=(
            f"An error occurred (AccessDeniedException) when calling the {operation} "
            "operation: User: arn:aws:sts::111122223333:assumed-role/ops/x is not authorized "
            f"to perform: scheduler:{operation} on resource: {arn} because no "
            "identity-based policy allows it"))

    monkeypatch.setattr(cloud, "_cli", lambda *a, **kw: denied("GetSchedule"))
    state = cloud.AwsAdapter().state(job)
    assert state["installed"] is None
    assert len(state["notes"]) == 1
    assert state["notes"][0].startswith("could not read the aws state: ")

    monkeypatch.setattr(cloud, "_cli", lambda *a, **kw: denied("DeleteSchedule"))
    with pytest.raises(OSError, match=f"delete-schedule for contextlake-{name} failed"):
        cloud.AwsAdapter().uninstall(job)

    # The same name with the service's real not-found answer is still absent.
    monkeypatch.setattr(cloud, "_cli", lambda *a, **kw: _ok(
        returncode=254, stderr="An error occurred (ResourceNotFoundException) when calling "
                               f"the GetSchedule operation: Schedule contextlake-{name} "
                               "does not exist."))
    assert cloud.AwsAdapter().state(job)["installed"] is False
    assert cloud.AwsAdapter().uninstall(job) == []


def test_azure_denied_answer_quoting_a_job_named_like_the_marker_is_not_a_not_found(
        monkeypatch):
    """The Azure twin: `ResourceNotFound` and `ResourceGroupNotFound` are valid job names, and
    an AuthorizationFailed answer quotes the job's resource path."""
    for name in ("ResourceNotFound", "ResourceGroupNotFound"):
        job = _job(name, platform="azure")
        monkeypatch.setattr(cloud, "_cli", lambda *a, _n=name, **kw: _ok(
            returncode=1, stderr=(
                "ERROR: (AuthorizationFailed) The client does not have authorization to "
                "perform action 'Microsoft.App/jobs/read' over scope '/subscriptions/"
                "00000000-0000-0000-0000-000000000000/resourceGroups/contextlake/providers/"
                f"Microsoft.App/jobs/contextlake-{_n}' or the scope is invalid.")))
        state = cloud.AzureAdapter().state(job)
        assert state["installed"] is None
        assert state["notes"][0].startswith("could not read the azure state: ")
        with pytest.raises(OSError,
                           match=f"az containerapp job delete for contextlake-{name} failed"):
            cloud.AzureAdapter().uninstall(job)


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


# ---- a config on the machine that ran install -----------------------------

def _pinned_argv():
    from contextlake.schedule import adapters

    return adapters.exec_argv_for("default", config="/home/me/x.ini",
                                  kb_config="/home/me/kb.toml")


def test_the_aws_container_command_has_no_host_config_path():
    rendered = cloud.AwsAdapter().render(_job(), 3600, _pinned_argv())
    assert "/home/me" not in rendered["schedule.json"]
    command = json.loads(json.loads(rendered["schedule.json"])["Target"]["Input"])[
        "containerOverrides"][0]["command"]
    assert command == ["schedule", "run", "--job", "default"]


def test_the_azure_command_has_no_host_config_path():
    rendered = cloud.AzureAdapter().render(_job(platform="azure"), 3600, _pinned_argv())
    assert "/home/me" not in rendered["az-command"]
    assert "--args" in rendered["az-command"]


def test_azure_install_sends_no_host_config_path(monkeypatch):
    calls = []
    monkeypatch.setattr(cloud, "_cli", lambda *a, **k: calls.append(a) or _ok())
    cloud.AzureAdapter().install(_job(platform="azure"), 3600, _pinned_argv())
    assert "/home/me" not in " ".join(calls[0])


def test_each_cloud_adapter_says_once_that_the_container_reads_its_own_config():
    for adapter, job in ((cloud.AwsAdapter(), _job()),
                         (cloud.AzureAdapter(), _job(platform="azure"))):
        notes = adapter.render(job, 3600, _pinned_argv())["notes"]
        assert sum("Neither is passed to the container" in n for n in notes) == 1


def test_a_job_with_no_config_gets_no_container_config_note():
    for adapter, job in ((cloud.AwsAdapter(), _job()),
                         (cloud.AzureAdapter(), _job(platform="azure"))):
        notes = adapter.render(job, 3600, _argv())["notes"]
        assert not any("Neither is passed" in n for n in notes)
