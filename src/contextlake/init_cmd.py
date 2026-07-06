"""``contextlake init`` — a guided, first-run config generator.

Turns the two-file setup (mirror ``~/.contextlake.ini`` + optional
``~/.contextlake/kb.toml``) into one command: detect the platform, tell the user
which auth path they'll use, write valid config with sensible defaults, and print
the next step. Interactive when stdin is a TTY; otherwise (or with ``--yes``)
non-interactive from flags + defaults, so it is scriptable and CI-safe.

Stdlib only. Never writes a token into a file — auth is always an env var,
referenced by name.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import style
from .config import CONFIG_FILE
from .core import PLATFORM_DEFAULTS, platform_name
from .logging_setup import log

_KB_CONFIG = os.path.expanduser("~/.contextlake/kb.toml")
_PLATFORMS = ("gitlab", "github", "bitbucket", "gitea", "codeberg", "forgejo")


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


def _kb_toml(enable_embeddings: bool) -> str:
    lines = [
        "# contextlake knowledge-layer configuration (written by `contextlake init`).",
        "",
        "[kb]",
        'store_dir = "~/.contextlake/kb"',
        "",
        "[embeddings]",
        "# Local-first semantic search. The built-in CPU embedder needs no Ollama",
        "# and no API key (pip install \"contextlake[kb-local]\").",
        f"enabled = {'true' if enable_embeddings else 'false'}",
        'provider = "auto"',
        "",
        "# Curated wiki (LLM tier), off by default. Enable with a provider:",
        "# [llm]",
        "# enabled = true",
        '# provider = "auto"   # reachable Ollama, else the built-in CPU model',
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
    p.write_text(content, encoding="utf-8")
    log(f"  {style.ok('wrote')} {path}")
    return True


def cmd_init(args) -> int:
    """Generate mirror (+ optional knowledge-layer) config, interactively or from flags."""
    interactive = _interactive() and not getattr(args, "yes", False)
    force = getattr(args, "force", False)

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
    default_group = getattr(args, "group", None) or "your-org"
    group = _ask("Group / org / workspace to mirror", default_group) if interactive \
        else default_group
    default_work = getattr(args, "work_dir", None) or os.path.expanduser("~/work")
    work_dir = _ask("Local workspace directory", default_work) if interactive \
        else default_work

    # --- knowledge layer ----------------------------------------------------
    kb_default = getattr(args, "kb", None)
    if kb_default is None:
        kb_default = True
    want_kb = _ask_yn("Set up the knowledge layer (graph + search)?", kb_default) \
        if interactive else kb_default
    enable_embeddings = False
    if want_kb:
        enable_embeddings = _ask_yn("  Enable semantic search (built-in CPU model)?",
                                    True) if interactive else \
            bool(getattr(args, "embeddings", False))

    # --- write --------------------------------------------------------------
    log("")
    wrote_any = _write(CONFIG_FILE, _mirror_ini(work_dir, platform, group), force=force)
    if want_kb:
        wrote_any |= _write(_KB_CONFIG, _kb_toml(enable_embeddings), force=force)

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
        log(f"  {style.cyan('contextlake sync')}")
    if not wrote_any and not force:
        log(style.dim("\n(nothing written — config already existed; --force to overwrite)"))
    return 0
