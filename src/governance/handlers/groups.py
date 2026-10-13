"""Group membership governance via SCIM (workspace- or account-scope).

Full lifecycle, applied in a safe order so nested-group references always resolve:

1. **create** groups present in config but missing in the workspace/account
2. **membership** reconcile -- add/remove members so each group matches config
   (full drift-removal: members not in config are removed)
3. **delete** groups present in the workspace/account but not in config
   -- only when the deletion switch is on, and never built-in or protected groups

The same handler works at either scope: it is given whichever SCIM client
(``WorkspaceClient`` or ``AccountClient``) governs groups for the environment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from databricks.sdk.service import iam

from governance.config.models import GroupEntry
from governance.diff import MembershipDiff, current_member_ids
from governance.errors import GovernanceError, ResolutionError
from governance.resolve import IdentityIndex, ScimClient

# Never created/deleted/renamed by this tool.
BUILTIN_GROUPS = frozenset({"admins", "users", "account users"})

MemberSpec = Tuple[str, str]  # (member_type, member_name)


@dataclass
class GroupMembershipChange:
    name: str
    add: List[str] = field(default_factory=list)     # human-readable "type:name"
    remove: List[str] = field(default_factory=list)   # human-readable member label

    @property
    def changed(self) -> bool:
        return bool(self.add or self.remove)


@dataclass
class GroupPlan:
    to_create: List[str] = field(default_factory=list)
    to_delete: List[str] = field(default_factory=list)
    protected_from_delete: List[str] = field(default_factory=list)
    membership: List[GroupMembershipChange] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.to_create or self.to_delete or any(m.changed for m in self.membership))


class GroupHandler:
    def __init__(
        self,
        scim: ScimClient,
        index: IdentityIndex,
        protected_principals=(),
        allow_deletion: bool = False,
    ):
        self.scim = scim
        self.index = index
        self.protected = set(protected_principals) | BUILTIN_GROUPS
        self.allow_deletion = allow_deletion

    # ------------------------------------------------------------------ plan
    def plan(self, desired: List[GroupEntry]) -> GroupPlan:
        existing = self.index.all_groups()
        desired_names = {g.name for g in desired}
        plan = GroupPlan()

        for group in desired:
            if group.name in BUILTIN_GROUPS and group.name not in existing:
                raise GovernanceError(f"Refusing to create built-in group '{group.name}'")
            if group.name not in existing:
                plan.to_create.append(group.name)
                # New group: every member is an addition (members must resolve, except
                # nested groups that are also being created this run).
                self._resolve_members(group, desired_names)  # validates existence, raises on unknown
                add = [f"{m.member_type}:{m.member_name}" for m in group.members]
                if add:
                    plan.membership.append(GroupMembershipChange(name=group.name, add=add))
            else:
                change = self._existing_membership_change(group, existing[group.name], desired_names)
                if change.changed:
                    plan.membership.append(change)

        if self.allow_deletion:
            for name in existing:
                if name in desired_names:
                    continue
                if name in self.protected:
                    plan.protected_from_delete.append(name)
                else:
                    plan.to_delete.append(name)

        return plan

    # ----------------------------------------------------------------- apply
    def apply(self, desired: List[GroupEntry]) -> List[dict]:
        """Execute the plan. Returns a list of action records for the audit log."""
        actions: List[dict] = []
        existing = self.index.all_groups()
        desired_names = {g.name for g in desired}

        # Phase 1: create missing groups (empty; membership set in phase 2).
        for group in desired:
            if group.name not in existing:
                self.scim.groups.create(display_name=group.name)
                actions.append({"action": "create_group", "group": group.name})
        if any(a["action"] == "create_group" for a in actions):
            self.index.invalidate_groups()
            existing = self.index.all_groups()

        # Phase 2: reconcile membership (drift-removal).
        for group in desired:
            grp = existing.get(group.name)
            if grp is None:  # pragma: no cover - only if create silently failed
                raise GovernanceError(f"Group '{group.name}' not found after creation")
            current_ids = current_member_ids(grp)
            desired_ids = {self.index.scim_id(m.member_type, m.member_name) for m in group.members}
            diff = MembershipDiff.compute(current_ids, desired_ids)
            if not diff.changed:
                continue
            ops = self._membership_ops(diff)
            self.scim.groups.patch(
                id=grp.id,
                operations=ops,
                schemas=[iam.PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP],
            )
            actions.append(
                {
                    "action": "update_membership",
                    "group": group.name,
                    "added": len(diff.to_add),
                    "removed": len(diff.to_remove),
                }
            )

        # Phase 3: delete groups not in config (guarded).
        if self.allow_deletion:
            for name, grp in existing.items():
                if name in desired_names or name in self.protected:
                    continue
                self.scim.groups.delete(id=grp.id)
                actions.append({"action": "delete_group", "group": name})

        return actions

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _membership_ops(diff: MembershipDiff) -> List[iam.Patch]:
        ops: List[iam.Patch] = []
        if diff.to_add:
            ops.append(
                iam.Patch(
                    op=iam.PatchOp.ADD,
                    path="members",
                    value=[{"value": i} for i in sorted(diff.to_add)],
                )
            )
        for member_id in sorted(diff.to_remove):
            ops.append(iam.Patch(op=iam.PatchOp.REMOVE, path=f'members[value eq "{member_id}"]'))
        return ops

    def _resolve_members(self, group: GroupEntry, desired_names) -> None:
        for m in group.members:
            self._resolve_member(m.member_type, m.member_name, desired_names)

    def _resolve_member(self, member_type: str, name: str, desired_names) -> Optional[str]:
        """Return SCIM id, or ``None`` for a nested group being created this run."""
        try:
            return self.index.scim_id(member_type, name)
        except ResolutionError:
            if member_type == "group" and name in desired_names:
                return None
            raise

    def _existing_membership_change(self, group: GroupEntry, grp, desired_names) -> GroupMembershipChange:
        current_ids = current_member_ids(grp)
        id_to_display: Dict[str, str] = {
            m.value: (m.display or m.value) for m in (grp.members or []) if m.value
        }
        desired_ids = set()
        id_to_spec: Dict[str, MemberSpec] = {}
        pending_add: List[MemberSpec] = []
        for m in group.members:
            member_id = self._resolve_member(m.member_type, m.member_name, desired_names)
            if member_id is None:
                pending_add.append((m.member_type, m.member_name))
            else:
                desired_ids.add(member_id)
                id_to_spec[member_id] = (m.member_type, m.member_name)

        diff = MembershipDiff.compute(current_ids, desired_ids)
        add = [f"{t}:{n}" for t, n in (id_to_spec[i] for i in diff.to_add)]
        add += [f"{t}:{n} (new group)" for t, n in pending_add]
        remove = [id_to_display.get(i, i) for i in diff.to_remove]
        return GroupMembershipChange(name=group.name, add=sorted(add), remove=sorted(remove))
