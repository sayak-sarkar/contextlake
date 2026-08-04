"""`contextlake doctor --fix` -- turn doctor's prose hints into real remediation.

Split out of doctor.py and imported only inside the ``--fix`` branch: plain
``doctor`` is the diagnostic people reach for when everything else is broken, so
it must not gain this module's imports (subprocess, the pip runner) on its way
to a health report.

Two privilege tiers, and the split is the whole point of the design:

* **Python packages** go into the *current* interpreter with
  ``sys.executable -m pip`` (never a bare ``pip`` off PATH, which can belong to
  a different environment entirely). Unprivileged, reversible, so ``--fix`` runs
  them after printing them.
* **System packages** need administrator rights. Those are *never* run unless a
  human answers a prompt at a real terminal: no TTY, or ``--skip-interactive``,
  means print and stop. A CI job or a scripted invocation must never trip a sudo
  prompt. git is the only system package offered; a missing C++ toolchain is
  reported with advice, since llm-local installs from a prebuilt wheel.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

from ... import style

# llama-cpp-python publishes NO wheels to PyPI at all -- every release is an
# sdist -- because llama.cpp is compiled per hardware backend and one PyPI
# namespace cannot hold the CPU, CUDA and Metal builds of the same version and
# platform tag. Upstream ships one index per accelerator instead (/whl/cpu,
# /whl/cu121, /whl/cu122, /whl/cu124, /whl/metal), the same convention PyTorch
# uses. So a plain `pip install 'contextlake[llm-local]'` always compiles from
# source and needs cmake plus a C++ toolchain; pointing pip at an index is what
# makes this tier installable without one. PEP 508 has no field for an index
# URL, so the extra itself can never carry this -- only a pip command can.
# CPU is the right default here: the built-in tier is deliberately a small
# CPU model, and a GPU user is better served by Ollama (docs/model-providers.md).
LLAMA_CPP_WHEEL_INDEX = "https://abetlen.github.io/llama-cpp-python/whl/cpu"

# Corporate TLS interception and slow mirrors make the default 15s socket
# timeout too tight for a 20MB+ wheel; a half-finished install is worse than a
# slow one, so buy patience up front rather than reporting a timeout.
PIP_TIMEOUT = "60"
PIP_RETRIES = "5"


@dataclass
class Remedy:
    """One planned repair. ``argv`` is exactly what would run, printed first."""

    key: str
    summary: str
    argv: list[str]
    privileged: bool = False
    notes: list[str] = field(default_factory=list)


def _module_present(name: str) -> bool:
    """Is an importable module installed? The single detection seam every
    capability check routes through, so tests can decide what is missing
    instead of inheriting the developer's own virtualenv."""
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # a broken/partial install is "not present"
        return False


