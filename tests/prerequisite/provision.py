"""Module 2 (TEST-ONLY): create inventory-declared resources.

Dry-run by default (prints what it WOULD do). Pass --create to actually create the
missing creatable resources. Resources that need a full spec (compute config, git url,
query text, ...) are reported as MANUAL and never created here.

Usage:
    # dry run (safe): show what would be created
    python3 tests/prerequisite/provision.py --inventory prerequisite/govtest-env/inventory.json

    # actually create (explicit flag required)
    python3 tests/prerequisite/provision.py --inventory <file> --create
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import List, Optional, Tuple

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "src"))
sys.path.insert(0, str(_HERE.parent))

import inventory as inv_mod  # noqa: E402
from governance.errors import GovernanceError  # noqa: E402
from governance.objecttypes import get_spec  # noqa: E402

# check.py provides the existence context and loader wiring.
from check import build_context, Context  # noqa: E402

CREATED = "CREATED"
EXISTS = "EXISTS"
MANUAL = "MANUAL"
FAILED = "FAILED"
PLAN = "WOULD-CREATE"


# --------------------------------------------------------------------------- #
# create operations (only for creatable types)
# --------------------------------------------------------------------------- #
def _create_user(client, entry: dict) -> None:
    client.users.create(user_name=entry["user_name"], display_name=entry.get("display_name"), active=True)


def _create_service_principal(client, entry: dict) -> None:
    client.service_principals.create(
        display_name=entry.get("display_name"),
        application_id=entry.get("application_id"),
    )


def _create_group(client, entry: dict) -> None:
    # Membership is reconciled by the governance framework, not here.
    client.groups.create(display_name=entry["name"])


def _create_directory(client, entry: dict) -> None:
    client.workspace.mkdirs(entry["path"])


def _create_secret_scope(client, entry: dict) -> None:
    client.secrets.create_scope(scope=entry["name"])


def _create_experiment(client, entry: dict) -> None:
    client.experiments.create_experiment(name=entry["name"])


# --- spec-based creators (entry must carry a "spec" block) ---
def _create_cluster(client, entry: dict) -> None:
    spec = entry.get("spec", {})
    kwargs = dict(
        cluster_name=entry["name"],
        spark_version=spec["spark_version"],
        node_type_id=spec["node_type_id"],
        num_workers=spec.get("num_workers", 0),
        autotermination_minutes=spec.get("autotermination_minutes", 10),
    )
    if spec.get("single_node"):
        kwargs["num_workers"] = 0
        kwargs["spark_conf"] = {"spark.databricks.cluster.profile": "singleNode", "spark.master": "local[*]"}
        kwargs["custom_tags"] = {"ResourceClass": "SingleNode"}
    client.clusters.create(**kwargs)


def _create_warehouse(client, entry: dict) -> None:
    spec = entry.get("spec", {})
    client.warehouses.create(
        name=entry["name"],
        cluster_size=spec.get("cluster_size", "2X-Small"),
        min_num_clusters=spec.get("min_num_clusters", 1),
        max_num_clusters=spec.get("max_num_clusters", 1),
        auto_stop_mins=spec.get("auto_stop_mins", 10),
        enable_serverless_compute=spec.get("enable_serverless_compute"),
    )


def _create_cluster_policy(client, entry: dict) -> None:
    spec = entry.get("spec", {})
    client.cluster_policies.create(name=entry["name"], definition=json.dumps(spec.get("definition", {})))


def _create_instance_pool(client, entry: dict) -> None:
    spec = entry.get("spec", {})
    client.instance_pools.create(
        instance_pool_name=entry["name"],
        node_type_id=spec["node_type_id"],
        min_idle_instances=spec.get("min_idle_instances", 0),
        max_capacity=spec.get("max_capacity"),
        idle_instance_autotermination_minutes=spec.get("idle_instance_autotermination_minutes", 10),
    )


_IDENTITY_CREATORS = {
    "users": _create_user,
    "service_principals": _create_service_principal,
    "groups": _create_group,
}
_RESOURCE_CREATORS = {
    "directories": _create_directory,
    "secret-scopes": _create_secret_scope,
    "experiments": _create_experiment,
    "clusters": _create_cluster,
    "warehouses": _create_warehouse,
    "cluster-policies": _create_cluster_policy,
    "instance-pools": _create_instance_pool,
}


def plan_and_apply(context: Context, inv: dict, do_create: bool) -> List[Tuple[str, str, str, str]]:
    """Return records: (kind, identifier, status, detail)."""
    out: List[Tuple[str, str, str, str]] = []
    client = context.client

    # identities
    for kind, entries in inv_mod.identities(inv).items():
        for entry in entries:
            ident = inv_mod.identity_id(kind, entry)
            if context.identity_exists(kind, ident):
                out.append((kind, ident, EXISTS, ""))
                continue
            out.append(_do(kind, ident, _IDENTITY_CREATORS.get(kind), client, entry, do_create))

    # resources
    for key, entries in inv_mod.resources(inv).items():
        try:
            spec = get_spec(key)
        except KeyError:
            for entry in entries:
                out.append((key, str(entry), FAILED, "unknown object type"))
            continue
        for entry in entries:
            ident = inv_mod.resource_id(spec.id_field, entry)
            creatable = key in inv_mod.CREATABLE_RESOURCES or key in inv_mod.CREATABLE_WITH_SPEC
            if not creatable:
                out.append((key, ident, MANUAL, "needs a full spec — provision manually"))
                continue
            if context.resource_exists(key, ident):
                out.append((key, ident, EXISTS, ""))
                continue
            if key in inv_mod.CREATABLE_WITH_SPEC and not entry.get("spec"):
                out.append((key, ident, MANUAL, "missing 'spec' block — add one to create it"))
                continue
            out.append(_do(key, ident, _RESOURCE_CREATORS.get(key), client, entry, do_create))

    return out


def _do(kind, ident, creator, client, entry, do_create) -> Tuple[str, str, str, str]:
    if creator is None:
        return (kind, ident, MANUAL, "no creator for this type")
    if not do_create:
        return (kind, ident, PLAN, "")
    try:
        creator(client, entry)
        return (kind, ident, CREATED, "")
    except Exception as exc:  # SDK/permission error — report, keep going
        return (kind, ident, FAILED, str(exc)[:120])


def render(records, do_create: bool) -> str:
    lines = []
    for kind, ident, status, detail in records:
        extra = f"  ({detail})" if detail else ""
        lines.append(f"  [{status:12s}] {kind}: {ident}{extra}")
    lines.append("")
    created = sum(1 for r in records if r[2] == CREATED)
    would = sum(1 for r in records if r[2] == PLAN)
    manual = sum(1 for r in records if r[2] == MANUAL)
    failed = sum(1 for r in records if r[2] == FAILED)
    exists = sum(1 for r in records if r[2] == EXISTS)
    if do_create:
        lines.append(f"Summary: {created} created, {exists} already existed, {manual} manual, {failed} failed.")
    else:
        lines.append(f"Summary (dry run): {would} would be created, {exists} already exist, "
                     f"{manual} manual. Re-run with --create to apply.")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Provision inventory-declared resources (test-only).")
    parser.add_argument("--inventory", required=True, help="path to inventory.json")
    parser.add_argument("--profile", default=None, help="override the profile in the inventory")
    parser.add_argument("--create", action="store_true",
                        help="EXPLICIT flag to actually create resources (otherwise dry-run)")
    args = parser.parse_args(argv)

    try:
        inv = inv_mod.load_inventory(args.inventory)
        context = build_context(inv, args.profile)
        records = plan_and_apply(context, inv, do_create=args.create)
    except (GovernanceError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    mode = "CREATE" if args.create else "DRY RUN"
    print(f"== prerequisite provision [{mode}] :: environment '{inv.get('environment', '?')}' ==")
    print(render(records, args.create))
    return 1 if any(r[2] == FAILED for r in records) else 0


if __name__ == "__main__":
    sys.exit(main())
