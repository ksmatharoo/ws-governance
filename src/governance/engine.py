"""Orchestrate reconcile across all governed object types for one environment.

Order matters: groups are reconciled first (they are placed first in the registry),
so any groups referenced by object ACLs exist before those ACLs are applied.

The engine is used for three CLI operations:

* ``validate`` -- resolve objects, check permission levels, and confirm principals /
  members exist; makes no changes.
* ``plan``     -- compute the full diff (dry run).
* ``apply``    -- compute and enact the diff (drift-removal).
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from governance.changes import Change, EnvironmentPlan
from governance.client import make_account_client, make_client
from governance.config.loader import Settings
from governance.config.models import EnvironmentConfig, GroupEntry, ObjectEntry
from governance.errors import GovernanceError, ResolutionError
from governance.handlers.groups import GroupHandler, GroupPlan
from governance.handlers.permissions import PermissionsHandler
from governance.handlers.secrets import SecretsHandler
from governance.objecttypes import SECRET_PERMISSION_LEVELS, all_keys, get_spec
from governance.resolve import IdentityIndex


class GovernanceEngine:
    def __init__(self, settings: Settings, env: str):
        self.settings = settings
        self.env = env
        self.env_settings = settings.environment(env)
        self._ws = None
        self._ws_index: Optional[IdentityIndex] = None
        self._acct = None
        self._acct_index: Optional[IdentityIndex] = None

    # ----------------------------------------------------------- clients
    @property
    def ws(self):
        if self._ws is None:
            self._ws = make_client(self.env_settings)
        return self._ws

    @property
    def ws_index(self) -> IdentityIndex:
        if self._ws_index is None:
            self._ws_index = IdentityIndex(self.ws)
        return self._ws_index

    def _account_client(self):
        if self._acct is None:
            if not self.settings.account:
                raise GovernanceError("Account-scope groups requested but no 'account' block in settings.json")
            self._acct = make_account_client(self.settings.account)
        return self._acct

    @property
    def acct_index(self) -> IdentityIndex:
        if self._acct_index is None:
            self._acct_index = IdentityIndex(self._account_client())
        return self._acct_index

    def _group_scim_and_index(self):
        if self.env_settings.group_scope == "account":
            return self._account_client(), self.acct_index
        return self.ws, self.ws_index

    # ----------------------------------------------------------- reconcile
    def run(
        self,
        config: EnvironmentConfig,
        types: Optional[List[str]] = None,
        dry_run: bool = True,
        allow_group_deletion: bool = False,
    ) -> Tuple[EnvironmentPlan, List[dict]]:
        plan = EnvironmentPlan(env=self.env)
        audit: List[dict] = []
        perm = PermissionsHandler(self.ws, self.settings.protected_principals)
        sec = SecretsHandler(self.ws, self.settings.protected_principals)

        for key in all_keys():
            if key not in config or (types and key not in types):
                continue
            spec = get_spec(key)
            entries = config[key]

            if spec.kind == "groups":
                scim, index = self._group_scim_and_index()
                handler = GroupHandler(
                    scim, index, self.settings.protected_principals, allow_deletion=allow_group_deletion
                )
                changes, group_audit = self._reconcile_groups(handler, entries, dry_run)
                plan.changes.extend(changes)
                audit.extend(group_audit)
                continue

            handler = sec if spec.kind == "secrets" else perm
            for entry in entries:
                ident = perm.identifier(spec, entry) if spec.kind != "secrets" else entry.name
                try:
                    change, records = handler.reconcile(spec, entry, dry_run)
                    audit.extend(records)
                except (ResolutionError, GovernanceError) as exc:
                    change = Change(kind=key, identifier=ident, error=str(exc))
                plan.changes.append(change)

        return plan, audit

    def _reconcile_groups(self, handler: GroupHandler, entries, dry_run: bool) -> Tuple[List[Change], List[dict]]:
        try:
            group_plan = handler.plan(entries)
        except (ResolutionError, GovernanceError) as exc:
            return [Change(kind="groups", identifier="<groups>", error=str(exc))], []
        changes = _group_changes(group_plan)
        audit: List[dict] = []
        if not dry_run and group_plan.changed:
            audit = handler.apply(entries)
        return changes, audit

    # ----------------------------------------------------------- validate
    def validate(self, config: EnvironmentConfig, types: Optional[List[str]] = None) -> List[str]:
        issues: List[str] = []
        perm = PermissionsHandler(self.ws, self.settings.protected_principals)

        for key in all_keys():
            if key not in config or (types and key not in types):
                continue
            spec = get_spec(key)

            if spec.kind == "groups":
                self._validate_groups(config[key], issues)
                continue

            for entry in config[key]:
                ident = perm.identifier(spec, entry) if spec.kind != "secrets" else entry.name
                ctx = f"{key} '{ident}'"
                if spec.kind == "secrets":
                    self._validate_secret(entry, ctx, issues)
                else:
                    self._validate_permission_object(perm, spec, entry, ctx, issues)

        return issues

    def _validate_permission_object(self, perm, spec, entry: ObjectEntry, ctx: str, issues: List[str]) -> None:
        try:
            object_id = perm.object_id(spec, entry)
        except ResolutionError as exc:
            issues.append(f"{ctx}: {exc}")
            return
        try:
            valid = set(perm.valid_permission_levels(spec, object_id))
        except Exception as exc:  # permission-levels lookup failed; skip level check
            valid = set()
            issues.append(f"{ctx}: could not fetch permission levels ({exc})")
        for acl in entry.acls:
            if valid and acl.permission not in valid:
                issues.append(f"{ctx}: invalid permission '{acl.permission}' (valid: {sorted(valid)})")
            self._validate_principal(acl.principal_type, acl.principal_name, ctx, issues)

    def _validate_secret(self, entry: ObjectEntry, ctx: str, issues: List[str]) -> None:
        try:
            list(self.ws.secrets.list_acls(scope=entry.name))
        except Exception as exc:
            issues.append(f"{ctx}: secret scope not found or unreadable ({exc})")
        for acl in entry.acls:
            if acl.permission not in SECRET_PERMISSION_LEVELS:
                issues.append(f"{ctx}: invalid permission '{acl.permission}' (valid: {list(SECRET_PERMISSION_LEVELS)})")
            self._validate_principal(acl.principal_type, acl.principal_name, ctx, issues)

    def _validate_groups(self, entries: List[GroupEntry], issues: List[str]) -> None:
        _, index = self._group_scim_and_index()
        desired_names = {g.name for g in entries}
        for group in entries:
            for member in group.members:
                if member.member_type == "group" and member.member_name in desired_names:
                    continue  # nested group created this run
                try:
                    index.scim_id(member.member_type, member.member_name)
                except ResolutionError as exc:
                    issues.append(f"groups '{group.name}': member {exc}")

    def _validate_principal(self, ptype: str, name: str, ctx: str, issues: List[str]) -> None:
        try:
            self.ws_index.validate(ptype, name)
        except ResolutionError as exc:
            issues.append(f"{ctx}: {exc}")


def _group_changes(group_plan: GroupPlan) -> List[Change]:
    by_name = {}

    def change_for(name: str) -> Change:
        if name not in by_name:
            by_name[name] = Change(kind="groups", identifier=name)
        return by_name[name]

    for name in group_plan.to_create:
        change_for(name).create = True
    for membership in group_plan.membership:
        change = change_for(membership.name)
        change.adds.extend(membership.add)
        change.removes.extend(membership.remove)
    for name in group_plan.to_delete:
        change_for(name).delete = True
    for name in group_plan.protected_from_delete:
        change_for(name).protected = True

    return list(by_name.values())
