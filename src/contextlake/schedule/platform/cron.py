"""The fallback for machines with no init-managed scheduler.

Two hazards shape this file.

**Cron cannot express most intervals.** ``*/70`` in a minute field does not mean
"every 70 minutes"; the field divides the hour, so it matches minute 0 and
nothing else. The adapter picks the nearest expressible interval BELOW the one
requested and reports the difference, because silently installing 60m where 70m
was computed breaks the duty-cycle cap the user configured.

**A crontab belongs to the user, not to us.** Every edit happens inside a marked
block; every other line comes back byte-identical, including comments, blank
lines and ordering. ``crontab -`` replaces the whole crontab, so a read that
failed must never pass for an empty one: the write would delete every user job.
A read that fails refuses the write, and a copy of the crontab as read is saved
before anything overwrites it.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time

from ...logging_setup import log
from .base import NO_CATCH_UP_PHRASE, Adapter, check_name

BEGIN = "# >>> contextlake ({name}) >>>"
END = "# <<< contextlake ({name}) <<<"

# The crontab travels as bytes. Decoding with surrogateescape carries a byte
# that is not UTF-8 through the str round trip and back out unchanged, where
# errors="replace" turned it into U+FFFD.
_ENCODING, _ERRORS = "utf-8", "surrogateescape"

# What `crontab -l` prints on stderr, exiting 1, when the user has no crontab
# yet. cronie and Debian's cron print "no crontab for <user>"; FreeBSD and
# macOS print the same through errx(3), which adds "crontab: " in front. A
# permission error also exits 1, so the status alone cannot tell the two
# apart. Nothing else counts as "empty".
_NO_CRONTAB = re.compile(r"(?:crontab: )?no crontab for \S+")

# Every interval cron can express, smallest first. Minutes must divide
# 60 and hours must divide 24, or the step wraps at the end of the field.
_MINUTE_STEPS = (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30)
_HOUR_STEPS = (1, 2, 3, 4, 6, 8, 12)


def _expressible():
    out = [(m * 60, f"*/{m} * * * *") for m in _MINUTE_STEPS]
    out.append((3600, "0 * * * *"))
    out.extend((h * 3600, f"0 */{h} * * *") for h in _HOUR_STEPS if h > 1)
    out.append((86400, "0 0 * * *"))
    return sorted(set(out))


def nearest_expressible(seconds):
    """``(seconds, cron_spec)`` for the nearest interval cron can run.

    Rounds DOWN above one minute. Running more often costs duty cycle, which
    the user set a bound on and can see; running less often costs freshness,
    which is what they installed a scheduler to protect.

    Below one minute it rounds UP to the one-minute floor, because cron has
    no finer resolution and there is nothing smaller to round to. ``render``
    reports the difference either way, so a caller who asked for 30s is told
    the job runs every minute.
    """
    wanted = float(seconds)
    candidates = [pair for pair in _expressible() if pair[0] <= wanted]
    if not candidates:
        return _expressible()[0]
    return max(candidates, key=lambda pair: pair[0])


def _lines(text):
    """``text`` cut after each newline and nowhere else.

    ``str.splitlines`` also cuts at ``\\r``, form feeds and other characters
    that cron reads as part of a line, so a user line holding one could be
    split into a piece that looks like a marker.
    """
    pieces = text.split("\n")
    lines = [piece + "\n" for piece in pieces[:-1]]
    if pieces[-1]:
        lines.append(pieces[-1])
    return lines


def _unclosed(opened_at, name, end, before) -> str:
    # No final full stop: callers append their own sentence after this one.
    return (f"line {opened_at} of the crontab opens the contextlake block for "
            f"{name!r}, but no matching {end!r} line closes it before {before}. "
            f"The crontab was not changed. Fix it with `crontab -e`")


def splice(existing, name, block):
    """Insert, replace, or (with ``block=None``) remove one marked block.

    Everything outside the markers comes back byte for byte, which is the
    whole contract of this function. The one addition: a last line with no
    newline gains one when the block is appended after it.

    Raises ValueError when this job's block has no END line before the
    crontab ends, or before its next BEGIN. Nothing says where such a block
    was meant to stop. Guess short and an old contextlake line stays behind
    and fires next to the new one; guess long and the user's own lines are
    deleted. So the user is asked to fix it by hand instead.
    """
    begin, end = BEGIN.format(name=name), END.format(name=name)
    out, opened_at, replaced = [], None, False
    for number, line in enumerate(_lines(existing), 1):
        stripped = line.strip()
        if stripped == begin:
            if opened_at is not None:
                raise ValueError(_unclosed(opened_at, name, end, f"line {number}"))
            opened_at = number
            if block is not None:
                out.append(begin + "\n")
                out.append(block if block.endswith("\n") else block + "\n")
                out.append(end + "\n")
                replaced = True
            continue
        if opened_at is not None:
            if stripped == end:
                opened_at = None
            continue
        out.append(line)
    if opened_at is not None:
        raise ValueError(_unclosed(opened_at, name, end, "the end of the crontab"))
    text = "".join(out)
    if block is None or replaced:
        return text
    # cron ignores a final line with no newline, so appending straight onto one
    # would silently disable the user's last job.
    if text and not text.endswith("\n"):
        text += "\n"
    return text + begin + "\n" + (block if block.endswith("\n") else block + "\n") + end + "\n"


def _exec_path_from_block(text, name) -> str | None:
    """The interpreter path out of one job's marked crontab block.

    The block's cron line is ``<5 time fields> <command>``, and ``command``
    is the shlex-quoted argv `render` wrote, so the first token after
    splitting the time fields off, then shlex-splitting the remainder, is
    the interpreter. Returns ``None`` when the block is absent or the line
    does not parse: "cannot tell", never "missing".
    """
    begin, end = BEGIN.format(name=name), END.format(name=name)
    inside = False
    for line in _lines(text):
        stripped = line.strip()
        if stripped == begin:
            inside = True
            continue
        if inside and stripped == end:
            return None
        if not inside or not stripped or stripped.startswith("MAILTO="):
            continue
        parts = stripped.split(None, 5)
        if len(parts) < 6:
            continue
        try:
            tokens = shlex.split(parts[5])
        except ValueError:
            return None
        return tokens[0] if tokens else None
    return None


def _interval_s_from_block(text, name) -> float | None:
    """The interval, in seconds, that the installed cron spec runs.

    Matches the block's first five fields against `_expressible()` rather
    than interpreting cron syntax in general: those are the only specs this
    adapter ever writes. A spec that does not match one of them (hand-edited,
    or written by some other version) returns ``None``: "cannot tell", never
    a wrong number.
    """
    begin, end = BEGIN.format(name=name), END.format(name=name)
    inside = False
    specs = {spec: seconds for seconds, spec in _expressible()}
    for line in _lines(text):
        stripped = line.strip()
        if stripped == begin:
            inside = True
            continue
        if inside and stripped == end:
            return None
        if not inside or not stripped or stripped.startswith("MAILTO="):
            continue
        parts = stripped.split(None, 5)
        if len(parts) < 6:
            continue
        return specs.get(" ".join(parts[:5]))
    return None


def _is_no_crontab(result) -> bool:
    """Whether a failed ``crontab -l`` means "this user has no crontab yet":
    exit 1, nothing on stdout, and stderr one line matching `_NO_CRONTAB`."""
    if result.returncode != 1 or result.stdout:
        return False
    message = result.stderr.decode(_ENCODING, "replace").strip()
    return _NO_CRONTAB.fullmatch(message) is not None


def _read_crontab() -> str:
    """The user's crontab, or ``""`` when they have none yet.

    Raises OSError for any other failure. ``install`` and ``uninstall`` write
    back what this returns, so a failed read taken as ``""`` would replace
    the whole crontab with the contextlake block alone.

    Bytes, not text mode: text mode also turns ``\\r\\n`` into ``\\n``.
    """
    result = subprocess.run(["crontab", "-l"], capture_output=True, check=False)
    if result.returncode == 0:
        return result.stdout.decode(_ENCODING, _ERRORS)
    if _is_no_crontab(result):
        return ""
    detail = result.stderr.decode(_ENCODING, "replace").strip() or "no output"
    raise OSError(f"crontab -l failed (exit {result.returncode}): {detail}")


def _write_crontab(text) -> None:
    # check=False, then raise OSError by hand: CalledProcessError is not an
    # OSError, so `cmd_install`'s degrade-on-OSError catch would not see it
    # and a failed write would crash the command instead of falling back to
    # printing the rendered crontab line.
    result = subprocess.run(["crontab", "-"], input=text.encode(_ENCODING, _ERRORS),
                            capture_output=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip()).decode(_ENCODING, "replace")
        raise OSError(f"crontab - failed: {detail or 'no output'}")


def _backup_dir() -> str:
    """Where copies of the crontab go: under contextlake's cache root, which
    holds each workspace's schedule state. The adapter is not given the
    config, so it cannot name one workspace's directory. A crontab belongs to
    the user, not to one workspace, so the shared root is the fitting place."""
    from ...config import _default_cache_root

    return os.path.join(_default_cache_root(), "crontab-backups")


def _save_backup(text) -> str | None:
    """Save ``text``, the crontab as read, and return the file's path.

    ``None`` when there is nothing to save. Raises OSError when the copy
    cannot be written, so the caller writes nothing: a crontab is replaced
    only once a copy of it exists. The file is readable by its owner only,
    because a crontab's environment lines can hold secrets.
    """
    if not text:
        return None
    directory = _backup_dir()
    os.makedirs(directory, mode=0o700, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    # mkstemp creates the file with mode 0600 and a name nothing else holds.
    fd, path = tempfile.mkstemp(prefix=f"crontab-{stamp}-", suffix=".txt", dir=directory)
    with os.fdopen(fd, "wb") as f:
        f.write(text.encode(_ENCODING, _ERRORS))
    return path


def _replace_crontab(before, after) -> None:
    """Write ``after`` over the crontab that ``before`` was read from. The
    copy of ``before`` is saved first, so this cannot lose text it read."""
    saved = _save_backup(before)
    if saved:
        log(f"Saved the previous crontab to {saved}")
    _write_crontab(after)


def _spliced(text, name, block) -> str:
    """`splice`, with its ValueError raised as OSError. Every caller of
    ``install`` and ``uninstall`` degrades on OSError, not ValueError."""
    try:
        return splice(text, name, block)
    except ValueError as e:
        raise OSError(str(e)) from e


class CronAdapter(Adapter):
    id = "cron"
    # cron has no equivalent of Persistent=true. A run missed while the machine
    # was off is lost, and `status` says so.
    catches_up_after_sleep = False
    # render()'s only artefact is "crontab". The rest are facts about the
    # install, not files: see Adapter.metadata_keys and Adapter.render.
    metadata_keys = frozenset({"spec", "interval_s", "notes", "name"})

    def usable(self) -> bool:
        return shutil.which("crontab") is not None

    def render(self, job, interval_s, exec_argv, **_options) -> dict:
        name = check_name(job.name)
        actual_s, spec = nearest_expressible(interval_s)
        command = " ".join(shlex.quote(str(a)) for a in exec_argv)
        # cron gives a bare environment and mails whatever a job prints
        # (cron(8)). Discarding this job's output on its own line stops a mail
        # per run on a box with no MTA, where every run would otherwise log a
        # delivery failure. Earlier versions wrote MAILTO="" instead, but cron
        # applies that to every line below it, so the user's own later jobs
        # lost their mail too. There is no way to unset it again (crontab(5)).
        line = f"{spec} {command} >/dev/null 2>&1\n"
        notes = ""
        if abs(actual_s - float(interval_s)) > 1:
            from ..recommend import format_duration

            notes = (f"cron cannot express {format_duration(interval_s)}, so this "
                     f"job runs every {format_duration(actual_s)} instead. "
                     f"Use systemd for an exact interval.")
        return {"crontab": line, "spec": spec, "interval_s": actual_s,
                "notes": notes, "name": name}

    def install(self, job, interval_s, exec_argv, **options) -> list:
        rendered = self.render(job, interval_s, exec_argv, **options)
        before = _read_crontab()
        _replace_crontab(before, _spliced(before, rendered["name"], rendered["crontab"]))
        return ["crontab"]

    def uninstall(self, job) -> list:
        name = check_name(job.name)
        before = _read_crontab()
        after = _spliced(before, name, None)
        if after == before:
            return []
        _replace_crontab(before, after)
        return ["crontab"]

    def installed_names(self):
        """Every marked block in the crontab, by job name.

        Parses the same BEGIN marker `splice` writes, so the two cannot drift
        apart on the naming. Lines outside a marked block belong to the user
        and are never reported.

        ``None`` when the crontab cannot be read. ``_read_crontab`` raises for
        every failure except "no crontab yet", so an unreadable crontab is not
        reported as one with no units in it.
        """
        try:
            text = _read_crontab()
        except OSError:
            return None
        pattern = re.escape(BEGIN).replace(re.escape("{name}"), r"(?P<name>.+?)")
        return sorted(m.group("name") for m in re.finditer(pattern, text))

    def state(self, job) -> dict:
        name = check_name(job.name)
        # Raises OSError when the crontab cannot be read. Both callers of
        # state() catch it and print the error as a note.
        text = _read_crontab()
        installed = BEGIN.format(name=name) in text
        exec_path = _exec_path_from_block(text, name) if installed else None
        interval_s = _interval_s_from_block(text, name) if installed else None
        notes = [f"cron {NO_CATCH_UP_PHRASE} while this machine was asleep "
                "or off."] if installed else []
        return {"installed": installed, "interval_s": interval_s, "next_run": None,
                "exec_path": exec_path, "notes": notes}
