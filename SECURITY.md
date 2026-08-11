# Security Policy

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue.

Email **sayak.bugsmith@gmail.com** with:

- a description of the issue and its impact,
- steps to reproduce (a proof of concept if you have one), and
- any suggested remediation.

You can expect an acknowledgement within a few days. Once a fix is available,
we'll coordinate disclosure.

## Scope and design notes

`contextlake` is a local developer tool. A few things worth knowing:

- **Credentials are read from environment variables, never stored, logged, or
  passed in argv/URLs.** Mirroring reads a platform token (`GITLAB_TOKEN` /
  `GITHUB_TOKEN` / `BITBUCKET_TOKEN` / `GITEA_TOKEN`) when set, or delegates to
  [`glab`](https://gitlab.com/gitlab-org/cli) / your `git` credential helper /
  SSH keys otherwise. The optional knowledge layer's connectors (Atlassian,
  Figma, Slack) and LLM providers (Anthropic, OpenAI, or an Ollama/OpenAI-
  compatible endpoint) read their own API key from an env var you name via
  config (`api_key_env`, default `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`) and send
  it directly to that provider's API — never inferred from an unrelated key
  already in your environment, and never written to disk or a log line.
- **Configuration may contain a private GitLab group name.** Keep your real
  `.contextlake.ini`/`kb.toml` out of version control (both are git-ignored by
  default).
- **Logs are scrubbed for you.** The `--log-file` copy of a run is redacted by
  default: workspace paths, `$HOME`, the group/org name, a self-hosted forge
  hostname and repository names are replaced with placeholders (repositories
  become a stable `repo-<digest>`, so a scrubbed log still reads coherently).
  Attach it to an issue as-is. `--redact` extends the same treatment to the
  console; `--no-redact` disables it. This is obfuscation for sharing, not a
  cryptographic guarantee — a short, guessable repository name can be confirmed
  by someone who guesses it — so a log from a highly sensitive environment still
  deserves a read-through before you post it. See
  [docs/console-output.md](docs/console-output.md#sharing-a-log---redact).
- **The project cache** (`/tmp/<...>.json` and `.txt` by default) lists the
  repositories you can access. Treat it as mildly sensitive and don't commit it.

## Workspace trust

contextlake discovers a project-local `.contextlake.kb.toml` (and `.contextlake.ini`)
by walking up from your current directory to the filesystem root, the same way git
finds `.git`. That is convenient — a config at a project root applies to every
subdirectory — but it means a config file can take effect **without you ever naming
it**, including one that arrived inside a repository somebody else wrote. Mirroring
(`contextlake mirror sync`) and the dashboard's "Add repo" action clone repositories
into your workspace, so such a file can land there without you creating it by hand.

A few config keys become part of a command line contextlake executes:

| Key | What it does |
|---|---|
| `[llm] command`, `[llm] args` | the agent CLI invoked when `provider = "cli"` |
| `[llm] provider`, `[llm] review_provider` | only when set to `"cli"` |
| `[[sources]] command`, `args`, `mcp_command` | the MCP server spawned over stdio |

**Those keys are honoured only from a config file you chose:** the global
`~/.contextlake/kb.toml`, or a path you passed to `--config`. When they appear in an
auto-discovered file, they are dropped and a warning naming the file and the key is
logged. Everything else in that file still applies as normal — `store_dir`,
`languages`, `[embeddings]`, `[[rules]]`, and non-`cli` LLM providers all keep working,
so directory-scoped config is unaffected.

Passing `--config ./.contextlake.kb.toml` from inside such a repository *does* make it
privileged. That is intended: naming the file is the explicit decision the gate asks
for. Only do it for repositories you trust.

To opt out of the discovered tier entirely — recommended for CI, containers, and
anything that processes untrusted checkouts in bulk:

```bash
export CONTEXTLAKE_NO_LOCAL_CONFIG=1
```

Ancestor discovery is then skipped for both `.contextlake.ini` and
`.contextlake.kb.toml`; only the global file and an explicit `--config` are read. With it set,
`contextlake kb source add --local` also writes to the global config rather than to a local file
this environment would never read.

What this gate does **not** cover, deliberately: a discovered config can still set `[llm] enabled`
and `[[sources]] url`. Neither runs attacker code, but the first can switch on an LLM tier your
global config already points at (spending against that key), and the second can point a connector
at a host of the file author's choosing. Those are data-egress questions rather than code
execution; use `CONTEXTLAKE_NO_LOCAL_CONFIG=1` if you need the discovered tier gone entirely.

`[[sources]] url` really is limited to a *host*: ingest fetchers open `http`/`https` only and
refuse any other scheme with a warning. That is enforced, not assumed — `urllib` also speaks
`file:`, `ftp:` and `data:`, so without the restriction a discovered config could have named
`file:///…` and read a local file into the graph, which would be disclosure rather than egress.
Requests to private or link-local addresses are *not* currently blocked, so a discovered config
can still reach an address only your machine can route to.

## Supported versions

This is a young project; security fixes land on `main` and ship in the next
release. Please run a recent version before reporting.

## Published advisories

Every advisory is published on this repository's
[security advisories page](https://github.com/sayak-sarkar/contextlake/security/advisories),
which is the canonical record; the CHANGELOG entry for the fixing release links to it.

| Advisory | Severity | Affected | Fixed in |
| --- | --- | --- | --- |
| [GHSA-fwx4-9qvg-98qc](https://github.com/sayak-sarkar/contextlake/security/advisories/GHSA-fwx4-9qvg-98qc) — stored XSS in generated graph pages, escalating to dashboard token theft | Critical, CVSS 3.1 9.3 | `>= 2.2.0, < 6.2.0` | **6.2.0** |

If you are on any version below 6.2.0, upgrade. The affected surface is every generated
graph page, so it is reached by `kb graph`, `kb graph --c4`, `kb graph --serve`, the built
site, and `kb dashboard --serve` — that last one being where it escalated, since those pages
share an origin with the script holding the per-process mutation and LLM token.

**Regenerate any graph page or site you saved from an affected version.** Upgrading fixes
the generator, not the HTML files it already wrote.

## Dependency advisories, and how they are dispositioned

`pip-audit` runs in CI over the **resolved** dependency set for every shipped extra, and it
carries **no ignore list**. A suppression that outlives its reason turns a security gate into
decoration, so an advisory here is either fixed or the job is red.

### CVE-2025-69872 (diskcache) — resolved in 7.0.0 by removal

`PYSEC-2026-2447` / `GHSA-w8v5-vhqr-4h9v` affected `diskcache` 5.6.3, which contextlake
reached transitively through `[llm-local]` -> `llama-cpp-python`. No fixed upstream version
existed, so the finding could not be cleared by upgrading, and it was carried in CI as an
explicit "known-unresolved, disposition pending" suppression rather than as a silent one.

**7.0.0 removes the dependency.** The built-in wiki LLM moved from `llama-cpp-python` to
`openvino-genai`, whose closure is `openvino-tokenizers` and `openvino` and contains no
`diskcache`. Verified by resolving the extra in a clean environment and listing what actually
installs, not by reading the advisory. The suppression is gone from `security.yml` with it.

Users on earlier versions who never installed `[llm-local]` were never exposed: the package
was only ever reached through that extra.
