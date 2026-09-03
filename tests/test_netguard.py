"""Offline mode has to be enforced, not asserted, so these tests try to get out.

"Local-first" is a claim, and the only version of it worth printing on a README is one a
user can check. So the tests here are adversarial about the two ways such a guarantee
usually turns out to be false:

* it blocks the call sites somebody remembered, and nothing else -- hence the test that
  goes out through a *library*, not through this package's own helpers;
* it blocks so much that nobody turns it on -- hence the loopback test, since the MCP
  server, the dashboard and a local model runtime are all loopback.

Nothing here opens a real remote connection. The guard raises before `connect`, which is
what makes these tests safe to run on a machine with no network at all.
"""

import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request

import pytest

from contextlake import netguard


@pytest.fixture(autouse=True)
def _no_ambient_offline(monkeypatch):
    """Clear both offline sources before every test in this module.

    Offline state has two sources and both are ambient: the `CONTEXTLAKE_OFFLINE` env
    var the runner may hold, and the one-way `_installed` latch that any earlier test
    driving `cli.main` under that var leaves on for the rest of the pytest worker.

    Measured, with this fixture's body removed: `CONTEXTLAKE_OFFLINE=1` in the
    environment plus `tests/kb/test_llm_providers.py` running first in the same worker
    failed 5 tests here, `test_the_env_var_turns_it_on_without_the_flag` and
    `test_the_flag_turns_it_on_without_the_env_var` among them, on
    `assert not netguard.offline()` returning True.

    The latch is what breaks them, and the env var is what puts it there. `offline()`
    returns True on `_installed` before it reads anything else, so the latch alone is
    enough; measured, `offline()` with the var deleted and `_installed = True` returns
    True. The env var alone is not enough, because both of those tests set and clear
    the var themselves. It reaches them only through `cli.main`, which installs the
    latch when it sees the var.

    Autouse and module-level so the next test added here inherits the precondition
    instead of having to remember it."""
    monkeypatch.delenv(netguard.OFFLINE_ENV, raising=False)
    monkeypatch.setattr(netguard, "_installed", False)


@pytest.fixture
def guarded(monkeypatch):
    """Install the guard with the module's install-once latch reset.

    `install()` is deliberately one-way in production -- a guard you can lift from
    inside the process it guards is decoration -- so a test has to reset the latch and
    restore the real socket functions itself."""
    monkeypatch.setattr(netguard, "_installed", False)
    monkeypatch.setattr(socket.socket, "connect", socket.socket.connect)
    monkeypatch.setattr(socket.socket, "connect_ex", socket.socket.connect_ex)
    monkeypatch.setattr(socket, "create_connection", socket.create_connection)
    netguard.install()
    yield
    netguard._installed = False


def test_an_outbound_connection_is_refused_before_it_is_attempted(guarded):
    s = socket.socket()
    try:
        with pytest.raises(netguard.NetworkBlocked):
            s.connect(("93.184.216.34", 443))
    finally:
        s.close()


def test_a_library_that_never_heard_of_this_package_is_also_blocked(guarded):
    """The test that makes the guarantee worth stating. urllib is a stand-in for every
    client whose requests this package does not write: the embedding model's downloader,
    an LLM SDK, a future connector. A flag checked at our own call sites would let all
    of them straight through."""
    with pytest.raises(urllib.error.URLError) as e:
        urllib.request.urlopen("https://example.com", timeout=1)
    assert isinstance(e.value.reason, netguard.NetworkBlocked)


def test_loopback_is_left_alone(guarded):
    """Offline means no egress, not no sockets. Port 1 on localhost has nothing
    listening, so reaching the OS and being refused is the pass condition -- the failure
    condition is NetworkBlocked."""
    s = socket.socket()
    s.settimeout(0.5)
    try:
        with pytest.raises(ConnectionRefusedError):
            s.connect(("127.0.0.1", 1))
    finally:
        s.close()


@pytest.mark.parametrize("host", ["127.0.0.1", "127.10.0.9", "localhost", "::1", "", "0.0.0.0"])
def test_every_form_of_local_is_recognised(host):
    """A guard that only knows the string "localhost" breaks a server bound to
    127.0.0.2 or an IPv6 loopback client."""
    assert netguard._is_local(host)


@pytest.mark.parametrize("host", ["example.com", "1.1.1.1", "10.0.0.5", "127.example.com"])
def test_what_is_not_local_is_not_local(host):
    """`127.example.com` is the interesting one: a hostname that merely starts with the
    loopback prefix is a remote host, and a substring check would wave it through -- the
    same anchoring bug this project has fixed elsewhere."""
    assert not netguard._is_local(host)


def test_create_connection_is_covered_too(guarded):
    """`socket.create_connection` is a separate entry point, and it is the one
    `http.client` actually uses -- patching only `socket.connect` would leave every HTTP
    library working normally while the guard reported itself installed."""
    with pytest.raises(netguard.NetworkBlocked):
        socket.create_connection(("example.com", 80), timeout=1)


