"""``contextlake init`` — a guided, first-run config generator.

Turns the two-file setup (mirror ``~/.contextlake.ini`` + optional
``~/.contextlake/kb.toml``) into one command: detect the platform, tell the user
which auth path they'll use, write valid config with sensible defaults, and print
the next step. Interactive when stdin is a TTY; otherwise (or with
``--skip-interactive``) non-interactive from flags + defaults, so it is
scriptable and CI-safe.

Stdlib only, plus one lazy import: ``argcomplete.shell_integration`` for the
fish completions file (argcomplete itself is a core dependency; see
pyproject.toml). Never writes a token into a file — auth is always an env
var, referenced by name.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import style
from .config import CONFIG_FILE, LOCAL_CONFIG_FILE
from .core import PLATFORM_DEFAULTS, platform_name
from .logging_setup import log

_KB_CONFIG = os.path.expanduser("~/.contextlake/kb.toml")
_PLATFORMS = ("gitlab", "github", "bitbucket", "gitea", "codeberg", "forgejo")

# Official hosted MCP endpoints, so the prompt has a real default instead of
# forcing the user to already know it. Verified against each provider's own
# docs (2026-07-28): Atlassian's Remote MCP Server guide, Figma's Dev Mode MCP
# Server guide. Self-hosted/enterprise setups can still type their own URL.
_MCP_DEFAULTS = {
    "atlassian": "https://mcp.atlassian.com/v1/mcp/authv2",
    "figma": "https://mcp.figma.com/mcp",
}


def _interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _ask(prompt: str, default: str) -> str:
    """Prompt with a default shown in brackets; blank input keeps the default."""
    try:
        reply = input(f"{prompt} [{style.cyan(default)}]: ").strip()
    except EOFError:
        return default
    return reply or default


def _ask_yn(prompt: str, default: bool) -> bool:
    d = "Y/n" if default else "y/N"
    try:
        reply = input(f"{prompt} [{d}]: ").strip().lower()
    except EOFError:
        return default
    if not reply:
        return default
    return reply[0] == "y"


def _token_status(platform: str) -> tuple[str, bool]:
    """(env var name, is it set) for the platform's API token."""
    env = PLATFORM_DEFAULTS[platform]["token_env"]
    return env, bool(os.environ.get(env))


def _mirror_ini(work_dir: str, platform: str, group: str) -> str:
    lines = [
        "# contextlake mirror configuration (written by `contextlake init`).",
        "# Auth is an env var, never stored here; keep this file out of version control.",
        "",
        "[contextlake]",
        f"work_dir = {work_dir}",
    ]
    if platform != "gitlab":
        lines.append(f"platform = {platform}")
    # `gitlab_group` is the accepted key for every platform (its `group` alias too);
    # keep the familiar key so existing docs/tools line up.
    lines.append(f"gitlab_group = {group}")
    lines.append("")
    return "\n".join(lines)


def _kb_toml(enable_embeddings: bool, store_dir: str) -> str:
    lines = [
        "# contextlake knowledge-layer configuration (written by `contextlake init`).",
        "",
        "[kb]",
        f'store_dir = "{store_dir}"',
        "",
        "[embeddings]",
        "# Local-first semantic search. The built-in CPU embedder needs no Ollama",
        "# and no API key (pip install \"contextlake[kb-local]\").",
        f"enabled = {'true' if enable_embeddings else 'false'}",
        "# Named explicitly rather than left as \"auto\": a generated file should",
        "# state which model runs, and a provider this file never chose can never",
        "# become a billed API call. Change to \"ollama\" for a local daemon, or",
        "# \"openai\" to opt into a paid API (key by env var, never stored here).",
        'provider = "builtin"',
        "",
        "# Curated wiki (LLM tier), off by default. Enable with a provider:",
        "# [llm]",
        "# enabled = true",
        '# provider = "builtin"   # in-process CPU model; "ollama" for a local daemon',
        "",
    ]
    return "\n".join(lines)


