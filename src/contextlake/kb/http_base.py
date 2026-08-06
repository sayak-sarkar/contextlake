"""One security policy for every local HTTP server contextlake starts.

Three stdlib ``ThreadingHTTPServer``s live in this package -- the dashboard
(:mod:`.dashboard.server`) and the two visualizer builders
(:func:`.visualize.serve.build_graph_server`,
:func:`.visualize.serve.build_site_server`) -- and each one used to re-derive its
own request-handling rules. That drift was not hypothetical: the dashboard's
``do_POST`` pinned the ``Host`` header against DNS rebinding while its own
``do_GET`` did not, so every read route (the whole code graph: file paths, symbol
names, owner identities) was readable cross-origin by any page whose domain
re-resolved to 127.0.0.1 -- and, worse, so was ``/dashboard.js``, which carries
the per-process auth token that gates mutations and LLM chat. The two visualizer
servers had no Host check at all.

Fixing each server separately would have left the drift free to recur, so the
policy lives here once and each server inherits it:

* :class:`LocalHttpHandler` -- Host pinning, the security response headers
  (:data:`_CSP`, ``nosniff``, ``no-referrer``) that every reply carries, response
  helpers, and a guard that turns a handler exception into a status code instead
  of a traceback dumped down the socket by ``BaseHTTPRequestHandler``;
* :func:`qs_int` -- query-param integers that clamp instead of raising, so a
  hostile ``?hops=99999`` costs nothing;
* :func:`allowed_host_headers` -- the accepted ``Host`` values for a bind;
* :data:`LOOPBACK_HOSTS` -- what "off the network" means for a *bind address*,
  which is a different question from which ``Host`` headers are answered.

New local servers subclass :class:`LocalHttpHandler`. Anything that re-derives
this policy inline is the bug this module exists to prevent.
"""

from __future__ import annotations

import json
import re
import traceback
from http.server import BaseHTTPRequestHandler

from .. import observability
from ..logging_setup import get_logger, log

__all__ = ["LOOPBACK_HOSTS", "BadRequest", "LocalHttpHandler", "allowed_host_headers",
           "host_pinning_hint", "qs_int"]

# Cap on a request body we're willing to buffer. These servers are loopback
# developer tools, not upload endpoints; every POST they accept is a small JSON
# object.
MAX_BODY_BYTES = 1_000_000

# Bind addresses that keep a server unreachable from the network. Every caller
# that decides "is this bind safe enough to enable a privileged feature on"
# reads this one set: the `--llm-chat` exposure (F-3) existed because the only
# such check lived inline in one command's `--allow-mutations` branch and the
# second flag added beside it never got a copy. A set, not a predicate, so an
# unexpected host (a sentinel, None) is simply absent rather than parsed. Callers
# reading a host out of *JSON* must still type-check first: an unhashable value
# raises TypeError from `in`, which is a 500 where a 400 belongs.
#
# This is a *bind* set, deliberately distinct from `allowed_host_headers`'s
# *Host-header* set: this one answers "is this bind private?", that one answers
# "may this request in?".
#
# It once held only the IPv4 spellings, because the only readers were stdlib
# `ThreadingHTTPServer`s (AF_INET), where accepting `::1` would have traded a
# clear refusal for a confusing bind failure -- on the rule that it should widen
# only alongside a server that can actually bind what's added. The MCP server's
# network transports are that server: they run under uvicorn and do bind IPv6.
# Both spellings are listed because the same address is written `::1` as a bind
# and `[::1]` in a Host header, and someone who binds one form should not be
# told it is remote. A stdlib server handed `::1` now fails at bind rather than
# at the guard, which is the honest error: the address is genuinely private, it
# is that server that cannot serve it.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

# Anything outside printable ASCII (plus the space) is replaced before a
# client-supplied request line reaches the access log -- see
# :meth:`LocalHttpHandler.log_message`.
_PRINTABLE = re.compile(r"[^\x20-\x7e]")


class BadRequest(ValueError):
    """A client-input error whose message is safe to send back verbatim.

    Deliberately distinct from a bare ``ValueError`` raised deep inside a
    handler: that one's message can carry a store path, a node id, or a SQL
    fragment, so it gets logged and answered with a fixed string. Only errors
    raised at the request-parsing boundary (see :func:`qs_int`) opt into being
    echoed to the client.
    """


