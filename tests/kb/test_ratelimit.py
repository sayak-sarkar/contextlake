"""The per-key quota: parsers, buckets, resolution and the limiter itself.

Everything here runs on two floats and an injected clock. Nothing binds a
socket, opens a store or loads the MCP SDK -- `test_serve_ratelimit.py` covers
the gate, and this file covers the arithmetic it rests on.

THE FAILURE THIS FILE EXISTS TO CATCH is `test_the_cost_bucket_refuses_while_
the_request_bucket_is_nearly_full`. Every other test here passes for a limiter
that never reads the cost bucket at all.
"""
from __future__ import annotations

import inspect
import threading
import time

import pytest

from contextlake.kb import ratelimit as rl
from contextlake.schedule import recommend

# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,per_second", [
    ("60/min", 1.0), ("60/m", 1.0), ("5/sec", 5.0), ("5/s", 5.0),
    ("200/hour", 200 / 3600), ("200/h", 200 / 3600), ("  60 / MIN ", 1.0),
])
def test_a_rate_parses_to_tokens_per_second(text, per_second):
    """Six spellings of a period, and the whitespace an operator leaves in."""
    rate = rl.parse_rate(text)
    assert rate.per_second == pytest.approx(per_second)
    assert rate.text == text.strip(), (
        "the operator's own spelling was re-rendered, so grepping their key "
        "config for the string in the refusal will not find it")


@pytest.mark.parametrize("text", ["60", "60/", "/min", "0/min", "-5/min",
                                  "60/week", "60/min/min", "abc/min"])
def test_a_rate_this_module_cannot_read_is_refused_by_name(text):
    """The defect: a garbage rate stored, replaced by a default, read as limited.

    `60` is the one worth naming. `recommend.parse_duration("60")` returns 60.0,
    reading a bare number as seconds, so handing the string on to that parser
    would have accepted `--rate 60` as something. A rate has a period and there
    is no default period that is not a guess.
    """
    with pytest.raises(ValueError) as caught:
        rl.parse_rate(text)
    assert text in str(caught.value), (
        f"the refusal does not name the offending string: {caught.value}")


@pytest.mark.parametrize("text", [None, "", "none", "NONE", "  none  "])
def test_the_three_spellings_of_no_limit_all_parse_to_none(text):
    """`None`, empty and `none` arrive from three places and mean one thing.

    `None` is an axis nobody set, `""` a hand-edited file, `none` an operator's
    own opt-out. `resolve_limits` is where the third one differs from the first
    two, and that is tested there.
    """
    assert rl.parse_rate(text) is None
    assert rl.parse_cost_budget(text) is None


def test_a_cost_budget_goes_through_the_shared_duration_parser(monkeypatch):
    """The defect: a second, hand-copied duration parser.

    `recommend.parse_duration` already reads `30s`, `2m` and a bare number, and
    already refuses zero and negatives. Patched to raise here, so a copy written
    inside this module would keep the test green while the two parsers drifted.
    """
    assert rl.parse_cost_budget("30s/min").per_second == pytest.approx(500.0)
    assert rl.parse_cost_budget("30s/min").amount == pytest.approx(30_000.0)

    monkeypatch.setattr(recommend, "parse_duration",
                        lambda text: (_ for _ in ()).throw(ValueError("mine")))
    with pytest.raises(ValueError, match="mine"):
        rl.parse_cost_budget("30s/min")


@pytest.mark.parametrize("text", ["30s", "/min", "abc/min", "30s/week"])
def test_a_cost_budget_needs_a_duration_and_a_period(text):
    with pytest.raises(ValueError):
        rl.parse_cost_budget(text)


def test_a_burst_with_no_rate_is_refused_and_names_the_flag():
    """The defect: `--burst 20` alone, which reads like a bound and is not one.

    A burst is the request bucket's capacity, and there is no request bucket
    without a rate.
    """
    with pytest.raises(ValueError, match="--rate"):
        rl.parse_burst("20", rate=None)
    assert rl.parse_burst("20", rate=rl.parse_rate("60/min")) == 20