def _interactive() -> bool:
    """A real terminal on stdin. Deliberately duplicated from
    ``init_cmd._interactive`` rather than imported: a kb command reaching into
    the mirror-side CLI module for four lines is the worse coupling."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _pip(*packages: str, extra_index: str | None = None,
         only_binary: str | None = None) -> list[str]:
    argv = [sys.executable, "-m", "pip", "install",
            "--timeout", PIP_TIMEOUT, "--retries", PIP_RETRIES, *packages]
    if only_binary:
        # Scoped to one package, never `:all:`: forbidding a source fallback for
        # every dependency would let one missing wheel anywhere fail the install.
        argv += ["--only-binary", only_binary]
    if extra_index:
        argv += ["--extra-index-url", extra_index]
    return argv


# ---------------------------------------------------------------------------
# system package managers
# ---------------------------------------------------------------------------

# Ordered: the first manager present on PATH wins. Each entry is
# (executable, install argv template, needs-sudo).
_PKG_MANAGERS = (
    ("dnf", ["dnf", "install", "-y"], True),
    ("apt-get", ["apt-get", "install", "-y"], True),
    ("zypper", ["zypper", "install", "-y"], True),
    ("pacman", ["pacman", "-S", "--noconfirm"], True),
    # brew refuses to run as root, and the Windows managers have no sudo.
    ("brew", ["brew", "install"], False),
    ("choco", ["choco", "install", "-y"], False),
    ("winget", ["winget", "install", "-e", "--id"], False),
)

# Per-manager package names, where they differ from the generic capability name.
# git is the only entry because git is the only system package `--fix` installs:
# `system_install_command` is never called with anything else. A "toolchain" entry
# used to sit here, which made the docs (and this module's own docstring) describe
# a compiler install that no code path could reach. The supported route for
# llm-local is the prebuilt wheel, which needs no compiler, so a missing toolchain
# is reported with advice instead of an install.
_PKG_NAMES = {
    "git": {"winget": "Git.Git"},
}


def _is_root() -> bool:
    # os.geteuid is POSIX-only; on Windows there is no sudo to prefix anyway.
    return getattr(os, "geteuid", lambda: 1)() == 0


def system_install_command(capability: str) -> list[str] | None:
    """The exact argv that would install a system package here, or None when no
    known package manager is on PATH."""
    for exe, template, needs_sudo in _PKG_MANAGERS:
        if shutil.which(exe) is None:
            continue
        names = _PKG_NAMES.get(capability, {}).get(exe, capability).split()
        argv = [*template, *names]
        if needs_sudo and not _is_root():
            argv = ["sudo", *argv]
        return argv
    return None


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------

# Capability keys accepted by `--fix <capability>`, in plan order.
CAPABILITIES = ("git", "embedder", "vectors", "llm-local")

_NO_PKG_MANAGER = ("git is missing and no supported system package manager was found on "
                   "PATH (dnf, apt-get, zypper, pacman, brew, choco, winget). Install git "
                   "with your platform's installer.")


def _llm_wants_builtin(cfg) -> bool:
    """Does the *resolved* config actually call for the local llama-cpp tier?

    ``provider = "auto"`` is not "builtin": build_llm's auto path prefers a
    reachable Ollama that already has the target model pulled (see
    llm/base.py::_resolve_auto_llm), and only falls back to the built-in runtime
    otherwise. Asking that same question here is what keeps `--fix` from pulling
    a 20MB+ wheel on a machine whose wiki tier would never use it.
    """
    llm = cfg.llm
    if not llm.enabled:
        return False
    if llm.provider == "builtin":
        return True
    if llm.provider != "auto":
        return False
    from .._util import ollama_has_model, ollama_reachable
    from ..llm.base import default_base_url

    base_url = getattr(llm, "base_url", None) or default_base_url("ollama")
    model = getattr(llm, "model", None) or "llama3.1"
    return not (ollama_reachable(base_url) and ollama_has_model(base_url, model))


def _embedder_module(cfg) -> tuple[str, str]:
    """(module, pip extra) for the built-in embedder this config would use."""
    engine = (getattr(cfg.embeddings, "engine", "model2vec") or "model2vec").lower()
    return ("fastembed", "kb-fastembed") if engine == "fastembed" else ("model2vec", "kb-local")


def _embeddings_want_builtin(cfg) -> bool:
    emb = cfg.embeddings
    if not emb.enabled:
        return False
    if emb.provider == "builtin":
        return True
    if emb.provider != "auto":
        return False
    from .._util import ollama_has_model, ollama_reachable

    base_url = getattr(emb, "base_url", None) or "http://127.0.0.1:11434"
    model = getattr(emb, "model", None) or "nomic-embed-text"
    return not (ollama_reachable(base_url) and ollama_has_model(base_url, model))


def _remedy(key: str, cfg) -> Remedy | None:
    """The repair for one capability, or None when nothing is needed."""
    if key == "git":
        # Never plan a privileged command for something already installed: an
        # explicit `--fix git` on a machine that has git must not ask for sudo.
        if shutil.which("git") is not None:
            return None
        argv = system_install_command("git")
        if argv is None:
            return None
        return Remedy("git", "needed to clone and index repositories",
                      argv, privileged=True)

    if key == "embedder":
        module, extra = _embedder_module(cfg)
        return Remedy(key, f"built-in embedder for semantic search ({module})",
                      _pip(f"contextlake[{extra}]"))

    if key == "vectors":
        return Remedy(key, "sqlite-vec ANN backend for semantic search at scale",
                      _pip("contextlake[kb-vec]"))

    if key == "llm-local":
        return Remedy(
            key, "built-in wiki LLM runtime (llama-cpp-python)",
            _pip("contextlake[llm-local]", extra_index=LLAMA_CPP_WHEEL_INDEX,
                 only_binary="llama-cpp-python"),
            notes=[
                "Adding the upstream CPU wheel index: llama-cpp-python publishes no "
                "wheels to PyPI (llama.cpp is built per hardware backend, so upstream "
                "ships one index per accelerator), and without one pip compiles C++ "
                "from source and needs cmake plus a compiler.",
            ],
        )
    return None


def build_plan(cfg, requested: str) -> tuple[list[Remedy], list[str]]:
    """Plan the repairs for ``requested`` ("auto" or a capability key).

    Returns (remedies, notes). "auto" installs only what the resolved config
    actually calls for and is actually missing; an explicit capability is
    planned regardless of config, since the user named it (pip itself is
    idempotent when the package is already satisfied).
    """
    notes: list[str] = []
    if requested != "auto":
        r = _remedy(requested, cfg)
        if r is None and requested == "git":
            notes.append("git is already on PATH." if shutil.which("git")
                         else _NO_PKG_MANAGER)
        return ([r] if r else []), notes

    plan: list[Remedy] = []

    if shutil.which("git") is None:
        r = _remedy("git", cfg)
        if r is not None:
            plan.append(r)
        else:
            notes.append(_NO_PKG_MANAGER)

    if _embeddings_want_builtin(cfg):
        module, _extra = _embedder_module(cfg)
        if not _module_present(module):
            plan.append(_remedy("embedder", cfg))

    if cfg.embeddings.enabled and cfg.embeddings.vector_backend in ("auto", "sqlite-vec"):
        # Present-but-unloadable is a sqlite3 extension-loading problem (a
        # sqlite3 built without enable_load_extension), which no pip install
        # fixes -- so only an absent module becomes a plan item.
        if not _module_present("sqlite_vec"):
            plan.append(_remedy("vectors", cfg))

    if _llm_wants_builtin(cfg) and not _module_present("llama_cpp"):
        plan.append(_remedy("llm-local", cfg))

    return plan, notes


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------

def _classify_failure(output: str) -> tuple[str, str] | None:
    """(cause, next step) for a recognised pip failure, else None.

    Keeps a wall of pip/compiler/traceback output from being the only thing the
    user sees when the real story is one sentence long.
    """
    low = output.lower()
    if "externally-managed-environment" in low or "externally managed environment" in low:
        return ("This Python is managed by your operating system (PEP 668), so pip "
                "refuses to install into it.",
                "Install contextlake into a virtual environment instead:\n"
                "    python3 -m venv .venv && . .venv/bin/activate\n"
                "    pip install 'contextlake[kb-full]'\n"
                "  or use pipx, which manages the environment for you:\n"
                "    pipx install 'contextlake[kb-full]'")
    # TLS is checked BEFORE the timeout: an intercepting proxy typically produces
    # both (pip retries until it times out), and the CA bundle is the actionable
    # half. Reporting "try again later" there sends the user down a dead end.
    if "certificate_verify_failed" in low or "sslerror" in low or "ssl: " in low:
        return ("TLS verification failed, which usually means an intercepting proxy "
                "whose CA certificate pip does not trust.",
                "Point pip at your organisation's CA bundle "
                "(PIP_CERT=/path/to/ca.pem or pip config set global.cert ...).")
    if "read timed out" in low or "timed out" in low or "connection timeout" in low:
        return ("The package index did not answer in time (network or proxy).",
                "Re-run when the network is healthy, or point pip at an internal "
                "mirror with --index-url.")
    if "no matching distribution" in low or "could not find a version" in low:
        return ("No installable distribution matched this interpreter and platform.",
                "Check `python3 --version` against the package's supported versions; "
                "for the built-in LLM a compiler-free install needs the CPU wheel "
                f"index ({LLAMA_CPP_WHEEL_INDEX}).")
    if "cmake" in low or "failed building wheel" in low or "compiler" in low:
        return ("A package tried to compile from source and no working C/C++ "
                "toolchain was found.",
                "Install a toolchain, or prefer a prebuilt wheel with "
                "--only-binary :all: plus the CPU wheel index.")
    return None


def _run(argv: list[str]) -> tuple[int, str]:
    """Run a repair command, capturing output so a failure can be explained
    rather than dumped. Never ``shell=True``, never ``check=True``."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True)  # noqa: S603
    except FileNotFoundError:
        return 127, f"{argv[0]}: not found"
    except OSError as e:  # noqa: BLE001 - reported, never a traceback
        return 1, str(e)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _print_wrapped(text: str, indent: str = "    ") -> None:
    """Prose wrapped to the terminal, so an explanation never hard-wraps
    mid-word past column 80. Commands are never wrapped: they must stay
    copy-pasteable on one line."""
    import textwrap

    width = max(40, style.terminal_width() - len(indent))
    for line in textwrap.wrap(text, width=width) or [""]:
        print(indent + style.dim(line))


