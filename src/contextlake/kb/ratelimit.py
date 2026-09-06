"""Per-key request and compute quotas for the networked MCP server.

PURE, in the sense ``schedule/recommend.py`` is pure: no socket, no config
lookup, no keyring iteration of its own. A clock arrives as an argument and the
key policy arrives through one callable. Nothing here imports ``kb/server.py``,
``kb/store/``, ``kb/keyfile.py`` or the MCP SDK, so a test can exercise every
branch with two floats and a fake clock, and an stdio run never loads it at all.

TWO BUCKETS PER KEY, and they answer different questions:

* the REQUEST bucket, filled at the key's ``rate`` and holding ``burst``
  tokens, spends one token per HTTP request the gate admits;
* the COST bucket, filled at the key's ``cost_budget`` and measured in
  milliseconds, is debited after each tool body by how long that body ran.

The cost bucket exists because a request count misprices ``ask`` by 8x. One
``ask`` is one admitted HTTP request and eight tool bodies: ``bounded_tool``
registers the wrapper and returns the bare function, so ``ask`` reaches its
siblings below the wrapper and a per-request count cannot see them. A duration
does see them, because the timer wraps the whole call including its legs.

TOKEN BUCKET, NOT A SLIDING-WINDOW LOG. A log stores one timestamp per request,
so its memory grows with traffic, which is the property a quota exists to
bound. Refill here is lazy and arithmetic: two floats of state per bucket, read
and rewritten on the request that touches them. No thread, no timer, no deque.

NOT a fixed window either. A key spends a full window's quota at the end of one
window and another full window's at the start of the next, so a fixed window
admits twice the rate it advertises across the boundary.

THE MEMORY BOUND, stated as a number rather than as "bounded": 2 buckets x 2
floats per key id EVER ADMITTED IN THIS PROCESS. A key created, used and
revoked while the server runs keeps its entry until the process ends, because
nothing here watches the key file. At 100 keys that is 400 floats. Keys with no
rate and no cost budget allocate nothing at all, which is what keeps the bound
on "key ids" rather than on "callers".

NOT PERSISTED, and not shared between processes. Bucket state lives in this
process's memory, so a restart refills every quota, and two ``kb serve``
processes behind one address give each key twice its quota. Persisting it would
put a write on the hot path of every request; restarting the server needs
operator access, which is a larger grant than any key holds. contextlake serves
one process by design. Both facts are in ``docs/serving-over-mcp.md`` so an
operator meets them before a quota surprises them.
"""
from __future__ import annotations

import re
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from ..schedule import recommend

# The periods a rate may name. ITS OWN TABLE, deliberately not an extension of
# `recommend._UNITS`: that map is read by the scheduler's interval maths, so
# teaching it `min` and `hour` would change what `kb schedule` accepts as an
# interval. Two readers, two vocabularies, one table each.
_PERIODS = {"s": 1.0, "sec": 1.0, "m": 60.0, "min": 60.0, "h": 3600.0, "hour": 3600.0}
_RATE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*/\s*([a-z]+)\s*$", re.IGNORECASE)

# The word that means "no limit on this axis", typed by an operator on a key.
# It is not the same fact as an ABSENT axis: absent falls through to the server
# default, `none` overrides it. See :func:`resolve_limits`.
NONE = "none"

# The request bucket's capacity when a rate is in force and nobody named a
# burst. A burst is what lets a client that has been idle spend a few requests
# at once, which is the normal shape of an editor waking up.
DEFAULT_BURST = 20

# The floor under `--burst`. The bundled MCP client sends three HTTP requests
# before its first tool call (initialize, notifications/initialized,
# tools/list), so a burst of three or less refuses a client that has done no
# work, and the operator reads that as a broken server rather than as a quota.
MIN_BURST = 4

_AXES = ("rate", "burst", "cost_budget")


class LimitError(ValueError):
    """A rate, burst or cost budget this module refuses to parse.

    A ``ValueError`` subclass so a caller that already catches ``ValueError``
    around ``recommend.parse_duration`` keeps working, and so the three
    validation sites can name this class when they want only this failure.
    """


@dataclass(frozen=True)
class Rate:
    """A quantity per period, plus the operator's own spelling of it.

    ``amount`` is in the bucket's own unit -- requests for a rate,
    MILLISECONDS for a cost budget -- and it doubles as that bucket's natural
    capacity. ``text`` is carried so a refusal can echo the string the operator
    typed into ``--rate``, which is what lets them grep their own key config
    and find it. A re-rendered `1.0/s` for a typed `60/min` is the same number
    and a different string, and the string is the one they search for.
    """

    amount: float
    period_s: float
    per_second: float
    text: str


