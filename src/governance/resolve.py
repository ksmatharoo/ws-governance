"""Resolve config identifiers against a live workspace.

Two concerns:

* **Objects** -- turn a human-friendly ``name``/``path`` into the ``request_object_id``
  the Permissions API expects (cluster id, job id, workspace object id, ...).
* **Principals / members** -- validate that a group / user / service principal named
  in the config actually exists, and (for group membership) resolve it to its SCIM id.

Principal name conventions (also the identifiers used in ACL requests):
    group             -> group displayName
    user              -> userName (email)
    service_principal -> applicationId

Group membership PATCH operations require the SCIM internal ``id`` instead, which is
what :meth:`IdentityIndex.scim_id` returns.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional, Union

from databricks.sdk import AccountClient, WorkspaceClient
from databricks.sdk.service import workspace as ws

from governance.errors import ResolutionError
from governance.objecttypes import ObjectTypeSpec

# Either client works: both expose identically-shaped .groups/.users/.service_principals.
ScimClient = Union[WorkspaceClient, AccountClient]


# --------------------------------------------------------------------------- #
# Identity: groups / users / service principals
# --------------------------------------------------------------------------- #
class IdentityIndex:
    """Lazily-loaded, cached SCIM lookups.

    Backed by either a ``WorkspaceClient`` (workspace-scope identities) or an
    ``AccountClient`` (account-scope identities); both expose the same SCIM APIs.
    """

    def __init__(self, scim: ScimClient):
        self._scim = scim
        self._groups: Optional[Dict[str, object]] = None
        self._users: Optional[Dict[str, object]] = None
        self._sps: Optional[Dict[str, object]] = None

    # -- caches --
    def _ensure_groups(self) -> None:
        if self._groups is None:
            self._groups = {
                g.display_name: g
                for g in self._scim.groups.list(attributes="id,displayName,members")
                if g.display_name
            }

    def _ensure_users(self) -> None:
        if self._users is None:
            self._users = {
                u.user_name: u
                for u in self._scim.users.list(attributes="id,userName")
                if u.user_name
            }

    def _ensure_sps(self) -> None:
        if self._sps is None:
            self._sps = {
                sp.application_id: sp
                for sp in self._scim.service_principals.list(attributes="id,applicationId,displayName")
                if sp.application_id
            }

    def all_groups(self) -> Dict[str, object]:
        """All groups by display name (used by the delete phase of group reconcile)."""
        self._ensure_groups()
        return dict(self._groups)

    def invalidate_groups(self) -> None:
        """Drop the group cache (after create/delete so a re-read sees changes)."""
        self._groups = None

    # -- lookups (raise ResolutionError if missing) --
    def group(self, name: str):
        self._ensure_groups()
        if name not in self._groups:
            raise ResolutionError(f"Group '{name}' not found in the workspace")
        return self._groups[name]

    def user(self, user_name: str):
        self._ensure_users()
        if user_name not in self._users:
            raise ResolutionError(f"User '{user_name}' not found in the workspace")
        return self._users[user_name]

    def service_principal(self, application_id: str):
        self._ensure_sps()
        if application_id not in self._sps:
            raise ResolutionError(
                f"Service principal (application id) '{application_id}' not found in the workspace"
            )
        return self._sps[application_id]

    def validate(self, principal_type: str, name: str) -> str:
        """Confirm a principal exists; return the name used in ACL requests (unchanged)."""
        if principal_type == "group":
            self.group(name)
        elif principal_type == "user":
            self.user(name)
        elif principal_type == "service_principal":
            self.service_principal(name)
        else:  # pragma: no cover - guarded by config validation
            raise ResolutionError(f"Unknown principal type '{principal_type}'")
        return name

    def scim_id(self, member_type: str, name: str) -> str:
        """Resolve a group member to its SCIM internal id (for membership PATCH)."""
        if member_type == "user":
            return self.user(name).id
        if member_type == "service_principal":
            return self.service_principal(name).id
        if member_type == "group":
            return self.group(name).id
        raise ResolutionError(f"Unknown member type '{member_type}'")  # pragma: no cover


# --------------------------------------------------------------------------- #
# Objects: identifier -> request_object_id
# --------------------------------------------------------------------------- #
def _find_cluster(w: WorkspaceClient, name: str) -> str:
    for c in w.clusters.list():
        if c.cluster_name == name:
            return c.cluster_id
    raise ResolutionError(f"Cluster '{name}' not found")


def _find_policy(w: WorkspaceClient, name: str) -> str:
    for p in w.cluster_policies.list():
        if p.name == name:
            return p.policy_id
    raise ResolutionError(f"Cluster policy '{name}' not found")


def _find_pool(w: WorkspaceClient, name: str) -> str:
    for p in w.instance_pools.list():
        if p.instance_pool_name == name:
            return p.instance_pool_id
    raise ResolutionError(f"Instance pool '{name}' not found")


def _find_warehouse(w: WorkspaceClient, name: str) -> str:
    for wh in w.warehouses.list():
        if wh.name == name:
            return wh.id
    raise ResolutionError(f"SQL warehouse '{name}' not found")


def _find_job(w: WorkspaceClient, name: str) -> str:
    matches = [j.job_id for j in w.jobs.list() if j.settings and j.settings.name == name]
    if not matches:
        raise ResolutionError(f"Job '{name}' not found")
    if len(matches) > 1:
        raise ResolutionError(
            f"Job name '{name}' is ambiguous ({len(matches)} jobs share it); "
            "job names must be unique to be governed by name"
        )
    return str(matches[0])


def _find_repo(w: WorkspaceClient, path: str) -> str:
    for r in w.repos.list():
        if r.path == path:
            return str(r.id)
    raise ResolutionError(f"Repo '{path}' not found")


def _workspace_object(w: WorkspaceClient, path: str, expected: ws.ObjectType) -> str:
    try:
        info = w.workspace.get_status(path)
    except Exception as exc:
        raise ResolutionError(f"Workspace path '{path}' not found: {exc}") from exc
    if info.object_type != expected:
        got = info.object_type.value if info.object_type else "unknown"
        raise ResolutionError(
            f"Workspace path '{path}' is a {got}, expected {expected.value}"
        )
    return str(info.object_id)


def _find_notebook(w: WorkspaceClient, path: str) -> str:
    return _workspace_object(w, path, ws.ObjectType.NOTEBOOK)


def _find_directory(w: WorkspaceClient, path: str) -> str:
    return _workspace_object(w, path, ws.ObjectType.DIRECTORY)


def _find_experiment(w: WorkspaceClient, name: str) -> str:
    try:
        resp = w.experiments.get_by_name(name)
    except Exception as exc:
        raise ResolutionError(f"MLflow experiment '{name}' not found: {exc}") from exc
    if not resp.experiment or not resp.experiment.experiment_id:
        raise ResolutionError(f"MLflow experiment '{name}' not found")
    return resp.experiment.experiment_id


def _find_serving_endpoint(w: WorkspaceClient, name: str) -> str:
    for e in w.serving_endpoints.list():
        if e.name == name:
            return e.id
    raise ResolutionError(f"Serving endpoint '{name}' not found")


def _find_pipeline(w: WorkspaceClient, name: str) -> str:
    matches = [p.pipeline_id for p in w.pipelines.list_pipelines() if p.name == name]
    if not matches:
        raise ResolutionError(f"Pipeline '{name}' not found")
    if len(matches) > 1:
        raise ResolutionError(
            f"Pipeline name '{name}' is ambiguous ({len(matches)} pipelines share it)"
        )
    return matches[0]


def _find_dashboard(w: WorkspaceClient, name: str) -> str:
    for d in w.lakeview.list():
        if d.display_name == name:
            return d.dashboard_id
    raise ResolutionError(f"Dashboard '{name}' not found")


def _find_query(w: WorkspaceClient, name: str) -> str:
    for q in w.queries.list():
        if q.display_name == name:
            return q.id
    raise ResolutionError(f"Query '{name}' not found")


def _find_alert(w: WorkspaceClient, name: str) -> str:
    for a in w.alerts.list():
        if a.display_name == name:
            return a.id
    raise ResolutionError(f"Alert '{name}' not found")


# object-type key -> resolver(client, identifier) -> request_object_id
_RESOLVERS: Dict[str, Callable[[WorkspaceClient, str], str]] = {
    "clusters": _find_cluster,
    "cluster-policies": _find_policy,
    "instance-pools": _find_pool,
    "warehouses": _find_warehouse,
    "jobs": _find_job,
    "notebooks": _find_notebook,
    "directories": _find_directory,
    "repos": _find_repo,
    "experiments": _find_experiment,
    "serving-endpoints": _find_serving_endpoint,
    "pipelines": _find_pipeline,
    "dashboards": _find_dashboard,
    "queries": _find_query,
    "alerts": _find_alert,
}


def resolve_object_id(client: WorkspaceClient, spec: ObjectTypeSpec, identifier: str) -> str:
    """Resolve one object's ``request_object_id`` for the Permissions API.

    Not used for ``secrets`` (the scope name *is* the id) or ``tokens`` (a fixed id).
    """
    resolver = _RESOLVERS.get(spec.key)
    if resolver is None:
        raise ResolutionError(f"No object resolver registered for type '{spec.key}'")
    return resolver(client, identifier)
