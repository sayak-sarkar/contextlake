"""Kubernetes and OpenShift, as a ``CronJob``.

One adapter covers both. OpenShift IS Kubernetes with a stricter default
security context, so the manifest is written to satisfy the stricter one and
runs on either. Three choices follow from that and each is load-bearing:

- ``concurrencyPolicy: Forbid`` gives single-writer semantics from the cluster
  rather than from the advisory lock. Two overlapping runs are the corruption
  the lock exists to prevent, and Forbid means the second one never starts.
- No ``runAsUser``. OpenShift's restricted SCC assigns an arbitrary UID, and a
  pinned numeric user is rejected there. A manifest that pins one works on
  vanilla Kubernetes and fails on OpenShift, which is the exact split the spec
  calls out.
- ``fsGroup`` so the mounted state directory is group-writable by whatever UID
  the cluster assigned.

The schedule is a cron expression, so it inherits cron's inexpressibility and
rounds the same way, reporting the difference.

**Nothing is patched in the cluster on its own.** Adjusting an interval means
re-running `schedule install`, which is a deliberate act by someone who already
has the rights. A background process that rewrote a CronJob would need
cluster-write RBAC for the whole life of the schedule, which is far more
authority than the benefit is worth.

Not runnable on this machine. Verified by rendering the manifest and asserting
the exact ``kubectl``/``oc`` argv, never by applying it.
"""
from __future__ import annotations

import shutil
import subprocess

from .base import Adapter, check_name
from .cron import nearest_expressible

#: Cheap to change, and named once so the manifest and the argv agree.
DEFAULT_NAMESPACE = "default"

#: The image the CronJob runs. Overridable per install; the default names the
#: published image rather than pretending a cluster has a local build.
DEFAULT_IMAGE = "ghcr.io/sayak-sarkar/contextlake:latest"


def resource_name(job_name) -> str:
    return f"contextlake-{check_name(job_name)}"


def _client() -> str | None:
    """``kubectl`` if present, else ``oc``, else ``None``.

    Both speak the same API for this resource, so whichever the operator has
    is the one used. ``None`` means neither, which is a degrade rather than a
    failure: the manifest still renders and can be applied by hand.
    """
    for candidate in ("kubectl", "oc"):
        if shutil.which(candidate):
            return candidate
    return None


def _run(*argv):
    client = _client()
    if client is None:
        raise OSError("neither kubectl nor oc is on PATH")
    return subprocess.run([client, *argv], capture_output=True, text=True,
                          errors="replace", check=False)


def _yaml_list(items) -> str:
    return "".join(f"\n                  - {i}" for i in items)


