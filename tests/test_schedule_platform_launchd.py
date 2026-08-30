"""The macOS LaunchAgent adapter.

NOT RUNNABLE HERE. This machine is Linux, so nothing below executes launchctl.
Every test asserts either the rendered plist or the exact argv the adapter
would run. That is a real contract and a weaker one than execution, and saying
so is the point: this backend is verified by rendering, not by running.
"""
from __future__ import annotations

import plistlib

import pytest

from contextlake.schedule import jobs as jobstore
from contextlake.schedule.platform import launchd


def _job(name="default"):
    return jobstore.new_job(name, ["bootstrap"], "auto", "launchd")


def _argv():
    return ["/venv/bin/python", "-m", "contextlake", "bootstrap"]


def _rendered(interval_s=4200):
    return launchd.LaunchdAdapter().render(_job(), interval_s, _argv())


# ---- render --------------------------------------------------------------

def test_the_plist_carries_the_interval_in_whole_seconds():
    """StartInterval is SECONDS as an integer. launchd rejects a float and
    ignores a unit suffix, so 70 minutes is 4200 and never "70m" or 70."""
    doc = plistlib.loads(_rendered(4200)[launchd.plist_name("default")].encode())

    assert doc["StartInterval"] == 4200
    assert isinstance(doc["StartInterval"], int)


def test_a_fractional_interval_is_rounded_to_a_whole_second_and_never_zero():
    """A sub-second interval must not round to 0: launchd treats 0 as "run
    constantly", which turns a scheduling bug into a busy loop."""
    doc = plistlib.loads(_rendered(0.2)[launchd.plist_name("default")].encode())
    assert doc["StartInterval"] == 1

    doc = plistlib.loads(_rendered(4200.6)[launchd.plist_name("default")].encode())
    assert doc["StartInterval"] == 4201


def test_the_plist_runs_the_exec_argv_it_was_given():
    doc = plistlib.loads(_rendered()[launchd.plist_name("default")].encode())
    assert doc["ProgramArguments"] == _argv()
    assert doc["Label"] == "in.sayak.contextlake.default"
    # RunAtLoad false: installing a schedule must not fire a run immediately.
    assert doc["RunAtLoad"] is False


def test_the_rendered_plist_is_valid_plist_xml():
    """Parsed, not string-matched. A plist that merely CONTAINS the right
    substrings can still be malformed, and launchd would reject it at load
    time on a machine this suite cannot reach."""
    text = _rendered()[launchd.plist_name("default")]
    assert plistlib.loads(text.encode())["Label"].startswith(launchd.LABEL_PREFIX)


def test_the_label_is_always_prefixed():
    """One rule, no conditional. A prefix applied only to names that lack it
    gives two naming rules that both pass their own tests and collide in the
    shared launchd namespace, which is the defect systemd.unit_name records."""
    assert launchd.label("default") == "in.sayak.contextlake.default"
    assert launchd.label("nightly") == "in.sayak.contextlake.nightly"


def test_metadata_keys_are_not_offered_as_files_to_write():
    """The degrade path prints every non-metadata key as a file. `label` and
    `interval_s` are facts, so a section headed `----- label -----` holding a
    string would be wrong."""
    rendered = _rendered()
    adapter = launchd.LaunchdAdapter()
    artefacts = set(rendered) - adapter.metadata_keys
    assert artefacts == {launchd.plist_name("default")}


# ---- install: assert the argv, because it cannot run here ----------------

def test_install_bootstraps_into_the_gui_domain_after_booting_out(tmp_path, monkeypatch):
    """`bootstrap gui/$UID`, not the deprecated `load`, which can return 0
    while doing nothing under a modern launchd.

    The bootout first is what makes a re-install replace the agent rather than
    layer a second one.
    """
    calls = []
    monkeypatch.setattr(launchd, "agent_dir", lambda: str(tmp_path))
    monkeypatch.setattr(launchd, "_launchctl",
                        lambda *a: calls.append(a) or _ok())

    written = launchd.LaunchdAdapter().install(_job(), 4200, _argv())

    assert written == [str(tmp_path / launchd.plist_name("default"))]
    assert [c[0] for c in calls] == ["bootout", "bootstrap"]
    assert calls[1][1].startswith("gui/"), calls[1]
    assert calls[1][2] == written[0]


def test_install_raises_when_launchctl_refuses(tmp_path, monkeypatch):
    """A plist on disk with no loaded agent is the silent-failure state this
    adapter exists to rule out. cmd_install degrades on OSError by printing
    the plist, so raising is what produces a useful message rather than a
    schedule that never fires."""
    monkeypatch.setattr(launchd, "agent_dir", lambda: str(tmp_path))

    def _refuse(*a):
        return _ok(returncode=0) if a[0] == "bootout" else _ok(returncode=1, stderr="denied")

    monkeypatch.setattr(launchd, "_launchctl", _refuse)

    with pytest.raises(OSError, match="denied"):
        launchd.LaunchdAdapter().install(_job(), 4200, _argv())

    # The file stays: it matches what the degrade path prints, and loading it
    # by hand later is a valid recovery.
    assert (tmp_path / launchd.plist_name("default")).exists()


