#!/usr/bin/env python3
"""Create a Databricks Job that runs the ``run_governance`` notebook, via the Jobs REST API.

The job has a single notebook task pointing at the in-workspace notebook (the one deployed
from ``deploy/run_governance.py``). It exposes the notebook's ``action`` widget as a job
base parameter so the same job can ``lint`` / ``plan`` / ``apply``.

Auth: pass ``--host`` and ``--token`` directly, or fall back to the Databricks SDK's
unified ``Config`` (``--profile`` -> .databrickscfg, else env vars / default). The job
itself is created with a direct REST call to ``POST /api/2.1/jobs/create`` (no SDK Jobs
client), per requirement.

By default the task runs on **serverless** compute (no cluster specified). Pass
``--existing-cluster-id`` to pin it to an existing all-purpose cluster instead.

Access: when a CI/CD service principal creates the job, only that SP can see it by
default. Grant yourself (or a team) access up front with ``--grant-user`` /
``--grant-group`` / ``--grant-sp`` (level via ``--grant-level``, default ``CAN_MANAGE``)
so you can view and manage the job after deployment. Optionally set the run identity with
``--run-as-sp`` / ``--run-as-user``.

Usage:
    python3 deploy/create_job.py --host https://adb-123.az.databricks.net --token dapi...
    python3 deploy/create_job.py --profile govtest --action plan --run-now
    # CI/CD: SP token creates the job, but grant your user CAN_MANAGE so you can check it:
    python3 deploy/create_job.py --host $DBX_HOST --token $SP_TOKEN \
        --job-name gov-test-governance \
        --grant-user me@databricks.com \
        --run-as-sp 90aec7bc-d01e-4b4e-a9be-3271c66686b0
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def _request(method: str, url: str, headers: dict, payload: dict | None = None) -> dict:
    """Make one JSON REST call and return the parsed response (raises on HTTP error)."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:  # surface the API error body, which is informative
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"HTTP {exc.code} from {url}:\n{detail}") from exc
    return json.loads(body) if body.strip() else {}


def build_acl(users: list, groups: list, sps: list, level: str) -> list:
    """Build a jobs access_control_list from users/groups/service-principals at one level."""
    acl = []
    acl += [{"user_name": u, "permission_level": level} for u in users]
    acl += [{"group_name": g, "permission_level": level} for g in groups]
    acl += [{"service_principal_name": s, "permission_level": level} for s in sps]
    return acl


def build_job_payload(name: str, notebook_path: str, action: str,
                      existing_cluster_id: str | None,
                      access_control_list: list | None = None,
                      run_as: dict | None = None) -> dict:
    """Assemble the /api/2.1/jobs/create request body."""
    task: dict = {
        "task_key": "run_governance",
        "notebook_task": {
            "notebook_path": notebook_path,
            "base_parameters": {"action": action},
        },
    }
    # No compute block -> serverless. An existing cluster id pins to all-purpose compute.
    if existing_cluster_id:
        task["existing_cluster_id"] = existing_cluster_id
    payload: dict = {
        "name": name,
        "tasks": [task],
        "max_concurrent_runs": 1,
    }
    # Grant access up front so the job is manageable even when a service principal (e.g. a
    # CI/CD identity) creates it. Without this, only the creator can see the job.
    if access_control_list:
        payload["access_control_list"] = access_control_list
    if run_as:
        payload["run_as"] = run_as
    return payload


def grant_existing(host: str, headers: dict, job_id: str, acl: list) -> None:
    """Add access_control entries to an existing job (additive PATCH, keeps current grants)."""
    _request("PATCH", f"{host}/api/2.0/permissions/jobs/{job_id}", headers,
             {"access_control_list": acl})
    print(f"Granted {len(acl)} principal(s) on job {job_id}")
    print(f"  {host}/jobs/{job_id}")


