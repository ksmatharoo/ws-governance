"""Load and classify a prerequisite inventory file (stdlib only)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Resource types the helper can create from a name/path descriptor alone.
CREATABLE_RESOURCES = {"directories", "secret-scopes", "experiments"}

# Resource types the helper can create when the entry carries a "spec" block.
CREATABLE_WITH_SPEC = {"clusters", "warehouses", "cluster-policies", "instance-pools"}

# Resource types that need a full spec we don't model (task graph, git url, query
# text, ...) and therefore must be provisioned manually / out of band.
MANUAL_RESOURCES = {
    "jobs", "pipelines", "notebooks", "repos",
    "serving-endpoints", "dashboards", "queries", "alerts",
}

# Identity types the helper can create.
CREATABLE_IDENTITIES = {"users", "service_principals", "groups"}


def load_inventory(path) -> Dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def identities(inv: Dict[str, Any]) -> Dict[str, List[dict]]:
    """Return {kind: [entry, ...]} for users/service_principals/groups."""
    section = inv.get("identities", {}) or {}
    out: Dict[str, List[dict]] = {}
    for kind in ("users", "service_principals", "groups"):
        items = section.get(kind, []) or []
        if isinstance(items, list):
            out[kind] = [e for e in items if isinstance(e, dict)]
    return out


def resources(inv: Dict[str, Any]) -> Dict[str, List[dict]]:
    """Return {object_type: [entry, ...]}, excluding docs and the tokens singleton."""
    section = dict(inv.get("resources", {}) or {})
    section.pop("description", None)
    section.pop("tokens", None)  # singleton, always exists
    out: Dict[str, List[dict]] = {}
    for key, items in section.items():
        if isinstance(items, list):
            out[key] = [e for e in items if isinstance(e, dict)]
    return out


def identity_id(kind: str, entry: dict) -> str:
    """The lookup identifier for an identity entry."""
    if kind == "users":
        return entry.get("user_name", "")
    if kind == "service_principals":
        return entry.get("application_id", "")
    if kind == "groups":
        return entry.get("name", "")
    return ""


def resource_id(id_field: str, entry: dict) -> str:
    """The lookup identifier (name or path) for a resource entry."""
    return entry.get(id_field, "") if id_field else ""