def allowed_host_headers(host: str, port: int) -> frozenset[str]:
    """``Host`` header values a server bound to ``host:port`` will answer.

    Deliberately the *exact* set the dashboard's POST path has always used --
    the bound host and ``localhost``, both with the port. Widening it (adding
    ``127.0.0.1`` unconditionally, or waving the check through for non-loopback
    binds) would loosen the existing, correct CSRF/rebinding model on POST while
    nominally fixing GET, so it isn't done here.

    Consequence worth knowing: with a wildcard bind (``--host 0.0.0.0``) only
    ``http://localhost:PORT`` is reachable, because a browser pointed at the
    machine's LAN address sends that address in ``Host`` and it isn't in this
    set. Binding to the address you intend to browse (``--host 192.0.2.10``)
    puts it in the set; that is the supported way to expose these servers, and
    it is what keeps the rebinding defence expressible at all.
    """
    return frozenset({f"{host}:{port}", f"localhost:{port}"})


def host_pinning_hint(host: str, port: int) -> str | None:
    """A startup line to print for binds where Host pinning will surprise someone.

    Only for a wildcard bind: everything a browser sends there (the LAN address,
    the machine's name) is outside :func:`allowed_host_headers`, so the whole UI
    answers 403 with nothing on screen to explain it. Said once at startup rather
    than discovered per-request.
    """
    if host not in ("0.0.0.0", "::", ""):
        return None
    return (f"Bound to {host} -- requests are only answered when their Host header is "
            f"'{host}:{port}' or 'localhost:{port}' (DNS-rebinding defence). Browse "
            f"http://localhost:{port}, or bind the address you'll actually use "
            f"(--host <that-address>).")


def qs_int(q: dict, name: str, default: int, *, lo: int, hi: int) -> int:
    """Read one integer query param, clamped to ``lo..hi``.

    ``q`` is ``urllib.parse.parse_qs`` output. Absent or empty -> ``default``.
    Out of range -> clamped, never an error: the bounds exist to stop a request
    like ``?hops=99999`` from walking the whole graph, and a caller who asks for
    more than we'll do is better served the largest answer we will give than a
    rejection. Non-numeric -> :class:`BadRequest`, i.e. a 400: that is a
    malformed request rather than an ambitious one, and silently substituting a
    default would hide a genuine client bug.
    """
    raw = (q.get(name) or [None])[0]
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise BadRequest(f"{name} must be an integer") from None
    return max(lo, min(hi, value))


# The origin `to_html(cdn=True)` loads cytoscape from. `kb graph --serve --cdn`
# renders a *served* page in that mode, so the CDN has to be nameable in
# `script-src` or that mode returns a blank graph. Widening script-src is the
# cheap half of the policy anyway: 'unsafe-inline' is already unavoidable while
# assets are inlined, so script-src was never the load-bearing clause here.
_CDN_ORIGIN = "https://cdn.jsdelivr.net"

# One Content-Security-Policy for every page these servers return.
#
# What it is actually for: it does not stop HTML/JS injection into a generated
# page (escaping does that -- see kb.security.json_for_script), it stops the
# *second* step. An injected script's goal on this origin is to read the
# per-process token out of /dashboard.js and then talk to somewhere else, so
# `default-src 'none'` plus `connect-src 'self'` is the clause that matters:
# no fetch/XHR/WebSocket/beacon to any other host, no form posts, no plugins.
# Defence in depth, deliberately -- not the fix.
#
# The pages are otherwise self-contained (assets inlined by default, or served
# as siblings), so everything else can stay tight:
#   * 'unsafe-inline' for script/style is unavoidable while assets are inlined;
#   * img-src needs data: for the node glyphs, which are data-URI PNGs;
#   * frame-src/frame-ancestors 'self' because the dashboard renders the graph
#     pages in a same-origin iframe -- with default-src 'none' and neither of
#     these, the architecture panel would come up empty;
#   * no font-src entry is needed: the stylesheets declare no @font-face.
_CSP = "; ".join((
    "default-src 'none'",
    f"script-src 'self' 'unsafe-inline' {_CDN_ORIGIN}",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "connect-src 'self'",
    "frame-src 'self'",
    "frame-ancestors 'self'",
    "base-uri 'none'",
    "form-action 'none'",
))


