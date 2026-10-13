"""Render plans/results as plain text and write JSON audit logs (stdlib only)."""

from __future__ import annotations

import datetime
import json
import os
from typing import List

from governance.changes import Change, EnvironmentPlan


def render_plan(plan: EnvironmentPlan, dry_run: bool = True) -> str:
    header = "PLAN (dry run)" if dry_run else "APPLIED"
    lines: List[str] = [f"== {header} :: environment '{plan.env}' =="]

    shown = [c for c in plan.changes if c.changed or c.error or c.protected]
    if not shown:
        lines.append("No changes — workspace matches config.")
        return "\n".join(lines)

    for change in shown:
        lines.append("")
        lines.append(_headline(change))
        if change.error:
            continue
        for add in change.adds:
            lines.append(f"    + {add}")
        for remove in change.removes:
            lines.append(f"    - {remove}")

    lines.append("")
    lines.append(_summary(plan))
    return "\n".join(lines)


def _headline(change: Change) -> str:
    tags = []
    if change.create:
        tags.append("CREATE")
    if change.delete:
        tags.append("DELETE")
    if change.protected:
        tags.append("protected: kept")
    suffix = f"  ({', '.join(tags)})" if tags else ""
    head = f"[{change.kind}] {change.identifier}{suffix}"
    if change.error:
        return f"{head}  ERROR: {change.error}"
    return head


def _summary(plan: EnvironmentPlan) -> str:
    n_changed = len(plan.changed)
    n_add = sum(len(c.adds) for c in plan.changes)
    n_remove = sum(len(c.removes) for c in plan.changes)
    n_create = sum(1 for c in plan.changes if c.create)
    n_delete = sum(1 for c in plan.changes if c.delete)
    n_err = len(plan.errors)
    parts = [
        f"{n_changed} object(s) with changes",
        f"+{n_add} grant/member",
        f"-{n_remove} grant/member",
    ]
    if n_create:
        parts.append(f"{n_create} group create")
    if n_delete:
        parts.append(f"{n_delete} group delete")
    if n_err:
        parts.append(f"{n_err} error(s)")
    return "Summary: " + ", ".join(parts)


def render_validation(issues: List[str], env: str) -> str:
    if not issues:
        return f"Validation passed for environment '{env}'. Config is consistent with the workspace."
    lines = [f"Validation found {len(issues)} issue(s) for environment '{env}':"]
    for issue in issues:
        lines.append(f"  - {issue}")
    return "\n".join(lines)


def write_audit_log(
    env: str,
    actions: List[dict],
    directory: str = "audit-logs",
    actor: str = None,
    git_sha: str = None,
    ci_run_url: str = None,
) -> str:
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{env}-{timestamp}.audit.json")
    payload = {
        "environment": env,
        "timestamp_utc": timestamp,
        # Provenance: who applied, from which commit, via which pipeline run. Lets an
        # auditor tie a live-workspace change back to the reviewed config commit.
        "actor": actor,
        "git_sha": git_sha,
        "ci_run_url": ci_run_url,
        "action_count": len(actions),
        "actions": actions,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return path