@pytest.mark.parametrize("text", ["3", "0", "-1", "twenty"])
def test_a_burst_below_the_handshake_is_refused(text):
    """The defect: a burst that refuses a client which has done no work.

    The bundled MCP client spends three HTTP requests on the handshake, so a
    burst of three or less throttles `initialize` and the operator reads a
    working quota as a broken server.
    """
    with pytest.raises(ValueError):
        rl.burst_number(text)


# ---------------------------------------------------------------------------
# the bucket
# ---------------------------------------------------------------------------


def test_a_bucket_refills_at_the_rate_it_is_given():
    """Pinned to 20/1/5/1, not to "it limits".

    A test asserting only that a bucket eventually refuses passes for a bucket
    that refuses always and for one that refills at any rate at all.
    """
    bucket = rl.Bucket(20.0, 0.0)
    kwargs = {"capacity": 20.0, "per_second": 1.0}
    assert sum(bucket.take(1.0, now=0.0, **kwargs) for _ in range(20)) == 20
    assert not bucket.take(1.0, now=0.0, **kwargs)
    assert sum(bucket.take(1.0, now=5.0, **kwargs) for _ in range(5)) == 5
    assert not bucket.take(1.0, now=5.0, **kwargs)


def test_a_refused_take_leaves_the_bucket_exactly_where_it_was():
    """The defect: a refused request pushing the bucket further into debt.

    Writing the refill back on a refusal would move the clock a caller is
    waiting on, so a flood would extend its own Retry-After.
    """
    # HALF a token has accrued, so a refill DID happen and was still not
    # enough. With the clock held still instead, a bucket that writes its refill
    # back on a refusal would pass: there would be nothing to write.
    bucket = rl.Bucket(0.0, 0.0)
    before = (bucket.tokens, bucket.last)
    assert not bucket.take(1.0, capacity=20.0, per_second=1.0, now=0.5)
    assert (bucket.tokens, bucket.last) == before, (
        "the refused take wrote its refill back, so a flood moves the clock "
        "the caller is waiting on")


def test_a_take_larger_than_the_capacity_refuses_without_parking():
    """The defect: waiting for a refill that has nowhere to land.

    A bucket cannot hold more than its capacity, so a take above it can never be
    satisfied and must refuse at once rather than block a request thread.
    """
    bucket = rl.Bucket(20.0, 0.0)
    started = time.perf_counter()
    assert not bucket.take(21.0, capacity=20.0, per_second=1.0, now=0.0)
    assert time.perf_counter() - started < 0.001


def test_wait_for_never_returns_a_negative_or_a_zero_that_rounds_down():
    """`Retry-After` is built on this, and `int(0.2)` is 0.

    A bucket a fifth of a token short at one token a second is 0.2 seconds away,
    and a header that says 0 tells a proxy to retry immediately.
    """
    bucket = rl.Bucket(0.8, 0.0)
    assert bucket.wait_for(1.0, capacity=20.0, per_second=1.0,
                           now=0.0) == pytest.approx(0.2)
    full = rl.Bucket(20.0, 0.0)
    assert full.wait_for(1.0, capacity=20.0, per_second=1.0, now=0.0) == 0.0


def test_debit_is_the_only_method_that_may_go_below_zero():
    """A tool body that ran for eight seconds spent them, budget or not."""
    bucket = rl.Bucket(1000.0, 0.0)
    bucket.debit(9000.0, capacity=30_000.0, per_second=500.0, now=0.0)
    assert bucket.tokens == pytest.approx(-8000.0)


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


def _defaults(**kwargs):
    return rl.ServeDefaults(
        rate=rl.parse_rate(kwargs.get("rate")),
        burst=rl.burst_number(kwargs.get("burst")),
        cost_budget=rl.parse_cost_budget(kwargs.get("cost_budget")))


