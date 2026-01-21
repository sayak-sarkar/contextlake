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

`gitlab-sync` is a local developer tool. A few things worth knowing:

- **It never handles your credentials.** Authentication is delegated entirely to
  [`glab`](https://gitlab.com/gitlab-org/cli) and your `git` credential helper /
  SSH keys. The tool stores no tokens and asks for none.
- **Configuration may contain a private GitLab group name.** Keep your real
  `.gitlab_sync.ini` out of version control (it's git-ignored by default) and
  scrub group names, URLs, and paths from any logs or issues you share.
- **The project cache** (`/tmp/<...>.json` and `.txt` by default) lists the
  repositories you can access. Treat it as mildly sensitive and don't commit it.

## Supported versions

This is a young project; security fixes land on `main` and ship in the next
release. Please run a recent version before reporting.
