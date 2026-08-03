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
  default) and scrub group names, URLs, and paths from any logs or issues you
  share.
- **The project cache** (`/tmp/<...>.json` and `.txt` by default) lists the
  repositories you can access. Treat it as mildly sensitive and don't commit it.

## Supported versions

This is a young project; security fixes land on `main` and ship in the next
release. Please run a recent version before reporting.