def test_a_key_value_wins_and_the_server_default_fills_the_rest():
    limits = rl.resolve_limits({"rate": "6/min"},
                               _defaults(rate="60/min", cost_budget="30s/min"))
    assert limits.rate.text == "6/min"
    assert limits.sources["rate"] == "key"
    assert limits.cost_budget.text == "30s/min"
    assert limits.sources["cost_budget"] == "config"


def test_none_on_the_key_beats_the_server_default():
    """The defect: an operator's own opt-out silently overridden by a default.

    An ABSENT axis and an axis set to `none` are opposite instructions: one has
    said nothing, the other has said "not this key". Reading both as absent
    would make `--rate none` do nothing whenever a default was configured.
    """
    limits = rl.resolve_limits({"rate": "none"}, _defaults(rate="60/min"))
    assert limits.rate is None
    assert limits.sources["rate"] == "key"
    assert limits.unlimited is False or limits.cost_budget is None


def test_a_key_that_names_nothing_with_no_default_is_unlimited():
    limits = rl.resolve_limits({}, rl.ServeDefaults())
    assert limits.unlimited
    assert set(limits.sources.values()) == {"unset"}


def test_a_burst_with_no_rate_is_not_a_limit():
    """The defect: a burst alone reading as a bound, on any surface."""
    limits = rl.resolve_limits({"burst": "9"}, rl.ServeDefaults())
    assert limits.rate is None
    assert limits.unlimited, "a burst with no rate made the key look limited"


def test_a_rate_with_no_burst_gets_the_built_in_capacity_and_keeps_its_tier():
    """Effective 20, source still `unset`: nobody chose it.

    The three source values are public API on `kb keys --json`. A fourth value
    for "built in" would have to be added to the renderer at the same time, so
    the built-in capacity keeps the tier that describes what the operator did.
    """
    limits = rl.resolve_limits({"rate": "60/min"}, rl.ServeDefaults())
    assert limits.burst == rl.DEFAULT_BURST
    assert limits.sources["burst"] == "unset"


def test_an_unparseable_stored_value_falls_back_instead_of_raising():
    """The defect: one hand-edited record turning every request into a 500.

    Unreachable through the three validation sites, which refuse or reject such
    a value. It is here so that if one is ever bypassed the failure is a key on
    the server default, not a crash inside the gate.
    """
    limits = rl.resolve_limits({"rate": "not-a-rate"}, _defaults(rate="60/min"))
    assert limits.rate.text == "60/min"


# ---------------------------------------------------------------------------
# the limiter
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


def _limiter(policy, clock=None, defaults=None):
    clock = clock or _Clock()
    limits = rl.resolve_limits(policy, defaults or rl.ServeDefaults())
    return rl.Limiter(lambda key_id: limits, now=clock), clock


def test_the_cost_bucket_refuses_while_the_request_bucket_is_nearly_full():
    """THE TEST THIS FILE EXISTS FOR: a cost bucket written and never read.

    `charge` debits it on every wire call and `admit` could consult the request
    bucket alone. Every other test here still passes in that build: the request
    quota works, the 429 renders, the Retry-After is right, and `kb keys show`
    prints `cost_budget=30s/min (enforced)` for a key that can spend unbounded
    compute.

    THE `19 TOKENS` CLAUSE IS THE WHOLE TEST. Without it a limiter that ignores
    the cost bucket passes, because "it eventually refuses" is satisfied by the
    request bucket once twenty calls have been made. Asserting that the refusal
    arrives while the request bucket is nearly full is the only form that can
    tell the two designs apart.
    """
    limiter, _ = _limiter({"rate": "60/min", "burst": "20",
                           "cost_budget": "30s/min"})
    limiter.charge("k_a", 40_000.0)
    verdict = limiter.admit("k_a")
    assert not verdict.admitted, "the cost budget is debited and never consulted"
    assert verdict.which == "cost", verdict
    assert limiter._buckets["k_a"][0].tokens == pytest.approx(19.0), (
        "the request bucket did the refusing, so this passes for a limiter "
        "that never reads the cost bucket")


