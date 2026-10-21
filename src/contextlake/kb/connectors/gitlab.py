"""GitLab connector: link each repo to its open merge requests and issues.

Uses the authenticated ``glab`` CLI (the same auth the mirror uses) to read a
project's open MRs/issues, then maps them onto graph nodes/edges. The command
runner is injectable so the mapping is testable without GitLab. Generic: the
optional ``group`` comes from config and only filters which repos are read.

The host comes from the repo id, but only a host listed in ``gitlab_hosts`` is
used. Unset, the list is ``GITLAB_HOST`` when that variable is set, else
``gitlab.com``. A repo on any other host is skipped, and no request is made for
it. Every call passes the listed host to ``glab api --hostname``. ``gitlab_hosts``
decides which host gets the authenticated call, so ``kb/trust.py`` refuses it from
a config file found by walking up from the current directory.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.parse

from ...logging_setup import log
from .._util import as_list
from ..ids import make_id
from ..model import EXTERNAL_REPO, Confidence, Node
from ..resilience import breaker_for, note_unavailable
from ..store.base import Store
from .common import link_edge, repo_node

DEFAULT_HOST = "gitlab.com"
# Hosts known to run another product. A skip message gives no `gitlab_hosts` advice for
# them: listing one would aim a `glab api --hostname` call, with GitLab credentials, at a
# server that is not GitLab.
NOT_GITLAB_HOSTS = frozenset({
    "github.com", "ssh.github.com", "bitbucket.org", "dev.azure.com", "ssh.dev.azure.com",
})
# A host name or IPv4 address, or a bracketed IPv6 address, then an optional port. No
# wildcard: an entry matches one host and nothing else.
_HOST = re.compile(
    r"^(?:[a-z0-9_][a-z0-9_.-]*|\[[0-9a-f:.]+\])(?::(?P<port>[0-9]{1,5}))?$")


def normalize_host(value) -> str | None:
    """``host`` or ``host:port`` in lower case, or ``None`` when ``value`` is not one.

    Takes a bare host, ``host:port``, or a URL such as ``https://git.corp:8443/``.
    ``GITLAB_HOST`` may hold either of the last two forms. Scheme, user info and path
    are dropped. The port is kept, because ``git.corp`` and
    ``git.corp:8443`` can be different servers. A port must be 1 to 65535.
    """
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].rsplit("@", 1)[-1]
    match = _HOST.match(text)
    if match is None:
        return None
    port = match.group("port")
    if port is not None and not 1 <= int(port) <= 65535:
        return None
    return text


def skip_advice(hosts) -> str:
    """The sentence that says how to read repos on skipped hosts, or ``""`` for none.

    The advice is conditional, because a repo id names the clone's host and says nothing
    about what runs there. A host that is not a valid ``gitlab_hosts`` entry gets none,
    as does one in :data:`NOT_GITLAB_HOSTS`, with or without a port or a trailing dot.
    """
    names = sorted(h for h in hosts if normalize_host(h) is not None
                   and re.sub(r":[0-9]+$", "", h).rstrip(".") not in NOT_GITLAB_HOSTS)
    if not names:
        return ""
    if len(names) == 1:
        return (f"If {names[0]} runs GitLab, add it to gitlab_hosts on that source to read "
                "its repos.")
    return (f"For each of {', '.join(names)} that runs GitLab, add it to gitlab_hosts on "
            "that source to read its repos.")


class GitLabConnector:
    def __init__(self, name: str, *, group: str | None = None, hosts=None,
                 timeout: float = 30, per_page: int = 50, runner=None):
        self.name = name
        self.group = group
        self.timeout = timeout
        self.per_page = per_page
        self._run = runner or self._glab
        # Unset means GITLAB_HOST, else gitlab.com. A set but empty list allows no host.
        if hosts is None:
            env = os.environ.get("GITLAB_HOST")
            self.hosts = self._listed([env or DEFAULT_HOST], "GITLAB_HOST" if env else None)
        else:
            self.hosts = self._listed(as_list(hosts), "gitlab_hosts")
        # Host label -> ids of the repos skipped this run, and the ids of the repos whose
        # host was listed, so the caller can tell a repo another source reads from one
        # nobody reads. See `begin_run`.
        self.skipped_hosts: dict[str, set[str]] = {}
        self.read_repos: set[str] = set()
        # Hosts that another GitLab source lists. The caller sets it when it builds more
        # than one source. A repo on such a host is read by that source, so the skip
        # message gives no `gitlab_hosts` advice for it.
        self.other_hosts: frozenset[str] = frozenset()

    def _listed(self, values: list, label: str | None) -> tuple[str, ...]:
        hosts: list[str] = []
        for raw in values:
            host = normalize_host(raw)
            if host is None:
                # The GITLAB_HOST text stays out of the line, it is the user's environment.
                shown = "the GITLAB_HOST value" if label == "GITLAB_HOST" else f"{raw!r}"
                log(f"gitlab source {self.name!r}: ignoring {shown}, it is not a host name. "
                    "Write it as git.example.com or git.example.com:8443.")
            elif host not in hosts:
                hosts.append(host)
        return tuple(hosts)

    def begin_run(self) -> None:
        """Forget the repos seen so far, so the next run logs and counts them again.

        The connector is built once and a ``--watch`` loop reuses it for every pass.
        """
        self.skipped_hosts.clear()
        self.read_repos.clear()

    def _glab(self, host: str, endpoint: str) -> list:
        # Two GitLab API calls per repo, each able to burn the full timeout: with
        # `glab` unauthenticated or GitLab down, a fleet run pays that on every
        # repo. The breaker (keyed on the CLI and the host it is asked to use) writes
        # the source off after a few failures instead, and
        # `note_unavailable` keeps the reason on screen -- this method's contract
        # is to return [] rather than raise, which is exactly the shape that
        # otherwise makes an outage read as "this project has no open MRs".
        try:
            # check=True is load-bearing, not tidiness. Without it a rejected call
            # (404, 401, rate limit) returns a non-zero code that subprocess does
            # not raise on, so the breaker never counted it, never opened, and the
            # empty list it produced was indistinguishable from "no open MRs". A
            # source whose every call was being refused therefore reported success.
            res = breaker_for(f"glab-api:{host}").call(
                subprocess.run, ["glab", "api", "--hostname", host, endpoint],
                capture_output=True, text=True, errors="replace", timeout=self.timeout,
                check=True)
        except Exception as e:  # noqa: BLE001 - OSError/SubprocessError/CircuitOpenError
            note_unavailable(f"gitlab (glab api, {host})", e)
            return []
        if not res.stdout.strip():
            return []
        try:
            data = json.loads(res.stdout)
        except (json.JSONDecodeError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _project_path(self, repo_id: str) -> str | None:
        """URL-encoded GitLab project path for a repo id, or ``None`` if it has none.

        A repo id is canonical ``host/namespace/project`` (see
        ``repo_identity.normalize_id``), while the API wants ``namespace/project``
        alone. This previously prepended the configured group to the *whole* id and
        encoded that, producing ``group%2Fgitlab.com%2Fns%2Fproj``, so every call
        404'd. The host segment is dropped instead, and ``group`` now acts as a
        filter rather than a prefix: the namespace it would have added is already
        in the id. :meth:`_host_for` reads the host segment this drops.

        A repo with no ``origin`` remote gets the ``name@root-commit`` fallback id,
        which names no GitLab project at all. That returns ``None`` so the caller
        skips it rather than issuing a request that cannot succeed.
        """
        if "/" not in repo_id:
            return None
        _host, path = repo_id.split("/", 1)
        if self.group and path != self.group and not path.startswith(f"{self.group}/"):
            return None
        return urllib.parse.quote(path, safe="")

    def _host_for(self, repo_id: str) -> str | None:
        """The ``gitlab_hosts`` entry a repo id names, or ``None`` when it names none.

        The segment before the first ``/`` is compared whole to each entry, port
        included, after both are lower-cased. There is no suffix or substring match,
        so ``evil-git.corp`` and ``git.corp.evil.test`` do not match ``git.corp``. The
        entry is returned, not the id's text, so the host handed to ``glab`` is one the
        user listed.

        A repo that names a listed host is recorded in :attr:`read_repos`. A repo that
        names another host is recorded in :attr:`skipped_hosts`, and the first one for
        each host in a run gets a log line.
        """
        segment = repo_id.split("/", 1)[0]
        host = normalize_host(segment)
        if host is not None and host in self.hosts:
            self.read_repos.add(repo_id)
            return host
        label = host or segment
        if label not in self.skipped_hosts:
            log(f"gitlab source {self.name!r}: skipping repos on {label}, which is not in "
                f"gitlab_hosts ({', '.join(self.hosts) or 'empty'}). "
                f"{skip_advice(h for h in [label] if h not in self.other_hosts)}".rstrip())
        self.skipped_hosts.setdefault(label, set()).add(repo_id)
        return None

    def fetch(self, repo_id: str) -> tuple[list, list]:
        """Open merge requests and issues for a repo (live)."""
        enc = self._project_path(repo_id)
        if enc is None:
            return [], []
        host = self._host_for(repo_id)
        if host is None:
            return [], []
        paging = f"state=opened&per_page={self.per_page}"
        mrs = self._run(host, f"projects/{enc}/merge_requests?{paging}")
        issues = self._run(host, f"projects/{enc}/issues?{paging}")
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
        if enc is None:
            return []
        host = self._host_for(repo_id)
        if host is None:
            return []
        try:
            diffs = self._run(host, f"projects/{enc}/merge_requests/{mr_iid}/diffs")
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
