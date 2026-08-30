"""The ad-hoc `schedule interval <dur> run -- <cmd>` path.

Split out of ``cmds.py``. Parsing the interval spec, validating the captured
command, and installing it as a named job. Its own module because the parsing
and validation here exist only for this one command: nothing else in the
package accepts a user-supplied argv.

Core tier. Nothing here may import ``contextlake.kb`` at module level, which
``tests/test_schedule_source_tier.py`` enforces. `validate_job_argv` checks
whether a command NEEDS the knowledge layer, and does it with a lazy import
inside the function for that reason.
"""
from __future__ import annotations

from ..logging_setup import log
from . import adapters, recommend
from . import jobs as jobstore
from .settings import resolve_interval

# Anything a shell would interpret. A job argument containing one of these is
# somebody expecting a shell, and these values land in unit files that run
# unattended, so it is refused rather than quoted and hoped for.
_SHELL_CHARS = set(";|&$`><\n\r\\\"'*?()[]{}!#~")


_SAFE_GLOB = "*?[]"  # allowed, because --repos takes a glob


def parse_interval_spec(rest):
    """``["6h", "run", "kb", "wiki"]`` to ``("6h", ["kb", "wiki"])``.

    Raises ``ValueError`` with a message naming the problem, because this is
    typed by a person and the error is the whole user interface for it.
    """
    words = [str(w) for w in (rest or [])]
    if not words:
        raise ValueError(
            "missing interval. Usage: contextlake schedule interval "
            "<duration|auto> run <contextlake command...>")
    setting = words[0].strip()
    if setting.lower() != "auto":
        try:
            recommend.parse_duration(setting)
        except ValueError as e:
            raise ValueError(f"{e}. Give a duration (45s, 30m, 2h, 7d) or `auto`.") from None
        setting = setting.lower()
    else:
        setting = "auto"

    if len(words) < 2 or words[1].lower() != "run":
        raise ValueError(
            "expected `run` after the interval. Usage: contextlake schedule "
            "interval <duration|auto> run <contextlake command...>")

    argv = words[2:]
    # argparse consumes a `--` separator before this runs, so `argv` never
    # begins with one through the CLI. The strip stays for a caller that builds
    # the word list by hand, and is a no-op when there is nothing to strip.
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise ValueError("missing command after `run`. Give a contextlake "
                         "command, for example: kb wiki --force")

    for word in argv:
        bad = (set(word) & _SHELL_CHARS) - set(_SAFE_GLOB)
        if bad:
            raise ValueError(
                f"{word!r} contains shell characters ({''.join(sorted(bad))}). "
                f"A scheduled job is a contextlake command and its flags, not a "
                f"shell line. Write the words separately.")
    return setting, argv


def validate_job_argv(argv):
    """Prove the job can parse AND can run. Returns a list of warnings.

    Parsing is not enough. A `kb index` job on a core-only install parses
    and then fails every night with an ImportError nobody reads, which
    is the silent-3am failure this validation exists to prevent.
    """
    from ..cli import _KB_COMMANDS, _resolve_command, build_parser

    if argv and argv[0] == "schedule":
        raise ValueError(
            "a scheduled job cannot be `schedule` itself; that would recurse.")

    parser = build_parser()
    try:
        parsed = parser.parse_args(list(argv))
    except SystemExit:
        raise ValueError(
            f"`contextlake {' '.join(argv)}` is not a valid command. Run it by "
            f"hand first, or see `contextlake --help`.") from None
    try:
        _resolve_command(parsed, parser)
    except SystemExit:
        raise ValueError(
            f"`contextlake {' '.join(argv)}` names a namespace with no command "
            f"after it.") from None

    warnings = []
    if parsed.command in _KB_COMMANDS:
        try:
            __import__("contextlake.kb")
        except ImportError as e:
            warnings.append(
                f"`{' '.join(argv)}` needs the knowledge-layer extra, which is not "
                f"installed here ({e}). Install it with "
                f"pip install 'contextlake[kb]', or this job fails on every run.")
    return warnings


def _name_from_argv(argv) -> str:
    """A readable default job name, from the command it runs."""
    from .platform.base import NAME_RE

    words = [w for w in argv if not w.startswith("-")][:2]
    candidate = "-".join(words) or "job"
    return candidate if NAME_RE.match(candidate) else "job"


def cmd_interval(args, config) -> int:
    """Create or replace an ad-hoc job."""
    from .. import style
    from .platform import base

    try:
        setting, argv = parse_interval_spec(getattr(args, "rest", []))
        warnings = validate_job_argv(argv)
    except ValueError as e:
        log(style.fail(str(e)))
        return 2

    name = getattr(args, "job", None) or _name_from_argv(argv)
    try:
        base.check_name(name)
    except ValueError as e:
        log(style.fail(str(e)))
        return 2

    jobs_file = jobstore.jobs_path(config)
    existing = jobstore.read_jobs(jobs_file).get(name)
    try:
        adapter = adapters._adapter_for(args, existing)
        interval_s, why = resolve_interval(config, setting)
    except (base.NoAdapter, ValueError) as e:
        log(style.fail(str(e)))
        return 2

    job = jobstore.new_job(name, argv, setting, adapter.id,
                           created=existing.created if existing else None)
    on_battery = config.get("schedule_on_battery", "skip")
    try:
        written = adapter.install(job, interval_s, adapters.exec_argv_for(name),
                                  on_battery=on_battery)
    except OSError as e:
        log(style.fail(f"Could not install the {adapter.id} unit: {e}"))
        return 1
    # Written only after the unit is in place, so a failed install never leaves
    # a record claiming a schedule that does not exist.
    jobstore.write_job(jobs_file, job)

    adapters._report_installed(
        adapter, job, interval_s, adapters.exec_argv_for(name), on_battery, why, written,
        lambda interval_str: (f"{style.ok()} Job {name!r}: contextlake "
                              f"{' '.join(argv)}, every {interval_str} on "
                              f"{adapter.id}."))
    for warning in warnings:
        log(f"  {style.warn()} {warning}")
    return 0