def test_the_retry_after_is_the_larger_of_the_two_waits():
    """The defect: a client told the smaller number, retrying into a refusal."""
    limiter, _ = _limiter({"rate": "1/sec", "burst": "4",
                           "cost_budget": "30s/min"})
    for _ in range(4):
        limiter.admit("k_a")
    limiter.charge("k_a", 38_000.0)   # 30_000 budget, so 8_000 ms of debt
    verdict = limiter.admit("k_a")
    assert not verdict.admitted
    # request bucket: 1 token at 1/s = 1.0s.  cost bucket: 8000 ms at 500 ms/s.
    assert verdict.retry_after_s == pytest.approx(16.0), verdict


def test_admission_names_both_buckets_over_the_two_shapes():
    requests, _ = _limiter({"rate": "1/min", "burst": "4"})
    for _ in range(4):
        requests.admit("k_a")
    assert requests.admit("k_a").which == "requests"

    cost, _ = _limiter({"cost_budget": "1s/min"})
    cost.charge("k_a", 5_000.0)
    assert cost.admit("k_a").which == "cost"


def test_neither_entry_point_takes_anything_the_caller_controls():
    """The memory property, read off the signatures rather than off a comment.

    A map keyed on a header, a bearer value or an address is a flood surface
    with a quota bolted to it.
    """
    for method in (rl.Limiter.admit, rl.Limiter.charge):
        names = set(inspect.signature(method).parameters) - {"self"}
        assert not names & {"header", "headers", "scope", "request", "address",
                            "presented", "token", "bearer"}, names


def test_the_map_holds_only_the_ids_it_was_handed():
    limiter, _ = _limiter({"rate": "600/min"})
    ids = [f"k_{n}" for n in range(20)]
    for _ in range(50):
        for key_id in ids:
            limiter.admit(key_id)
            limiter.charge(key_id, 1.0)
    assert set(limiter._buckets) == set(ids), "the map grew keys nobody passed"


def test_an_unlimited_key_allocates_nothing_and_a_limited_one_allocates_once():
    """Both halves. A limiter that allocates for nobody passes the first alone.

    The limiter is derived unconditionally on a networked server, so without
    the unlimited short-circuit the memory bound stops being "key ids ever
    admitted" and becomes "every key that ever called".
    """
    unlimited = rl.Limiter(lambda key_id: rl.UNLIMITED, now=_Clock())
    for _ in range(1000):
        unlimited.admit("k_open")
        unlimited.charge("k_open", 5.0)
    assert unlimited._buckets == {}

    limited, _ = _limiter({"rate": "6000/min"})
    for _ in range(10):
        limited.admit("k_shut")
    assert list(limited._buckets) == ["k_shut"]


def test_fifty_create_use_revoke_cycles_leave_fifty_entries():
    """The bound, stated as a number: key ids EVER ADMITTED in this process.

    Nothing here watches the key file, so a revoked key keeps its entry until
    the process ends. That is the honest bound and this pins it, rather than
    letting a reader assume the map shrinks.
    """
    limiter, _ = _limiter({"rate": "600/min"})
    for n in range(50):
        limiter.admit(f"k_{n}")
    for _ in range(50):
        for n in range(50):
            limiter.admit(f"k_{n}")
    assert len(limiter._buckets) == 50


def test_narrowing_the_policy_takes_effect_on_the_next_call():
    """The defect: a bucket that caches its policy and holds an old quota.

    The keyring's rule is that a key narrowed in the file takes effect on the
    next request with no invalidation window. Capacity and refill are therefore
    arguments to the bucket, not state on it.

    The token count must NOT reset: the same bucket object is kept, so the
    narrowing changes the refill and not the position.
    """
    clock = _Clock()
    policy = {"rate": "60/min", "burst": "20"}
    holder = {"limits": rl.resolve_limits(policy, rl.ServeDefaults())}
    limiter = rl.Limiter(lambda key_id: holder["limits"], now=clock)
    for _ in range(5):
        limiter.admit("k_a")
    bucket = limiter._buckets["k_a"][0]
    tokens = bucket.tokens
    assert tokens == pytest.approx(15.0)

    holder["limits"] = rl.resolve_limits({"rate": "6/min", "burst": "20"},
                                         rl.ServeDefaults())
    clock.now = 10.0
    limiter.admit("k_a")
    assert limiter._buckets["k_a"][0] is bucket, (
        "the bucket was replaced, so its position was thrown away")
    # 10 seconds at the NEW 0.1/s is 1 token back, then one spent: 15.0.
    assert bucket.tokens == pytest.approx(15.0), bucket.tokens


