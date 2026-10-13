"""Reusable in-memory fakes for the Databricks SDK clients.

The fakes implement just the methods the handlers/engine call, returning real SDK
model objects so tests exercise the same code paths as production. No network.

Run the suite with either:
    PYTHONPATH=src python3 -m unittest discover -s tests
    pytest            (uses pyproject's pythonpath = ["src"])
"""

from __future__ import annotations

from typing import Dict, List, Optional

from databricks.sdk.service import compute, dashboards, iam, pipelines, sql
from databricks.sdk.service.workspace import AclItem, AclPermission


def acr(levels, **principal) -> iam.AccessControlResponse:
    """AccessControlResponse with direct (non-inherited) permissions."""
    return iam.AccessControlResponse(
        all_permissions=[iam.Permission(permission_level=l, inherited=False) for l in levels],
        **principal,
    )


class FakePermissions:
    def __init__(self, state: Optional[Dict] = None, levels=None):
        self.state = state or {}
        self.levels = levels or [
            iam.PermissionLevel.CAN_MANAGE,
            iam.PermissionLevel.CAN_RESTART,
            iam.PermissionLevel.CAN_ATTACH_TO,
            iam.PermissionLevel.CAN_USE,
            iam.PermissionLevel.CAN_MANAGE_RUN,
            iam.PermissionLevel.CAN_VIEW,
        ]
        self.set_calls: List = []

    def get(self, request_object_type, request_object_id):
        return iam.ObjectPermissions(
            access_control_list=self.state.get((request_object_type, request_object_id), [])
        )

    def set(self, request_object_type, request_object_id, *, access_control_list=None):
        self.set_calls.append((request_object_type, request_object_id, access_control_list))
        self.state[(request_object_type, request_object_id)] = [
            acr([r.permission_level], group_name=r.group_name, user_name=r.user_name,
                service_principal_name=r.service_principal_name)
            for r in (access_control_list or [])
        ]

    def get_permission_levels(self, request_object_type, request_object_id):
        return iam.GetPermissionLevelsResponse(
            permission_levels=[iam.PermissionsDescription(permission_level=l) for l in self.levels]
        )


class FakeSecrets:
    def __init__(self, acls: Optional[Dict[str, List[AclItem]]] = None):
        self.acls = acls or {}
        self.puts: List = []
        self.deletes: List = []

    def list_acls(self, *, scope):
        return iter(self.acls.get(scope, []))

    def put_acl(self, *, scope, principal, permission):
        self.puts.append((scope, principal, permission.value))

    def delete_acl(self, *, scope, principal):
        self.deletes.append((scope, principal))


class _FakeList:
    def __init__(self, items):
        self.items = list(items)

    def list(self, **kw):
        return iter(self.items)


class FakeGroups(_FakeList):
    def __init__(self, items):
        super().__init__(items)
        self.created: List[str] = []
        self.patched: List = []
        self.deleted: List[str] = []

    def create(self, *, display_name=None, **kw):
        group = iam.Group(id=f"g-{display_name}", display_name=display_name, members=[])
        self.items.append(group)
        self.created.append(display_name)
        return group

    def patch(self, *, id=None, operations=None, schemas=None):
        self.patched.append((id, operations))

    def delete(self, *, id=None):
        self.deleted.append(id)


class _FakePipelines:
    def __init__(self, items):
        self.items = list(items)

    def list_pipelines(self, **kw):
        return iter(self.items)


class FakeWS:
    """A minimal fake WorkspaceClient. Customize via constructor kwargs."""

    def __init__(self, *, permissions=None, secrets=None, groups=None, users=None,
                 service_principals=None, clusters=None, pipelines=None, queries=None,
                 alerts=None, lakeview=None):
        self.permissions = permissions or FakePermissions()
        self.secrets = secrets or FakeSecrets()
        self.groups = FakeGroups(groups or [])
        self.users = _FakeList(users or [])
        self.service_principals = _FakeList(service_principals or [])
        self.clusters = _FakeList(clusters or [])
        self.pipelines = _FakePipelines(pipelines or [])
        self.queries = _FakeList(queries or [])
        self.alerts = _FakeList(alerts or [])
        self.lakeview = _FakeList(lakeview or [])


def user(uid, name):
    return iam.User(id=uid, user_name=name)


def sp(spid, app_id):
    return iam.ServicePrincipal(id=spid, application_id=app_id)


def group(gid, name, member_ids_displays=()):
    return iam.Group(
        id=gid,
        display_name=name,
        members=[iam.ComplexValue(value=v, display=d) for v, d in member_ids_displays],
    )


def cluster(cid, name):
    return compute.ClusterDetails(cluster_id=cid, cluster_name=name)


def pipeline(pid, name):
    return pipelines.PipelineStateInfo(pipeline_id=pid, name=name)


def query(qid, name):
    return sql.ListQueryObjectsResponseQuery(id=qid, display_name=name)


def alert(aid, name):
    return sql.ListAlertsResponseAlert(id=aid, display_name=name)


def dashboard(did, name):
    return dashboards.Dashboard(dashboard_id=did, display_name=name)


def aclitem(principal, permission):
    return AclItem(principal=principal, permission=AclPermission(permission))
