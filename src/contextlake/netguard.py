"""Offline mode: make "it does not phone home" something you can enforce, not just read.

contextlake is local-first, and the honest way to say so is to let anyone switch the
network off and watch it keep working. `--offline` (or ``CONTEXTLAKE_OFFLINE=1``) does
that from inside the process.

**Where the guarantee is enforced, and why there.** Nine ``urlopen`` call sites exist
across six modules, and connectors, model loaders and future code will add more. A flag
checked at each of them is a promise that decays: the guarantee would hold only for the
sites somebody remembered. So the block sits one layer below all of them, at the socket:
outbound TCP to anything that is not loopback raises :class:`NetworkBlocked`. Every
in-process client inherits it -- ``urllib``, ``requests``, ``httpx``, and the model
downloaders inside the embedding and LLM libraries, which is precisely the code most
likely to reach the network without anyone here writing a call.

**Loopback stays open, deliberately.** "Offline" here means no egress, not no sockets.
The MCP server, the dashboard, the graph viewer and a local Ollama are all loopback
services, and blocking them would make offline mode mean "most features off", which
nobody would turn on.

**The boundary, stated rather than glossed.** This is an in-process guard. A *subprocess*
has its own sockets: ``git fetch``, ``glab``, a spawned model runtime. Those cannot be
stopped from here, so commands that shell out to the network refuse up front instead
(see :func:`refuse`), and that refusal is the enforcement for them. A guard that silently
does not cover half the egress would be worse than none, because it would be believed.
"""

from __future__ import annotations

import os
import socket

OFFLINE_ENV = "CONTEXTLAKE_OFFLINE"

_installed = False


class NetworkBlocked(OSError):
    """Raised instead of connecting, when offline mode is on.

    An ``OSError`` on purpose: callers already handle connection failures, so an
    offline run degrades the way an unplugged cable does rather than crashing with an
    exception nothing expects."""


def offline(args=None) -> bool:
    """Whether offline mode is on, from the flag or the environment.

    The environment is checked at call time rather than cached, so a test (or a shell)
    can turn it on for one command."""
    if args is not None and getattr(args, "offline", False):
        return True
    return os.environ.get(OFFLINE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


_LOCAL_NAMES = frozenset({"localhost", "ip6-localhost", ""})
"""Names, as opposed to addresses. Trusting the resolver for these is a deliberate, small
concession: a hostile ``/etc/hosts`` could aim ``localhost`` elsewhere, and defending
against an attacker who already edits your ``/etc/hosts`` is not what this is for."""


def _is_local(host) -> bool:
    """What counts as "not leaving this machine".

    The address is **parsed**, never prefix-matched. A first version tested
    ``host.startswith("127.")``, which reads as loopback and also accepts
    ``127.example.com`` -- an ordinary remote hostname that would have walked straight
    through the guard. An unanchored string test on something that decides an egress
    question is a hole, not a shortcut.

    Unspecified addresses (``0.0.0.0``, ``::``) are allowed because they appear as *bind*
    addresses rather than destinations, so a server binding one is not mistaken for
    outbound traffic.
    """
    import ipaddress

    if not isinstance(host, str):
        return False
    h = host.strip("[]").lower()
    if h in _LOCAL_NAMES:
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False          # a hostname that is not one of the local names
    return ip.is_loopback or ip.is_unspecified


def install() -> None:
    """Block outbound connections to anything but loopback, process-wide.

    Idempotent, and never uninstalled: a guard you can turn off from inside the process
    it guards is decoration. A test that needs the network back gets a fresh process,
    which is also how the real thing behaves.
    """
    global _installed
    if _installed:
        return
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create = socket.create_connection

    def _check(address):
        host = address[0] if isinstance(address, tuple) and address else address
        if not _is_local(host):
            raise NetworkBlocked(
                f"offline mode: refusing to connect to {host!r}. "
                f"contextlake runs local-first; unset {OFFLINE_ENV} (or drop --offline) "
                "if this command is meant to reach the network.")

    def connect(self, address):
        _check(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        _check(address)
        return real_connect_ex(self, address)

    def create_connection(address, *a, **kw):
        _check(address)
        return real_create(address, *a, **kw)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.create_connection = create_connection
    _installed = True


def refuse(what: str) -> str:
    """The message a command shows when it shells out to something that needs network.

    Subprocesses own their own sockets, so :func:`install` cannot reach them; refusing
    before spawning is what makes offline mode true for ``git fetch`` and friends
    rather than merely mostly true."""
    return (f"offline mode: {what} needs the network, so it was not run. "
            f"Unset {OFFLINE_ENV} (or drop --offline) to allow it.")