def parse_rate(text) -> Rate | None:
    """``"60/min"`` to a :class:`Rate`. ``None`` means no limit on this axis.

    ``None``, ``""`` and ``"none"`` all mean unlimited, and they arrive from
    different places: ``None`` from an axis nobody set, ``""`` from a key file
    somebody hand-edited, ``"none"`` from an operator who typed it.

    A BARE NUMBER IS REFUSED. ``recommend.parse_duration("60")`` returns 60.0,
    reading a bare number as seconds, so handing this string on to that parser
    would silently accept `--rate 60` as something. A rate has a period and
    there is no default period that is not a guess about what was meant.
    """
    if text is None:
        return None
    value = str(text).strip()
    if not value or value.casefold() == NONE:
        return None
    match = _RATE_RE.match(value)
    period = _PERIODS.get(match.group(2).lower()) if match else None
    if period is None:
        raise LimitError(
            f"not a rate: {text!r} (try 60/min, 5/sec, 200/hour). A rate needs "
            f"a period; `none` means no limit.")
    amount = float(match.group(1))
    if amount <= 0:
        raise LimitError(
            f"a rate must be positive: {text!r}. A rate of zero admits nothing "
            f"and is not how a key is switched off -- revoke it, or pass "
            f"`none` for no limit.")
    return Rate(amount=amount, period_s=period, per_second=amount / period,
                text=value)


def burst_number(text) -> int | None:
    """A burst's NUMBER, with no opinion about whether a rate exists.

    Split from :func:`parse_burst` so a key file carrying a burst and no rate
    still LOADS. That combination binds nothing, so it is inert rather than
    dangerous, and refusing to serve over it would take down every other key in
    the file. The create-time flag refuses it, which is where the operator can
    still act on the message.
    """
    if text is None:
        return None
    value = str(text).strip()
    if not value or value.casefold() == NONE:
        return None
    try:
        number = int(value)
    except ValueError:
        raise LimitError(
            f"not a burst: {text!r} (a whole number of requests, e.g. 20)") from None
    if number < MIN_BURST:
        raise LimitError(
            f"--burst {number} is below {MIN_BURST}. An MCP client spends "
            f"three requests on the handshake before its first tool call, so a "
            f"burst under {MIN_BURST} refuses a client that has done no work.")
    return number


def parse_burst(text, *, rate: Rate | None) -> int | None:
    """The request bucket's capacity, or ``None`` when there is no such bucket.

    REFUSED WITH NO RATE. A burst is the capacity of the request bucket and the
    request bucket only exists beside a rate, so ``--burst 20`` alone bounds
    nothing while reading like a bound. The message names ``--rate`` because
    that is the flag that makes the value mean something.
    """
    number = burst_number(text)
    if number is None:
        return None
    if rate is None:
        raise LimitError(
            f"--burst {number} was given with no rate. A burst is how many "
            f"requests may arrive at once against a rate, so on its own it "
            f"bounds nothing. Pass --rate too, or leave --burst off.")
    return number


def parse_cost_budget(text) -> Rate | None:
    """``"30s/min"`` to a :class:`Rate` in MILLISECONDS per second.

    The numerator goes through ``recommend.parse_duration``, which already
    reads ``30s``, ``2m`` and a bare number as seconds, and already refuses
    zero and negatives with a message naming the offending string. A second
    duration parser here would be a hand-copied twin, and this workspace has
    one of those on record already.
    """
    if text is None:
        return None
    value = str(text).strip()
    if not value or value.casefold() == NONE:
        return None
    head, sep, tail = value.partition("/")
    period = _PERIODS.get(tail.strip().lower()) if sep else None
    if period is None:
        raise LimitError(
            f"not a compute budget: {text!r} (try 30s/min, 2m/hour). It is a "
            f"duration per period; `none` means no limit.")
    seconds = recommend.parse_duration(head.strip())
    return Rate(amount=seconds * 1000.0, period_s=period,
                per_second=seconds * 1000.0 / period, text=value)