def _write(path: str, content: str, *, force: bool) -> bool:
    """Write ``content`` to ``path`` unless it exists and ``force`` is False.
    Returns True if written."""
    p = Path(path)
    if p.exists() and not force:
        log(f"  {style.warn('exists')} {path} — kept (use --force to overwrite)")
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    # Owner-only, and the mode is set as the file is created rather than
    # chmod-ed afterwards (no world-readable window). These files name the
    # fleet, and the kb one is what `kb source add` later appends connector
    # options to -- both belong to the user who ran init, nobody else.
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    log(f"  {style.ok('wrote')} {path}")
    return True


# argcomplete is a core dependency (see pyproject.toml), so registering it is
# just a matter of wiring it into the user's shell -- fish gets a static
# completions file, bash/zsh get a one-line `eval` (evaluated fresh each shell
# start, so it never goes stale the way pasting the ~3KB generated script
# itself into an rc file would).
_COMPLETION_EVAL_CMD = 'eval "$(register-python-argcomplete contextlake)"'
_COMPLETION_MARKER = "register-python-argcomplete contextlake"


def _bash_zsh_rc(shell: str) -> str | None:
    if shell == "bash":
        return os.path.expanduser("~/.bashrc")
    if shell == "zsh":
        return os.path.expanduser("~/.zshrc")
    return None


