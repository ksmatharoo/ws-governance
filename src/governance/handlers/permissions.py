"""Reconcile object ACLs via the Permissions API (drift-removal).

Handles both the ``permissions`` kind (clusters, jobs, notebooks, ...) and the
``tokens`` kind (the singleton ``authorization/tokens`` object) -- they share the
same API. ``permissions.set()`` replaces all direct permissions, so sending the
*effective desired* grant set is exactly a full reconcile; the platform always
retains object owners and workspace admins.
"""

from __future__ import annotations

from typing import List, Tuple

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import iam

from governance.changes import Change
from governance.config.models import ObjectEntry
from governance.diff import AclDiff, Grant, current_grants, desired_grants
from governance.objecttypes import ObjectTypeSpec
from governance.resolve import resolve_object_id


def _fmt(grant: Grant) -> str:
    ptype, name, level = grant
    return f"{ptype}:{name} = {level}"


def _to_request(grant: Grant) -> iam.AccessControlRequest:
    ptype, name, level = grant
    kwargs = {"permission_level": iam.PermissionLevel(level)}
    if ptype == "group":
        kwargs["group_name"] = name
    elif ptype == "user":
        kwargs["user_name"] = name
    else:  # service_principal
        kwargs["service_principal_name"] = name
    return iam.AccessControlRequest(**kwargs)


class PermissionsHandler:
    def __init__(self, client: WorkspaceClient, protected_principals=()):
        self.w = client
        self.protected = set(protected_principals)

    def object_id(self, spec: ObjectTypeSpec, entry: ObjectEntry) -> str:
        if spec.kind == "tokens":
            return spec.fixed_id
        identifier = entry.name if spec.id_field == "name" else entry.path
        return resolve_object_id(self.w, spec, identifier)

    def identifier(self, spec: ObjectTypeSpec, entry: ObjectEntry) -> str:
        if spec.kind == "tokens":
            return "tokens"
        return entry.identifier(spec.id_field)

    def reconcile(self, spec: ObjectTypeSpec, entry: ObjectEntry, dry_run: bool) -> Tuple[Change, List[dict]]:
        ident = self.identifier(spec, entry)
        object_id = self.object_id(spec, entry)

        current = current_grants(self.w.permissions.get(spec.api_type, object_id))
        desired = desired_grants(entry.acls)
        diff = AclDiff.compute(current, desired, self.protected)

        change = Change(
            kind=spec.key,
            identifier=ident,
            adds=sorted(_fmt(g) for g in diff.to_add),
            removes=sorted(_fmt(g) for g in diff.to_remove),
            unchanged=len(diff.unchanged),
        )

        audit: List[dict] = []
        if not dry_run and diff.changed:
            acl = [_to_request(g) for g in sorted(diff.effective_desired)]
            self.w.permissions.set(spec.api_type, object_id, access_control_list=acl)
            audit.append(
                {
                    "action": "set_permissions",
                    "object_type": spec.key,
                    "object": ident,
                    "object_id": object_id,
                    "added": sorted(_fmt(g) for g in diff.to_add),
                    "removed": sorted(_fmt(g) for g in diff.to_remove),
                }
            )
        return change, audit

    def valid_permission_levels(self, spec: ObjectTypeSpec, object_id: str) -> List[str]:
        resp = self.w.permissions.get_permission_levels(spec.api_type, object_id)
        return [
            pd.permission_level.value
            for pd in (resp.permission_levels or [])
            if pd.permission_level
        ]