class Bucket:
    """Two floats. Lazily refilled, read and rewritten by whoever touches it.

    CAPACITY AND REFILL ARE ARGUMENTS, NOT STATE, and that is the whole reason
    this class is this small. Caching the policy on the bucket would keep a key
    on its old quota until the process restarted, and the keyring's own rule is
    that a key narrowed in the file takes effect on the next request with no
    invalidation window. So the policy is read fresh per call and handed in.

    :meth:`take` and :meth:`debit` are not the same operation.  ``take`` asks
    permission and refuses when the tokens are not there; ``debit`` records
    work that already happened and is the only method that may go below zero.
    A tool body that ran for eight seconds spent eight seconds whether or not
    the budget had room for it.
    """

    __slots__ = ("tokens", "last")

    def __init__(self, tokens: float, last: float) -> None:
        self.tokens = float(tokens)
        self.last = float(last)

    def _filled(self, capacity: float, per_second: float, now: float) -> float:
        return min(capacity, self.tokens + max(0.0, now - self.last) * per_second)

    def take(self, n: float, *, capacity: float, per_second: float,
             now: float) -> bool:
        """Spend ``n`` tokens if they are there. ``False`` changes nothing.

        A REFUSAL LEAVES THE STATE UNTOUCHED, refill included. Writing the
        refill back on a refused take would push a bucket further from its own
        recorded position on every rejected request, so a flood would move the
        clock a caller is waiting on.

        ``n`` above ``capacity`` can never be satisfied, so it refuses at once
        rather than parking, sleeping or waiting for a refill that has nowhere
        to land.
        """
        if n > capacity:
            return False
        tokens = self._filled(capacity, per_second, now)
        if tokens < n:
            return False
        self.tokens = tokens - n
        self.last = now
        return True

    def debit(self, n: float, *, capacity: float, per_second: float,
              now: float) -> None:
        """Record ``n`` units already spent. May take the bucket below zero."""
        self.tokens = self._filled(capacity, per_second, now) - n
        self.last = now

    def wait_for(self, n: float, *, capacity: float, per_second: float,
                 now: float) -> float:
        """Seconds until ``n`` tokens are available. Never negative."""
        if per_second <= 0:
            return 0.0
        return max(0.0, (n - self._filled(capacity, per_second, now)) / per_second)


@dataclass(frozen=True)
class ServeDefaults:
    """The parsed ``[serve]`` block: what a key that names nothing inherits.

    UNSET BY DEFAULT, all three. 9.1.0 is public, and a built-in default rate
    would start refusing keys that worked yesterday with nothing typed by the
    operator. The access-control story already ruled the same way about an
    absent axis, and for the same reason. ``docs/serving-over-mcp.md`` prints
    starting values instead.
    """

    rate: Rate | None = None
    burst: int | None = None
    cost_budget: Rate | None = None

    def is_empty(self) -> bool:
        return self.rate is None and self.cost_budget is None


def parse_serve_defaults(table: Mapping[str, object] | None) -> ServeDefaults:
    """The ``[serve]`` quota keys, parsed. Raises :class:`LimitError` by NAME.

    Read once at serve start, before the socket binds, so a bad value exits 1
    with the offending string named rather than raising inside a request and
    surfacing to a caller as a 500.

    The message names the config key, not the flag, because that is what the
    operator has to go and edit. ``kb keys create --rate`` gets its own message
    from :func:`parse_rate` naming the flag.
    """
    table = table or {}
    try:
        rate = parse_rate(table.get("default_rate"))
    except ValueError as exc:
        raise LimitError(f"[serve] default_rate: {exc}") from None
    try:
        burst = parse_burst(table.get("default_burst"), rate=rate)
    except ValueError as exc:
        raise LimitError(f"[serve] default_burst: {exc}") from None
    try:
        cost = parse_cost_budget(table.get("default_cost_budget"))
    except ValueError as exc:
        raise LimitError(f"[serve] default_cost_budget: {exc}") from None
    return ServeDefaults(rate=rate, burst=burst, cost_budget=cost)


@dataclass(frozen=True)
class Limits:
    """One key's effective quota, and WHERE EACH AXIS CAME FROM.

    ``sources`` is per axis rather than one word for the record, because the
    three axes resolve independently and the middle state is the one that lies:
    a key that names no rate can still be limited by ``[serve] default_rate``,
    and an operator who reads a bare `unset` beside it hands that key out
    believing it is unlimited. ``kb keys`` renders this map, so the three
    values (`key`, `config`, `unset`) are public API.
    """

    rate: Rate | None = None
    burst: int | None = None
    cost_budget: Rate | None = None
    sources: Mapping[str, str] = field(
        default_factory=lambda: {axis: "unset" for axis in _AXES})

    @property
    def unlimited(self) -> bool:
        """Whether this key needs no bucket at all.

        A burst alone is not a limit: it is the request bucket's capacity and
        there is no request bucket without a rate.
        """
        return self.rate is None and self.cost_budget is None


UNLIMITED = Limits()


