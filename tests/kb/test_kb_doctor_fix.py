"""Tests for `contextlake doctor --fix` -- the dependency-remediation pass.

The load-bearing property here is the privilege boundary: pip runs into THIS
interpreter unattended, a privileged system command never runs without a human
answering a prompt at a real terminal. Every test that touches execution asserts
on the captured argv, so a rename or a shell string can't slip past.
"""

import sys

import pytest

from contextlake.cli import main
from contextlake.kb.cmds import doctor_fix


def _run(argv):
    with pytest.raises(SystemExit) as e:
        main(argv)
    return e.value.code


def _kb_config(tmp_path, body: str = "") -> str:
    cfg = tmp_path / "kb.toml"
    cfg.write_text(f'[kb]\nstore_dir = "{tmp_path / "kb"}"\n{body}')
    return str(cfg)


class _Spy:
    """Records every subprocess argv the fix pass would spawn."""

    def __init__(self, returncode=0, output=""):
        self.calls = []
        self.returncode = returncode
        self.output = output

    def __call__(self, argv):
        self.calls.append(list(argv))
        return self.returncode, self.output


@pytest.fixture
def spy(monkeypatch):
    s = _Spy()
    monkeypatch.setattr(doctor_fix, "_run", s)
    return s


@pytest.fixture
def nothing_installed(monkeypatch):
    """The dev venv has llama_cpp/model2vec/sqlite_vec installed; without this
    seam the config-scope tests would pass or fail by accident of environment."""
    monkeypatch.setattr(doctor_fix, "_module_present", lambda name: False)


@pytest.fixture
def git_present(monkeypatch):
    """The other environment seam: a runner without git on PATH would otherwise
    add a `git` item to every plan and break the exact-equality assertions."""
    monkeypatch.setattr(doctor_fix.shutil, "which", lambda exe, *a, **k: f"/usr/bin/{exe}")


def _plan_keys(cfg_body, tmp_path, requested="auto"):
    from contextlake.kb.config import load_kb_config

    cfg = load_kb_config(_kb_config(tmp_path, cfg_body))
    plan, _notes = doctor_fix.build_plan(cfg, requested)
    return [r.key for r in plan]


# --- the privilege boundary -------------------------------------------------

def test_missing_system_dep_is_printed_never_executed_without_a_tty(
        tmp_path, monkeypatch, capsys, spy):
    # git missing, and dnf is the package manager found on PATH.
    monkeypatch.setattr(doctor_fix.shutil, "which",
                        lambda exe: None if exe == "git" else f"/usr/bin/{exe}")
    monkeypatch.setattr(doctor_fix, "_interactive", lambda: False)

    code = _run(["doctor", "--config", _kb_config(tmp_path), "--fix", "git"])
    out = capsys.readouterr().out

    assert "dnf install -y git" in out          # the exact command is printed in full
    assert "not run" in out
    assert spy.calls == []                      # ...and nothing was spawned
    assert code == 1                            # git missing -> doctor's own verdict


def test_skip_interactive_never_spawns_a_privileged_command(
        tmp_path, monkeypatch, capsys, spy):
    monkeypatch.setattr(doctor_fix.shutil, "which",
                        lambda exe: None if exe == "git" else f"/usr/bin/{exe}")
    # A real terminal -- and still nothing runs, because --skip-interactive was passed.
    monkeypatch.setattr(doctor_fix, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input",
                        lambda *a: pytest.fail("--skip-interactive must never prompt"))

    _run(["doctor", "--config", _kb_config(tmp_path), "--fix", "git",
          "--skip-interactive"])
    assert "sudo dnf install -y git" in capsys.readouterr().out
    assert spy.calls == []


