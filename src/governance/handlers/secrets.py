"""Reconcile secret-scope ACLs via the Secrets API (drift-removal).

The Secrets API is not part of the Permissions API: each principal has exactly one
permission on a scope (READ / WRITE / MANAGE), set with ``put_acl`` and removed with
``delete_acl``. The scope name is its own identifier (no id resolution needed).

Because the API is type-agnostic (principal is a bare name), diffing is done per
principal name. ``protected_principals`` keep their current permission.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.workspace import AclPermission

from governance.changes import Change
from governance.config.models import ObjectEntry
from governance.objecttypes import ObjectTypeSpec


class SecretsHandler:
    def __init__(self, client: WorkspaceClient, protected_principals=()):
        self.w = client
        self.protected = set(protected_principals)

    def reconcile(self, spec: ObjectTypeSpec, entry: ObjectEntry, dry_run: bool) -> Tuple[Change, List[dict]]:
        scope = entry.name

        current: Dict[str, str] = {
            item.principal: item.permission.value
            for item in self.w.secrets.list_acls(scope=scope)
            if item.principal and item.permission
        }
        desired: Dict[str, str] = {a.principal_name: a.permission for a in entry.acls}

        # Effective desired = config, plus current permission for protected principals
        # not otherwise specified (so they are never revoked).
        effective = dict(desired)
        for principal, perm in current.items():
            if principal in self.protected and principal not in effective:
                effective[principal] = perm

        adds: List[str] = []
        to_put: List[Tuple[str, str]] = []
        unchanged = 0
        for principal, perm in effective.items():
            cur = current.get(principal)
            if cur is None:
                adds.append(f"{principal} = {perm}")
                to_put.append((principal, perm))
            elif cur != perm:
                adds.append(f"{principal} = {cur} -> {perm}")
                to_put.append((principal, perm))
            else:
                unchanged += 1

        removes: List[str] = []
        to_delete: List[str] = []
        for principal, perm in current.items():
            if principal not in effective:
                removes.append(f"{principal} = {perm}")
                to_delete.append(principal)

        change = Change(
            kind=spec.key,
            identifier=scope,
            adds=sorted(adds),
            removes=sorted(removes),
            unchanged=unchanged,
        )

        audit: List[dict] = []
        if not dry_run and change.changed:
            for principal, perm in to_put:
                self.w.secrets.put_acl(scope=scope, principal=principal, permission=AclPermission(perm))
            for principal in to_delete:
                self.w.secrets.delete_acl(scope=scope, principal=principal)
            audit.append(
                {
                    "action": "set_secret_acls",
                    "object_type": spec.key,
                    "object": scope,
                    "added": sorted(adds),
                    "removed": sorted(removes),
                }
            )
        return change, audit
