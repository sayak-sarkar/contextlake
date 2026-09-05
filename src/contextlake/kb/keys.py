"""API key format, digest and record for the MCP network keyring.

Three things live here, and nothing else: the wire format of a key, the digest
that is stored in place of it, and the record that carries a key's lifecycle.
The key file, the lookup dict and the CLI verbs are separate modules.

The format
==========

::

    ctxlake_ + 43 base62 chars + 6 base62 chars
    |          |                 |
    prefix     32 random bytes   CRC32 over the 51 characters before it

57 characters. The alphabet is base62 (``0-9A-Za-z``), not base64url, because
base64url contains ``_``. The separator was chosen so that no other random
string produces one and so that a double-click selects the whole key, and a
``_`` inside the body destroys both properties.

Both variable fields are LEFT-PADDED to a fixed width, and that is the whole
reason the encoder is written out here rather than inlined:

* ``log2(62**43) = 256.03`` and ``log2(62**42) = 250.08``, so ``62**42 / 2**256
  = 1.647%`` of drawn values encode in 42 characters or fewer. Unpadded, about
  165 keys in 10,000 come out short.
* ``62**5 = 916,132,832`` is 21.33% of ``2**32``, so about 2,133 checksums in
  10,000 encode in 5 characters or fewer. 5 base62 characters cannot hold a
  CRC32 and 6 can.

Without the padding the tool mints keys of three different lengths, every
length check downstream refuses a fraction of the keys contextlake issued
itself, and the refusal is byte-identical to an unknown key. Nobody can then
tell a padding bug from a typo from the wire.

The digest
==========

``sha256`` of the whole 57-character string, hex, no salt, no pepper, no KDF.

Measured on the development machine, CPython 3.14, over a 57-character key.
The first row is the spec's own run on 2026-09-02; the second is a re-run on
2026-09-05 with ``timeit``, and it is the one to trust here because a quoted
number that nobody re-ran is how a stale baseline gets built on.

===========================  ==========  ==========
call                         2026-09-02  2026-09-05
===========================  ==========  ==========
``hashlib.sha256``              0.28 us     0.310 us
``zlib.crc32``                  0.051 us    0.068 us
``check_format`` (whole)        n/a         0.947 us
``scrypt(n=2**14,r=8,p=1)``    27.9 ms     27.40 ms
``pbkdf2_hmac(sha256,600k)``   65.1 ms     67.87 ms
===========================  ==========  ==========

The two KDFs are 88,000x and 219,000x the cost of the hash this module uses.

Do not "upgrade" this to a key-derivation function. The argument for a slow
hash is guessing rate, and it does not transfer to a secret drawn from 256
uniform random bits: there is nothing to guess. What a KDF would buy on this
path is 65 ms of CPU per unauthenticated request at an attacker cost of one
HTTP request, against a tool concurrency default of 2. That is a denial of
service this module would be inflicting on its own server.

A per-key salt costs more than the time: it forces an iterate-and-hash over
every record, which reintroduces the linear scan the O(1) digest lookup exists
to avoid. A pepper needs a store apart from the protected data, which a
local-first single binary does not have.

No key fragment is stored. Every key is admin-named, so a last-4 display hint
would spend entropy for nothing.

The refusal clock
=================

The 401 is byte-identical for every refusal class. That is worth nothing while
the TIME differs, and it did. ``Keyring.resolve`` returns ``None`` the moment
the digest lookup misses, so an unknown key skipped the clock read and the
:meth:`KeyRecord.state` call that a known key paid, and ``state`` ran
``datetime.strptime`` on ``expires_at`` every time it was asked. An
unauthenticated caller could time two 401s and learn whether a presented key is
in the operator's key file.

Two changes close it. ``expires_at`` is parsed ONCE, when the record is built,
so no request pays a ``strptime``; and the gate calls :func:`decoy_state` on
the unknown branch, so both branches read the clock once and run the same
comparisons.

THE COMPARISON THAT MATTERS IS BETWEEN TWO REFUSALS. A live key answers 200,
so an attacker holding one already knows it works. The oracle is whether a
presented key is IN THE FILE at all, and that reads as ``revoked`` or
``expired`` against ``unknown`` -- three refusals, one 401, three different
costs before the fix.

Measured on the development machine, CPython 3.14, 4,001 interleaved samples
per outcome through ``KeyAuthMiddleware._authenticate`` with no socket, median
microseconds, both columns run back to back on 2026-09-05:

============================  ======  ======
outcome                          was     now
============================  ======  ======
live key, admitted (200)       15.14    9.35
unknown key (401)               9.92    9.12
revoked key (401)               9.35    9.08
expired key (401)              14.19    9.15
``expired`` minus ``unknown``  +4.27   +0.03
``revoked`` minus ``unknown``  -0.57   -0.04
============================  ======  ======

What that buys, in the attacker's own units. A 401 over a real loopback socket
on this machine measured a 2,909 us standard deviation (n=2,000). Averaging a
difference ``d`` out of noise ``s`` takes on the order of ``(s / d) ** 2``
requests, so the ``expired``-vs-``unknown`` channel cost about 460,000 requests
per key before the fix and hundreds of millions after it: 9 billion at the
0.03 us in the table above, 530 million at the widest median the first residual
below re-measured.

What remains, with its number rather than a word. The first bullet was WRONG as
written for three rounds and is corrected here from a re-measurement, not
softened, and a residual that was missing from the list is added: a residual
note that is wrong is worse than none, because it is what a later reader trusts
instead of measuring, and a list that says "what remains" is read as complete.

* Both remaining differences are NEGATIVE and neither is zero: a ``revoked`` and
  an ``expired`` 401 are each a little FASTER than an ``unknown`` one. Over 30
  runs of 4,001 interleaved samples each, in two sessions:

  - ``revoked`` minus ``unknown``: per-session medians -0.038 and -0.047 us,
    negative in 26 of the 30 runs, whole range -0.094 to +0.057 us.
  - ``expired`` minus ``unknown``: per-session medians -0.126 and -0.088 us,
    negative in 30 of 30, whole range -0.146 to -0.032 us.

  Taking the worst session for the defender, an attacker averaging the
  difference out of the 2,909 us of loopback noise recorded above needs about
  ``(2909 / 0.126) ** 2`` requests per key for ``expired``, 530 million, and
  ``(2909 / 0.047) ** 2``, 3.8 billion, for ``revoked``. Both sit inside the
  unknown path's own 8.8-12.3 us 10th-90th spread.

  What is NOT written here any more, because it did not survive re-measurement:
  this bullet claimed ``revoked`` minus ``unknown`` had a sign that flipped run
  to run, from 8 negative and 7 positive with a median of -0.002 us. Two fresh
  sessions put it negative in 26 of 30 with medians around -0.04 us, so the
  reading it carried -- one difference real, the other indistinguishable from
  noise -- is deleted rather than softened. Both are small, both are one-sided
  in the same direction, and both are bounded by the request counts above.
  The 2,909 us loopback figure is the earlier session's and was not re-measured.

  Measured 2026-09-05 on the development machine, CPython 3.14, through
  ``KeyAuthMiddleware._authenticate`` with no socket, medians. A NEW harness:
  none for the table above exists in this repository, so the two are not the
  same run, which is also why the medians here read about 0.8 us higher.
* **The table and the bullet above measure ``_authenticate`` alone, and the
  refusal path does not end there.** ``KeyAuthMiddleware.__call__`` sends the
  401 and then calls ``server._report_refusal``, which is outside every number
  on this page. It costs, medians over 4,001 samples with stderr on a real file:
  0.61 us for ``unknown``, 0.64 for ``revoked`` and 0.67 for ``expired`` while
  the class is under ``REFUSAL_LOG_CAP``, and 0.40, 0.40 and 0.42 once the class
  is suppressed. The class gap under the cap is +0.03 us and comes from the line
  itself -- ``revoked`` and ``expired`` name the key id, ``unknown`` has nothing
  to name -- and it falls to +0.00 us suppressed.

  What bounds it is the cap, not the size: the log admits 20 lines per class per
  60-second window, so a caller sending enough requests to average anything out
  of 2,909 us of noise is measuring the suppressed cost on all but 20 of them
  per minute. At +0.03 us the under-cap gap is 8 billion requests per key by the
  same arithmetic, and only 20 per class per minute carry it at all. Named here
  because the earlier list said what remains and did not include it, and a list
  that claims to be complete is read as complete.
* A dict hit costs 0.026 us and a miss 0.021 us. That 0.005 us is inside the
  hash lookup and cannot be removed from here.
* ``malformed`` and ``bad_checksum`` cost LESS than ``unknown``, because the
  format check runs before the file is touched, and it is still not an oracle.
  The reason is the same for both: the format is PUBLISHED, so the sender
  computes the length, the alphabet and the CRC32 of their own value offline,
  for free, without asking this server. The timing tells them which of the two
  classes their own value falls in, which they knew before they sent it. Both
  are named rather than only the obvious one: ``bad_checksum`` differs from
  ``unknown`` in the CRC tail alone, so a reader could take it for the same
  value with one character wrong.

  What the gap IS was wrong here, and it is corrected rather than dropped. It
  read "one ``os.stat`` less". On the run above: ``unknown`` 9.91 us,
  ``bad_checksum`` 2.99 us, ``malformed`` 0.86 us, so the gaps are 6.89 us and
  9.06 us, while one ``os.lstat`` on that key file measures 0.91 us. Naming one
  syscall understated the gap about tenfold and named the wrong cost. What these
  two classes skip is the whole path after ``check_format``: the reload stat,
  the sha256, the dict lookup and the decoy ``state()`` call. In the attacker's
  units, ``(2909 / d) ** 2`` puts them at about 180,000 and 100,000 requests per
  value -- a channel, and one this module accepts with its reason stated above,
  not a rounding error.
* The numbers move with the machine. What the test suite pins is not a time, it
  is the WORK: zero parses and one clock read on both paths, counted rather
  than timed, in ``tests/kb/test_key_auth_middleware.py``. A timing assertion
  on a loaded machine proves nothing in either direction.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import secrets
import zlib
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta, timezone
from typing import Any

from ..schedule.recommend import parse_duration

__all__ = [
    "ALPHABET", "BODY_LEN", "CHECKSUM_LEN", "DEFAULT_EXPIRY", "DIGEST_LEN",
    "EXPIRED", "ID_PREFIX", "KEY_LEN", "KEY_PREFIX", "LIVE", "NEVER",
    "RECORD_FIELDS", "REVOKED", "SECRET_BYTES", "TS_FORMAT",
    "KeyRecord", "check_format", "create", "decoy_state", "digest", "mint",
    "new_id", "parse_expiry", "prune", "revoke", "rotate", "verify_key",
]

# 0-9, then A-Z, then a-z. The order is part of the format: it decides what
# every key string looks like, so it is pinned by a test against a literal.
ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_ALPHABET_SET = frozenset(ALPHABET)

KEY_PREFIX = "ctxlake_"
SECRET_BYTES = 32
BODY_LEN = 43
CHECKSUM_LEN = 6
KEY_LEN = len(KEY_PREFIX) + BODY_LEN + CHECKSUM_LEN  # 57
_BODY_AT = len(KEY_PREFIX)
_CHECKSUM_AT = _BODY_AT + BODY_LEN  # 51
DIGEST_LEN = 64


def _b62_encode(value: int, width: int) -> str:
    """``value`` in base62, left-padded with the alphabet's first character.

    The padding is the point. See the module docstring for the two rates it
    fixes (1.647% of bodies and 21.33% of checksums come out short without it).
    """
    if value < 0:
        raise ValueError(f"base62 takes a non-negative value: {value!r}")
    out = ""
    while value:
        value, remainder = divmod(value, 62)
        out = ALPHABET[remainder] + out
    if len(out) > width:
        raise ValueError(f"value needs {len(out)} base62 characters, not {width}")
    return out.rjust(width, ALPHABET[0])


def _b62_decode(text: str) -> int:
    """The inverse of :func:`_b62_encode`. Raises on a character off the alphabet."""
    value = 0
    for char in text:
        position = ALPHABET.find(char)
        if position < 0:
            raise ValueError(f"not a base62 character: {char!r}")
        value = value * 62 + position
    return value


def encode_body(raw: bytes) -> str:
    """32 random bytes to the 43-character body of a key."""
    if len(raw) != SECRET_BYTES:
        raise ValueError(f"a key body is {SECRET_BYTES} bytes, not {len(raw)}")
    return _b62_encode(int.from_bytes(raw, "big"), BODY_LEN)


def decode_body(body: str) -> bytes:
    """The 43-character body back to the 32 bytes that were drawn."""
    return _b62_decode(body).to_bytes(SECRET_BYTES, "big")


def _checksum(head: str) -> str:
    """The 6-character CRC32 tail over ``head``, the 51 characters before it.

    This tail is a typo filter and a secret-scanner anchor. It is NOT a
    security control and it holds no secret: the format is published, so
    anybody can compute the tail for any body offline. What it buys is that a
    mistyped key is refused as malformed before any lookup, which is a
    different diagnosis from an unknown key and reaches the operator as one.
    """
    return _b62_encode(zlib.crc32(head.encode("ascii")), CHECKSUM_LEN)


def key_from_bytes(raw: bytes) -> str:
    """Build the 57-character key string for a given 32 bytes.

    Split out from :func:`mint` so tests can pin deterministic vectors. Nothing
    that issues a real key may call this: use :func:`mint`, which draws its own
    bytes from :mod:`secrets`.
    """
    head = KEY_PREFIX + encode_body(raw)
    return head + _checksum(head)


def mint() -> tuple[str, str]:
    """Draw a new key. Returns ``(key, digest)`` and nothing else.

    ``secrets.token_bytes``, never ``random``. No fragment is returned because
    none is stored.
    """
    key = key_from_bytes(secrets.token_bytes(SECRET_BYTES))
    return key, digest(key)


def check_format(value: object) -> bool:
    """Is ``value`` shaped like a key contextlake minted?

    Length, prefix, alphabet, then the checksum, in that order. It touches no
    file, calls no hash and reads no record, so an unparseable value is refused
    before anything expensive runs.

    This is not constant-time and does not need to be. Every input to it is a
    public format check against a published format, and the value it reads came
    off the wire from whoever sent it.
    """
    if not isinstance(value, str) or len(value) != KEY_LEN:
        return False
    if not value.startswith(KEY_PREFIX):
        return False
    if not _ALPHABET_SET.issuperset(value[_BODY_AT:]):
        return False
    return value[_CHECKSUM_AT:] == _checksum(value[:_CHECKSUM_AT])


def digest(key: str) -> str:
    """The stored form of a key: ``sha256`` hex over the whole 57 characters.

    No salt and no pepper parameter, on purpose. The module docstring carries
    the measured numbers and the reason.
    """
    return hashlib.sha256(key.encode("ascii")).hexdigest()


def verify_key(presented: object, stored_digest: str) -> bool:
    """Does ``presented`` hash to ``stored_digest``?

    Format first, so a malformed value never reaches the hash, then
    ``hmac.compare_digest``. A plain ``==`` on the hex string returns early on
    the first differing character and leaks how much of a guess was right.

    Consumer, named 2026-09-05: the single-record compare behind
    ``kb keys check`` (S4.2.5). The authenticated request path does NOT come
    through here. It resolves an identity through one dict keyed by digest
    (S4.2.4), which is O(1) and compares nothing.
    """
    if not isinstance(presented, str) or not check_format(presented):
        return False
    return hmac.compare_digest(digest(presented), stored_digest)


# --------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
ID_PREFIX = "k_"
_ID_BYTES = 3
_ID_ATTEMPTS = 32
DEFAULT_EXPIRY = "90d"
NEVER = "never"
DEFAULT_OVERLAP = "7d"

LIVE = "live"
REVOKED = "revoked"
EXPIRED = "expired"


def _now() -> datetime:
    """The wall clock, in UTC.

    Never ``time.monotonic``. A monotonic reading has no meaning across
    processes, so a restart would reset every expiry: keys that should have
    lapsed keep working, and no test that runs inside one process notices.
    """
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    """An ISO-8601 UTC stamp ending ``Z``.

    A naive datetime is refused rather than read as local time. Reading it as
    local time would write an expiry that is hours off, and nothing would say
    so.
    """
    if moment.tzinfo is None:
        raise ValueError("a key record timestamp must be timezone-aware")
    return moment.astimezone(timezone.utc).strftime(TS_FORMAT)


# The "not parsed yet" marker for the cached deadline. A plain None cannot be
# it: None is also a legitimate parsed value, for a key set to never expire.
_UNPARSED = object()


def _parse_ts(text: str | None) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        return datetime.strptime(text, TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass
class KeyRecord:
    """One key's lifecycle. The secret itself is not here, only its digest.

    ``policy`` carries the access-control and rate-limit block (``tools``,
    ``repos``, ``owners``, ``external``, ``rate``, ``burst``, ``cost_budget``).
    This module stores it, deep-copies it on rotate, and asserts nothing about
    what any of it means. Those keys are owned by phases that are specified and
    deliberately unbuilt.
    """

    id: str
    name: str
    digest: str
    created_at: str
    expires_at: str | None = None
    revoked_at: str | None = None
    revoked_reason: str | None = None
    rotated_from: str | None = None
    rotated_to: str | None = None
    grant_version: str | None = None
    policy: dict[str, Any] = field(default_factory=dict)

    # The parsed `expires_at`, and the string it was parsed from. Assigned
    # WITHOUT an annotation on purpose: an annotated class attribute inside a
    # dataclass becomes a FIELD, and a field lands in RECORD_FIELDS, in
    # to_dict(), and therefore in the key file.
    #
    # They carry class-level defaults as well as being set in __post_init__, so
    # a record built by a path that skips __post_init__ re-parses instead of
    # raising AttributeError inside the auth gate. An AttributeError there is a
    # 500 on the one path that must answer 401 or nothing.
    _deadline_src = _UNPARSED
    _deadline = None

    def __post_init__(self) -> None:
        self._refresh_deadline()

    def _refresh_deadline(self) -> None:
        """Parse ``expires_at`` and remember the string it came from.

        Called at construction, so every parse happens on the load or reload
        path and none of them happens on a request. NOT an ``lru_cache`` over
        ``_parse_ts``: with a cache the FIRST request presenting a given key
        pays the parse and later ones do not, which trades a known-vs-unknown
        channel for a first-touch one.
        """
        self._deadline_src = self.expires_at
        self._deadline = _parse_ts(self.expires_at)

    def state(self, now: datetime | None = None) -> str:
        """One of ``live``, ``revoked``, ``expired``. Never anything else.

        Revoked beats expired. A record that is both stopped working because
        somebody revoked it, and that is the answer an audit has to give.

        An ``expires_at`` that will not parse reads as ``expired``. A stamp
        this module cannot read is a corrupt record, and a corrupt record must
        not authenticate anybody.

        THE WORK IS THE SAME FOR ALL THREE ANSWERS, and that is a security
        property, not a style choice: this runs on the auth path, and an early
        return for one answer makes the time an oracle for which answer it was.
        So the clock is read, the deadline is compared and both verdicts are
        computed before anything branches. The module docstring carries the
        measurement and what is left.

        The ``!=`` against the cached source string is what keeps a later
        assignment to ``expires_at`` (:func:`rotate` makes one) from being
        served from a stale parse. It costs a string compare, and CPython
        answers it on the pointer when nothing changed.
        """
        moment = now or _now()
        if self.expires_at != self._deadline_src:
            self._refresh_deadline()
        deadline = self._deadline
        lapsed = deadline is not None and deadline <= moment
        unreadable = self.expires_at is not None and deadline is None
        if self.revoked_at:
            return REVOKED
        if lapsed or unreadable:
            return EXPIRED
        return LIVE

    def to_dict(self) -> dict[str, Any]:
        """A plain dict, ready for the JSON writer that owns the file."""
        return {name: copy.deepcopy(getattr(self, name)) for name in RECORD_FIELDS}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KeyRecord:
        """Rebuild a record, refusing a field this version does not know.

        Dropping an unknown field silently is how a newer file's ``revoked_at``
        goes missing and a revoked key comes back to life. The version gate on
        the file catches this first; this is the second check, and it costs a
        set difference.
        """
        unknown = set(data) - set(RECORD_FIELDS)
        if unknown:
            raise ValueError(f"unknown key record fields: {sorted(unknown)}")
        missing = {"id", "name", "digest", "created_at"} - set(data)
        if missing:
            raise ValueError(f"key record is missing fields: {sorted(missing)}")
        return cls(**copy.deepcopy(data))


RECORD_FIELDS = tuple(f.name for f in fields(KeyRecord))

# The decoy, and the one thing it exists for.
#
# `Keyring.resolve` returns None the instant the digest lookup misses, so an
# unknown key skips the clock read and the `state()` call a known key pays. The
# gate calls `decoy_state()` on its unknown branch to pay the same work. The
# module docstring carries the measurement.
#
# Built from `_now()` plus a long delta, never from a literal date: a literal
# lapses one day and the decoy quietly starts taking the revoked-or-expired
# branch, which is the SHORT one, with every test still green.
_DECOY_DAYS = 36_500
_DECOY = KeyRecord(
    id=ID_PREFIX + "decoy0",
    name="decoy",
    # 64 zeros. Not a digest of anything, and it is never in a key file: the
    # decoy is not looked up, it is only asked for its state.
    digest="0" * DIGEST_LEN,
    created_at=_stamp(_now()),
    expires_at=_stamp(_now() + timedelta(days=_DECOY_DAYS)),
)


def decoy_state(now: datetime | None = None) -> str:
    """Do the work an unknown key would otherwise skip. Always ``live``.

    The gate calls this and throws the answer away. It is here rather than in
    the gate because the work being matched is :meth:`KeyRecord.state`, and a
    second module reimplementing "what state() costs" would drift from it on
    the first change.

    Always ``live`` because that is the LONGEST path through ``state``: revoked
    returns before the expiry work in a reader's eyes even though the code
    computes both, and a decoy on any shorter path would under-pay.
    """
    return _DECOY.state(now)


def _random_id() -> str:
    """``k_`` plus 6 lowercase hex, drawn independently of the digest.

    Never derived from the digest. The file's non-secret half (ids, names, and
    the usage rows that reference them) must carry no information about the
    secret half.
    """
    return ID_PREFIX + secrets.token_hex(_ID_BYTES)


def new_id(taken) -> str:
    """A record id not already in ``taken``.

    The retry is bounded. An unbounded loop cannot tell a collision from a
    generator that is stuck returning one value, and one of those hangs the
    process instead of failing.
    """
    for _ in range(_ID_ATTEMPTS):
        candidate = _random_id()
        if candidate not in taken:
            return candidate
    raise RuntimeError(f"no free key id after {_ID_ATTEMPTS} draws")


def parse_expiry(text: str | None) -> float | None:
    """Seconds until expiry, or ``None`` for the literal ``never``.

    ``None`` for ``text`` means the 90-day default. Non-expiring is typed, it
    is never what you get by leaving the flag off: a key handed over once
    otherwise outlives whatever it was handed over for, and nothing prompts
    anybody to revisit it.

    The duration itself is parsed by ``schedule.recommend.parse_duration``,
    which already raises on zero and on negatives. ``never`` is a mode word and
    is decided here before that call, the way ``auto`` already is at
    ``schedule/recommend.py:45``.
    """
    if text is None:
        text = DEFAULT_EXPIRY
    if str(text).strip().lower() == NEVER:
        return None
    return parse_duration(text)


def create(records: list[KeyRecord], name: str, *, expires: str | None = None,
           policy: dict[str, Any] | None = None, key: str | None = None,
           grant_version: str | None = None,
           now: datetime | None = None) -> tuple[KeyRecord, str]:
    """Mint a key, append its record to ``records``, return ``(record, key)``.

    ``key`` is for tests and for a caller that already holds one. Leave it
    unset and a fresh key is drawn. The plaintext is returned once and is never
    stored: the record carries the digest.
    """
    moment = now or _now()
    seconds = parse_expiry(expires)
    if key is None:
        key, key_digest = mint()
    else:
        key_digest = digest(key)
    record = KeyRecord(
        id=new_id({existing.id for existing in records}),
        name=name,
        digest=key_digest,
        created_at=_stamp(moment),
        expires_at=None if seconds is None else _stamp(moment + timedelta(seconds=seconds)),
        grant_version=grant_version,
        policy=copy.deepcopy(policy) if policy else {},
    )
    records.append(record)
    return record, key


def revoke(records: list[KeyRecord], record: KeyRecord, *,
           reason: str | None = None, now: datetime | None = None) -> bool:
    """Stamp ``revoked_at``. Returns ``False`` if the record was already revoked.

    The record stays. Usage rows reference key ids, so deleting one turns "who
    had access in March" into a question nobody can answer, and it removes the
    one place a deletion could have been reviewed. Deletion happens through
    :func:`prune`, typed with a date, and nowhere else.

    Re-revoking leaves the original timestamp and reason alone. The first
    revocation is the one the audit answer is about.

    ``records`` is taken so all four lifecycle verbs read the same way, and it
    is used: revoking a record the keyring does not hold writes a tombstone
    nothing will ever load, and that reads as success at the exit code.
    """
    if not any(held is record for held in records):
        raise ValueError(f"key {record.id} is not in this keyring")
    if record.revoked_at:
        return False
    record.revoked_at = _stamp(now or _now())
    record.revoked_reason = reason
    return True


def rotate(records: list[KeyRecord], record: KeyRecord, *,
           overlap: str = DEFAULT_OVERLAP, expires: str | None = None,
           now: datetime | None = None) -> tuple[KeyRecord, str]:
    """Create a replacement key and give the old one a handover window.

    Never an in-place swap of the secret behind one id. A swap breaks the
    holder at the instant the admin runs the command, before the new value has
    been handed over.

    The old key's expiry becomes ``min(existing, now + overlap)``. Assigning
    ``now + overlap`` unconditionally would give a key that expires tomorrow six
    more days at ``--overlap 7d``: the admin typed a command that shortens a
    key's life and it lengthened it. A key set to never expire gets the overlap
    window and nothing more, because rotating it is what retires it.
    """
    moment = now or _now()
    seconds = parse_duration(overlap)
    window = moment + timedelta(seconds=seconds)
    existing = _parse_ts(record.expires_at)
    # Computed before the new record is appended, so every way this call can
    # raise happens while the keyring is untouched. Otherwise a bad argument
    # leaves a new record in the file with the old one never linked to it, and
    # the audit trail reads in one direction only.
    retired_at = _stamp(window if existing is None else min(existing, window))
    new_record, new_key = create(
        records, record.name, expires=expires, policy=record.policy,
        grant_version=record.grant_version, now=moment)
    record.expires_at = retired_at
    record.rotated_to = new_record.id
    new_record.rotated_from = record.id
    return new_record, new_key


def prune(records: list[KeyRecord], before: datetime, *,
          now: datetime | None = None) -> list[KeyRecord]:
    """Remove terminal records older than ``before``. Returns what was removed.

    ``records`` is edited in place. A record is removed only when it has
    stopped working AND its terminal timestamp (``revoked_at``, or
    ``expires_at`` when it expired) is strictly before the cutoff. A live
    record is never removed, whatever its ``created_at`` says: pruning by date
    alone deletes a key that is still in use, and the holder finds out from a
    401.
    """
    moment = now or _now()
    removed = []
    kept = []
    for record in records:
        state = record.state(moment)
        terminal = None
        if state == REVOKED:
            terminal = _parse_ts(record.revoked_at)
        elif state == EXPIRED:
            terminal = _parse_ts(record.expires_at)
        if terminal is not None and terminal < before:
            removed.append(record)
        else:
            kept.append(record)
    records[:] = kept
    return removed
