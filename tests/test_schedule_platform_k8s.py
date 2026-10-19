"""The Kubernetes / OpenShift CronJob adapter.

NOT RUNNABLE HERE. There is no cluster, so nothing below applies a manifest.
The manifest is PARSED with a real YAML loader rather than string-matched: a
document that merely contains the right substrings can still be malformed, and
the cluster would reject it somewhere this suite cannot reach.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from contextlake.schedule import jobs as jobstore
from contextlake.schedule.platform import k8s

# The dev extra provides PyYAML. Parsing is what makes these assertions mean
# "the cluster would accept this shape" rather than "this string appears".
yaml = pytest.importorskip("yaml")


def _job(name="default"):
    return jobstore.new_job(name, ["bootstrap"], "auto", "k8s")


def _argv():
    return ["/venv/bin/python", "-m", "contextlake", "bootstrap"]


def _ok(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def _doc(interval_s=3600, **kw):
    rendered = k8s.K8sAdapter().render(_job(), interval_s, _argv(), **kw)
    return yaml.safe_load(rendered["cronjob.yaml"])


# ---- the manifest --------------------------------------------------------

def test_the_manifest_is_valid_yaml_and_is_a_cronjob():
    doc = _doc()
    assert doc["kind"] == "CronJob"
    assert doc["apiVersion"] == "batch/v1"
    assert doc["metadata"]["name"] == "contextlake-default"


def test_concurrency_is_forbidden_so_two_runs_cannot_overlap():
    """Forbid is what gives single-writer semantics from the cluster. Allow
    would let a second run start while the first still holds the store, which
    is the corruption the advisory lock exists to prevent."""
    assert _doc()["spec"]["concurrencyPolicy"] == "Forbid"


def test_the_pod_runs_unprivileged_and_pins_no_uid():
    """OpenShift's restricted SCC assigns an arbitrary UID and REJECTS a
    pinned one. A manifest with runAsUser works on vanilla Kubernetes and
    fails on OpenShift, which is the split this adapter exists to avoid.
    """
    pod = _doc()["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    security = pod["securityContext"]

    assert security["runAsNonRoot"] is True
    assert "runAsUser" not in security, "a pinned UID is rejected by OpenShift"
    # fsGroup so the mounted state dir is writable by whatever UID is assigned.
    assert "fsGroup" in security

    container = pod["containers"][0]["securityContext"]
    assert container["allowPrivilegeEscalation"] is False
    assert container["capabilities"]["drop"] == ["ALL"]


def test_the_state_volume_is_a_claim_not_an_emptydir():
    """An emptyDir is ephemeral, and an ephemeral store re-indexes the whole
    fleet every run instead of doing an incremental pass. `schedule run`
    already refuses that without --allow-ephemeral; rendering one here would
    ship a manifest whose every run is refused."""
    pod = _doc()["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    volume = pod["volumes"][0]

    assert "persistentVolumeClaim" in volume
    assert "emptyDir" not in volume
    assert pod["containers"][0]["volumeMounts"][0]["mountPath"] == "/work"


def test_the_claim_is_mounted_where_the_image_keeps_its_state():
    """The image sets HOME=/work (Dockerfile), and the store, config and caches resolve
    under it. The volume was mounted at /var/lib/contextlake, which nothing writes to, so
    the state stayed on the container layer and `store_is_ephemeral` refused every run."""
    import re

    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
    home = re.search(r"^ENV HOME=(\S+)", dockerfile, re.M).group(1)
    pod = _doc()["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["containers"][0]["volumeMounts"][0]["mountPath"] == home


def test_the_claim_the_pod_needs_is_named_as_a_prerequisite():
    """Only a CronJob is applied, so nothing creates the claim and the pod stays Pending."""
    rendered = k8s.K8sAdapter().render(_job(), 3600, _argv(), namespace="team-a")
    claim = yaml.safe_load(rendered["cronjob.yaml"])["spec"]["jobTemplate"]["spec"][
        "template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"]
    note = " ".join(rendered["notes"])
    assert claim in note and "team-a" in note and "before the first run" in note


def test_the_container_args_drop_the_callers_interpreter():
    """exec_argv starts with the interpreter path on the machine that ran
    install. That path means nothing inside the image, so carrying it over
    would produce a CronJob that cannot start."""
    args = _doc()["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["args"]
    assert args == ["bootstrap"], args
    assert not any("/venv" in a for a in args)


def test_the_container_args_come_from_the_real_producer_not_one_fixture():
    """Driven by `adapters.exec_argv_for`, the function that actually builds
    the unit's argv, rather than only by this file's synthetic one. A helper
    tested against a single hand-written shape passes while disagreeing with
    the shape the product really produces.
    """
    from contextlake.schedule import adapters

    real = adapters.exec_argv_for("nightly")
    rendered = k8s.K8sAdapter().render(_job("nightly"), 3600, real)
    doc = yaml.safe_load(rendered["cronjob.yaml"])
    args = doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["args"]

    assert args == ["schedule", "run", "--job", "nightly"], args
    # No interpreter path and no -m survive into the image.
    assert "-m" not in args
    assert not any(a.endswith(("python", "python3")) or "/" in a for a in args)


def test_the_container_args_do_not_sniff_the_interpreter_name():
    """An interpreter can be named anything: python3.12, uv, a wrapper script,
    a bare venv path. Splitting on the literal package name is what makes this
    independent of that."""
    for interpreter in ("/opt/uv", "/usr/bin/python3.12", "/x/my-wrapper"):
        args = k8s._container_args([interpreter, "-m", "contextlake", "bootstrap"])
        assert args == ["bootstrap"], (interpreter, args)


def test_a_namespace_and_image_can_be_overridden():
    doc = _doc(namespace="contextlake-system", image="example.test/contextlake:1.2.3")
    assert doc["metadata"]["namespace"] == "contextlake-system"
    container = doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "example.test/contextlake:1.2.3"


# ---- the schedule expression --------------------------------------------

def test_the_schedule_is_a_cron_expression_and_rounds_like_cron():
    """A CronJob schedule IS a cron expression, so it inherits the same
    inexpressibility, and it must round through the SAME function the cron
    adapter uses. Rounding differently would give one request two intervals
    depending on the backend.

    70 minutes is the case that shows the difference from Task Scheduler:
    `/MO 70` is valid there, but cron's minute field divides the hour, so
    `*/70` is not a thing and 70 minutes falls back to hourly. Asserting 4200
    here (the Windows answer) would have been asserting the wrong contract.
    """
    from contextlake.schedule.platform import cron

    rendered = k8s.K8sAdapter().render(_job(), 4259, _argv())
    expected_s, expected_spec = cron.nearest_expressible(4259)

    assert rendered["interval_s"] == expected_s
    assert rendered["schedule"] == expected_spec
    assert any("cron expression" in n for n in rendered["notes"])

    exact = k8s.K8sAdapter().render(_job(), 3600, _argv())
    assert not any("cron expression" in n for n in exact["notes"]), (
        "no rounding note when nothing was rounded")
    assert yaml.safe_load(exact["cronjob.yaml"])["spec"]["schedule"] == exact["schedule"]


# ---- apply and delete: assert the argv ----------------------------------

def test_install_sends_the_manifest_on_stdin_not_just_the_right_argv(monkeypatch):
    """`apply -f -` means READ FROM STDIN. Without input= the client gets an
    empty stream and applies nothing while the argv still looks correct, which
    is why asserting the argv alone is not enough: that assertion passes on a
    call that applies an empty document.
    """
    calls = []
    monkeypatch.setattr(k8s, "_run",
                        lambda *a, **kw: calls.append((a, kw)) or _ok())

    written = k8s.K8sAdapter().install(_job(), 3600, _argv(), namespace="ns1")

    assert written == ["cronjob/contextlake-default"]
    argv, kwargs = calls[0]
    assert argv[0] == "apply"
    assert list(argv)[list(argv).index("-n") + 1] == "ns1"
    assert "-f" in argv and "-" in argv

    # The manifest itself reaches the client, and it is the SAME document
    # render produced rather than something rebuilt on the way.
    sent = kwargs.get("input")
    assert sent, "the manifest was never sent to the cluster client"
    assert yaml.safe_load(sent)["kind"] == "CronJob"
    assert sent == k8s.K8sAdapter().render(
        _job(), 3600, _argv(), namespace="ns1")["cronjob.yaml"]


def test_install_raises_when_the_cluster_refuses(monkeypatch):
    """A refused apply must not read as an installed schedule. cmd_install
    degrades on OSError by printing the manifest to apply by hand."""
    monkeypatch.setattr(k8s, "_run",
                        lambda *a, **kw: _ok(returncode=1, stderr="forbidden"))
    with pytest.raises(OSError, match="forbidden"):
        k8s.K8sAdapter().install(_job(), 3600, _argv())


def test_install_degrades_when_no_client_is_on_path(monkeypatch):
    """No kubectl and no oc is a degrade, not a crash: the manifest still
    renders and someone can apply it. The OSError is what cmd_install catches
    to print it."""
    monkeypatch.setattr(k8s, "_client", lambda: None)
    with pytest.raises(OSError, match="neither kubectl nor oc"):
        k8s.K8sAdapter().install(_job(), 3600, _argv())

    # Rendering still works with no client at all, which is the whole point.
    assert k8s.K8sAdapter().render(_job(), 3600, _argv())["cronjob.yaml"]


def test_uninstall_reports_a_removal_only_when_something_was_removed(monkeypatch):
    """Every other adapter returns [] when there was nothing to remove:
    systemd when no file was unlinked, cron when the crontab text did not
    change, Windows on a non-zero exit. k8s must match, which rules out
    `--ignore-not-found`: that exits 0 whether or not anything matched, so
    uninstall would claim to have removed a CronJob that never existed.
    """
    calls = []
    monkeypatch.setattr(k8s, "_run", lambda *a, **kw: calls.append(a) or _ok())
    assert k8s.K8sAdapter().uninstall(_job()) == ["cronjob/contextlake-default"]
    assert "--ignore-not-found" not in calls[0], (
        "--ignore-not-found makes a missing resource indistinguishable from a "
        "removed one")

    # Already gone: not an error, and not a removal either.
    monkeypatch.setattr(k8s, "_run", lambda *a, **kw: _ok(
        returncode=1,
        stderr='Error from server (NotFound): cronjobs.batch "contextlake-default" not found'))
    assert k8s.K8sAdapter().uninstall(_job()) == []


def test_uninstall_raises_when_the_cluster_could_not_be_asked(monkeypatch):
    """A dead route, expired credentials and a Forbidden all exit non-zero, like a missing
    CronJob does. Only NotFound means gone. Read as gone, `schedule uninstall` deleted the job
    record and printed "Removed job" while the CronJob kept firing."""
    for stderr in ("Unable to connect to the server: dial tcp: lookup api.example.invalid: "
                   "no such host",
                   'Error from server (Forbidden): cronjobs.batch "contextlake-default" is '
                   "forbidden: User cannot delete resource"):
        monkeypatch.setattr(k8s, "_run", lambda *a, _e=stderr, **kw: _ok(returncode=1, stderr=_e))
        with pytest.raises(OSError, match="kubectl delete for contextlake-default failed"):
            k8s.K8sAdapter().uninstall(_job())


def test_every_query_names_the_namespace_that_install_applied_to(monkeypatch):
    """`apply -n default` put the CronJob there. delete and get without -n use the current
    context's namespace, so on a context pointing elsewhere uninstall, status and list
    never found it."""
    calls = []
    monkeypatch.setattr(k8s, "_run", lambda *a, **kw: calls.append(a) or _ok())
    adapter = k8s.K8sAdapter()
    adapter.uninstall(_job())
    adapter.state(_job())
    adapter.installed_names()
    assert len(calls) == 3
    for argv in calls:
        assert list(argv)[list(argv).index("-n") + 1] == k8s.DEFAULT_NAMESPACE, argv


def test_the_client_prefers_kubectl_then_falls_back_to_oc(monkeypatch):
    monkeypatch.setattr(k8s.shutil, "which",
                        lambda c: "/usr/bin/kubectl" if c == "kubectl" else None)
    assert k8s._client() == "kubectl"

    monkeypatch.setattr(k8s.shutil, "which", lambda c: "/usr/bin/oc" if c == "oc" else None)
    assert k8s._client() == "oc"

    monkeypatch.setattr(k8s.shutil, "which", lambda _c: None)
    assert k8s._client() is None


# ---- enumeration and state ----------------------------------------------

def test_installed_names_selects_by_label_not_by_name_prefix(monkeypatch):
    """The label is what the manifest sets. A name-prefix match would also
    claim a CronJob somebody else happened to call contextlake-something."""
    seen = {}

    def _query(*argv, **kw):
        seen["argv"] = argv
        return _ok(stdout="default nightly\n")

    monkeypatch.setattr(k8s, "_run", _query)

    assert k8s.K8sAdapter().installed_names() == ["default", "nightly"]
    assert "-l" in seen["argv"]
    assert any("app.kubernetes.io/name=contextlake" in a for a in seen["argv"])


def test_installed_names_is_none_when_there_is_no_client_or_the_query_fails(monkeypatch):
    """None is "cannot tell", and cmd_list reports it as an unchecked platform
    rather than as a clean result. An empty cluster answer IS a measurement and
    stays an empty list."""
    def _boom(*a, **kw):
        raise OSError("neither kubectl nor oc is on PATH")

    monkeypatch.setattr(k8s, "_run", _boom)
    assert k8s.K8sAdapter().installed_names() is None

    monkeypatch.setattr(k8s, "_run", lambda *a, **kw: _ok(returncode=1))
    assert k8s.K8sAdapter().installed_names() is None

    monkeypatch.setattr(k8s, "_run", lambda *a, **kw: _ok(stdout="  \n"))
    assert k8s.K8sAdapter().installed_names() == []


def test_state_does_not_report_the_last_run_as_the_next_one(monkeypatch):
    """A CronJob reports lastScheduleTime, not a next-fire time. Putting the
    last one in next_run would be actively wrong, so next_run stays None and
    the fact goes in a note."""
    monkeypatch.setattr(k8s, "_run",
                        lambda *a, **kw: _ok(stdout="0 * * * *|2026-08-28T00:00:00Z"))

    state = k8s.K8sAdapter().state(_job())

    assert state["installed"] is True
    assert state["next_run"] is None
    assert any("last scheduled" in n for n in state["notes"])
    assert any("0 * * * *" in n for n in state["notes"])


def test_state_says_nothing_is_patched_in_the_cluster_on_its_own(monkeypatch):
    """Auto-adjust does not reach into a cluster. Rewriting a CronJob in the
    background would need cluster-write RBAC for the life of the schedule,
    which is more authority than the benefit is worth, so status says how an
    interval actually changes."""
    monkeypatch.setattr(k8s, "_run", lambda *a, **kw: _ok(stdout="0 * * * *|"))
    notes = " ".join(k8s.K8sAdapter().state(_job())["notes"])
    assert "schedule install" in notes
    assert "nothing is patched" in notes


def test_state_reports_not_installed_without_inventing_the_rest(monkeypatch):
    """Only a NotFound answer from the API proves the CronJob is absent."""
    monkeypatch.setattr(k8s, "_run", lambda *a, **kw: _ok(
        returncode=1, stderr='Error from server (NotFound): cronjobs.batch '
                             '"contextlake-default" not found'))
    state = k8s.K8sAdapter().state(_job())
    assert state["installed"] is False
    assert state["next_run"] is None
    assert state["exec_path"] is None
    assert state["notes"] == []


def test_state_is_none_when_the_cluster_cannot_be_asked(monkeypatch):
    """No route to the cluster, an expired login and a Forbidden all exit non-zero too. Read
    as "not installed", `status` told the user to re-run `schedule install`, which needs the
    same access and fails the same way. None is "cannot tell", with the error in a note."""
    for stderr, shown in (
            ("Unable to connect to the server: dial tcp 10.0.0.1:443: i/o timeout\n",
             "Unable to connect to the server"),
            ("Error from server (Forbidden): cronjobs.batch is forbidden: User cannot get\n",
             "(Forbidden)"),
            ("", "no output")):
        monkeypatch.setattr(k8s, "_run",
                            lambda *a, _e=stderr, **kw: _ok(returncode=1, stderr=_e))
        state = k8s.K8sAdapter().state(_job())
        assert state["installed"] is None
        assert len(state["notes"]) == 1
        assert state["notes"][0].startswith("could not read the k8s state: ")
        assert shown in state["notes"][0]


def test_a_forbidden_answer_quoting_a_job_named_notfound_is_not_a_not_found(monkeypatch):
    """`NotFound` is a valid job name. Only the bracketed `(NotFound)` the API prints counts."""
    job = _job("NotFound")
    monkeypatch.setattr(k8s, "_run", lambda *a, **kw: _ok(
        returncode=1, stderr='Error from server (Forbidden): cronjobs.batch '
                             '"contextlake-NotFound" is forbidden: User cannot get resource'))
    assert k8s.K8sAdapter().state(job)["installed"] is None
    with pytest.raises(OSError, match="kubectl delete for contextlake-NotFound failed"):
        k8s.K8sAdapter().uninstall(job)


def test_state_is_none_when_there_is_no_client(monkeypatch):
    def _boom(*a, **kw):
        raise OSError("neither kubectl nor oc is on PATH")

    monkeypatch.setattr(k8s, "_run", _boom)
    state = k8s.K8sAdapter().state(_job())
    assert state["installed"] is None
    assert state["notes"] == ["neither kubectl nor oc is on PATH"]
