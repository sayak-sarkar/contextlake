"""A count flag must refuse 0 and negatives at the parser, not absorb them downstream.

`--max-symbols 0` reached `getattr(args, "max_symbols", None) or 500` and Python's `or`
treats an explicit 0 as unset, so asking for zero symbols produced all 500 of them with
exit 0. `--max-symbols -5` was worse: it was fed straight into `symbols[:max_symbols]`,
where a negative slice silently drops the LAST five entries, while the generated page's
own text said "capped at -5 entries".

The project already had `_COUNT` for exactly this, with a comment saying zero "used to
silently mean the default instead". The flag simply never adopted it, which is the
failure this test pins: the convention existed and one flag was outside it.
"""

from __future__ import annotations

import pytest

from contextlake.cli import build_parser


def _parse(argv):
    return build_parser().parse_args(argv)


@pytest.mark.parametrize("value", ["0", "-5", "-1"])
def test_max_symbols_refuses_a_value_that_cannot_mean_a_cap(value):
    with pytest.raises(SystemExit) as e:
        _parse(["kb", "docs", "--max-symbols", value])
    assert e.value.code == 2


@pytest.mark.parametrize("value", ["1", "500", "2000"])
def test_max_symbols_still_accepts_a_real_cap(value):
    assert _parse(["kb", "docs", "--max-symbols", value]).max_symbols == int(value)


def test_min_workers_refuses_zero():
    """A pool with a zero floor is a pool that can stop working."""
    with pytest.raises(SystemExit):
        _parse(["--min-workers", "0", "mirror", "status"])


@pytest.mark.parametrize("value", ["1", "3", "6"])
def test_max_retries_accepts_a_real_attempt_budget(value):
    assert _parse(["--max-retries", value, "mirror", "status"]).max_retries == int(value)


def test_max_retries_still_refuses_a_negative():
    with pytest.raises(SystemExit):
        _parse(["--max-retries", "-1", "mirror", "status"])


def test_max_retries_refuses_zero_because_zero_attempts_cannot_work():
    """The flag is named "retries" and counts ATTEMPTS.

    `retry_with_backoff` is `for attempt in range(max_retries)`, so zero runs the body
    zero times, leaves `last_error` as None, and ends at `raise last_error` -- which
    fails with "exceptions must derive from BaseException" without ever attempting the
    operation. An earlier version of this change accepted 0 on the reasoning that "try
    once, do not retry" is a real request. It is, and it is spelled 1.
    """
    with pytest.raises(SystemExit):
        _parse(["--max-retries", "0", "mirror", "status"])


def test_the_retry_primitive_refuses_a_zero_budget_itself():
    """A config file sets this too, so the flag is not the only way in."""
    from contextlake.core import retry_with_backoff

    with pytest.raises(ValueError, match="at least 1"):
        retry_with_backoff(lambda: None, max_retries=0)


def test_one_attempt_runs_the_operation_exactly_once():
    """The break-test for the guard above: 1 must still DO something."""
    from contextlake.core import retry_with_backoff

    calls = []
    assert retry_with_backoff(lambda: calls.append(1) or "done", max_retries=1) == "done"
    assert len(calls) == 1