def test_installing_twice_does_not_stack_wrappers(guarded):
    """Without the latch each install would wrap the previous wrapper, so a long-lived
    process re-entering setup pays a growing chain per connection."""
    before = socket.socket.connect
    netguard.install()
    assert socket.socket.connect is before


def test_the_env_var_turns_it_on_without_the_flag(monkeypatch):
    monkeypatch.setenv(netguard.OFFLINE_ENV, "1")
    assert netguard.offline()
    monkeypatch.setenv(netguard.OFFLINE_ENV, "no")
    assert not netguard.offline()
    monkeypatch.delenv(netguard.OFFLINE_ENV)
    assert not netguard.offline()


def test_the_flag_turns_it_on_without_the_env_var(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.delenv(netguard.OFFLINE_ENV, raising=False)
    assert netguard.offline(SimpleNamespace(offline=True))
    assert not netguard.offline(SimpleNamespace(offline=False))
    # A namespace from a command that has no such flag must not read as offline.
    assert not netguard.offline(SimpleNamespace())


def test_the_installed_guard_is_visible_to_code_with_no_args(monkeypatch):
    """`build_llm()` sits several frames below `cli.main` and has no argparse namespace,
    so `--offline` has to survive as process state or it reaches nothing.

    Asserted on the latch itself, with no fixture: the `guarded` fixture installs the
    guard as a side effect of patching sockets, so a test written through it would pass
    on two grounds at once and a later edit to that fixture would move what this
    proves. Both halves are here so the first assertion can never go vacuous."""
    monkeypatch.delenv(netguard.OFFLINE_ENV, raising=False)
    monkeypatch.setattr(netguard, "_installed", False)
    assert not netguard.offline()
    monkeypatch.setattr(netguard, "_installed", True)
    assert netguard.offline()


def test_the_latch_overrides_an_args_namespace_that_says_the_flag_is_off(monkeypatch):
    """The precondition `bootstrap` decides its stage list under.

    `cli.main` installs the guard whenever `CONTEXTLAKE_OFFLINE` is set, then hands one
    argparse namespace to every stage. That namespace carries `offline = False` on an
    env-var run, because nobody typed the flag. `bootstrap` asks `netguard.offline(args)`
    and skips its mirror stage on True, so the latch is what changes the stage list.

    Different from `test_the_installed_guard_is_visible_to_code_with_no_args`, which
    passes no namespace: the latch is read BEFORE the namespace, and a version written
    as `if args is None and _installed` would pass that test and fail this one.

    Both halves are here so the True assertion cannot go vacuous."""
    from types import SimpleNamespace

    monkeypatch.delenv(netguard.OFFLINE_ENV, raising=False)
    args = SimpleNamespace(offline=False)

    monkeypatch.setattr(netguard, "_installed", False)
    assert not netguard.offline(args)

    monkeypatch.setattr(netguard, "_installed", True)
    assert netguard.offline(args)


def _child_says_offline(env) -> bool:
    """Ask a real child process, with the env it would be spawned with, what it thinks.

    Reading the dict would pass for a value the child never acts on. This runs the same
    interpreter the spawn sites run and reads its answer. `env=None` means "inherit",
    which is what an unfixed spawn site produces."""
    probe = "from contextlake import netguard; print(netguard.offline())"
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                       timeout=120, env=env)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip() == "True"


def test_a_child_spawned_with_child_env_inherits_offline_mode(monkeypatch):
    """The spawn boundary, proved on the CHILD rather than on the parent's call.

    The latch is the discriminating precondition. `CONTEXTLAKE_OFFLINE=1` already rides
    across inside `dict(os.environ)`, so a version of this written through the env var
    would pass with `child_env` reduced to `dict(os.environ)` and prove nothing.
    `--offline` as a flag sets only the latch, and translating the latch into something
    a child can read is the whole job.

    Both halves are here so the offline assertion cannot go vacuous: an online parent
    has to produce an online child."""
    assert not _child_says_offline(netguard.child_env())

    monkeypatch.setattr(netguard, "_installed", True)
    assert _child_says_offline(netguard.child_env())


def test_child_env_does_not_write_to_this_process_environment(monkeypatch):
    """The returned dict is the child's, not ours. Mutating `os.environ` here would turn
    one spawn into a process-wide latch that no later caller asked for."""
    monkeypatch.setattr(netguard, "_installed", True)

    env = netguard.child_env()

    assert env[netguard.OFFLINE_ENV] == "1"
    assert netguard.OFFLINE_ENV not in os.environ


def test_child_env_leaves_an_online_run_alone():
    """Offline mode off means the child gets the environment it always had. A spawn
    that set the var unconditionally would make every run offline."""
    env = netguard.child_env()

    assert netguard.OFFLINE_ENV not in env
    assert env["PATH"] == os.environ["PATH"]


def test_the_subprocess_boundary_is_stated_rather_than_pretended():
    """The honest half. `git fetch` has its own sockets and this guard cannot touch
    them, so the refusal message is the enforcement there -- and it names the escape
    hatch, because a block with no way out gets worked around instead of used."""
    msg = netguard.refuse("`mirror fetch`")
    assert "mirror fetch" in msg
    assert netguard.OFFLINE_ENV in msg
