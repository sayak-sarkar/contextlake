"""AWS and Azure, as managed scheduled jobs.

Two adapters in one module because they answer the same question the same way:
a managed scheduler fires a container on an interval, and neither exposes the
machine the container runs on.

**On EKS and AKS, use ``--platform k8s`` instead.** Both are Kubernetes, so the
CronJob adapter serves them and gives ``concurrencyPolicy: Forbid`` with it.
Reaching for ``aws`` on EKS is the obvious wrong move and this is the only
place that says so.

**Neither has an equivalent of Forbid.** EventBridge Scheduler and Container
Apps Jobs will start a second execution while the first is still running, so
single-writer protection here rests entirely on the store's advisory lock: the
second run takes the lock, fails to get it, and skips with a logged reason.
That is the same protection a bare `schedule run` has, and it is weaker than
what the cluster gives, so it is stated rather than implied.

No SDK. Both shell out to an already-authenticated ``aws`` or ``az``, which is
what keeps contextlake free of a cloud dependency.

Not runnable on this machine. Verified by rendering the request documents and
asserting the exact argv, never by calling either API.
"""
from __future__ import annotations

import json
import shutil
import subprocess

from .base import Adapter, check_name

#: EventBridge Scheduler and Container Apps Jobs both take whole minutes.
MIN_MINUTES = 1


def schedule_name(job_name) -> str:
    return f"contextlake-{check_name(job_name)}"