class K8sAdapter(Adapter):
    id = "k8s"
    # A CronJob fires on the cluster's clock. There is no machine to be asleep,
    # so "catching up" is not a property this backend has either way. Reported
    # as False so nothing claims systemd's replay behaviour.
    catches_up_after_sleep = False
    metadata_keys = frozenset({"schedule", "interval_s", "namespace", "notes", "name"})

    def usable(self) -> bool:
        return _client() is not None

    def render(self, job, interval_s, exec_argv, namespace=DEFAULT_NAMESPACE,
               image=DEFAULT_IMAGE, **_options) -> dict:
        name = check_name(job.name)
        actual_s, spec = nearest_expressible(interval_s)
        notes = []
        if abs(actual_s - float(interval_s)) > 1:
            from ..recommend import format_duration

            notes.append(
                f"a CronJob schedule is a cron expression, so this job runs every "
                f"{format_duration(actual_s)} instead of "
                f"{format_duration(interval_s)}.")
        # The container runs the contextlake command directly. exec_argv's
        # first element is the caller's interpreter path, which is meaningless
        # inside the image, so only the arguments after it are carried over.
        args = [str(a) for a in exec_argv]
        if args and args[0] not in ("contextlake",):
            args = [a for a in args if a != "-m"]
            args = args[1:] if args and args[0].endswith(("python", "python3")) else args
            if args and args[0] == "contextlake":
                args = args[1:]
        manifest = f"""apiVersion: batch/v1
kind: CronJob
metadata:
  name: {resource_name(name)}
  namespace: {namespace}
  labels:
    app.kubernetes.io/name: contextlake
    app.kubernetes.io/instance: {name}
spec:
  schedule: "{spec}"
  # Forbid, not Allow: two overlapping runs are the corruption the advisory
  # lock exists to prevent, and this stops the second one starting at all.
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  startingDeadlineSeconds: 300
  jobTemplate:
    spec:
      backoffLimit: 0
      template:
        spec:
          restartPolicy: Never
          securityContext:
            # No runAsUser. OpenShift's restricted SCC assigns an arbitrary
            # UID and rejects a pinned one, so pinning here would work on
            # vanilla Kubernetes and fail on OpenShift.
            runAsNonRoot: true
            fsGroup: 1001
            seccompProfile:
              type: RuntimeDefault
          containers:
            - name: contextlake
              image: {image}
              args:{_yaml_list(args)}
              securityContext:
                allowPrivilegeEscalation: false
                capabilities:
                  drop:
                  - ALL
              volumeMounts:
                - name: state
                  mountPath: /var/lib/contextlake
          volumes:
            # A PersistentVolumeClaim, not an emptyDir. An ephemeral store
            # re-indexes the whole fleet every run instead of doing an
            # incremental pass, which `schedule run` refuses without
            # --allow-ephemeral.
            - name: state
              persistentVolumeClaim:
                claimName: {resource_name(name)}-state
"""
        return {
            "cronjob.yaml": manifest,
            "schedule": spec,
            "interval_s": actual_s,
            "namespace": namespace,
            "notes": notes,
            "name": name,
        }

    def install(self, job, interval_s, exec_argv, **options) -> list:
        rendered = self.render(job, interval_s, exec_argv, **options)
        result = _run("apply", "-n", rendered["namespace"], "-f", "-")
        if result.returncode != 0:
            raise OSError(
                f"kubectl apply for {resource_name(rendered['name'])} failed: "
                f"{result.stderr.strip() or result.stdout.strip() or 'no output'}")
        return [f"cronjob/{resource_name(rendered['name'])}"]

    def uninstall(self, job) -> list:
        name = resource_name(job.name)
        result = _run("delete", "cronjob", name, "--ignore-not-found")
        return [f"cronjob/{name}"] if result.returncode == 0 else []

    def installed_names(self):
        """CronJobs carrying this tool's label.

        Selected by label rather than by name prefix: a label is what the
        manifest actually sets, and matching on a prefix would also claim a
        CronJob somebody else happened to name that way.

        ``None`` when no client is on PATH or the query fails, which is
        "cannot tell". A cluster that answers with an empty list IS a
        measurement.
        """
        try:
            selector = "app.kubernetes.io/name=contextlake"
            path = "jsonpath={.items[*].metadata.labels.app\\.kubernetes\\.io/instance}"
            result = _run("get", "cronjobs", "-l", selector, "-o", path)
        except OSError:
            return None
        if result.returncode != 0:
            return None
        return sorted(n for n in result.stdout.split() if n)

    def state(self, job) -> dict:
        name = resource_name(job.name)
        notes, interval_s, next_run, exec_path = [], None, None, None
        try:
            result = _run("get", "cronjob", name,
                          "-o", "jsonpath={.spec.schedule}|{.status.lastScheduleTime}")
        except OSError as e:
            return {"installed": False, "interval_s": None, "next_run": None,
                    "exec_path": None, "notes": [str(e)]}
        installed = result.returncode == 0
        if installed:
            schedule, _, last = result.stdout.partition("|")
            schedule = schedule.strip()
            if schedule:
                notes.append(f"cluster schedule: {schedule}")
            # A CronJob reports its LAST schedule time, not its next one.
            # Reporting the last as if it were the next would be wrong, so
            # next_run stays None and the fact goes in a note.
            if last.strip():
                notes.append(f"last scheduled: {last.strip()}")
            notes.append("interval changes are applied by re-running "
                         "`contextlake schedule install`; nothing is patched "
                         "in the cluster on its own.")
        return {
            "installed": installed,
            "interval_s": interval_s,
            "next_run": next_run,
            "exec_path": exec_path,
            "notes": notes,
        }