def test_uninstall_boots_out_and_removes_the_plist(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(launchd, "agent_dir", lambda: str(tmp_path))
    monkeypatch.setattr(launchd, "_launchctl", lambda *a: calls.append(a) or _ok())
    path = tmp_path / launchd.plist_name("default")
    path.write_text("x", encoding="utf-8")

    assert launchd.LaunchdAdapter().uninstall(_job()) == [str(path)]
    assert calls[0][0] == "bootout"
    assert not path.exists()


def test_uninstalling_what_is_not_there_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(launchd, "agent_dir", lambda: str(tmp_path))
    monkeypatch.setattr(launchd, "_launchctl", lambda *a: _ok())
    assert launchd.LaunchdAdapter().uninstall(_job()) == []


# ---- state and enumeration ----------------------------------------------

def test_state_reads_the_interval_back_off_the_installed_plist(tmp_path, monkeypatch):
    """Read back, not reported from the request. The two differ whenever an
    install rounded, and reporting the request would hide the rounding."""
    monkeypatch.setattr(launchd, "agent_dir", lambda: str(tmp_path))
    (tmp_path / launchd.plist_name("default")).write_bytes(plistlib.dumps({
        "Label": launchd.label("default"),
        "ProgramArguments": ["/other/python", "-m", "contextlake", "bootstrap"],
        "StartInterval": 900,
    }))

    state = launchd.LaunchdAdapter().state(_job())

    assert state["installed"] is True
    assert state["interval_s"] == 900.0
    assert state["exec_path"] == "/other/python"
    # launchd exposes no next-fire time for a StartInterval agent. None is
    # "cannot tell", which beats a computed guess that drifts from reality.
    assert state["next_run"] is None


def test_state_says_not_installed_without_inventing_the_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(launchd, "agent_dir", lambda: str(tmp_path))
    state = launchd.LaunchdAdapter().state(_job())
    assert state["installed"] is False
    assert state["interval_s"] is None
    assert state["exec_path"] is None


def test_state_notes_a_plist_it_cannot_parse_rather_than_claiming_nothing(tmp_path, monkeypatch):
    """A corrupt plist is installed-but-unreadable, which is not the same as
    not installed. Reporting False here would tell the user to install a
    schedule that is already there."""
    monkeypatch.setattr(launchd, "agent_dir", lambda: str(tmp_path))
    (tmp_path / launchd.plist_name("default")).write_text("not a plist", encoding="utf-8")

    state = launchd.LaunchdAdapter().state(_job())

    assert state["installed"] is True
    assert state["interval_s"] is None
    assert state["notes"], "an unreadable plist must be reported, not swallowed"


def test_installed_names_lists_agents_and_claims_nothing_else(tmp_path, monkeypatch):
    monkeypatch.setattr(launchd, "agent_dir", lambda: str(tmp_path))
    (tmp_path / launchd.plist_name("default")).write_text("x", encoding="utf-8")
    (tmp_path / launchd.plist_name("nightly")).write_text("x", encoding="utf-8")
    # Somebody else's agent, and a stray file, must not be claimed.
    (tmp_path / "com.apple.something.plist").write_text("x", encoding="utf-8")
    (tmp_path / "in.sayak.contextlake.notes.txt").write_text("x", encoding="utf-8")

    assert launchd.LaunchdAdapter().installed_names() == ["default", "nightly"]


def test_installed_names_separates_no_directory_from_cannot_read(tmp_path, monkeypatch):
    """[] is a measurement, None is the absence of one. cmd_list reports the
    second as an unchecked platform rather than as a clean result."""
    monkeypatch.setattr(launchd, "agent_dir", lambda: str(tmp_path / "nope"))
    assert launchd.LaunchdAdapter().installed_names() == []

    monkeypatch.setattr(launchd, "agent_dir", lambda: str(tmp_path))
    def _boom(_p):
        raise OSError("permission denied")
    monkeypatch.setattr(launchd.os, "listdir", _boom)
    assert launchd.LaunchdAdapter().installed_names() is None


def test_usable_is_false_on_this_machine():
    """Honest about the platform. usable() gates install; render and the tests
    above work anywhere, which is what makes this backend assertable here
    without being runnable."""
    import sys

    assert (launchd.LaunchdAdapter().usable() is False) or sys.platform == "darwin"


def _ok(returncode=0, stdout="", stderr=""):
    import subprocess
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)
