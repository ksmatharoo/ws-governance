"""Static (offline) config validation for CI.

Unlike :meth:`governance.engine.GovernanceEngine.validate` (which contacts the live
workspace to resolve objects and principals), this module validates config **values**
without any network access — suitable for a CI gate on every commit.

It enforces that configs hold **constrained values, not free-form text**:

* permission levels come from the allowed enum for that object type,
* principals are well-formed (users are emails, service principals are UUIDs,
  group names are clean single-line strings),
* object identifiers are clean (names single-line; paths absolute),
* no empty / whitespace-only / control-character strings anywhere.

Structural validation (unknown keys, wrong types, exactly-one-principal, id-field
rules, duplicates) is already enforced by the loader; the linter runs after a
successful load and reports *all* value issues it finds.
"""

from __future__ import annotations

import re
from typing import List, Optional

from governance.config.models import AclEntry, EnvironmentConfig, GroupEntry, MemberEntry, ObjectEntry
from governance.objecttypes import get_spec

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")  # control chars incl. newlines/tabs


# --------------------------------------------------------------------------- #
# primitive value checks
# --------------------------------------------------------------------------- #
def is_clean(value) -> bool:
    """A non-empty, single-line string with no leading/trailing whitespace or control chars."""
    return (
        isinstance(value, str)
        and value == value.strip()
        and value != ""
        and not _CONTROL_RE.search(value)
    )


def is_email(value) -> bool:
    return is_clean(value) and bool(_EMAIL_RE.match(value))


def is_uuid(value) -> bool:
    return is_clean(value) and bool(_UUID_RE.match(value))


def is_abs_path(value) -> bool:
    return is_clean(value) and value.startswith("/")


# --------------------------------------------------------------------------- #
# principal format (shared by ACLs and group members)
# --------------------------------------------------------------------------- #
def principal_issue(ptype: str, name: str) -> Optional[str]:
    """Return a message if the principal value is malformed, else ``None``."""
    if ptype == "user":
        if not is_email(name):
            return f"user '{name}' is not a valid email address"
    elif ptype == "service_principal":
        if not is_uuid(name):
            return f"service_principal '{name}' is not a valid UUID (application id)"
    elif ptype == "group":
        if not is_clean(name):
            return f"group name '{name}' must be a clean, single-line, non-empty string"
    return None


# --------------------------------------------------------------------------- #
# environment linting
# --------------------------------------------------------------------------- #
def lint_environment(config: EnvironmentConfig) -> List[str]:
    """Validate a loaded environment config's values. Returns all issues found."""
    issues: List[str] = []
    for key, entries in config.items():
        spec = get_spec(key)
        if spec.kind == "groups":
            for group in entries:
                _lint_group(group, issues)
        else:
            for entry in entries:
                _lint_object(spec.key, spec.id_field, spec.allowed_permissions, entry, issues)
    return issues


def _lint_object(key, id_field, allowed, entry: ObjectEntry, issues: List[str]) -> None:
    ident = entry.identifier(id_field)
    ctx = f"{key} '{ident}'"

    if id_field == "name" and not is_clean(entry.name):
        issues.append(f"{ctx}: name must be a clean, single-line, non-empty string")
    if id_field == "path" and not is_abs_path(entry.path):
        issues.append(f"{ctx}: path must be a clean absolute path starting with '/'")

    for i, acl in enumerate(entry.acls):
        _lint_permission(f"{ctx} acls[{i}]", acl, allowed, issues)
        msg = principal_issue(acl.principal_type, acl.principal_name)
        if msg:
            issues.append(f"{ctx} acls[{i}]: {msg}")


def _lint_permission(ctx, acl: AclEntry, allowed, issues: List[str]) -> None:
    if allowed is None:
        return
    if acl.permission not in allowed:
        issues.append(
            f"{ctx}: invalid permission '{acl.permission}' (allowed: {sorted(allowed)})"
        )


def _lint_group(group: GroupEntry, issues: List[str]) -> None:
    ctx = f"groups '{group.name}'"
    if not is_clean(group.name):
        issues.append(f"{ctx}: group name must be a clean, single-line, non-empty string")
    for i, member in enumerate(group.members):
        msg = principal_issue(member.member_type, member.member_name)
        if msg:
            issues.append(f"{ctx} members[{i}]: {msg}")
