"""`RepoTooLarge` must point at a control that exists.

Split out of `tests/test_error_messages_name_real_flags.py`, which stays in the core
tier: this half imports `contextlake.kb`, and CI's core job installs no `[kb]` extra,
so importing it there is a ModuleNotFoundError on every Python version. A guard test
catches that, and caught this.

The general rule -- no exception message may name an unregistered flag -- lives in the
core file and needs no kb import. This is the one case it was written for, pinned
separately because the general guard also passes if the advice is deleted outright, and
advice that is merely absent is a quieter defect: the error still says the repository is
too big and nothing about what to do.
"""

from __future__ import annotations


def test_the_repo_too_large_message_names_the_setting_that_exists():
    """The specific case above, pinned separately.

    The general guard passes if the advice is deleted outright, and advice that is
    merely absent is a different, quieter defect: the error still tells the reader
    their repository is too big and nothing about what to do.
    """
    from contextlake.kb.parse import RepoTooLarge

    msg = str(RepoTooLarge("some/repo", 9 * 1073741824, 4 * 1073741824, {"c": 5}))
    assert "kb.languages" in msg
    assert "--languages" not in msg
    assert "kb.max_repo_memory" in msg