def test_charge_never_raises_and_counts_what_it_dropped():
    """The defect: a raise from accounting replacing the tool's return value.

    The charge sits inside `guarded`'s one outer `try`, whose
    `except BaseException` would swallow the real answer.

    The ENTRY is corrupted, not `charge` itself: patching the method under test
    proves nothing about the method under test.
    """
    limiter, _ = _limiter({"cost_budget": "30s/min"})
    limiter.admit("k_a")

    class _Exploding:
        def debit(self, *args, **kwargs):
            raise AttributeError("boom")

    limiter._buckets["k_a"] = (limiter._buckets["k_a"][0], _Exploding())
    assert limiter.charge("k_a", 5.0) is None
    assert limiter.dropped_charges == 1


def test_the_lock_is_actually_taken():
    """A counting lock, not a source scan.

    A `grep` for `with self._lock` passes the moment the code is reshaped and
    says nothing about whether the arithmetic ran inside it.
    """
    class _Counting:
        def __init__(self):
            self.entries = 0
            self._real = threading.Lock()

        def __enter__(self):
            self.entries += 1
            return self._real.__enter__()

        def __exit__(self, *exc):
            return self._real.__exit__(*exc)

    limiter, _ = _limiter({"rate": "600/min", "cost_budget": "30s/min"})
    counting = _Counting()
    limiter._lock = counting
    for _ in range(7):
        limiter.charge("k_a", 1.0)
    assert counting.entries == 7


def test_the_policy_is_resolved_outside_the_lock():
    """The defect: a keyring scan under a lock held from the event loop thread.

    `policy_for` walks every record, and `admit` runs on that thread. Resolving
    under the lock serialises the whole gate behind one request.
    """
    seen = []

    class _Watching:
        def __init__(self):
            self.held = False
            self._real = threading.Lock()

        def __enter__(self):
            self.held = True
            return self._real.__enter__()

        def __exit__(self, *exc):
            self.held = False
            return self._real.__exit__(*exc)

    watching = _Watching()
    limits = rl.resolve_limits({"rate": "600/min"}, rl.ServeDefaults())

    def policy_source(key_id):
        seen.append(watching.held)
        return limits

    limiter = rl.Limiter(policy_source, now=_Clock())
    limiter._lock = watching
    limiter.admit("k_a")
    limiter.charge("k_a", 1.0)
    assert seen and not any(seen), (
        "the policy was read with the lock held, so every request serialises "
        "behind one keyring scan")


def test_two_keys_do_not_share_a_bucket():
    limiter, _ = _limiter({"rate": "1/min", "burst": "4"})
    for _ in range(4):
        assert limiter.admit("k_a").admitted
    assert not limiter.admit("k_a").admitted
    assert limiter.admit("k_b").admitted, "one key's flood refused another's call"


def test_debt_carries_and_refills_on_the_same_clock():
    """The defect: two clocks, one of which a test can hold still.

    Held at t=0 the debt must not refill, and advanced it must come back at the
    stated rate. With the limiter reading its own `time.monotonic` the first
    half passes anyway on a fast machine, which is why both halves are here.
    """
    clock = _Clock()
    limiter, _ = _limiter({"cost_budget": "30s/min"}, clock=clock)
    limiter.admit("k_a")
    for _ in range(4):
        limiter.charge("k_a", 8_000.0)
    assert limiter._buckets["k_a"][1].tokens == pytest.approx(-2_000.0)
    assert not limiter.admit("k_a").admitted

    clock.now = 4.0    # 4 s at 500 ms/s puts 2_000 ms back.
    assert limiter.admit("k_a").admitted


