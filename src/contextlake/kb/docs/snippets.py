"""Source lines for a generated document, quoted only when they can be proved current.

The API reference names a file and a line for every call site. Showing the LINE ITSELF is what
makes an entry an example rather than a pointer, and it is also the one part of the document
that can be confidently wrong: the graph's line numbers were recorded when the repository was
indexed, and the file on disk may have moved on since.

So this module answers one question and refuses to guess at it:

    **can the line printed be proved to be the line that was indexed?**

Yes when the file's mtime is no newer than the repository's `indexed_at` stamp. No, or
unknown, in every other case -- and then nothing is quoted. The alternative, printing whatever
the line holds today, produces an example that looks exactly as authoritative as a correct one,
which is this project's most-repeated defect rather than a new risk.

The same `mtime_ns <= indexed_at` reasoning backs the stale-slice guard in `store/drift.py`.
That guard weighs a citation that a caller already has; this reads a line the caller does not
have yet, so it cannot reuse it, but the rule is deliberately identical: a change to how
freshness is decided should not mean two answers.
"""

from __future__ import annotations

from pathlib import Path

from ..store.drift import parse_indexed_at

# How much of one line to quote. A minified bundle or a generated table is one enormous line,
# and a document is not improved by ten thousand characters of it.
MAX_LINE_CHARS = 240


class SnippetReader:
    """Reads call-site lines for ONE repository, or reports why it cannot.

    Built per repository and per run. It caches the files it opens, because a widely-called
    symbol has many sites in one file and re-reading it per site is how a documentation pass
    over a large repository becomes too slow to run.

    ``reason`` is set when nothing can be quoted at all, and the caller is expected to print
    it: a document that silently contains no examples reads as a repository with no call
    sites, which is a different and wrong statement.
    """

    def __init__(self, store, repo_id: str):
        self.reason: str | None = None
        self._lines: dict[str, list[str] | None] = {}
        self._root: Path | None = None
        self._baseline: int | None = None

        repo = store.get_repo(repo_id)
        path = Path(repo.path) if repo and repo.path else None
        if path is None or not path.is_dir():
            # Ordinary rather than broken: a store outlives the checkout it was built from,
            # and the graph is still entirely usable without the working tree.
            self.reason = ("the repository's working tree is not on this machine, so no call "
                           "site could be quoted")
            return

        getter = getattr(store, "get_repo_indexed_at", None)
        self._baseline = parse_indexed_at(getter(repo_id) if getter else None)
        if self._baseline is None:
            # No baseline means no way to tell a current file from a changed one. That is
            # "cannot prove", which here is the same as no.
            self.reason = ("this repository has no `indexed_at` stamp, so no line could be "
                           "proved current and none is quoted")
            return
        self._root = path

    def line(self, rel_path: str, lineno: int) -> str | None:
        """The source at ``rel_path:lineno``, or None when it cannot be proved current.

        None covers every uncertain case and they are deliberately not distinguished here: a
        file changed after indexing, a file that no longer exists, an unreadable or binary
        file, and a line number past the end of the file all mean the same thing to a reader,
        which is that this line is not shown.
        """
        if self._root is None or not rel_path or not lineno or lineno < 1:
            return None
        lines = self._file(rel_path)
        if lines is None or lineno > len(lines):
            # A CRASH guard, not a correctness one, and worth being precise about which:
            # without it a line number past the end raises IndexError and takes the whole
            # documentation run down. It cannot catch a line that MOVED inside a file of the
            # same length; only the mtime gate does that. Reaching this branch at all means
            # the file shrank since indexing without its mtime changing.
            return None
        text = lines[lineno - 1].strip()
        if not text:
            return None
        return text[:MAX_LINE_CHARS] + ("..." if len(text) > MAX_LINE_CHARS else "")

    def _inside(self, rel_path: str) -> Path | None:
        """``rel_path`` resolved under the repository root, or None if it escapes it.

        A `..` in a stored path, an absolute path, or a symlink pointing out of the tree all
        read a file this document has no business quoting. Checked by RESOLVING both sides and
        asking whether one contains the other, never by comparing the strings: a prefix test
        passes `/repo-backup` for a root of `/repo`, and this project has been bitten by
        unanchored matching on an identity question more than once.

        `resolve()` also collapses the `..`, so a symlink is followed before the check rather
        than after it.
        """
        if self._root is None:
            return None
        try:
            root = self._root.resolve()
            path = (root / rel_path).resolve()
        except OSError:
            return None
        return path if path.is_relative_to(root) else None

    def _file(self, rel_path: str) -> list[str] | None:
        if rel_path not in self._lines:
            self._lines[rel_path] = self._read(rel_path)
        return self._lines[rel_path]

    def _read(self, rel_path: str) -> list[str] | None:
        path = self._inside(rel_path)
        if path is None:
            return None
        try:
            if path.stat().st_mtime_ns > (self._baseline or 0):
                # Written after the graph was built. Every line number the graph holds for
                # this file is now a candidate for having moved, so none of them is quotable.
                return None
            return path.read_text(encoding="utf-8", errors="strict").splitlines()
        except (OSError, ValueError, UnicodeDecodeError):
            # Missing, a directory, outside the tree, or not text. All of them are "no line".
            return None