def test_privileged_command_declined_at_the_prompt_runs_nothing(monkeypatch, spy):
    monkeypatch.setattr(doctor_fix, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    plan = [doctor_fix.Remedy("git", "git", ["sudo", "dnf", "install", "-y", "git"],
                              privileged=True)]

    assert doctor_fix.apply_plan(plan, dry_run=False, interactive=True) is True
    assert spy.calls == []


def test_privileged_command_accepted_at_the_prompt_runs_exactly_once(monkeypatch, spy):
    monkeypatch.setattr(doctor_fix, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    argv = ["sudo", "dnf", "install", "-y", "git"]

    doctor_fix.apply_plan([doctor_fix.Remedy("git", "git", argv, privileged=True)],
                          dry_run=False, interactive=True)
    assert spy.calls == [argv]


def test_every_unattended_command_runs_this_interpreter(
        tmp_path, monkeypatch, spy, nothing_installed):
    """The one assertion that covers both halves of the boundary: without a TTY,
    everything that actually runs is `sys.executable -m pip`."""
    monkeypatch.setattr(doctor_fix.shutil, "which",
                        lambda exe: None if exe == "git" else f"/usr/bin/{exe}")
    monkeypatch.setattr(doctor_fix, "_interactive", lambda: False)

    body = ('[llm]\nenabled = true\nprovider = "builtin"\n'
            '[embeddings]\nenabled = true\nprovider = "builtin"\n')
    _run(["doctor", "--config", _kb_config(tmp_path, body), "--fix"])

    assert spy.calls, "the pip tier should still have run"
    for argv in spy.calls:
        assert argv[:3] == [sys.executable, "-m", "pip"]


# --- pip invocation ---------------------------------------------------------

def test_pip_is_the_current_interpreter_not_a_bare_pip(tmp_path, spy, nothing_installed):
    _run(["doctor", "--config", _kb_config(tmp_path), "--fix", "vectors"])

    assert len(spy.calls) == 1
    argv = spy.calls[0]
    assert argv[0] == sys.executable
    assert argv[1:4] == ["-m", "pip", "install"]
    assert "contextlake[kb-vec]" in argv


def test_local_llm_install_carries_the_upstream_wheel_index(
        tmp_path, capsys, spy, nothing_installed):
    _run(["doctor", "--config", _kb_config(tmp_path), "--fix", "llm-local"])
    out = capsys.readouterr().out

    argv = spy.calls[0]
    assert "--extra-index-url" in argv
    assert argv[argv.index("--extra-index-url") + 1] == doctor_fix.LLAMA_CPP_WHEEL_INDEX
    assert "contextlake[llm-local]" in argv
    # a source build is refused for llama-cpp-python ONLY: `:all:` would forbid a
    # source fallback for every other dependency too
    assert argv[argv.index("--only-binary") + 1] == "llama-cpp-python"
    # generous timeout/retries: TLS-intercepting proxies make a 20MB+ wheel slow
    assert "--timeout" in argv and "--retries" in argv
    # ...and the output says why an index is being added at all (the explanation
    # is wrapped to the terminal, so compare on collapsed whitespace)
    assert "no wheels to PyPI" in " ".join(out.split())


# --- scope resolution -------------------------------------------------------

def test_disabled_llm_is_not_in_the_plan(tmp_path, nothing_installed):
    assert "llm-local" not in _plan_keys("", tmp_path)


def test_ollama_llm_is_not_in_the_plan(tmp_path, nothing_installed):
    body = '[llm]\nenabled = true\nprovider = "ollama"\n'
    assert "llm-local" not in _plan_keys(body, tmp_path)


def test_auto_llm_with_a_usable_ollama_is_not_in_the_plan(
        tmp_path, monkeypatch, nothing_installed):
    # provider = "auto" resolves to Ollama when the daemon has the model, so the
    # 23MB llama-cpp wheel must not be pulled on such a machine.
    monkeypatch.setattr("contextlake.kb._util.ollama_reachable", lambda *a, **k: True)
    monkeypatch.setattr("contextlake.kb._util.ollama_has_model", lambda *a, **k: True)
    body = '[llm]\nenabled = true\nprovider = "auto"\n'
    assert "llm-local" not in _plan_keys(body, tmp_path)


def test_auto_llm_without_ollama_falls_back_to_the_local_runtime(
        tmp_path, monkeypatch, nothing_installed):
    monkeypatch.setattr("contextlake.kb._util.ollama_reachable", lambda *a, **k: False)
    body = '[llm]\nenabled = true\nprovider = "auto"\n'
    assert "llm-local" in _plan_keys(body, tmp_path)


def test_builtin_llm_is_in_the_plan(tmp_path, nothing_installed, git_present):
    body = '[llm]\nenabled = true\nprovider = "builtin"\n'
    assert _plan_keys(body, tmp_path) == ["llm-local"]


def test_explicit_llm_local_ignores_the_config(tmp_path, spy, nothing_installed):
    # [llm] disabled entirely, and the user asked for it by name anyway.
    _run(["doctor", "--config", _kb_config(tmp_path), "--fix", "llm-local"])
    assert "contextlake[llm-local]" in spy.calls[0]


def test_installed_capability_is_not_replanned(tmp_path, monkeypatch, git_present):
    monkeypatch.setattr(doctor_fix, "_module_present", lambda name: True)
    body = ('[llm]\nenabled = true\nprovider = "builtin"\n'
            '[embeddings]\nenabled = true\nprovider = "builtin"\n')
    assert _plan_keys(body, tmp_path) == []


def test_fastembed_engine_selects_its_own_extra(tmp_path, nothing_installed):
    from contextlake.kb.config import load_kb_config

    body = ('[embeddings]\nenabled = true\nprovider = "builtin"\nengine = "fastembed"\n')
    cfg = load_kb_config(_kb_config(tmp_path, body))
    plan, _ = doctor_fix.build_plan(cfg, "auto")
    assert any("contextlake[kb-fastembed]" in r.argv for r in plan)


def test_unknown_capability_is_rejected_without_running_anything(
        tmp_path, capsys, spy):
    code = _run(["doctor", "--config", _kb_config(tmp_path), "--fix", "nonsense"])
    out = capsys.readouterr().out
    assert "unknown capability" in out
    assert "llm-local" in out          # the valid keys are listed
    assert spy.calls == []
    assert code == 1


# --- dry run ----------------------------------------------------------------

def test_dry_run_executes_nothing(tmp_path, capsys, spy, nothing_installed):
    body = '[llm]\nenabled = true\nprovider = "builtin"\n'
    _run(["doctor", "--config", _kb_config(tmp_path, body), "--fix", "--dry-run"])
    out = capsys.readouterr().out

    assert "dry run" in out
    assert "-m pip install" in out     # the full command is still shown
    assert spy.calls == []


# --- failure reporting ------------------------------------------------------

def test_pep_668_failure_explains_the_venv_path(tmp_path, monkeypatch, capsys,
                                                nothing_installed):
    monkeypatch.setattr(doctor_fix, "_run", lambda argv: (
        1, "error: externally-managed-environment\nThis environment is externally managed"))
    code = _run(["doctor", "--config", _kb_config(tmp_path), "--fix", "vectors"])
    out = capsys.readouterr().out

    assert "PEP 668" in out
    assert "venv" in out and "pipx" in out
    assert "Traceback" not in out
    assert code == 1


def test_network_failure_reports_the_real_cause(tmp_path, monkeypatch, capsys,
                                                nothing_installed):
    monkeypatch.setattr(doctor_fix, "_run",
                        lambda argv: (1, "WARNING: Read timed out. (read timeout=15)"))
    _run(["doctor", "--config", _kb_config(tmp_path), "--fix", "vectors"])
    out = capsys.readouterr().out

    assert "did not answer in time" in out
    assert "Traceback" not in out


def test_tls_interception_beats_the_timeout_it_also_caused(tmp_path, monkeypatch, capsys,
                                                           nothing_installed):
    # A TLS-intercepting proxy produces both symptoms; the CA bundle is the
    # actionable one, so "try again later" must not win the classification.
    monkeypatch.setattr(doctor_fix, "_run", lambda argv: (
        1, "WARNING: Read timed out.\nSSLError(CERTIFICATE_VERIFY_FAILED)"))
    _run(["doctor", "--config", _kb_config(tmp_path), "--fix", "vectors"])
    out = " ".join(capsys.readouterr().out.split())

    assert "CA bundle" in out
    assert "did not answer in time" not in out


def test_unrecognised_failure_still_shows_the_tail_not_a_traceback(
        tmp_path, monkeypatch, capsys, nothing_installed):
    monkeypatch.setattr(doctor_fix, "_run", lambda argv: (2, "something went sideways"))
    _run(["doctor", "--config", _kb_config(tmp_path), "--fix", "vectors"])
    out = capsys.readouterr().out

    assert "something went sideways" in out
    assert "Traceback" not in out


# --- the existing surface is untouched --------------------------------------

def test_plain_doctor_output_and_exit_code_are_unchanged(tmp_path, capsys, spy):
    code = _run(["doctor", "--config", _kb_config(tmp_path)])
    out = capsys.readouterr().out

    assert code == 0
    # the verdict is still the last thing printed: no fix section was appended
    assert out.rstrip().endswith("OK")
    assert spy.calls == []


def test_sudo_is_not_prefixed_for_brew(monkeypatch):
    monkeypatch.setattr(doctor_fix.shutil, "which",
                        lambda exe: "/opt/homebrew/bin/brew" if exe == "brew" else None)
    assert doctor_fix.system_install_command("git") == ["brew", "install", "git"]


def test_no_package_manager_is_reported_not_guessed(tmp_path, monkeypatch):
    from contextlake.kb.config import load_kb_config

    monkeypatch.setattr(doctor_fix.shutil, "which", lambda exe: None)
    cfg = load_kb_config(_kb_config(tmp_path))
    plan, notes = doctor_fix.build_plan(cfg, "auto")

    assert plan == []
    assert any("no supported system package manager" in n for n in notes)