def resolve_auth(host: str | None, token: str | None, profile: str | None):
    """Return (host, headers). Prefer explicit --host/--token, else the SDK's Config."""
    if host or token:
        if not (host and token):
            raise SystemExit("--host and --token must be given together")
        return host.rstrip("/"), {"Authorization": f"Bearer {token}"}
    from databricks.sdk.core import Config  # imported lazily so --host/--token path needs no SDK
    cfg = Config(profile=profile) if profile else Config()
    return cfg.host.rstrip("/"), dict(cfg.authenticate())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Create a Databricks job for the run_governance notebook")
    ap.add_argument("--host", default=None, help="workspace URL, e.g. https://adb-123.azuredatabricks.net")
    ap.add_argument("--token", default=None, help="personal access token (use with --host)")
    ap.add_argument("--profile", default=None,
                    help=".databrickscfg profile used when --host/--token are not given")
    ap.add_argument("--job-name", default="gov-test-governance", help="name for the created job")
    ap.add_argument("--notebook-path", default="/Workspace/Shared/gov-test/run_governance",
                    help="workspace path of the run_governance notebook")
    ap.add_argument("--action", default="plan", choices=("lint", "plan", "apply"),
                    help="value for the notebook's 'action' widget (job base parameter)")
    ap.add_argument("--existing-cluster-id", default=None,
                    help="pin the task to this all-purpose cluster (default: serverless)")
    # Grant access so you can see/manage the job even when a CI/CD service principal creates it.
    ap.add_argument("--grant-user", action="append", default=[], metavar="EMAIL",
                    help="grant a user access to the job (repeatable or comma-separated)")
    ap.add_argument("--grant-group", action="append", default=[], metavar="NAME",
                    help="grant a group access to the job (repeatable or comma-separated)")
    ap.add_argument("--grant-sp", action="append", default=[], metavar="APP_ID",
                    help="grant a service principal access to the job (repeatable or comma-separated)")
    ap.add_argument("--grant-level", default="CAN_MANAGE",
                    choices=("CAN_VIEW", "CAN_MANAGE_RUN", "CAN_MANAGE", "IS_OWNER"),
                    help="permission level for --grant-* principals (default: CAN_MANAGE)")
    ap.add_argument("--run-as-sp", default=None,
                    help="run the job as this service principal application id")
    ap.add_argument("--run-as-user", default=None, help="run the job as this user")
    ap.add_argument("--grant-existing", default=None, metavar="JOB_ID",
                    help="skip creation; add the --grant-* principals to an existing job's permissions")
    ap.add_argument("--run-now", action="store_true", help="trigger a run immediately after creating")
    args = ap.parse_args(argv)

    def _split(values):
        """Flatten repeated flags and comma-separated values into a clean list."""
        out = []
        for v in values:
            out += [p.strip() for p in v.split(",") if p.strip()]
        return out

    acl = build_acl(_split(args.grant_user), _split(args.grant_group),
                    _split(args.grant_sp), args.grant_level)
    run_as = None
    if args.run_as_sp:
        run_as = {"service_principal_name": args.run_as_sp}
    elif args.run_as_user:
        run_as = {"user_name": args.run_as_user}

    host, auth = resolve_auth(args.host, args.token, args.profile)
    headers = {**auth, "Content-Type": "application/json"}

    # Grant-only mode: patch permissions on an already-created job and stop.
    if args.grant_existing:
        if not acl:
            raise SystemExit("--grant-existing needs at least one --grant-user/-group/-sp")
        grant_existing(host, headers, args.grant_existing, acl)
        return 0

    payload = build_job_payload(args.job_name, args.notebook_path, args.action,
                                args.existing_cluster_id, access_control_list=acl, run_as=run_as)
    if acl:
        print(f"Granting {args.grant_level} to {len(acl)} principal(s)", file=sys.stderr)
    print(f"Creating job '{args.job_name}' on {host} ...", file=sys.stderr)
    created = _request("POST", f"{host}/api/2.1/jobs/create", headers, payload)
    job_id = created.get("job_id")
    print(f"Created job_id={job_id}")
    print(f"  {host}/jobs/{job_id}")

    if args.run_now:
        print(f"Triggering run (action={args.action}) ...", file=sys.stderr)
        run = _request("POST", f"{host}/api/2.1/jobs/run-now", headers, {"job_id": job_id})
        run_id = run.get("run_id")
        print(f"Started run_id={run_id}")
        print(f"  {host}/jobs/{job_id}/runs/{run_id}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