class LocalHttpHandler(BaseHTTPRequestHandler):
    """Base for contextlake's loopback HTTP handlers.

    Subclasses set :attr:`allowed_hosts` (from :func:`allowed_host_headers`) and
    call :meth:`reject_bad_host` first in every ``do_*`` method. The default is
    the empty set so a handler that forgets fails closed -- an over-strict
    server is a bug report, a server that forgot its Host check is a breach.

    :attr:`csp` is the Content-Security-Policy every response carries. It is a
    class attribute so a subclass can tighten or widen it deliberately, but it
    is set here, once, for the same reason the Host check is: the drift this
    module exists to prevent is exactly "one server got the header and the next
    one did not".
    """

    allowed_hosts: frozenset[str] = frozenset()
    csp: str = _CSP

    def log_message(self, fmt="", *args):
        """The access log: silent unless ``--access-log`` asked for it.

        Silence used to be unconditional, which left no way at all to answer
        "what did the dashboard actually serve, and to whom" -- the one question
        an access log exists for, and the one worth being able to answer about a
        server that holds the whole code graph. It stays off by default (these
        are loopback developer tools whose console is already a command's
        output), so the no-flag behaviour is byte-for-byte what it was.

        ``log_error`` routes through here too, so an error line is equally
        opt-in.

        The request line is attacker-controlled, and stdlib only started escaping
        control characters here in Python 3.10 -- on 3.9 a crafted request could
        inject newlines or ANSI escapes straight into the log a human then reads.
        So it is sanitised locally rather than trusted to the base class.
        """
        if not observability.access_log_enabled():
            return
        try:
            message = fmt % args if args else str(fmt)
        except (TypeError, ValueError):  # a subclass with an unexpected format
            message = str(fmt)
        message = _PRINTABLE.sub(".", message)
        client = _PRINTABLE.sub(".", self.address_string())
        log(f"{client} {message}", inline=True, client=client, http=message)

    def send_bytes(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Every response, not just the HTML ones: `nosniff` is only meaningful on
        # the JSON and asset replies, and a policy applied per-content-type is a
        # policy someone will forget to extend. This is the single response path
        # for all three servers, so stating it here covers every route.
        self.send_header("Content-Security-Policy", self.csp)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # the client (browser tab, curl, etc.) went away mid-write -- nothing
            # left to send it, and ThreadingHTTPServer already isolates this to its
            # own request thread, so there's nothing to do but not print a traceback.
            pass

    def send_json(self, code: int, obj) -> None:
        self.send_bytes(code, "application/json", json.dumps(obj).encode("utf-8"))

    def reject_bad_host(self) -> bool:
        """403 the request unless its ``Host`` names this server. True if rejected.

        DNS rebinding: an attacker domain that resolves to 127.0.0.1 would
        otherwise sail straight past the loopback bind, since the browser's
        same-origin check is about the domain, not the resolved address.
        Requiring the Host header to literally name this host:port closes that
        gap without needing a real TLS cert on localhost.

        Must run on GET as well as POST, with no route exempt: the read API is
        the entire code graph, and ``/dashboard.js`` embeds the per-process
        token that gates mutations and paid LLM calls, so an "assets are
        harmless" carve-out would hand a rebinding page the token itself.
        """
        if (self.headers.get("Host") or "") in self.allowed_hosts:
            return False
        self.send_bytes(403, "text/plain", b"forbidden")
        return True

    def read_body(self, limit: int = MAX_BODY_BYTES) -> bytes:
        """Read the request body, tolerating a missing/garbage Content-Length.

        ``int(self.headers.get("Content-Length"))`` on client-controlled input is
        the same defect shape as an unguarded query param: a non-numeric value
        would raise inside the handler thread and drop the connection with no
        response. An unreadable length is treated as no body -- the JSON routes
        then answer their own "field required" 400.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        if length <= 0:
            return b""
        return self.rfile.read(min(length, limit))

    def send_guarded(self, fn, *args, ctype: str = "application/json") -> None:
        """Run ``fn(*args)`` and send its response, mapping exceptions to statuses.

        Without this, any exception in a route escapes to
        ``BaseHTTPRequestHandler``, which dumps a traceback and closes the socket
        with no response at all -- a crash that both fingerprints the internals
        and reads to the client as a network failure.

        ``fn`` returns ``(code, body)`` or ``(code, ctype, body)``.
        :class:`BadRequest` -> 400 with its message (raised at the parsing
        boundary, safe to echo); other ``ValueError``/``KeyError`` -> 400 with a
        fixed message; anything else -> 500. The traceback is logged, never sent:
        it names local paths and store internals.
        """
        try:
            result = fn(*args)
        except BadRequest as e:
            self.send_json(400, {"error": str(e)})
        except (ValueError, KeyError):
            get_logger().warning(
                "Bad request on %s:\n%s", self.path, traceback.format_exc())
            self.send_json(400, {"error": "bad request"})
        except Exception:  # noqa: BLE001 - last line before the socket dies
            get_logger().error(
                "Unhandled error serving %s:\n%s", self.path, traceback.format_exc())
            self.send_json(500, {"error": "internal server error"})
        else:
            if len(result) == 2:
                code, body = result
            else:
                code, ctype, body = result
            self.send_bytes(code, ctype, body)
