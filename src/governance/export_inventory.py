#!/usr/bin/env python3
"""Export a live workspace into a governance `_inventory.json` manifest.

Queries a Databricks workspace for the identities and governed object types the
governance framework understands, and writes a manifest in the exact shape
``governance.inventory.Inventory.from_dict`` parses. The result lets
``governance lint --inventory <file>`` do an **offline** existence check of config
against real workspace state (no credentials needed at lint time).

Resource keys match ``governance.objecttypes`` registry keys (e.g. ``cluster-policies``,
``secret-scopes``); ``directories`` are keyed by ``path``, everything else by ``name``.

The manifest can be written as JSON or YAML. The format is taken from ``--format`` when
given, otherwise inferred from the ``--out`` extension (``.yaml``/``.yml`` -> YAML, else
JSON). YAML output requires PyYAML.

Usage:
    python3 scripts/export_inventory.py --profile govtest --env govtest \
        --out config-govtest/environments/govtest/_inventory.json
    python3 scripts/export_inventory.py --profile govtest --env govtest \
        --out config-govtest-yaml/environments/govtest/_inventory.yaml   # YAML (by extension)

Auth uses the SDK's unified auth (``--profile`` -> .databrickscfg, else env vars).
Each query is best-effort: a type that errors (e.g. SQL not enabled) is skipped with
a warning rather than failing the whole export.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Callable, Dict, List, Set

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.workspace import ObjectType


def _collect(label: str, fn: Callable[[], List[str]], warnings: List[str]) -> List[str]:
    """Run one best-effort query; on error, record a warning and return []."""
    try:
        vals = sorted({v for v in fn() if v})
        print(f"  {label}: {len(vals)}", file=sys.stderr)
        return vals
    except Exception as exc:  # noqa: BLE001 - any API/auth error -> skip this type
        warnings.append(f"{label}: skipped ({type(exc).__name__}: {exc})")
        print(f"  {label}: SKIPPED ({exc})", file=sys.stderr)
        return []


def _walk_directories(w: WorkspaceClient, roots: List[str], max_depth: int) -> List[str]:
    """Breadth-first list of DIRECTORY paths under each root, bounded by max_depth."""
    found: Set[str] = set()
    frontier = [(r, 0) for r in roots]
    while frontier:
        path, depth = frontier.pop()
        if depth > max_depth:
            continue
        for obj in w.workspace.list(path):
            if obj.object_type == ObjectType.DIRECTORY and obj.path:
                found.add(obj.path)
                frontier.append((obj.path, depth + 1))
    return sorted(found)


def export(w: WorkspaceClient, env: str, dir_roots: List[str], max_depth: int) -> Dict:
    warnings: List[str] = []
    print("Querying identities...", file=sys.stderr)

    users = _collect("users", lambda: [u.user_name for u in w.users.list()], warnings)
    sps = []
    try:
        for sp in w.service_principals.list():
            sps.append({"application_id": sp.application_id, "display_name": sp.display_name})
        print(f"  service_principals: {len(sps)}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"service_principals: skipped ({exc})")
        print(f"  service_principals: SKIPPED ({exc})", file=sys.stderr)
    groups = _collect("groups", lambda: [g.display_name for g in w.groups.list()], warnings)

    print("Querying resources...", file=sys.stderr)
    resources: Dict[str, List] = {}
    resources["clusters"] = [
        {"name": n} for n in _collect("clusters", lambda: [c.cluster_name for c in w.clusters.list()], warnings)
    ]
    resources["cluster-policies"] = [
        {"name": n} for n in _collect("cluster-policies", lambda: [p.name for p in w.cluster_policies.list()], warnings)
    ]
    resources["instance-pools"] = [
        {"name": n}
        for n in _collect("instance-pools", lambda: [p.instance_pool_name for p in w.instance_pools.list()], warnings)
    ]
    resources["warehouses"] = [
        {"name": n} for n in _collect("warehouses", lambda: [wh.name for wh in w.warehouses.list()], warnings)
    ]
    resources["jobs"] = [
        {"name": n}
        for n in _collect("jobs", lambda: [j.settings.name for j in w.jobs.list() if j.settings], warnings)
    ]
    resources["secret-scopes"] = [
        {"name": n} for n in _collect("secret-scopes", lambda: [s.name for s in w.secrets.list_scopes()], warnings)
    ]
    resources["directories"] = [
        {"path": p} for p in _collect("directories", lambda: _walk_directories(w, dir_roots, max_depth), warnings)
    ]

    # Drop empty buckets so the manifest only asserts what we actually observed.
    resources = {k: v for k, v in resources.items() if v}

    manifest = {
        "environment": env,
        "identities": {
            "users": [{"user_name": u} for u in users],
            "service_principals": sps,
            "groups": [{"name": g} for g in groups],
        },
        "resources": resources,
    }
    if warnings:
        manifest["_warnings"] = warnings
    return manifest


def _resolve_format(fmt: str, out: str) -> str:
    """Return 'json' or 'yaml', inferring from the --out extension when fmt is 'auto'."""
    if fmt != "auto":
        return fmt
    lowered = out.lower()
    return "yaml" if lowered.endswith((".yaml", ".yml")) else "json"


def write_manifest(manifest: Dict, out: str, fmt: str) -> None:
    """Write the manifest to ``out`` as JSON or YAML."""
    with open(out, "w", encoding="utf-8") as fh:
        if fmt == "yaml":
            try:
                import yaml
            except ImportError:  # pragma: no cover - exercised only where PyYAML is absent
                raise SystemExit(
                    "YAML output requires PyYAML (pip install pyyaml), "
                    "or write JSON with --format json / a .json --out."
                )
            yaml.safe_dump(manifest, fh, sort_keys=False, default_flow_style=False)
        else:
            json.dump(manifest, fh, indent=2)
            fh.write("\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Export a workspace into a governance _inventory manifest")
    ap.add_argument("--profile", default=None, help=".databrickscfg profile (else env-var/default auth)")
    ap.add_argument("--env", required=True, help="environment name to stamp into the manifest")
    ap.add_argument("--out", required=True, help="output path for the inventory (JSON or YAML)")
    ap.add_argument("--format", choices=("auto", "json", "yaml"), default="auto",
                    help="output format; 'auto' infers from the --out extension (default)")
    ap.add_argument("--dir-roots", default="/Workspace/Shared",
                    help="comma-separated workspace roots to walk for directories")
    ap.add_argument("--max-depth", type=int, default=4, help="max recursion depth for directory walk")
    args = ap.parse_args(argv)

    fmt = _resolve_format(args.format, args.out)
    w = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()
    manifest = export(w, args.env, [r.strip() for r in args.dir_roots.split(",") if r.strip()], args.max_depth)

    write_manifest(manifest, args.out, fmt)
    print(f"Wrote {args.out} ({fmt})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
