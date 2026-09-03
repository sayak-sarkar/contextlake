"""Offline mode: make "it does not phone home" something you can enforce, not just read.

contextlake is local-first, and the honest way to say so is to let anyone switch the
network off and watch it keep working. `--offline` (or ``CONTEXTLAKE_OFFLINE=1``) does
that from inside the process.

**Where the guarantee is enforced, and why there.** ``urlopen`` call sites are spread
across the source modules, and connectors, model loaders and future code will add more.
A flag checked at each of them is a promise that decays: the guarantee would hold for
the sites somebody remembered and nowhere else. So the block sits one layer below all of
them, at the socket: outbound TCP to anything that is not loopback raises
:class:`NetworkBlocked`. Every in-process client inherits it -- ``urllib``, ``requests``,
``httpx``, and the model downloaders inside the embedding and LLM libraries, which is the
code most likely to reach the network without anyone here writing a call.

**Loopback stays open, deliberately.** "Offline" here means no egress, not no sockets.
The MCP server, the dashboard, the graph viewer and a local Ollama are all loopback
services, and blocking them would make offline mode mean "most features off", which
nobody would turn on.

**The boundary, stated rather than glossed.** This is an in-process guard. A *subprocess*
has its own sockets, so :func:`install` cannot reach one. Three things happen at that
boundary, and the third is that nothing happens:

1. A command that shells out to a third-party network tool refuses before the spawn
   (see :func:`refuse`), and that refusal is the enforcement for it. ``mirror fetch``,
   ``clone``, ``update``, ``branches`` and ``sync`` take this route, and ``bootstrap``
   skips its mirror stage the same way.
2. A command that spawns **contextlake itself** hands the child :func:`child_env`, which
   sets ``CONTEXTLAKE_OFFLINE`` so the child installs its own guard. Three sites do
   this: ``kb/dashboard/mutations.py`` (``mcp_start``, ``wiki_generate_start``) and
   ``kb/cmds/refresh.py`` (``_spawn_refresh``).
3. The rest: spawn sites that take no offline signal. They are named below, because a
   gap nobody wrote down is a gap nobody closes.

**Spawn sites offline mode does not reach.** This list is open, not closed. It was
audited on 2026-09-03 and a spawn site added after that date belongs on it until someone
routes it through 1 or 2 above. Three reasons, and each is why the fix is not one line:

*The MCP SDK replaces the child environment.* ``mcp.client.stdio`` builds the child's
environment from a six-name whitelist (``HOME``, ``LOGNAME``, ``PATH``, ``SHELL``,
``TERM``, ``USER``) merged with the source's own ``env``, so ``CONTEXTLAKE_OFFLINE`` is
dropped on the way through and neither offline route arrives. ``kb/mcp_client.py`` (every
connector's tool calls) and ``kb/sources/mcp.py`` (the ``mcp`` ingest source) spawn that
way. Both also have a ``url`` route, which connects over HTTP in this process, and that
route this guard does cover.

*The child is a third-party tool that never reads the variable.* It inherits
``os.environ``, so ``CONTEXTLAKE_OFFLINE=1`` reaches it and means nothing to it.
``kb/dashboard/mutations.py`` runs ``git pull --ff-only`` (refresh a repo) and
``git clone`` (add a repo); ``kb/connectors/gitlab.py`` runs ``glab api``, which is how
``kb connect`` reaches a forge; ``kb/cmds/doctor_fix.py`` runs ``pip install`` for
``contextlake doctor --fix``. Closing these means a refusal at each call site, the way the mirror
verbs already do it.

*The child is contextlake, spawned with an environment built elsewhere.*
``schedule/runner.py:_spawn`` uses that module's own ``child_env``, which is
``dict(os.environ)`` plus three scheduler variables. ``CONTEXTLAKE_OFFLINE=1`` survives
that copy, so the environment route works there; ``--offline`` does not, because the
flag writes nothing into ``os.environ``.

A guard that silently did not cover half the egress would be worse than none, because it
would be believed. Naming the half it does not cover is what keeps the other half worth
believing.
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
    """Whether offline mode is on: the guard, the flag, or the environment.

    The environment is checked at call time rather than cached, so a test (or a shell)
    can turn it on for one command.

    True once :func:`install` has run, because the guard is one-way and code far below
    ``cli.main`` has no argparse namespace to consult. ``kb/llm/base.py:build_llm`` is
    the case that forced this: it must refuse the ``cli`` provider before spawning it,
    and without this line ``--offline`` (as opposed to ``CONTEXTLAKE_OFFLINE=1``) was
    invisible to it.
    """
    if _installed:
        return True
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

    **Note for tests.** Because this is one-way and process-wide, a test that drives
    ``cli.main()`` with ``--offline`` (or sets the env var before it) installs the guard
    for the rest of that pytest worker, and every later test in it. Run CLI-level offline
    tests in a subprocess; the unit tests here reset the latch and restore the real
    socket functions themselves.
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


def child_env() -> dict:
    """The environment to hand a subprocess, so offline mode survives the spawn.

    :func:`install` guards this process and no other, and ``--offline`` as a flag leaves
    nothing on disk or in the environment for a child to read. A child spawned from an
    offline parent therefore starts unguarded: ``kb wiki`` builds a ``CliLlm`` and runs
    an agent CLI, ``kb embed`` downloads a model, ``kb serve`` answers a chat turn from a
    hosted provider. Setting the env var here is what closes that gap, because
    :func:`offline` in the child reads it.

    Only useful for a child that is contextlake itself. The module docstring lists the
    spawn sites this does not reach and why.

    The return value is a fresh copy of ``os.environ``, so the caller can add its own
    keys (the dashboard adds the server token) without touching this process's
    environment. There is no ``base`` parameter: an earlier version took one, no caller
    ever passed it, and a parameter nothing exercises is a parameter that drifts.
    """
    env = dict(os.environ)
    if offline():
        env[OFFLINE_ENV] = "1"
    return env