def _axis(policy: Mapping[str, object], axis: str, parse, default):
    """One axis's value and tier. Presence decides the tier, the parse the value.

    A key that stores ``none`` is an explicit opt-out and WINS over the server
    default: the operator typed a word meaning "not this one". A key that
    stores nothing has said nothing, so the default applies.

    An unparseable stored value falls back to the default rather than raising.
    That branch is unreachable through the three validation sites (create-time,
    serve-start and reload all refuse or reject a value this module will not
    parse), and it is here so one hand-edited record cannot turn every request
    on the server into a 500 from inside the gate.
    """
    if axis in policy and str(policy[axis]).strip() != "":
        try:
            return parse(policy[axis]), "key"
        except ValueError:
            return default, "config" if default is not None else "unset"
    return default, "config" if default is not None else "unset"


def resolve_limits(policy: Mapping[str, object] | None,
                   defaults: ServeDefaults) -> Limits:
    """The three axes, resolved key-first then config, with their tiers.

    ``policy`` is a key record's policy block, or ``None`` for no record. Both
    read the same way here, and deliberately so: unlike the grant check, where
    ``None`` means a key revoked mid-flight and must grant nothing, a missing
    record on this axis means there is no per-key quota to read and the server
    default applies. The shared token, which has no record at all, lands here.
    """
    policy = policy or {}
    rate, rate_source = _axis(policy, "rate", parse_rate, defaults.rate)
    burst, burst_source = _axis(
        policy, "burst", lambda value: parse_burst(value, rate=rate), defaults.burst)
    cost, cost_source = _axis(policy, "cost_budget", parse_cost_budget,
                              defaults.cost_budget)
    if rate is not None and burst is None:
        # A rate with no burst named anywhere. The built-in capacity applies and
        # the SOURCE stays `unset`, because nobody chose it: the rendering says
        # "unset" and prints the effective number beside it.
        burst = DEFAULT_BURST
    return Limits(rate=rate, burst=burst, cost_budget=cost,
                  sources={"rate": rate_source, "burst": burst_source,
                           "cost_budget": cost_source})


@dataclass(frozen=True)
class Admission:
    """The gate's answer for one request.

    ``which`` names the bucket that refused, so the 429 can say "too many
    calls" or "too much time" rather than one sentence covering both. When both
    refuse it names the one with the LONGER wait, so the message and the
    ``Retry-After`` header describe the same bucket.
    """

    admitted: bool
    which: str | None = None
    limit_text: str | None = None
    retry_after_s: float = 0.0


_ADMITTED = Admission(admitted=True)


