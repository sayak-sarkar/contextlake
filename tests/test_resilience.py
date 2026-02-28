"""Tests for bug #2 (AdaptiveWorkerPool) and #3 (retry/backoff wiring)."""

import pytest

from contextlake.core import AdaptiveWorkerPool, classify_error, retry_with_backoff


def test_pool_initializes_current_workers():
    # Bug #2: __init__ used to never set current_workers -> AttributeError.
    pool = AdaptiveWorkerPool(max_workers=8, min_workers=2, error_threshold=0.5)
    assert pool.get_worker_count() == 8


def test_pool_shrinks_under_high_error_rate():
    pool = AdaptiveWorkerPool(max_workers=8, min_workers=2, error_threshold=0.5)
    for _ in range(pool.window_size):
        pool.record_result(False)
    assert pool.get_worker_count() < 8


def test_pool_grows_back_when_healthy():
    pool = AdaptiveWorkerPool(max_workers=8, min_workers=2, error_threshold=0.5)
    pool.current_workers = 4
    for _ in range(pool.window_size):
        pool.record_result(True)
    assert pool.get_worker_count() > 4


def test_pool_never_below_min():
    pool = AdaptiveWorkerPool(max_workers=4, min_workers=2, error_threshold=0.1)
    for _ in range(100):
        pool.record_result(False)
    assert pool.get_worker_count() >= 2


@pytest.mark.parametrize(
    "msg,expected",
    [
        ("Connection reset by peer", "network"),
        ("operation timed out", "timeout"),
        ("could not resolve host: lookup failed", "dns"),
        ("SSL handshake failed", "tls"),
        ("something else", "other"),
        ("fatal: couldn't find remote ref feature/x", "missing-ref"),
        ("fatal: Not possible to fast-forward, aborting.", "diverged"),
        ("hint: You have divergent branches", "diverged"),
        # a dropped TLS connection ("eof") is transient/network, not a tls failure
        ("TLS connect error: error:0A000126:SSL routines::unexpected eof while reading", "network"),
    ],
)
def test_classify_error(msg, expected):
    assert classify_error(msg) == expected


def test_retry_fails_fast_on_missing_ref(no_sleep):
    calls = {"n": 0}

    def gone():
        calls["n"] += 1
        raise RuntimeError("fatal: couldn't find remote ref feature/x")

    with pytest.raises(RuntimeError):
        retry_with_backoff(gone, max_retries=5)
    assert calls["n"] == 1  # deleted upstream -> not retried


def test_retry_fails_fast_on_diverged(no_sleep):
    calls = {"n": 0}

    def diverged():
        calls["n"] += 1
        raise RuntimeError("fatal: Not possible to fast-forward, aborting.")

    with pytest.raises(RuntimeError):
        retry_with_backoff(diverged, max_retries=5)
    assert calls["n"] == 1  # diverged -> not retried


def test_retry_succeeds_after_transient_failures(no_sleep):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("connection reset")
        return "ok"

    assert retry_with_backoff(flaky, max_retries=5) == "ok"
    assert calls["n"] == 3


def test_retry_fails_fast_on_dns(no_sleep):
    calls = {"n": 0}

    def dns_fail():
        calls["n"] += 1
        raise RuntimeError("could not resolve host: lookup failed")

    with pytest.raises(RuntimeError):
        retry_with_backoff(dns_fail, max_retries=5)
    assert calls["n"] == 1  # not retried


def test_retry_reraises_after_exhaustion(no_sleep):
    def always_fail():
        raise RuntimeError("connection reset")

    with pytest.raises(RuntimeError):
        retry_with_backoff(always_fail, max_retries=3)
