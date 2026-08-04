"""GitLab connector: link each repo to its open merge requests and issues.

Uses the authenticated ``glab`` CLI (the same auth the mirror uses) to read a
project's open MRs/issues, then maps them onto graph nodes/edges. The command
runner is injectable so the mapping is testable without GitLab. Generic: the
group/host come from config.
"""

from __future__ import annotations

import json
import subprocess
import urllib.parse

from ..ids import make_id
from ..model import EXTERNAL_REPO, Confidence, Node
from ..resilience import breaker_for, note_unavailable
from ..store.base import Store
from .common import link_edge, repo_node


class GitLabConnector:
    def __init__(self, name: str, *, group: str | None = None, timeout: float = 30,
                 per_page: int = 50, runner=None):
        self.name = name
        self.group = group
        self.timeout = timeout
        self.per_page = per_page
        self._run = runner or self._glab

    def _glab(self, endpoint: str) -> list:
        # Two GitLab API calls per repo, each able to burn the full timeout: with
        # `glab` unauthenticated or GitLab down, a fleet run pays that on every
        # repo. The breaker (keyed on the CLI, which is authenticated against one
        # host per run) writes the source off after a few failures instead, and
        # `note_unavailable` keeps the reason on screen -- this method's contract
        # is to return [] rather than raise, which is exactly the shape that
        # otherwise makes an outage read as "this project has no open MRs".
        try:
            res = breaker_for("glab-api").call(
                subprocess.run, ["glab", "api", endpoint], capture_output=True,
                text=True, timeout=self.timeout)
        except Exception as e:  # noqa: BLE001 - OSError/SubprocessError/CircuitOpenError
            note_unavailable("gitlab (glab api)", e)
            return []
        if res.returncode != 0 or not res.stdout.strip():
            return []
        try:
            data = json.loads(res.stdout)
        except (json.JSONDecodeError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _project_path(self, repo_id: str) -> str:
        full = f"{self.group}/{repo_id}" if self.group else repo_id
        return urllib.parse.quote(full, safe="")

    def fetch(self, repo_id: str) -> tuple[list, list]:
        """Open merge requests and issues for a repo (live)."""
        enc = self._project_path(repo_id)
        mrs = self._run(f"projects/{enc}/merge_requests?state=opened&per_page={self.per_page}")
        issues = self._run(f"projects/{enc}/issues?state=opened&per_page={self.per_page}")
        return mrs, issues

    def fetch_changes(self, repo_id: str, mr_iid: str) -> list[str]:
        """Changed file paths for one MR's diff (live). Never raises -- an
        unreachable GitLab or a malformed response yields an empty list, same
        style as fetch().

        Uses ``.../merge_requests/:iid/diffs``, not ``.../changes``: verified
        live against a public GitLab project that ``/changes`` wraps the diff
        inside the whole MR object under a ``changes`` key (a dict), while
        ``/diffs`` returns a bare array of per-file diff objects -- matching
        ``_run``'s existing list-only contract (see ``_glab``) with no extra
        unwrapping and no change to that shared method.
        """
        enc = self._project_path(repo_id)
        try:
            diffs = self._run(f"projects/{enc}/merge_requests/{mr_iid}/diffs")
        except Exception:
            return []
        if not isinstance(diffs, list):
            return []
        return [d["new_path"] for d in diffs if isinstance(d, dict) and d.get("new_path")]


# --- pure graph mapping (no network) ---------------------------------------

def _item_node(repo_id: str, kind: str, sigil: str, item: dict) -> Node:
    iid = item.get("iid") or item.get("id")
    attrs = {k: v for k, v in {
        "title": item.get("title"), "state": item.get("state"),
        "url": item.get("web_url"),
    }.items() if v}
    return Node(id=make_id("gitlab", kind, repo_id, str(iid)), repo=EXTERNAL_REPO,
                kind=kind, name=f"{repo_id}{sigil}{iid}", attrs=attrs)


def match_files_to_nodes(
    store: Store, repo_id: str, file_paths: list[str]
) -> list[tuple[str, Confidence]]:
    """Existing code-file nodes for repo_id whose ``file`` matches one of file_paths.

    A GitLab diff's file list is a hard fact -- a diff literally lists changed
    files -- so every match is ``Confidence.EXTRACTED``, not an inference.
    File nodes are indexed with ``name == file`` by every producer (see
    ``manifest.py``'s and ``parse.py``'s file-node construction), so an exact
    per-path ``Store.nodes_by_name(path, kind="file", repo=repo_id)`` lookup
    finds them without a repo-wide scan (``Store`` has no such scan method).
    """
    matches: list[tuple[str, Confidence]] = []
    for path in file_paths:
        for node in store.nodes_by_name(path, kind="file", repo=repo_id):
            matches.append((node.id, Confidence.EXTRACTED))
    return matches


def associate_gitlab(repo_id: str, mrs, issues) -> tuple[list, list]:
    """Map a repo's MRs/issues to external nodes + edges (no network)."""
    repo = repo_node(repo_id)
    nodes = [repo]
    edges = []
    for mr in mrs:
        if mr.get("iid") is None and mr.get("id") is None:
            continue
        node = _item_node(repo_id, "mr", "!", mr)
        nodes.append(node)
        edges.append(link_edge(repo_id, node, "has_merge_request", "gitlab",
                               confidence=Confidence.EXTRACTED))
    for issue in issues:
        if issue.get("iid") is None and issue.get("id") is None:
            continue
        node = _item_node(repo_id, "issue", "#", issue)
        nodes.append(node)
        edges.append(link_edge(repo_id, node, "has_issue", "gitlab",
                               confidence=Confidence.EXTRACTED))
    return nodes, edges