def _report_failure(output: str) -> None:
    classified = _classify_failure(output)
    if classified:
        cause, fix = classified
        print(f"    {style.red('failed')}")
        _print_wrapped(cause, indent="      ")
        # The next-step text carries its own commands on their own lines, so it
        # is printed verbatim rather than re-wrapped.
        print(f"      {style.dim('next:')} {fix}")
        return
    tail = [ln for ln in output.strip().splitlines() if ln.strip()][-8:]
    print(f"    {style.red('failed')} the install command did not succeed:")
    for line in tail:
        print(f"      {style.dim(line)}")


def apply_plan(plan: list[Remedy], *, dry_run: bool, interactive: bool) -> bool:
    """Print every command, then run the ones we are allowed to run.

    ``interactive`` gates the privileged tier only: False means print and move
    on, never prompt, never execute. Returns True when nothing failed.
    """
    ok = True
    for r in plan:
        tier = "system package" if r.privileged else "python package"
        print(f"  {style.cyan(r.key)} {style.dim('— ' + r.summary)} [{tier}]")
        for note in r.notes:
            _print_wrapped(note)
        # The command is always printed before anything can run it, both tiers.
        print(f"    $ {' '.join(r.argv)}")

        if dry_run:
            print(f"    {style.dim('dry run: not executed')}")
            continue

        if r.privileged:
            if not interactive:
                print(f"    {style.yellow('not run')} "
                      + style.dim("— needs administrator rights, and this is not an "
                                  "interactive terminal"))
                print("    " + style.dim("run the command above yourself, then re-run "
                                         "contextlake doctor"))
                continue
            try:
                reply = input("    Run this privileged command now? [y/N]: ").strip().lower()
            except EOFError:
                reply = ""
            if reply not in ("y", "yes"):
                print(f"    {style.dim('skipped')}")
                continue

        code, output = _run(r.argv)
        if code == 0:
            print(f"    {style.green('done')}")
        else:
            ok = False
            _report_failure(output)
    return ok


def run_fix(cfg, requested: str, *, dry_run: bool, skip_interactive: bool) -> bool:
    """The whole ``--fix`` pass. Returns True when nothing failed."""
    if requested not in ("auto", *CAPABILITIES):
        print(style.red(f"unknown capability for --fix: {requested}"))
        print(f"  known capabilities: {', '.join(CAPABILITIES)} "
              f"(or --fix with no value for what your config calls for)")
        return False

    plan, notes = build_plan(cfg, requested)
    print()
    print(style.bold("fix" + (" (dry run)" if dry_run else "")))
    for note in notes:
        print(f"  {style.yellow('⚠')} {note}")
    if not plan:
        print(f"  {style.dim('nothing to install: your configuration is already satisfied')}")
        return True

    interactive = _interactive() and not skip_interactive
    return apply_plan(plan, dry_run=dry_run, interactive=interactive)
