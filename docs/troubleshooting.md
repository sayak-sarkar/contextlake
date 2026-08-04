# Troubleshooting

Problems that have actually been hit while installing or running contextlake, each with the
fix and the reason behind it. If you hit something that is not here, please
[open an issue](https://github.com/sayak-sarkar/contextlake/issues): a reproducible failure
is worth more to this page than a guess.

Working on contextlake itself rather than using it? The contributor-side problems
(a distro-owned PyJWT, `pre-commit` diffs, slow local test runs) live in
[CONTRIBUTING.md](../CONTRIBUTING.md).

## The knowledge layer will not install

`[kb]` needs Python 3.10 or newer, because the `mcp` SDK does. The mirror side of contextlake
supports 3.9, so an install can resolve and then fail at import if you are on 3.9. Check with
`python3 --version`.

The extra also pulls a tree-sitter grammar per supported language, several with native
wheels. On a platform without prebuilt wheels those build from source, which is slow rather
than broken.

## `pip install "contextlake[llm-local]"` tries to compile C++

```
Building wheel for llama-cpp-python (pyproject.toml) ... error
CMake Error: could not find cmake ...
```

Expected on every platform and every Python version, and only on a **pip** install.
`llama-cpp-python` publishes no wheels to PyPI at all, so pip has nothing to install but the
sources and falls back to compiling `llama.cpp`, which needs `cmake` plus a C++ toolchain.

Let contextlake attach the prebuilt CPU wheel index for you:

```bash
contextlake doctor --fix llm-local          # --dry-run prints the command and stops
```

The standalone binary already carries that index in its bootstrap configuration, and the full
Docker image ships the runtime baked in, so neither channel hits this. For the command by
hand, see [Install and upgrade](install.md#the-built-in-wiki-llm-needs-one-extra-flag); for
the CUDA and Metal indexes, and why the extra cannot carry the index URL itself, see
[Installing the built-in LLM](model-providers.md#installing-the-built-in-llm-and-why-it-needs-a-wheel-index).

If you would rather skip the native build entirely, use Ollama for the wiki tier
(`--llm ollama`).

## `contextlake doctor --fix` says the environment is externally managed

```
error: externally-managed-environment
```

Your distribution marked the system Python as managed by its own package manager (PEP 668),
and pip refuses to write into it. `--fix` reports this rather than retrying, because the fix
is where contextlake lives, not which flag you pass:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install "contextlake[kb-full]"
```

or let `pipx` own the environment: `pipx install "contextlake[kb-full]"`. Never reach for
`--break-system-packages`; it does what it says.

## `doctor --fix` printed a `sudo` command instead of running it

Working as designed. `--fix` installs **Python** packages into the current interpreter
unattended, but a **system** package needs administrator rights, so it is only ever offered
with a y/N prompt at a real terminal. `git` is the only such package `--fix` offers. Without
a TTY, or with `--skip-interactive`, the exact command is printed and nothing privileged
runs. That keeps a CI job or a scripted run from ever tripping a sudo prompt. Copy the
printed command, run it yourself, and re-run `contextlake doctor`.

## The mirror

Symptoms specific to mirroring a fleet, and what to do about each.

| Symptom | What to do |
| --- | --- |
| **"Cache file not found"** | Run `contextlake mirror fetch` first to populate the projects cache. |
| **"Permission denied" during cloning** | Make sure `glab` is authenticated (`glab auth login`) and you can reach the repositories. |
| **"Timeout" errors** | Raise the relevant `*_timeout` settings, check connectivity, or lower `max_workers` (set it to `1` to run serially). Behind a TLS-inspecting proxy, set `GITLAB_TOKEN` so enumeration uses the built-in HTTP client. |
| **"Detached HEAD" states** | Handled automatically, the repo is skipped for pulls rather than failing. |
| **Nested `.git` directories** | A repo cloned into a subfolder of itself. `contextlake mirror verify` flags it; fix by moving the inner tree up one level and removing the empty folder. |
| **Cron job not running** | Check `crontab -l`, use absolute paths, and test the exact command in a shell first; inspect cron logs (`grep CRON /var/log/syslog`). See [Keep it fresh on a schedule](keep-fresh.md#keep-it-fresh-on-a-schedule). |
| **Large log files** | `--log-file` rotates itself; a shell redirect does not. See [Log files and rotation](keep-fresh.md#log-files-and-rotation). |

## Still stuck

Run `contextlake doctor` and include its full output in your issue. It reports the resolved
config paths, what is on your PATH, the store's real counts, and which optional pieces are
missing, which is most of what anyone would ask you for anyway.

## See also

- [Install and upgrade](install.md), every channel, and how to upgrade safely
- [Reading the console output](console-output.md), decoding a run
- [Command reference](cli-reference.md), the exact flags
- [CONTRIBUTING.md](../CONTRIBUTING.md), problems you hit working on contextlake itself
