#!/usr/bin/env python3
"""Bare-script launcher for contextlake.

Lets the tool run without installation via ``python3 contextlake.py <command>``.
It puts the ``src/`` layout on ``sys.path`` (ahead of the cwd) so that
``import contextlake`` resolves to the package under ``src/gitlab_sync`` rather
than to this launcher file, then delegates to the package CLI.

For an installed copy, prefer the ``gitlab-sync`` console command or
``python -m contextlake``.
"""

import os
import sys

# Put src/ ahead of everything (including this script's own directory) so that
# `import contextlake` resolves to the package, not to this same-named launcher.
# Insert unconditionally: an editable install may already have src/ further down
# sys.path, which must not let the repo-root launcher shadow the package.
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
sys.path.insert(0, _SRC)

from contextlake.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