def test_build_limiter_returns_none_only_when_nothing_could_carry_a_quota():
    """A token-only server with no default has no key and no configured limit."""
    assert rl.build_limiter(None, None) is None
    assert rl.build_limiter(None, rl.ServeDefaults()) is None
    assert rl.build_limiter(None, _defaults(rate="60/min")) is not None

    class _Ring:
        def policy_for(self, key_id):
            return {"rate": "60/min"}

    assert rl.build_limiter(_Ring(), None) is not None


def test_a_token_only_server_with_a_default_never_calls_policy_for():
    """The defect: `AttributeError` on the first request of a token-only server.

    That is the most exposed configuration contextlake ships and the first one
    an operator who sets `default_rate` will try. Resolution must start at the
    config tier when there is no keyring to ask.
    """
    limiter = rl.build_limiter(None, _defaults(rate="2/min", burst="4"),
                               now=_Clock())
    assert limiter is not None
    for _ in range(4):
        assert limiter.admit("shared-token").admitted
    assert not limiter.admit("shared-token").admitted


def test_the_module_imports_nothing_from_the_server_or_the_sdk():
    """Local-first property P1, read off the source rather than off sys.modules.

    An in-process check is decided by whatever the rest of the session already
    imported. This reads the text: nothing here may name the server, the store,
    the keystore or the SDK.
    """
    import pathlib

    source = pathlib.Path(rl.__file__).read_text(encoding="utf-8")
    body = "\n".join(line for line in source.splitlines()
                     if line.lstrip().startswith(("import ", "from ")))
    for banned in ("kb.server", ".server", "kb.store", ".store", "keyfile",
                   "mcp"):
        assert banned not in body, (
            f"kb/ratelimit.py imports {banned!r}; it must stay loadable with no "
            "server, no store and no SDK")


# ---------------------------------------------------------------------------
# the [serve] defaults
# ---------------------------------------------------------------------------


def test_the_serve_defaults_parse_and_name_the_config_key_they_refuse():
    """The message names the key an operator has to go and edit.

    `parse_rate`'s own message names the flag, which is the wrong address when
    the value came out of kb.toml.
    """
    parsed = rl.parse_serve_defaults({"default_rate": "60/min",
                                      "default_burst": "30",
                                      "default_cost_budget": "30s/min"})
    assert parsed.rate.text == "60/min"
    assert parsed.burst == 30
    assert parsed.cost_budget.text == "30s/min"

    with pytest.raises(rl.LimitError, match=r"\[serve\] default_rate"):
        rl.parse_serve_defaults({"default_rate": "60"})
    with pytest.raises(rl.LimitError, match=r"\[serve\] default_cost_budget"):
        rl.parse_serve_defaults({"default_cost_budget": "30s"})


def test_the_serve_defaults_are_unset_out_of_the_box():
    """The defect: an upgrade starting to 429 keys that worked yesterday.

    9.1.0 is public. A built-in `default_rate` would apply a quota nobody typed
    to every key already in every operator's file. `default_burst` keeps a
    built-in because it only has meaning beside a rate.
    """
    empty = rl.parse_serve_defaults({})
    assert empty.rate is None and empty.cost_budget is None
    assert empty.is_empty()


def test_validate_policy_refuses_a_rate_and_tolerates_an_inert_burst():
    """A key file is refused for an unparseable RATE, not for an inert burst.

    A burst beside no rate binds nothing, and refusing to serve over it would
    stop every other key in the file for a value that limits nobody.
    """
    with pytest.raises(rl.LimitError):
        rl.validate_policy({"rate": "60"})
    rl.validate_policy({"burst": "9"})
    rl.validate_policy({"rate": "60/min", "burst": "20",
                        "cost_budget": "30s/min"})