class Limiter:
    """The buckets, keyed by RESOLVED key id and by nothing else.

    Both entry points take a key id and nothing else -- no header, no bearer
    value, no address. That is the memory property: the gate resolves identity
    first, so a caller with no valid key never reaches this map and cannot grow
    it. A map keyed on anything the caller controls is a flood surface with a
    quota bolted to it.

    ORDER INSIDE BOTH METHODS: resolve the policy, THEN take the lock. The
    policy read walks the keyring's records, and ``admit`` runs on the event
    loop thread; a keyring scan under a lock held from that thread serialises
    the whole gate behind one request.
    """

    def __init__(self, policy_source: Callable[[str], Limits], *,
                 now=time.monotonic) -> None:
        self._policy_source = policy_source
        self._now = now
        self._buckets: dict[str, tuple[Bucket, Bucket]] = {}
        # One lock over the map and the token arithmetic. The MCP SDK runs
        # synchronous tool bodies on `anyio.to_thread.run_sync` worker threads,
        # so `charge` is reachable from two threads at once. A lost update
        # shows up as under-billing, which no functional test can see.
        self._lock = threading.Lock()
        # Charges this limiter threw away rather than raise. Read by the
        # one-per-process warning below and by the test that corrupts an entry.
        self.dropped_charges = 0
        self._warned = False

    def _buckets_for(self, key_id: str, limits: Limits, now: float):
        """The pair for this key, created on first use. CALLER HOLDS THE LOCK."""
        pair = self._buckets.get(key_id)
        if pair is None:
            pair = (Bucket(float(limits.burst or DEFAULT_BURST), now),
                    Bucket(limits.cost_budget.amount if limits.cost_budget else 0.0,
                           now))
            self._buckets[key_id] = pair
        return pair

    def admit(self, key_id: str) -> Admission:
        """Whether this key may spend one request now.

        The request bucket pays a token. The cost bucket pays NOTHING here: it
        is debited after the fact by :meth:`charge`, so admission only asks
        whether it is already in debt.
        """
        limits = self._policy_source(key_id)
        if limits.unlimited:
            # Allocate nothing. The limiter is derived unconditionally on a
            # networked server, so without this the bound stops being "key ids
            # ever admitted" and becomes "every key that ever called".
            return _ADMITTED
        now = self._now()
        with self._lock:
            requests, cost = self._buckets_for(key_id, limits, now)
            request_ok = request_wait = None
            if limits.rate is not None:
                capacity = float(limits.burst or DEFAULT_BURST)
                request_ok = requests.take(
                    1.0, capacity=capacity, per_second=limits.rate.per_second, now=now)
                request_wait = 0.0 if request_ok else requests.wait_for(
                    1.0, capacity=capacity, per_second=limits.rate.per_second, now=now)
            cost_ok = cost_wait = None
            if limits.cost_budget is not None:
                budget = limits.cost_budget
                cost_ok = cost.take(0.0, capacity=budget.amount,
                                    per_second=budget.per_second, now=now)
                cost_wait = 0.0 if cost_ok else cost.wait_for(
                    0.0, capacity=budget.amount, per_second=budget.per_second,
                    now=now)
        if request_ok is not False and cost_ok is not False:
            return _ADMITTED
        # The wait is the LARGER of the two. A client told the smaller one
        # retries on time for one bucket and is refused again by the other.
        wait = max(request_wait or 0.0, cost_wait or 0.0)
        if request_ok is False and (cost_ok is not False
                                    or (request_wait or 0.0) >= (cost_wait or 0.0)):
            return Admission(False, "requests", limits.rate.text, wait)
        return Admission(False, "cost", limits.cost_budget.text, wait)

    def charge(self, key_id: str, ms: float) -> None:
        """Debit ``ms`` milliseconds of tool time against this key. NEVER RAISES.

        The guard is here rather than at the call site because the call site is
        inside ``guarded``'s one outer ``try``: a raise from here would land on
        its ``except BaseException`` and REPLACE the tool's real return value
        with a traceback about accounting. Losing a data point is strictly
        better, which is the rule ``schedule/history.py`` already states.
        """
        try:
            limits = self._policy_source(key_id)
            if limits.cost_budget is None:
                return
            budget = limits.cost_budget
            now = self._now()
            with self._lock:
                _, cost = self._buckets_for(key_id, limits, now)
                cost.debit(float(ms), capacity=budget.amount,
                           per_second=budget.per_second, now=now)
        except Exception as exc:  # noqa: BLE001 - stated above; never raises
            self.dropped_charges += 1
            if not self._warned:
                self._warned = True
                print(f"  MCP cost accounting dropped a charge for key={key_id}: "
                      f"{type(exc).__name__}: {exc}. Quotas still admit; further "
                      f"drops are counted, not printed.", file=sys.stderr)


def build_limiter(keyring, defaults: ServeDefaults | None, *,
                  now=time.monotonic) -> Limiter | None:
    """The derivation ``build_http_app`` runs. THE ONE TEST SEAM.

    Derived rather than accepted as a ``build_http_app`` parameter, following
    the ruling already written beside ``grant_source``: there are two
    production call sites, and one that forgot the parameter would be a socket
    serving unlimited traffic with every test still green. A test that needs a
    recording limiter patches this function, which is one seam rather than a
    parameter every caller can omit.

    ``None`` when there is no keyring AND no server default: a token-only
    server with nothing configured has no key that could carry a quota and
    nothing for a limiter to read, so no object is built and the charge inside
    ``guarded`` stays switched off.
    """
    defaults = defaults or ServeDefaults()
    if keyring is None and defaults.is_empty():
        return None

    def policy_source(key_id: str) -> Limits:
        # `keyring is None` is a TOKEN-ONLY server, which is the most exposed
        # configuration contextlake ships and the first one an operator who
        # sets `default_rate` will try. There is no keyring to call
        # `policy_for` on, so resolution starts at the config tier. Without
        # this skip that server raises AttributeError on its first request.
        policy = keyring.policy_for(key_id) if keyring is not None else None
        return resolve_limits(policy, defaults)

    return Limiter(policy_source, now=now)


def validate_policy(policy: Mapping[str, object] | None) -> None:
    """Raise :class:`LimitError` if this record's quota axes cannot be parsed.

    Run over every stored value when a key file is loaded for serving. A
    garbage rate quietly replaced by a default is an unlimited key that reads
    as limited, which is the failure this whole changeset exists to stop.
    """
    policy = policy or {}
    parse_rate(policy.get("rate"))
    # `burst_number`, not `parse_burst`: a stored burst beside no rate is inert,
    # and refusing to serve over it would stop every other key in the file for a
    # value that bounds nothing. The create-time flag is where that is refused.
    burst_number(policy.get("burst"))
    parse_cost_budget(policy.get("cost_budget"))
