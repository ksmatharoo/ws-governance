"""Compute the difference between live workspace state and desired config.

The core is deliberately pure (operates on plain sets/tuples) so it can be unit
tested without a workspace. Thin normalizer helpers convert SDK response models
into those plain forms.

ACL model
---------
A *grant* is a ``(principal_type, principal_name, permission_level)`` triple. Because
the Permissions API ``set()`` call replaces all direct permissions at once, the
level-aware triple is the natural unit: changing a principal's level shows up as one
removed grant + one added grant.

Reconcile is full drift-removal: the *effective desired* set is what ``set()`` will
send. ``protected_principals`` are preserved by folding their current grants into the
effective desired set, so they are never revoked (in addition to owner/admins, which
the platform always retains).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Set, Tuple

from governance.config.models import AclEntry, MemberEntry

# (principal_type, principal_name, permission_level)
Grant = Tuple[str, str, str]

# permission levels that are never managed as direct grants (platform-owned).
_UNMANAGED_LEVELS = frozenset({"IS_OWNER"})


# --------------------------------------------------------------------------- #
# ACL diff
# --------------------------------------------------------------------------- #
def desired_grants(entries: Iterable[AclEntry]) -> Set[Grant]:
    """Normalize config ACL entries into a grant set."""
    return {(e.principal_type, e.principal_name, e.permission) for e in entries}


def current_grants(object_permissions) -> Set[Grant]:
    """Normalize an ``ObjectPermissions`` (from ``permissions.get``) into a grant set.

    Only *direct* (non-inherited), non-owner permissions are considered, because those
    are the ones ``set()`` manages.
    """
    grants: Set[Grant] = set()
    for acr in getattr(object_permissions, "access_control_list", None) or []:
        principal = _principal_of(acr)
        if principal is None:
            continue
        ptype, name = principal
        for perm in acr.all_permissions or []:
            if perm.inherited:
                continue
            level = perm.permission_level.value if perm.permission_level else None
            if not level or level in _UNMANAGED_LEVELS:
                continue
            grants.add((ptype, name, level))
    return grants


def _principal_of(acr) -> Tuple[str, str] | None:
    if acr.group_name:
        return ("group", acr.group_name)
    if acr.user_name:
        return ("user", acr.user_name)
    if acr.service_principal_name:
        return ("service_principal", acr.service_principal_name)
    return None


@dataclass
class AclDiff:
    to_add: Set[Grant]
    to_remove: Set[Grant]
    unchanged: Set[Grant]
    effective_desired: Set[Grant]

    @classmethod
    def compute(
        cls,
        current: Set[Grant],
        desired: Set[Grant],
        protected_principals: Iterable[str] = (),
    ) -> "AclDiff":
        protected = set(protected_principals)
        preserved = {g for g in current if g[1] in protected}
        effective = set(desired) | preserved
        return cls(
            to_add=effective - current,
            to_remove=current - effective,
            unchanged=current & effective,
            effective_desired=effective,
        )

    @property
    def changed(self) -> bool:
        return bool(self.to_add or self.to_remove)


# --------------------------------------------------------------------------- #
# Group membership diff
# --------------------------------------------------------------------------- #
def desired_member_specs(members: Iterable[MemberEntry]) -> List[Tuple[str, str]]:
    """Return ``(member_type, member_name)`` pairs for config members."""
    return [(m.member_type, m.member_name) for m in members]


def current_member_ids(group) -> Set[str]:
    """SCIM member ids currently in a ``Group``."""
    return {m.value for m in (group.members or []) if m.value}


@dataclass
class MembershipDiff:
    to_add: Set[str]
    to_remove: Set[str]
    unchanged: Set[str]

    @classmethod
    def compute(cls, current_ids: Set[str], desired_ids: Set[str]) -> "MembershipDiff":
        current_ids = set(current_ids)
        desired_ids = set(desired_ids)
        return cls(
            to_add=desired_ids - current_ids,
            to_remove=current_ids - desired_ids,
            unchanged=current_ids & desired_ids,
        )

    @property
    def changed(self) -> bool:
        return bool(self.to_add or self.to_remove)
