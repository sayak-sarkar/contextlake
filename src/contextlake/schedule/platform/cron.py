"""The fallback for machines with no init-managed scheduler.

Two hazards shape this file.

**Cron cannot express most intervals.** ``*/70`` in a minute field does not mean
"every 70 minutes"; the field divides the hour, so it matches minute 0 and
nothing else. The adapter picks the nearest expressible interval BELOW the one
requested and reports the difference, because silently installing 60m where 70m
was computed breaks the duty-cycle cap the user configured.

**A crontab belongs to the user, not to us.** Every edit happens inside a marked
block; every other line comes back byte-identical, including comments, blank
lines and ordering.
"""
from __future__ import annotations

import shlex
import shutil
import subprocess

from .base import NO_CATCH_UP_PHRASE, Adapter, check_name

BEGIN = "# >>> contextlake ({name}) >>>"
END = "# <<< contextlake ({name}) <<<"

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


def splice(existing, name, block):
    """Insert, replace, or (with ``block=None``) remove one marked block.

    Everything outside the markers is preserved, which is the whole
    contract of this function.
    """
    begin, end = BEGIN.format(name=name), END.format(name=name)
    lines = existing.splitlines(keepends=True)
    out, inside, replaced = [], False, False
    for line in lines:
        if line.strip() == begin:
            inside = True
            if block is not None:
                out.append(begin + "\n")
                out.append(block if block.endswith("\n") else block + "\n")
                out.append(end + "\n")
                replaced = True
            continue
        if inside:
            if line.strip() == end:
                inside = False
            continue
        out.append(line)
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
    for line in text.splitlines():
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


def _read_crontab() -> str:
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True,
                            errors="replace", check=False)
    # Exit 1 with "no crontab for ..." is the empty case, not a failure.
    return result.stdout if result.returncode == 0 else ""


def _write_crontab(text) -> None:
    # check=False, then raise OSError by hand: CalledProcessError is not an
    # OSError, so `cmd_install`'s degrade-on-OSError catch would not see it
    # and a failed write would crash the command instead of falling back to
    # printing the rendered crontab line.
    result = subprocess.run(["crontab", "-"], input=text, text=True,
                            errors="replace", capture_output=True, check=False)
    if result.returncode != 0:
        raise OSError(
            f"crontab - failed: {result.stderr.strip() or result.stdout.strip() or 'no output'}")


class CronAdapter(Adapter):
    id = "cron"
    # cron has no equivalent of Persistent=true. A run missed while the machine
    # was off is lost, and `status` says so.
    catches_up_after_sleep = False

    def usable(self) -> bool:
        return shutil.which("crontab") is not None

    def render(self, job, interval_s, exec_argv, **_options) -> dict:
        name = check_name(job.name)
        actual_s, spec = nearest_expressible(interval_s)
        command = " ".join(shlex.quote(str(a)) for a in exec_argv)
        # cron gives a bare environment. MAILTO="" stops a mail per run on a box
        # with no MTA, where every run would otherwise log a delivery failure.
        line = f'MAILTO=""\n{spec} {command}\n'
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
        _write_crontab(splice(_read_crontab(), rendered["name"], rendered["crontab"]))
        return ["crontab"]

    def uninstall(self, job) -> list:
        name = check_name(job.name)
        before = _read_crontab()
        after = splice(before, name, None)
        if after == before:
            return []
        _write_crontab(after)
        return ["crontab"]

    def state(self, job) -> dict:
        name = check_name(job.name)
        text = _read_crontab()
        installed = BEGIN.format(name=name) in text
        exec_path = _exec_path_from_block(text, name) if installed else None
        notes = [f"cron {NO_CATCH_UP_PHRASE} while this machine was asleep "
                "or off."] if installed else []
        return {"installed": installed, "interval_s": None, "next_run": None,
                "exec_path": exec_path, "notes": notes}
