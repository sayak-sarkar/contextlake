"""Tests for the MCP key format, digest and record (`contextlake.kb.keys`).

Every key in this file is synthetic. The byte vectors are 32 zero bytes,
``bytes(range(32))`` and two arithmetic edge values, so no string here can be
mistaken for a key anybody issued, and nothing written to disk leaves tmp_path.
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import inspect
import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

from contextlake.kb import keys
from contextlake.schedule import recommend

# The stamp format, written out rather than read from the module, so a change
# to TS_FORMAT fails here instead of being agreed with silently.
STAMP = "%Y-%m-%dT%H:%M:%SZ"

# Deterministic bodies. Named for what each one exercises.
ZERO_BYTES = b"\x00" * 32                       # body value 0: 43 pad characters
JUST_UNDER_BYTES = (62 ** 42 - 1).to_bytes(32, "big")   # 42 characters, 1 pad
SHORT_CRC_BYTES = (4).to_bytes(32, "big")       # its CRC32 is below 62**5
SAMPLE_BYTES = bytes(range(32))                 # the mutation sweep subject

VECTORS = (ZERO_BYTES, JUST_UNDER_BYTES, SHORT_CRC_BYTES, SAMPLE_BYTES)

SWEEP = 10_000


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(STAMP)


def used_names(source) -> set[str]:
    """Every identifier the code USES, with comments and docstrings excluded.

    A plain text grep cannot separate ``hashlib.pbkdf2_hmac(...)`` from a
    docstring paragraph explaining why this module does not call it, and this
    module is required to carry that paragraph. The parse tree can: a docstring
    is a string constant and is never collected here.
    """
    tree = ast.parse(source) if isinstance(source, str) else source
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.update(node.name.split("."))
            if node.asname:
                names.add(node.asname)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.update(node.module.split("."))
    return names


def second_module_object():
    """A second, independent copy of the module, loaded from the same file.

    ``importlib.reload`` would rebind the module every other test in the
    session holds, and ``KeyRecord`` would become a different class object
    part-way through a run. This gives the same fresh module state with none of
    that: a private module name under the same package, so the relative import
    of ``parse_duration`` still resolves, and nothing in ``sys.modules`` that
    another test reads is touched.
    """
    name = "contextlake.kb._keys_restart_probe"
    spec = importlib.util.spec_from_file_location(name, keys.__file__)
    module = importlib.util.module_from_spec(spec)
    # Registered while it executes because `dataclass` resolves annotations
    # through `sys.modules[__name__]`, then removed so the probe leaves nothing
    # behind for the next test.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    assert module is not keys
    assert sys.modules["contextlake.kb.keys"] is keys
    return module


def module_source() -> str:
    return pathlib.Path(keys.__file__).read_text(encoding="utf-8")


def function_node(name: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(module_source())):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is not a function in keys.py")


# ---------------------------------------------------------------------------
# S4.2.1 - the format
# ---------------------------------------------------------------------------


def test_the_pinned_vectors_encode_to_the_expected_strings():
    """The padding branches, pinned to values, not to a draw.

    A 10,000-key sweep hits these cases at 1.647% and 21.33%, which is a rate,
    not a guarantee. These four are the cases themselves.
    """
    assert keys.KEY_PREFIX == "ctxlake_"
    assert keys.ALPHABET.startswith("0123456789ABC")
    assert keys.ALPHABET.endswith("xyz")
    assert len(keys.ALPHABET) == 62
    assert keys.KEY_LEN == 57
    assert keys.BODY_LEN == 43
    assert keys.CHECKSUM_LEN == 6

    # A body of zero is 43 pad characters. Unpadded it is one character.
    assert keys.encode_body(ZERO_BYTES) == "0" * 43
    # One below 62**42 needs 42 characters, so exactly one pad character.
    assert keys.encode_body(JUST_UNDER_BYTES) == "0" + "z" * 42
    # 62**5 = 916,132,832 and this body's CRC32 is 831,321,275, which is below
    # it, so the checksum field needs a pad character too.
    short = keys.key_from_bytes(SHORT_CRC_BYTES)
    assert short[51:] == "0uG8df"
    assert keys.key_from_bytes(ZERO_BYTES)[51:] == "10JHL0"
    # The digest of the zero-byte key, pinned. Recomputing sha256 here would
    # assert that sha256 equals sha256.
    zero_key = keys.key_from_bytes(ZERO_BYTES)
    assert keys.digest(zero_key) == (
        "71eefc8810a3a87529d4827e115e53b73a82684b3f81f021f4312786249ac03c")


def test_every_minted_key_is_57_chars():
    """Lengths are collected, never the keys.

    A failing assertion prints what it collected, and this suite must not put
    key material in a pytest report or a CI log.
    """
    seen: dict[int, int] = {}
    for _ in range(SWEEP):
        key, _digest = keys.mint()
        seen[len(key)] = seen.get(len(key), 0) + 1
    assert sorted(seen) == [57], seen
    for raw in VECTORS:
        assert len(keys.key_from_bytes(raw)) == 57


def test_the_checksum_field_is_always_six_chars():
    seen: dict[int, int] = {}
    for _ in range(SWEEP):
        key, _digest = keys.mint()
        seen[len(key[51:])] = seen.get(len(key[51:]), 0) + 1
    assert sorted(seen) == [6], seen
    for raw in VECTORS:
        assert len(keys.key_from_bytes(raw)[51:]) == 6


def test_the_body_uses_only_the_base62_alphabet():
    allowed = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
    stray = 0
    for _ in range(SWEEP):
        key, _digest = keys.mint()
        stray += sum(1 for char in key[8:] if char not in allowed)
    assert stray == 0


def test_the_body_round_trips_to_the_drawn_bytes():
    import secrets as _secrets

    mismatches = 0
    for _ in range(SWEEP):
        raw = _secrets.token_bytes(32)
        if keys.decode_body(keys.encode_body(raw)) != raw:
            mismatches += 1
    assert mismatches == 0
    for raw in VECTORS:
        assert keys.decode_body(keys.key_from_bytes(raw)[8:51]) == raw


def test_a_flipped_body_character_fails_the_checksum():
    """Every position, every replacement. Not one flip and not one position.

    CRC-32 detects every error burst up to 32 bits and one substituted ASCII
    character is a burst of 8, so this passes by mechanism, not by draw. The
    sweep covers all 57 positions rather than the body's 43, because a
    checksum computed over a PREFIX of the key would still refuse a flip in
    the part it covers and pass every flip after it. Off-alphabet replacements
    are in the list so the alphabet branch is exercised too.
    """
    key = keys.key_from_bytes(SAMPLE_BYTES)
    assert keys.check_format(key) is True

    replacements = keys.ALPHABET + "+/=_- "
    checked = refused = 0
    for position in range(len(key)):
        for char in replacements:
            if char == key[position]:
                continue
            checked += 1
            if not keys.check_format(key[:position] + char + key[position + 1:]):
                refused += 1
    assert checked >= 57 * 60
    assert refused == checked


def test_check_format_accepts_minted_keys_and_refuses_everything_else():
    for _ in range(200):
        key, _digest = keys.mint()
        assert keys.check_format(key) is True
    for raw in VECTORS:
        assert keys.check_format(keys.key_from_bytes(raw)) is True

    good = keys.key_from_bytes(SAMPLE_BYTES)
    refused = [
        None,
        "",
        b"x" * 57,
        good[:-1],                       # 56 characters
        good + "0",                      # 58 characters
        good.replace("ctxlake_", "ctxlaka_", 1),
        good[:8] + "-" + good[9:],       # off-alphabet in the body
        good[:8] + "/" + good[9:],
        good[:56] + "/",                 # off-alphabet in the checksum
        "ctxlake_" + "0" * 49,           # right length, wrong checksum
        good.upper(),
    ]
    for value in refused:
        assert keys.check_format(value) is False, value


# ---------------------------------------------------------------------------
# S4.2.1 - the digest
# ---------------------------------------------------------------------------


def test_mint_returns_a_key_and_a_digest_and_nothing_else():
    minted = keys.mint()
    assert len(minted) == 2
    key, key_digest = minted
    assert len(key) == 57
    assert len(key_digest) == 64
    assert set(key_digest) <= set("0123456789abcdef")
    assert keys.digest(key) == key_digest
    assert keys.DIGEST_LEN == 64


def test_the_format_check_runs_before_the_digest(monkeypatch):
    """Both directions, one counter.

    The 0-call half alone passes on a digest function nothing ever calls, so
    the well-formed key and its 1 call are asserted in the same test.
    """
    key, _minted = keys.mint()          # minted before the counter is installed
    stored = keys.digest(key)
    malformed = "ctxlake_" + "0" * 49
    assert len(malformed) == 57

    calls = []
    real = keys.digest

    def counting(value):
        calls.append(value)
        return real(value)

    monkeypatch.setattr(keys, "digest", counting)

    assert keys.verify_key(malformed, stored) is False
    assert len(calls) == 0
    assert keys.verify_key(key, stored) is True
    assert len(calls) == 1


def test_the_digest_takes_no_salt_and_names_no_kdf():
    assert list(inspect.signature(keys.digest).parameters) == ["key"]

    names = used_names(module_source())
    for banned in ("scrypt", "pbkdf2", "pbkdf2_hmac", "bcrypt", "argon2"):
        hits = sorted(name for name in names if banned in name)
        assert hits == [], hits

    # Positive control. An AST walk over the wrong node set finds nothing in
    # any input, and would pass this test on a module that calls a KDF.
    control = used_names("import hashlib\nv = hashlib.pbkdf2_hmac('sha256', b'', b'', 1)\n")
    assert "pbkdf2_hmac" in control

    # The prose the scan is required to ignore, and the measured numbers that
    # are the reason. A scan that read the docstring would refuse this module.
    doc = keys.__doc__ or ""
    for fragment in ("scrypt", "pbkdf2_hmac", "0.28 us", "27.9 ms", "65.1 ms"):
        assert fragment in doc


def test_the_secret_comparison_is_constant_time():
    inside = used_names(function_node("verify_key"))
    assert "compare_digest" in inside

    # Positive control on both sides: the scanner finds the call when it is
    # there, and does not find it in the plain `==` this test exists to refuse.
    assert "compare_digest" in used_names("import hmac\nhmac.compare_digest(a, b)\n")
    assert "compare_digest" not in used_names("x = (a == b)\n")


def test_verify_key_both_directions():
    key = keys.key_from_bytes(SAMPLE_BYTES)
    other = keys.key_from_bytes(ZERO_BYTES)
    stored = keys.digest(key)

    assert keys.verify_key(key, stored) is True
    assert keys.verify_key(other, stored) is False
    assert keys.verify_key(other, keys.digest(other)) is True

    # A stored digest differing in its last character only.
    last = "0" if stored[-1] != "0" else "1"
    assert keys.verify_key(key, stored[:-1] + last) is False

    # A presented key differing in its last character only is malformed, so it
    # is refused one step earlier. Both refusals matter and they are different
    # diagnoses.
    mutated = key[:-1] + ("0" if key[-1] != "0" else "1")
    assert keys.check_format(mutated) is False
    assert keys.verify_key(mutated, stored) is False
    assert keys.verify_key(None, stored) is False


def test_the_checksum_docstring_says_it_is_not_a_security_control():
    doc = keys._checksum.__doc__ or ""
    assert "typo filter" in doc
    assert "secret-scanner anchor" in doc
    assert "NOT a" in doc and "security control" in doc


# ---------------------------------------------------------------------------
# S4.2.2 - the record
# ---------------------------------------------------------------------------


def test_the_record_field_names_are_pinned():
    """Adding a field has to be typed here.

    That is the whole effect. This pin buys a checkpoint where the durable-name
    rule gets re-read (every scope is a repo id, a path glob or a tool name,
    never a store-internal row id, so a key survives a store rebuild). No test
    can tell a durable name from a row id by looking at the string, so it is
    not enforcement and must not be written up as any.
    """
    assert keys.RECORD_FIELDS == (
        "id", "name", "digest", "created_at", "expires_at", "revoked_at",
        "revoked_reason", "rotated_from", "rotated_to", "grant_version",
        "policy",
    )
    record, _key = keys.create([], "pinned")
    assert set(record.to_dict()) == set(keys.RECORD_FIELDS)


def test_the_same_key_bytes_twice_get_two_different_ids():
    key = keys.key_from_bytes(SAMPLE_BYTES)
    records: list[keys.KeyRecord] = []
    first, _one = keys.create(records, "one", key=key)
    second, _two = keys.create(records, "two", key=key)

    assert first.digest == second.digest       # the fixture can show the difference
    assert first.id != second.id
    assert first.id.startswith("k_") and len(first.id) == 8


def test_an_id_collision_retries(monkeypatch):
    drawn = iter(["k_aaaaaa", "k_aaaaaa", "k_bbbbbb"])
    monkeypatch.setattr(keys, "_random_id", lambda: next(drawn))

    records: list[keys.KeyRecord] = []
    first, _one = keys.create(records, "one")
    second, _two = keys.create(records, "two")

    assert (first.id, second.id) == ("k_aaaaaa", "k_bbbbbb")


def test_the_id_retry_is_bounded(monkeypatch):
    monkeypatch.setattr(keys, "_random_id", lambda: "k_aaaaaa")
    records: list[keys.KeyRecord] = []
    keys.create(records, "one")
    with pytest.raises(RuntimeError):
        keys.create(records, "two")


def test_the_default_expiry_is_ninety_days_and_never_is_typed():
    now = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    records: list[keys.KeyRecord] = []

    default, _a = keys.create(records, "default", now=now)
    assert default.created_at == "2026-03-01T12:00:00Z"
    assert default.expires_at == "2026-05-30T12:00:00Z"     # 90 days
    assert default.expires_at is not None

    typed, _b = keys.create(records, "forever", expires="never", now=now)
    assert typed.expires_at is None
    assert typed.state(now) == "live"

    upper, _c = keys.create(records, "shouty", expires="NEVER", now=now)
    assert upper.expires_at is None

    short, _d = keys.create(records, "short", expires="7d", now=now)
    assert short.expires_at == "2026-03-08T12:00:00Z"


def test_parse_expiry_reuses_the_shared_duration_parser(monkeypatch):
    assert keys.parse_duration is recommend.parse_duration

    calls = []

    def counting(text):
        calls.append(text)
        return 604800.0

    monkeypatch.setattr(keys, "parse_duration", counting)

    assert keys.parse_expiry("7d") == 604800.0
    assert calls == ["7d"]
    assert keys.parse_expiry("never") is None
    assert calls == ["7d"]                       # the mode word never reaches it
    keys.parse_expiry(None)
    assert calls == ["7d", "90d"]                # the default is a duration


def test_a_revoked_and_expired_record_reads_revoked():
    now = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    records: list[keys.KeyRecord] = []
    record, _key = keys.create(records, "both", expires="1d", now=now)
    keys.revoke(records, record, reason="left the team", now=now)

    later = now + timedelta(days=30)
    assert record.state(later) == "revoked"
    assert record.state(now) == "revoked"

    live, _a = keys.create(records, "live", expires="30d", now=now)
    expired, _b = keys.create(records, "expired", expires="1d", now=now)
    mid = now + timedelta(days=2)
    assert live.state(mid) == "live"
    assert expired.state(mid) == "expired"
    assert {r.state(mid) for r in records} == {"revoked", "live", "expired"}


def test_revoke_keeps_the_record_and_stamps_revoked_at():
    now = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    records: list[keys.KeyRecord] = []
    keys.create(records, "kept", expires="30d", now=now)
    record, _key = keys.create(records, "going", expires="30d", now=now)
    assert len(records) == 2
    live_before = sum(1 for r in records if r.state(now) == "live")

    assert keys.revoke(records, record, reason="laptop lost", now=now) is True

    assert len(records) == 2                     # a tombstone, never a delete
    assert record in records
    assert record.revoked_at == "2026-03-01T12:00:00Z"
    assert record.revoked_reason == "laptop lost"
    assert record.digest and len(record.digest) == 64
    live_after = sum(1 for r in records if r.state(now) == "live")
    assert live_before - live_after == 1

    # A record the keyring does not hold is refused, so a tombstone nothing
    # will load never reads as success.
    stray = keys.KeyRecord(id="k_ffffff", name="stray", digest="e" * 64,
                           created_at="2026-03-01T12:00:00Z")
    with pytest.raises(ValueError):
        keys.revoke(records, stray, now=now)

    # Re-revoking leaves the first answer alone.
    assert keys.revoke(records, record, reason="second thoughts",
                       now=now + timedelta(days=1)) is False
    assert record.revoked_at == "2026-03-01T12:00:00Z"
    assert record.revoked_reason == "laptop lost"


def test_rotate_keeps_the_old_digest_and_adds_one_record():
    now = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    records: list[keys.KeyRecord] = []
    policy = {"tools": ["read"], "repos": ["acme/*"], "limits": {"rate": "60/min"}}
    old, old_key = keys.create(records, "alice-laptop", expires="30d",
                               policy=policy, now=now)
    old_digest = old.digest

    new, new_key = keys.rotate(records, old, overlap="7d", now=now)

    assert len(records) == 2
    assert new.id != old.id
    assert new.digest != old.digest
    assert new_key != old_key
    assert old.digest == old_digest              # never an in-place swap
    assert keys.verify_key(old_key, old.digest) is True
    assert keys.verify_key(new_key, new.digest) is True

    # The policy block is copied field for field, and it is a copy.
    assert new.policy == old.policy
    assert new.policy is not old.policy
    assert new.policy["limits"] is not old.policy["limits"]
    new.policy["limits"]["rate"] = "1/min"
    new.policy["tools"].append("write")
    assert old.policy == policy
    assert copy.deepcopy(policy) == {"tools": ["read"], "repos": ["acme/*"],
                                     "limits": {"rate": "60/min"}}


def test_rotate_links_both_ways():
    now = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    records: list[keys.KeyRecord] = []
    old, _key = keys.create(records, "alice-laptop", expires="30d", now=now)
    new, _new_key = keys.rotate(records, old, overlap="7d", now=now)

    assert old.rotated_to == new.id
    assert new.rotated_from == old.id
    assert old.rotated_from is None
    assert new.rotated_to is None


def test_rotate_never_extends_a_key_that_expires_sooner():
    """min(existing, now + overlap). Three cases, and the third has no ticket.

    A key set to never expire has no `existing` to take a minimum against, so
    it takes the overlap window. Rotating it is what retires it.
    """
    now = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)

    sooner_records: list[keys.KeyRecord] = []
    sooner, _a = keys.create(sooner_records, "expires-tomorrow", expires="1d", now=now)
    assert sooner.expires_at == "2026-03-02T12:00:00Z"
    keys.rotate(sooner_records, sooner, overlap="7d", now=now)
    assert sooner.expires_at == "2026-03-02T12:00:00Z"          # unchanged

    later_records: list[keys.KeyRecord] = []
    later, _b = keys.create(later_records, "expires-in-30", expires="30d", now=now)
    assert later.expires_at == "2026-03-31T12:00:00Z"
    keys.rotate(later_records, later, overlap="7d", now=now)
    assert later.expires_at == "2026-03-08T12:00:00Z"           # now + 7d

    never_records: list[keys.KeyRecord] = []
    never, _c = keys.create(never_records, "no-expiry", expires="never", now=now)
    assert never.expires_at is None
    keys.rotate(never_records, never, overlap="7d", now=now)
    assert never.expires_at == "2026-03-08T12:00:00Z"


def test_rotate_leaves_the_keyring_alone_when_it_raises():
    """Every way rotate can raise happens before the new record is appended.

    A half-done rotate is worse than a refused one: the file carries a new key
    the old record does not point at, so the audit trail reads in one
    direction only and nothing says so.
    """
    now = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    records: list[keys.KeyRecord] = []
    old, _key = keys.create(records, "alice-laptop", expires="30d", now=now)

    for bad in (datetime(2026, 3, 1, 12, 0, 0), None):
        with pytest.raises((ValueError, TypeError)):
            if bad is None:
                keys.rotate(records, old, overlap="not a duration", now=now)
            else:
                keys.rotate(records, old, overlap="7d", now=bad)
        assert len(records) == 1
        assert old.expires_at == "2026-03-31T12:00:00Z"
        assert old.rotated_to is None


def test_prune_never_removes_a_live_record():
    """Four records, and only the two terminal ones older than the cutoff go.

    `created_at` is set on every record on purpose. A prune keyed on the
    creation date with no liveness test would drop three of these four, and the
    live one is the record that must survive both readings.
    """
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    cutoff = now - timedelta(days=30)

    live = keys.KeyRecord(
        id="k_000001", name="live", digest="a" * 64,
        created_at=stamp(now - timedelta(days=100)),
        expires_at=stamp(now + timedelta(days=30)))
    revoked_old = keys.KeyRecord(
        id="k_000002", name="revoked-old", digest="b" * 64,
        created_at=stamp(now - timedelta(days=100)),
        expires_at=stamp(now + timedelta(days=30)),
        revoked_at=stamp(now - timedelta(days=60)))
    expired_old = keys.KeyRecord(
        id="k_000003", name="expired-old", digest="c" * 64,
        created_at=stamp(now - timedelta(days=100)),
        expires_at=stamp(now - timedelta(days=60)))
    revoked_recent = keys.KeyRecord(
        id="k_000004", name="revoked-recent", digest="d" * 64,
        created_at=stamp(now - timedelta(days=10)),
        expires_at=stamp(now + timedelta(days=30)),
        revoked_at=stamp(now - timedelta(days=1)))

    records = [live, revoked_old, expired_old, revoked_recent]
    assert [r.state(now) for r in records] == ["live", "revoked", "expired", "revoked"]

    removed = keys.prune(records, cutoff, now=now)

    assert len(records) == 2
    assert {r.id for r in removed} == {"k_000002", "k_000003"}
    assert live in records
    assert revoked_recent in records


def test_expiry_survives_a_restart(tmp_path):
    """A wall-clock expiry read back in a fresh module state.

    A monotonic clock has no meaning across processes, so a restart would reset
    every expiry and this record would read `live` forever.
    """
    now = datetime.now(timezone.utc)
    records: list[keys.KeyRecord] = []
    record, _key = keys.create(records, "already-lapsed", expires="1s",
                               now=now - timedelta(seconds=2))
    assert record.expires_at is not None

    path = tmp_path / "mcp-keys.json"
    path.write_text(json.dumps({"version": 1, "keys": [record.to_dict()]}),
                    encoding="utf-8")

    fresh = second_module_object()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    reloaded = fresh.KeyRecord.from_dict(loaded["keys"][0])

    assert reloaded.expires_at == record.expires_at
    assert reloaded.state() == "expired"


def test_the_module_never_reads_a_monotonic_clock():
    names = used_names(module_source())
    assert "monotonic" not in names

    # Positive control, and the prose the scan has to ignore.
    assert "monotonic" in used_names("import time\nx = time.monotonic()\n")
    assert "time.monotonic" in (keys._now.__doc__ or "")


def test_a_record_round_trips_through_a_dict():
    records: list[keys.KeyRecord] = []
    record, _key = keys.create(records, "round-trip", policy={"tools": ["read"]})
    assert keys.KeyRecord.from_dict(record.to_dict()) == record

    with pytest.raises(ValueError):
        keys.KeyRecord.from_dict({**record.to_dict(), "version": 2})
    with pytest.raises(ValueError):
        keys.KeyRecord.from_dict({"id": "k_000001", "name": "x"})


def test_an_unreadable_expiry_reads_expired():
    """A corrupt stamp must not authenticate anybody."""
    record = keys.KeyRecord(id="k_000001", name="corrupt", digest="a" * 64,
                            created_at="2026-03-01T12:00:00Z",
                            expires_at="not a date")
    assert record.state(datetime(2026, 3, 1, tzinfo=timezone.utc)) == "expired"


# ---------------------------------------------------------------------------
# S4.2.2 - the refusal clock. `state()` runs on the auth path, so what it
# COSTS is a security property and not a performance note.
# ---------------------------------------------------------------------------


def count_parses(monkeypatch) -> list:
    """Count every `_parse_ts` call. Returns the list the counter appends to.

    Patched at module scope, which is where `_refresh_deadline` looks the name
    up, so the count covers construction as well as `state()`.
    """
    calls: list = []
    real = keys._parse_ts

    def counting(text):
        calls.append(text)
        return real(text)

    monkeypatch.setattr(keys, "_parse_ts", counting)
    return calls


def test_the_expiry_is_parsed_at_construction_and_never_by_state(monkeypatch):
    """The strptime that made a found key slower than an unknown one.

    `state()` used to run `datetime.strptime` on `expires_at` every time it was
    asked, and it is asked once per authenticated request. That put 3 us on the
    known-key path and nothing on the unknown one, so an unauthenticated caller
    could time two byte-identical 401s and learn whether a presented key is in
    the operator's key file.
    """
    calls = count_parses(monkeypatch)
    now = datetime.now(timezone.utc)

    record = keys.KeyRecord(id="k_000001", name="lapsing", digest="a" * 64,
                            created_at=stamp(now),
                            expires_at=stamp(now + timedelta(days=90)))

    # The positive control for the zero below: the counter DOES see a parse,
    # and it sees it where the parse belongs, on the construction path.
    assert calls == [record.expires_at], calls

    calls.clear()
    assert [record.state(now) for _ in range(50)] == ["live"] * 50
    assert calls == [], calls

    # The other two verdicts cost no parse either, so the answer cannot be read
    # off the work done.
    revoked = keys.KeyRecord(id="k_000002", name="withdrawn", digest="b" * 64,
                             created_at=stamp(now), expires_at=record.expires_at,
                             revoked_at=stamp(now))
    lapsed = keys.KeyRecord(id="k_000003", name="lapsed", digest="c" * 64,
                            created_at=stamp(now),
                            expires_at=stamp(now - timedelta(days=1)))
    never = keys.KeyRecord(id="k_000004", name="forever", digest="d" * 64,
                           created_at=stamp(now))
    calls.clear()
    assert [r.state(now) for r in (record, revoked, lapsed, never)] == [
        "live", "revoked", "expired", "live"]
    assert calls == [], calls


def test_a_reassigned_expiry_is_never_served_from_the_stale_parse():
    """`rotate` writes `expires_at` after the record was built.

    A cache that is filled once and never checked would keep serving the old
    deadline, so a rotated key would go on reading `live` past the handover
    window it was given.
    """
    now = datetime.now(timezone.utc)
    records: list[keys.KeyRecord] = []
    old, _key = keys.create(records, "rotating", expires="never", now=now)
    assert old.state(now) == "live"

    keys.rotate(records, old, overlap="1s", now=now)

    assert old.expires_at == stamp(now + timedelta(seconds=1))
    assert old.state(now) == "live"
    assert old.state(now + timedelta(seconds=2)) == "expired"

    # Assigned directly, not through rotate, because a caller outside this
    # module can do that and the cache has to survive it.
    old.expires_at = stamp(now + timedelta(days=30))
    assert old.state(now + timedelta(seconds=2)) == "live"


def test_the_decoy_is_live_so_it_pays_the_longest_path(monkeypatch):
    """`decoy_state` exists to cost what a real lookup costs.

    A decoy that read `revoked` or `expired` would take a shorter path than the
    key it is standing in for, and the gap it exists to close would reopen by
    however much the difference is.
    """
    assert keys.decoy_state() == "live"
    assert keys._DECOY.revoked_at is None
    assert keys._DECOY.expires_at is not None

    # Built from the clock plus a delta, never from a literal date. A literal
    # lapses one day and the decoy silently starts taking the short path.
    parsed = datetime.strptime(keys._DECOY.expires_at, STAMP).replace(
        tzinfo=timezone.utc)
    assert parsed - datetime.now(timezone.utc) > timedelta(days=36_000)

    # It reads the clock, which is the other half of the work an unknown key
    # was skipping: `Keyring.resolve` calls `now()` only after the dict hits.
    reads: list = []
    monkeypatch.setattr(keys, "_now", lambda: (reads.append(1),
                                               datetime.now(timezone.utc))[1])
    assert keys.decoy_state() == "live"
    assert len(reads) == 1, reads

    # And it parses nothing per call.
    calls = count_parses(monkeypatch)
    keys.decoy_state()
    assert calls == [], calls


def test_the_parsed_deadline_is_not_a_record_field():
    """The cache must not reach the key file.

    An annotated class attribute inside a dataclass becomes a field, a field
    lands in `RECORD_FIELDS`, and `from_dict` then refuses every file written
    by a version that did not have it.
    """
    assert "_deadline" not in keys.RECORD_FIELDS
    assert "_deadline_src" not in keys.RECORD_FIELDS
    record, _key = keys.create([], "field-check")
    assert "_deadline" not in record.to_dict()
    assert keys.KeyRecord.from_dict(record.to_dict()) == record

    # A record built without `__post_init__` re-parses instead of raising
    # AttributeError, which inside the gate would be a 500 on the one path that
    # must answer 401 or nothing.
    bare = keys.KeyRecord.__new__(keys.KeyRecord)
    object.__setattr__(bare, "revoked_at", None)
    object.__setattr__(bare, "expires_at", record.expires_at)
    assert bare.state() == "live"
