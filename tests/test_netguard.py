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

import socket
import urllib.error
import urllib.request

import pytest

from contextlake import netguard


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


def test_the_subprocess_boundary_is_stated_rather_than_pretended():
    """The honest half. `git fetch` has its own sockets and this guard cannot touch
    them, so the refusal message is the enforcement there -- and it names the escape
    hatch, because a block with no way out gets worked around instead of used."""
    msg = netguard.refuse("`mirror fetch`")
    assert "mirror fetch" in msg
    assert netguard.OFFLINE_ENV in msg
