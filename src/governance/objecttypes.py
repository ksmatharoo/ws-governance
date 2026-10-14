"""Registry of governed workspace object types.

Each supported object type is described by an :class:`ObjectTypeSpec`. The spec is
pure metadata (no SDK calls) so it can be imported and tested in isolation. The
resolve/handler layers consume these specs to know:

- ``kind``       -- which API family governs the object:
                    * ``permissions`` -> Permissions API (``WorkspaceClient.permissions``)
                    * ``secrets``     -> Secret ACL API (``WorkspaceClient.secrets``)
                    * ``tokens``      -> Permissions API on the singleton ``authorization/tokens``
- ``id_field``   -- the YAML field used to identify an object (``name`` or ``path``),
                    or ``None`` for singletons (tokens).
- ``api_type``   -- the ``request_object_type`` string passed to the Permissions API.
- ``fixed_id``   -- the ``request_object_id`` for singleton objects (tokens).

Permission-level *values* differ per object type; they are validated at runtime
against ``permissions.get_permission_levels`` (or the fixed secret levels below),
so they are intentionally not hard-coded here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Optional

# Valid permission levels for secret-scope ACLs (the secrets API is not part of
# the Permissions API and has a fixed, small set of levels).
SECRET_PERMISSION_LEVELS = ("READ", "WRITE", "MANAGE")

# Workspace entitlements assignable to users, service principals and groups via SCIM.
ENTITLEMENTS = (
    "workspace-access",
    "workspace-consume",
    "databricks-sql-access",
    "allow-cluster-create",
    "allow-instance-pool-create",
)


@dataclass(frozen=True)
class ObjectTypeSpec:
    """Metadata describing how one governed object type behaves."""

    key: str
    """Top-level key used in the JSON config (e.g. ``jobs``)."""

    kind: str
    """One of ``permissions``, ``secrets``, ``tokens``, ``groups``, ``entitlements``."""

    label: str
    """Human-friendly label for reports."""

    id_field: Optional[str] = None
    """``name`` or ``path`` -- the config field identifying an object. ``None`` for singletons."""

    api_type: Optional[str] = None
    """Permissions API ``request_object_type`` (unused for the ``secrets`` kind)."""

    fixed_id: Optional[str] = None
    """Fixed ``request_object_id`` for singleton objects (e.g. ``tokens``)."""

    allowed_permissions: Optional[FrozenSet[str]] = None
    """Permission levels accepted in config for this type (static, for offline linting).
    ``None`` for ``groups`` (no permissions -- membership only); for ``entitlements``
    the allowed entitlement values. The live ``validate``
    command additionally cross-checks against ``permissions.get_permission_levels``."""


# Permission-level enums per object type (Databricks Permissions API). Static, so the
# linter can validate config values offline without a workspace.
_CODE_LEVELS = frozenset({"CAN_READ", "CAN_RUN", "CAN_EDIT", "CAN_MANAGE"})

# The order here is the canonical order used when iterating "all" object types.
_SPECS = [
    # --- Identity (group membership governance via SCIM) ---
    # Governed first so that groups referenced by ACLs exist before ACLs are applied.
    ObjectTypeSpec("groups", "groups", "Groups (membership)", "name"),
    # Entitlements come right after groups so groups created this run can be entitled.
    ObjectTypeSpec("entitlements", "entitlements", "Entitlements (workspace)", None,
                   allowed_permissions=frozenset(ENTITLEMENTS)),
    # --- Compute ---
    ObjectTypeSpec("clusters", "permissions", "Clusters", "name", api_type="clusters",
                   allowed_permissions=frozenset({"CAN_ATTACH_TO", "CAN_RESTART", "CAN_MANAGE"})),
    ObjectTypeSpec("cluster-policies", "permissions", "Cluster policies", "name", api_type="cluster-policies",
                   allowed_permissions=frozenset({"CAN_USE"})),
    ObjectTypeSpec("instance-pools", "permissions", "Instance pools", "name", api_type="instance-pools",
                   allowed_permissions=frozenset({"CAN_ATTACH_TO", "CAN_MANAGE"})),
    ObjectTypeSpec("warehouses", "permissions", "SQL warehouses", "name", api_type="warehouses",
                   allowed_permissions=frozenset({"CAN_USE", "CAN_MONITOR", "CAN_MANAGE"})),
    # --- Workflows & code ---
    ObjectTypeSpec("jobs", "permissions", "Jobs", "name", api_type="jobs",
                   allowed_permissions=frozenset({"CAN_VIEW", "CAN_MANAGE_RUN", "CAN_MANAGE"})),
    ObjectTypeSpec("pipelines", "permissions", "Lakeflow/DLT pipelines", "name", api_type="pipelines",
                   allowed_permissions=frozenset({"CAN_VIEW", "CAN_RUN", "CAN_MANAGE"})),
    ObjectTypeSpec("notebooks", "permissions", "Notebooks", "path", api_type="notebooks",
                   allowed_permissions=_CODE_LEVELS),
    ObjectTypeSpec("directories", "permissions", "Directories", "path", api_type="directories",
                   allowed_permissions=_CODE_LEVELS),
    ObjectTypeSpec("repos", "permissions", "Repos", "path", api_type="repos",
                   allowed_permissions=_CODE_LEVELS),
    # --- ML assets (ACL-governed; UC-registered models are handled in Part 2) ---
    ObjectTypeSpec("experiments", "permissions", "MLflow experiments", "name", api_type="experiments",
                   allowed_permissions=frozenset({"CAN_READ", "CAN_EDIT", "CAN_MANAGE"})),
    ObjectTypeSpec("serving-endpoints", "permissions", "Serving endpoints", "name", api_type="serving-endpoints",
                   allowed_permissions=frozenset({"CAN_VIEW", "CAN_QUERY", "CAN_MANAGE"})),
    # --- SQL / BI (DBSQL + Lakeview AI/BI) ---
    ObjectTypeSpec("dashboards", "permissions", "Dashboards (Lakeview AI/BI)", "name", api_type="dashboards",
                   allowed_permissions=frozenset({"CAN_READ", "CAN_RUN", "CAN_EDIT", "CAN_MANAGE"})),
    ObjectTypeSpec("queries", "permissions", "DBSQL queries", "name", api_type="queries",
                   allowed_permissions=frozenset({"CAN_VIEW", "CAN_RUN", "CAN_EDIT", "CAN_MANAGE"})),
    ObjectTypeSpec("alerts", "permissions", "DBSQL alerts", "name", api_type="alerts",
                   allowed_permissions=frozenset({"CAN_VIEW", "CAN_RUN", "CAN_EDIT", "CAN_MANAGE"})),
    # --- Secrets & tokens ---
    ObjectTypeSpec("secret-scopes", "secrets", "Secret scopes", "name",
                   allowed_permissions=frozenset(SECRET_PERMISSION_LEVELS)),
    ObjectTypeSpec("tokens", "tokens", "Tokens (PAT usage)", None, api_type="authorization", fixed_id="tokens",
                   allowed_permissions=frozenset({"CAN_USE", "CAN_MANAGE"})),
]

REGISTRY: Dict[str, ObjectTypeSpec] = {spec.key: spec for spec in _SPECS}


def all_keys() -> list[str]:
    """Object type keys in canonical order."""
    return [spec.key for spec in _SPECS]


def get_spec(key: str) -> ObjectTypeSpec:
    """Look up a spec by key, raising ``KeyError`` with the known keys on miss."""
    try:
        return REGISTRY[key]
    except KeyError:
        raise KeyError(
            f"Unknown object type '{key}'. Known types: {', '.join(all_keys())}"
        ) from None