def nearest_expressible(seconds):
    """``(seconds, minutes)`` for the nearest interval either service accepts.

    Rounds DOWN above a minute and UP below it, the same rule cron and Task
    Scheduler follow. Three backends rounding three ways would give one
    request three intervals.
    """
    minutes = int(float(seconds) // 60)
    if minutes < MIN_MINUTES:
        minutes = MIN_MINUTES
    return float(minutes * 60), minutes


def _cli(binary, *argv, input=None):  # noqa: A002 - matches subprocess.run
    if shutil.which(binary) is None:
        raise OSError(f"{binary} is not on PATH")
    return subprocess.run([binary, *argv], capture_output=True, text=True,
                          errors="replace", check=False, input=input)


def _rounding_note(actual_s, wanted_s, service):
    if abs(actual_s - float(wanted_s)) <= 1:
        return []
    from ..recommend import format_duration

    return [f"{service} counts whole minutes, so this job runs every "
            f"{format_duration(actual_s)} instead of "
            f"{format_duration(wanted_s)}."]


#: Said by both adapters, in the same words, for the same reason.
NO_FORBID_NOTE = ("this service has no equivalent of a CronJob's "
                  "concurrencyPolicy: Forbid, so two runs can overlap and the "
                  "second one skips on the store's advisory lock rather than "
                  "never starting.")


class AwsAdapter(Adapter):
    """EventBridge Scheduler firing an ECS task."""

    id = "aws"
    catches_up_after_sleep = False
    metadata_keys = frozenset({"rate", "interval_s", "minutes", "notes", "name"})

    def usable(self) -> bool:
        return shutil.which("aws") is not None

    def render(self, job, interval_s, exec_argv, cluster="contextlake",
               task_definition="contextlake", role_arn="", subnets=(),
               **_options) -> dict:
        name = check_name(job.name)
        actual_s, minutes = nearest_expressible(interval_s)
        from .k8s import _container_args

        args = _container_args(exec_argv)
        # rate() takes an INTEGER and a UNIT WORD. "rate(4200 seconds)" is not
        # a valid expression: seconds is not a rate() unit, and the schedule is
        # rejected when it is created rather than when it would fire.
        rate = f"rate({minutes} minute{'s' if minutes != 1 else ''})"
        document = {
            "Name": schedule_name(name),
            "ScheduleExpression": rate,
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "Target": {
                "Arn": f"arn:aws:ecs:::cluster/{cluster}",
                "RoleArn": role_arn,
                "EcsParameters": {
                    "TaskDefinitionArn": task_definition,
                    "LaunchType": "FARGATE",
                    "NetworkConfiguration": {
                        "awsvpcConfiguration": {"Subnets": list(subnets)},
                    },
                },
                "Input": json.dumps({
                    "containerOverrides": [
                        {"name": "contextlake", "command": args},
                    ],
                }),
            },
        }
        notes = _rounding_note(actual_s, interval_s, "EventBridge Scheduler")
        notes.append(NO_FORBID_NOTE)
        notes.append("the ECS task must mount persistent storage (EFS). An "
                     "ephemeral store re-indexes the whole fleet every run, "
                     "which `schedule run` refuses without --allow-ephemeral.")
        return {
            "schedule.json": json.dumps(document, indent=2, sort_keys=True) + "\n",
            "rate": rate,
            "interval_s": actual_s,
            "minutes": minutes,
            "notes": notes,
            "name": name,
        }

    def install(self, job, interval_s, exec_argv, **options) -> list:
        rendered = self.render(job, interval_s, exec_argv, **options)
        # --cli-input-json file:///dev/stdin, and the document goes ON stdin.
        # Naming the file without sending the bytes creates a schedule from an
        # empty document, which is the defect the k8s adapter shipped and had
        # corrected before release.
        result = _cli("aws", "scheduler", "create-schedule",
                      "--cli-input-json", "file:///dev/stdin",
                      input=rendered["schedule.json"])
        if result.returncode != 0:
            raise OSError(
                f"aws scheduler create-schedule for {rendered['name']} failed: "
                f"{result.stderr.strip() or result.stdout.strip() or 'no output'}")
        return [f"schedule/{schedule_name(rendered['name'])}"]

    def uninstall(self, job) -> list:
        name = schedule_name(job.name)
        result = _cli("aws", "scheduler", "delete-schedule", "--name", name)
        # Non-zero means it was not there. Not an error, and not a removal:
        # the same rule every other adapter follows.
        return [f"schedule/{name}"] if result.returncode == 0 else []

    def installed_names(self):
        try:
            result = _cli("aws", "scheduler", "list-schedules",
                          "--name-prefix", "contextlake-",
                          "--query", "Schedules[].Name", "--output", "text")
        except OSError:
            return None
        if result.returncode != 0:
            return None
        prefix = "contextlake-"
        return sorted(n[len(prefix):] for n in result.stdout.split()
                      if n.startswith(prefix))

    def state(self, job) -> dict:
        name = schedule_name(job.name)
        try:
            result = _cli("aws", "scheduler", "get-schedule", "--name", name,
                          "--query", "ScheduleExpression", "--output", "text")
        except OSError as e:
            return {"installed": False, "interval_s": None, "next_run": None,
                    "exec_path": None, "notes": [str(e)]}
        installed = result.returncode == 0
        notes = []
        if installed:
            expression = result.stdout.strip()
            if expression:
                notes.append(f"cloud schedule: {expression}")
            notes.append(NO_FORBID_NOTE)
        return {
            "installed": installed,
            # EventBridge reports the expression, not a next-fire time, and not
            # a duration this can read back without parsing rate() by hand.
            "interval_s": None,
            "next_run": None,
            "exec_path": None,
            "notes": notes,
        }


class AzureAdapter(Adapter):
    """A Container Apps Job with a schedule trigger."""

    id = "azure"
    catches_up_after_sleep = False
    metadata_keys = frozenset({"cron", "interval_s", "minutes", "notes", "name"})

    def usable(self) -> bool:
        return shutil.which("az") is not None

    def render(self, job, interval_s, exec_argv, resource_group="contextlake",
               environment="contextlake", image="", **_options) -> dict:
        from .cron import nearest_expressible as cron_nearest
        from .k8s import DEFAULT_IMAGE, _container_args

        name = check_name(job.name)
        # A Container Apps Job schedule trigger IS a cron expression, so it
        # rounds through the cron function rather than the whole-minute one.
        actual_s, spec = cron_nearest(interval_s)
        args = _container_args(exec_argv)
        notes = _rounding_note(actual_s, interval_s, "a cron trigger")
        notes.append(NO_FORBID_NOTE)
        notes.append("the job must mount Azure Files for its store. An "
                     "ephemeral store re-indexes the whole fleet every run, "
                     "which `schedule run` refuses without --allow-ephemeral.")
        return {
            "az-command": subprocess.list2cmdline(
                self._create_argv(name, spec, resource_group, environment,
                                  image or DEFAULT_IMAGE, args)),
            "cron": spec,
            "interval_s": actual_s,
            "minutes": int(actual_s // 60),
            "notes": notes,
            "name": name,
        }

    def _create_argv(self, name, spec, resource_group, environment, image, args) -> list:
        return ["az", "containerapp", "job", "create",
                "--name", schedule_name(name),
                "--resource-group", resource_group,
                "--environment", environment,
                "--trigger-type", "Schedule",
                "--cron-expression", spec,
                "--replica-timeout", "3600",
                "--image", image,
                "--args", " ".join(args)]

    def install(self, job, interval_s, exec_argv, **options) -> list:
        rendered = self.render(job, interval_s, exec_argv, **options)
        from .k8s import DEFAULT_IMAGE, _container_args

        argv = self._create_argv(
            rendered["name"], rendered["cron"],
            options.get("resource_group", "contextlake"),
            options.get("environment", "contextlake"),
            options.get("image") or DEFAULT_IMAGE,
            _container_args(exec_argv))
        result = _cli("az", *argv[1:])
        if result.returncode != 0:
            raise OSError(
                f"az containerapp job create for {rendered['name']} failed: "
                f"{result.stderr.strip() or result.stdout.strip() or 'no output'}")
        return [f"containerapp-job/{schedule_name(rendered['name'])}"]

    def uninstall(self, job) -> list:
        name = schedule_name(job.name)
        result = _cli("az", "containerapp", "job", "delete", "--name", name, "--yes")
        return [f"containerapp-job/{name}"] if result.returncode == 0 else []

    def installed_names(self):
        try:
            result = _cli("az", "containerapp", "job", "list",
                          "--query", "[].name", "--output", "tsv")
        except OSError:
            return None
        if result.returncode != 0:
            return None
        prefix = "contextlake-"
        return sorted(n[len(prefix):] for n in result.stdout.split()
                      if n.startswith(prefix))

    def state(self, job) -> dict:
        name = schedule_name(job.name)
        try:
            result = _cli("az", "containerapp", "job", "show", "--name", name,
                          "--query", "properties.configuration.scheduleTriggerConfig"
                                     ".cronExpression", "--output", "tsv")
        except OSError as e:
            return {"installed": False, "interval_s": None, "next_run": None,
                    "exec_path": None, "notes": [str(e)]}
        installed = result.returncode == 0
        notes = []
        if installed:
            expression = result.stdout.strip()
            if expression:
                notes.append(f"cloud schedule: {expression}")
            notes.append(NO_FORBID_NOTE)
        return {
            "installed": installed,
            "interval_s": None,
            "next_run": None,
            "exec_path": None,
            "notes": notes,
        }
