"""Workspace entitlement governance via SCIM (users, service principals, groups).

Each config entry lists one principal's *complete* entitlement set, so reconcile is
full drift-removal per listed principal: missing entitlements are added, extra ones
removed. Principals not listed in config are never read or changed.

Entitlements are a workspace-level property, so this always goes through the
workspace SCIM API, even when groups themselves are account-scope.

Safety: ``admins`` and ``protected_principals`` never lose entitlements (additions
still apply), so a config mistake cannot lock administrators out.
"""

from __future__ import annotations

from typing import Iterable, List, Set, Tuple

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import iam

from governance.changes import Change
from governance.config.models import EntitlementEntry
from governance.resolve import IdentityIndex

# Groups whose entitlements are never removed, regardless of protected_principals.
_ALWAYS_PROTECTED_GROUPS = frozenset({"admins"})


class EntitlementsHandler:
    def __init__(self, client: WorkspaceClient, index: IdentityIndex, protected_principals=()):
        self.w = client
        self.index = index
        self.protected = set(protected_principals)

    def _api(self, principal_type: str):
        if principal_type == "group":
            return self.w.groups
        if principal_type == "user":
            return self.w.users
        return self.w.service_principals

    def _is_protected(self, entry: EntitlementEntry) -> bool:
        if entry.principal_type == "group" and entry.principal_name in _ALWAYS_PROTECTED_GROUPS:
            return True
        return entry.principal_name in self.protected

    def current(self, entry: EntitlementEntry) -> Tuple[str, Set[str]]:
        """Return ``(scim_id, current entitlement values)`` for the entry's principal."""
        scim_id = self.index.scim_id(entry.principal_type, entry.principal_name)
        identity = self._api(entry.principal_type).get(id=scim_id)
        return scim_id, {e.value for e in (identity.entitlements or []) if e.value}

    def reconcile(
        self, entry: EntitlementEntry, dry_run: bool, pending_groups: Iterable[str] = ()
    ) -> Tuple[Change, List[dict]]:
        """``pending_groups``: groups the plan will create but that don't exist yet (dry run)."""
        ident = entry.identifier()
        if dry_run and entry.principal_type == "group" and entry.principal_name in set(pending_groups):
            scim_id, current = None, set()
        else:
            scim_id, current = self.current(entry)
        desired = set(entry.entitlements)

        to_add = desired - current
        to_remove = current - desired
        kept = set()
        if self._is_protected(entry):
            kept, to_remove = to_remove, set()

        change = Change(
            kind="entitlements",
            identifier=ident,
            adds=sorted(to_add),
            removes=sorted(to_remove),
            unchanged=len(current & desired),
            protected=bool(kept),
        )

        audit: List[dict] = []
        if not dry_run and (to_add or to_remove):
            self._api(entry.principal_type).patch(
                id=scim_id,
                operations=_patch_ops(to_add, to_remove),
                schemas=[iam.PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP],
            )
            audit.append(
                {
                    "action": "update_entitlements",
                    "principal": ident,
                    "added": sorted(to_add),
                    "removed": sorted(to_remove),
                }
            )
        return change, audit


def _patch_ops(to_add: Set[str], to_remove: Set[str]) -> List[iam.Patch]:
    ops: List[iam.Patch] = []
    if to_add:
        ops.append(
            iam.Patch(op=iam.PatchOp.ADD, path="entitlements", value=[{"value": v} for v in sorted(to_add)])
        )
    for value in sorted(to_remove):
        ops.append(iam.Patch(op=iam.PatchOp.REMOVE, path=f'entitlements[value eq "{value}"]'))
    return ops