def _completion_already_registered(rc_path: str) -> bool:
    try:
        return _COMPLETION_MARKER in Path(rc_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False


def _append_completion_block(rc_path: str, shell: str) -> None:
    """Append a clearly-delimited, idempotent-marker-bearing block -- same
    append-only, never-clobber convention `contextlake steer` already uses for
    AGENTS.md/.mcp.json, just for a shell rc file instead."""
    lines = ["", "# >>> contextlake shell completion >>>"]
    if shell == "zsh":
        # bash-style completion needs zsh's bashcompinit shim loaded first;
        # most interactive zsh setups already load it, but not guaranteeing
        # that would leave the eval line below silently doing nothing.
        lines.append("autoload -U bashcompinit && bashcompinit")
    lines.append(_COMPLETION_EVAL_CMD)
    lines.append("# <<< contextlake shell completion <<<")
    lines.append("")
    Path(rc_path).parent.mkdir(parents=True, exist_ok=True)
    with open(rc_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _write_fish_completion() -> str:
    from argcomplete.shell_integration import shellcode  # core dep; see pyproject.toml

    path = os.path.expanduser("~/.config/fish/completions/contextlake.fish")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(shellcode(["contextlake"], shell="fish"), encoding="utf-8")
    return path


def _completion_decision_marker() -> Path:
    """A tool-owned state file (not a dotfile the user maintains) recording
    that shell completion has been decided one way or another -- accepted,
    declined, or an unrecognized shell -- so a later, zero-step check (see
    ``maybe_auto_register_completion``) never re-asks or silently overrides
    an explicit ``init --no-completion``/"n" decline."""
    return Path(os.path.expanduser("~/.contextlake/.completion_setup_done"))


def _mark_completion_decided(outcome: str) -> None:
    try:
        marker = _completion_decision_marker()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(outcome + "\n", encoding="utf-8")
    except OSError:
        pass  # best-effort -- a missed marker just means asking again next run


def _setup_shell_completion(*, interactive: bool, default_on: bool,
                            shell_override: str | None = None) -> None:
    """Register shell tab-completion, on by default (see docs/usage.md
    #shell-completion for the plain manual steps this automates). Every write
    here is visible in the log before it happens and is either idempotent
    (bash/zsh: skips if the marker's already present) or a full overwrite of a
    file contextlake itself owns (fish's dedicated completions file) -- never
    a silent, unannounced mutation of a dotfile the user didn't ask about.
    Every branch records its outcome via ``_mark_completion_decided`` so this
    is asked (or auto-run) at most once."""
    want = _ask_yn("Enable shell tab-completion for contextlake?", default_on) \
        if interactive else default_on
    if not want:
        _mark_completion_decided("declined")
        return
    shell_path = os.environ.get("SHELL", "")
    shell_name = shell_override or os.path.basename(shell_path)
    log("")
    if shell_name in ("bash", "zsh"):
        rc_path = _bash_zsh_rc(shell_name)
        if _completion_already_registered(rc_path):
            log(f"{style.ok('completion')} already registered in {rc_path}")
        else:
            log(f"{style.ok('completion')} adding to {rc_path}:")
            log(f"    {_COMPLETION_EVAL_CMD}")
            _append_completion_block(rc_path, shell_name)
            log(f"  Open a new {shell_name} shell (or `source {rc_path}`) for it to take effect.")
        _mark_completion_decided(f"registered:{shell_name}")
    elif shell_name == "fish":
        try:
            path = _write_fish_completion()
        except ImportError:
            log(f"{style.warn('completion')} argcomplete not importable; skipping "
                "(this shouldn't happen -- it's a core dependency).")
            _mark_completion_decided("argcomplete-missing")
        else:
            log(f"{style.ok('completion')} wrote {path}")
            log("  Open a new fish shell for it to take effect.")
            _mark_completion_decided("registered:fish")
    else:
        log(f"{style.warn('completion')} unrecognized shell "
            f"({shell_path or '$SHELL not set'}) — see "
            "docs/usage.md#shell-completion for manual setup (bash/zsh/fish).")
        _mark_completion_decided(f"unrecognized-shell:{shell_name or 'unset'}")


def maybe_auto_register_completion(*, quiet: bool = False) -> None:
    """Zero-step completion setup for anyone who installs via pip/uv/pipx and
    never runs `init` -- the entry point most tools (gh, kubectl) leave as a
    manual step. Fires at most once (guarded by the same decision marker
    `init`/`contextlake completion` write), only in a genuinely interactive
    terminal (never CI/Docker/a piped command, where there's no shell to
    configure and no one watching the log line explaining what happened), and
    never overrides an explicit prior accept/decline. ``quiet=True`` (-q) also
    skips it outright: `_setup_shell_completion`'s own contract is to never
    mutate a dotfile silently, and -q would make every one of its log() lines
    disappear -- the marker is deliberately NOT written on this skip, so it's
    simply retried on the user's next non-quiet run instead of never happening."""
    if quiet:
        return
    if os.environ.get("CONTEXTLAKE_NO_AUTO_COMPLETION"):
        return
    if _completion_decision_marker().exists():
        return
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return
    _setup_shell_completion(interactive=False, default_on=True)


_COMPLETION_SHELLS = ("bash", "zsh", "fish")


def cmd_completion(args) -> int:
    """`contextlake completion [bash|zsh|fish]` -- register tab-completion for
    the current (or named) shell right now, without needing a full `init` run
    first. Reuses the exact mechanism `init` uses, so both stay in sync.
    Validates `shell` itself (not argparse `choices=`, see the definition in
    cli.py for why) -- same pattern as `init`'s own `--platform` check."""
    shell = getattr(args, "shell", None)
    if shell is not None and shell not in _COMPLETION_SHELLS:
        log(style.warn(f"Unknown shell {shell!r} — expected one of "
                       f"{', '.join(_COMPLETION_SHELLS)}"))
        return 2
    _setup_shell_completion(interactive=False, default_on=True, shell_override=shell)
    return 0


def cmd_init(args) -> int:
    """Generate mirror (+ optional knowledge-layer) config, interactively or from flags."""
    interactive = _interactive() and not getattr(args, "skip_interactive", False)
    force = getattr(args, "force", False)

    # --config redirects both generated files -- init is the one command that
    # writes both the mirror INI and kb.toml, so an explicit --config (every
    # other command's isolation flag) must not be silently ignored here: it
    # targets the mirror INI (matching --config's documented meaning for
    # mirror commands), with kb.toml written alongside it as a sibling, same
    # relative layout as the real ~/.contextlake.ini + ~/.contextlake/kb.toml
    # defaults. --local (lower precedence than --config) writes into cwd
    # instead of ~/ -- a project-scoped config every subdirectory underneath
    # this one inherits (see config.find_ancestor_config).
    config_path = getattr(args, "config", None)
    local = getattr(args, "local", False)
    if config_path:
        mirror_config_file = os.path.expanduser(config_path)
        kb_config_file = str(Path(mirror_config_file).parent / "kb.toml")
    elif local:
        mirror_config_file = LOCAL_CONFIG_FILE
        kb_config_file = ".contextlake.kb.toml"
    else:
        mirror_config_file = CONFIG_FILE
        kb_config_file = _KB_CONFIG

    log(style.bold("contextlake init") + " — let's set up your workspace.\n")

    # --- platform -----------------------------------------------------------
    platform_in = getattr(args, "platform", None) or "gitlab"
    if interactive:
        platform_in = _ask(f"Platform ({'/'.join(_PLATFORMS)})", platform_in)
    try:
        platform = platform_name({"platform": platform_in})
    except Exception:  # noqa: BLE001 - normalize an unknown choice to a clean error
        log(style.warn(f"Unknown platform {platform_in!r} — expected one of "
                       f"{', '.join(_PLATFORMS)}"))
        return 2

    # --- group / work dir ---------------------------------------------------
    # No placeholder fallback here: group/org/workspace is the one prompt with
    # no value that's ever actually right to guess, on either path. Offering
    # "your-org" as an acceptable default (interactive, accept via enter) or
    # silently substituting it (--skip-interactive, no --group) both write a
    # config naming a group that doesn't exist -- caught once already when the
    # non-interactive path did this and the stale-placeholder detector below
    # didn't fire, because it checks a different literal. Refusing to write a
    # placeholder at all makes that check's exact spelling stop mattering.
    group = getattr(args, "group", None) or ""
    if interactive:
        group = _ask("Group / org / workspace to mirror", group)
    if not group:
        log(f"{style.warn('group')} No group/org/workspace given -- pass "
            "--group or answer the prompt; there's no safe default to guess.")
        return 2
    # cwd, not a fixed literal: `init` is run from wherever the user wants this
    # workspace to live (this is the whole point of --local), so "here" is the
    # only default that's ever actually right -- a hardcoded guess is wrong for
    # everyone except by coincidence.
    default_work = getattr(args, "work_dir", None) or os.getcwd()
    work_dir = _ask("Local workspace directory", default_work) if interactive \
        else default_work

    # --- knowledge layer ----------------------------------------------------
    kb_default = getattr(args, "kb", None)
    if kb_default is None:
        kb_default = True
    want_kb = _ask_yn("Set up the knowledge layer (graph + search)?", kb_default) \
        if interactive else kb_default
    enable_embeddings = False
    store_dir = None
    if want_kb:
        enable_embeddings = _ask_yn("  Enable semantic search (built-in CPU model)?",
                                    True) if interactive else \
            bool(getattr(args, "embeddings", False))
        # --local scopes the mirror workspace to cwd, so the KB store should
        # live alongside it by default too -- otherwise "everything is scoped
        # to this directory" would be true for the repos but not for the
        # graph/vectors built from them, which is the more surprising half.
        default_store = getattr(args, "store_dir", None) or (
            os.path.join(work_dir, ".contextlake", "kb") if local
            else "~/.contextlake/kb"
        )
        store_dir = _ask("  Knowledge-layer store directory", default_store) \
            if interactive else default_store

    # --- write --------------------------------------------------------------
    log("")
    wrote_any = _write(mirror_config_file, _mirror_ini(work_dir, platform, group), force=force)
    if want_kb:
        wrote_any |= _write(kb_config_file, _kb_toml(enable_embeddings, store_dir), force=force)

    # --- optional data source(s) ---------------------------------------------
    # Purely optional and skippable: default is "no", and --skip-interactive
    # never reaches this prompt. Only a source *type*, *name*, and MCP server
    # *URL* are collected here -- never a secret value (auth stays an env var,
    # set later via `contextlake source add --set token_env=...`). Loops so
    # more than one source can be added in a single init run (Confluence *and*
    # Figma, two Atlassian sites, etc.) instead of forcing a second pass
    # through `contextlake source add` for every source after the first.
    if want_kb and interactive:
        first = True
        while _ask_yn(
            "Connect a data source now (Confluence/Jira/Figma/GitLab/MCP)?"
            if first else "Connect another data source?", False,
        ):
            first = False
            log("  Source type: atlassian = Confluence/Jira, figma = Figma Dev "
                "Mode, gitlab = a GitLab MCP server, mcp = any other MCP server.")
            src_type = _ask("Source type (atlassian/figma/gitlab/mcp)", "atlassian")
            log("  Source name is a local nickname you pick to reference this "
                "connection later (contextlake kb source test <name>) -- it is not "
                "your Atlassian site, Figma team, or any other provider-side ID.")
            src_name = _ask("Source name", src_type)
            src = {"type": src_type, "name": src_name}
            if src_type in ("atlassian", "figma"):
                default_mcp = _MCP_DEFAULTS.get(src_type, "")
                log(f"  MCP server URL: {src_type}'s official hosted endpoint is "
                    "suggested below; press enter to accept it, or supply your "
                    "own self-hosted/enterprise MCP URL instead.")
                mcp_url = _ask("MCP server URL (blank to configure later)", default_mcp)
                if mcp_url:
                    src["mcp"] = mcp_url
            try:
                from .kb import config_edit  # lazy: needs tomlkit ([kb] extra)
            except ImportError:
                log("")
                log(f"{style.warn('source')} Install contextlake[kb] to connect "
                    "a data source; skipping.")
                break
            else:
                config_edit.add_source(kb_config_file, src)
                log("")
                log(f"{style.ok('source')} Added {style.cyan(src_name)} "
                    f"(type={src_type}) to {kb_config_file}")
                log(f"  Run {style.cyan('contextlake kb source list')} to review, or "
                    f"{style.cyan('contextlake kb source test ' + src_name)} "
                    "to check reachability.")
                log("")

    # --- auth guidance ------------------------------------------------------
    env, is_set = _token_status(platform)
    log("")
    if is_set:
        log(f"{style.ok('auth')} {env} is set — mirroring will use it.")
    elif platform == "gitlab":
        log(f"{style.warn('auth')} Set {env} (a read_api + read_repository token), "
            "or run `glab auth login`. Public groups need neither.")
    else:
        log(f"{style.warn('auth')} Set {env} to mirror private repos "
            "(public orgs work without a token, rate-limited).")

    # --- shell completion ----------------------------------------------------
    # Registering completion appends to the user's ~/.bashrc or ~/.zshrc. That is
    # a write well outside what `init` implies -- and flatly at odds with `init
    # --local`, which is the user scoping everything to this directory -- so a run
    # that never asked must never do it. `--skip-interactive` (or a piped stdin)
    # is exactly "nobody was asked", so there it happens only on an explicit
    # --completion. `contextlake completion` remains the deliberate way in.
    #
    # No decision marker is written on that skip, deliberately: a marker here
    # would permanently suppress the offer that a later interactive run -- or
    # maybe_auto_register_completion -- would otherwise make.
    completion_opt_in = getattr(args, "completion", None)
    if interactive:
        _setup_shell_completion(interactive=True,
                                default_on=True if completion_opt_in is None
                                else completion_opt_in)
    elif completion_opt_in:
        _setup_shell_completion(interactive=False, default_on=True)
    else:
        log("")
        log(f"{style.warn('completion')} shell tab-completion not registered — a "
            "non-interactive run does not modify your shell startup file.")
        log(f"  Run {style.cyan('contextlake completion')} (or "
            f"{style.cyan('init --completion')}) when you want it.")

    # --- next steps ---------------------------------------------------------
    log("")
    if want_kb:
        # Recommend the extra that matches what they just chose: [kb-full] bundles
        # the built-in embedder + sqlite-vec so semantic search works with no extra
        # steps; plain [kb] has no embedder, so enabling semantic search without it
        # makes every embed fail. See the QUICKSTART install guidance.
        extra = "kb-full" if enable_embeddings else "kb"
        install = style.cyan(f'pip install "contextlake[{extra}]"')
        log("Next: install the knowledge layer and bootstrap everything:")
        log(f"  {install}")
        log(f"  {style.cyan('contextlake bootstrap')}")
    else:
        log("Next: mirror your repositories:")
        log(f"  {style.cyan('contextlake mirror sync')}")
    if not wrote_any and not force:
        log(style.dim("\n(nothing written — config already existed; --force to overwrite)"))
    return 0
