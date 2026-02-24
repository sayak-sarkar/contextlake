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
from ..model import Confidence, Node
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
        try:
            res = subprocess.run(["glab", "api", endpoint], capture_output=True,
                                 text=True, timeout=self.timeout)
        except (OSError, subprocess.SubprocessError):
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


# --- pure graph mapping (no network) ---------------------------------------

def _item_node(repo_id: str, kind: str, sigil: str, item: dict) -> Node:
    iid = item.get("iid") or item.get("id")
    attrs = {k: v for k, v in {
        "title": item.get("title"), "state": item.get("state"),
        "url": item.get("web_url"),
    }.items() if v}
    return Node(id=make_id("gitlab", kind, repo_id, str(iid)), repo="(external)",
                kind=kind, name=f"{repo_id}{sigil}{iid}", attrs=attrs)


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
