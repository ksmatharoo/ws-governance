"""Dataclass models for governance config files (stdlib only).

The config is deliberately uniform across object types: every governed object is a
list entry with an identifier (``name`` or ``path``, depending on the object type)
and an ``acls`` list. Each ACL entry names exactly one principal (group, user, or
service principal) and the permission level to grant it.

Config files are JSON. Example (``config/environments/prod/jobs.json``)::

    {
      "jobs": [
        {
          "name": "daily-etl",
          "acls": [
            {"group": "data-engineers", "permission": "CAN_MANAGE"},
            {"user": "analyst@corp.com", "permission": "CAN_VIEW"},
            {"service_principal": "etl-sp-app-id", "permission": "CAN_MANAGE_RUN"}
          ]
        }
      ]
    }
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

from governance.errors import ConfigError

# principal fields, in preference order; the ACL-request field mapping is handled
# in the resolve layer. Here we only enforce that exactly one is set.
_PRINCIPAL_FIELDS = ("group", "user", "service_principal")
_ACL_KEYS = set(_PRINCIPAL_FIELDS) | {"permission"}
_OBJECT_KEYS = {"name", "path", "acls"}

# group membership: a member is a user, service principal, or nested group.
_MEMBER_FIELDS = ("user", "service_principal", "group")
_MEMBER_KEYS = set(_MEMBER_FIELDS)
_GROUP_KEYS = {"name", "members"}


def _single_principal(obj: Any, fields: Tuple[str, ...], where: str) -> None:
    """Validate that exactly one of ``fields`` is set on ``obj``."""
    chosen = [f for f in fields if getattr(obj, f)]
    if len(chosen) != 1:
        got = ", ".join(chosen) if chosen else "none"
        raise ConfigError(f"{where} must set exactly one of {', '.join(fields)} (got: {got})")


@dataclass
class AclEntry:
    """A single access-control grant: one principal + one permission level."""

    permission: str
    group: Optional[str] = None
    user: Optional[str] = None
    service_principal: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Any, where: str) -> "AclEntry":
        if not isinstance(data, dict):
            raise ConfigError(f"{where} must be an object, got {type(data).__name__}")
        unknown = set(data) - _ACL_KEYS
        if unknown:
            raise ConfigError(
                f"{where} has unknown field(s): {', '.join(sorted(unknown))}. "
                f"Allowed: {', '.join(sorted(_ACL_KEYS))}"
            )
        entry = cls(
            permission=data.get("permission") or "",
            group=data.get("group"),
            user=data.get("user"),
            service_principal=data.get("service_principal"),
        )
        entry._validate(where)
        return entry

    def _validate(self, where: str) -> None:
        _single_principal(self, _PRINCIPAL_FIELDS, where)
        if not isinstance(self.permission, str) or not self.permission.strip():
            raise ConfigError(f"{where} is missing a non-empty 'permission'")

    @property
    def principal(self) -> Tuple[str, str]:
        """Return ``(principal_type, principal_name)`` for the single set principal."""
        for field_name in _PRINCIPAL_FIELDS:
            value = getattr(self, field_name)
            if value:
                return field_name, value
        raise ConfigError("ACL entry has no principal")  # guarded by _validate

    @property
    def principal_type(self) -> str:
        return self.principal[0]

    @property
    def principal_name(self) -> str:
        return self.principal[1]


@dataclass
class ObjectEntry:
    """One governed object and its desired ACLs.

    ``name``/``path`` are both optional here; the loader enforces the correct
    identifier per object type (using each type's ``id_field``), and singleton
    objects such as ``tokens`` carry neither.
    """

    acls: List[AclEntry] = field(default_factory=list)
    name: Optional[str] = None
    path: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Any, where: str) -> "ObjectEntry":
        if not isinstance(data, dict):
            raise ConfigError(f"{where} must be an object, got {type(data).__name__}")
        unknown = set(data) - _OBJECT_KEYS
        if unknown:
            raise ConfigError(
                f"{where} has unknown field(s): {', '.join(sorted(unknown))}. "
                f"Allowed: {', '.join(sorted(_OBJECT_KEYS))}"
            )
        acls_raw = data.get("acls", [])
        if not isinstance(acls_raw, list):
            raise ConfigError(f"{where}.acls must be a list, got {type(acls_raw).__name__}")
        acls = [AclEntry.from_dict(item, f"{where}.acls[{i}]") for i, item in enumerate(acls_raw)]
        return cls(name=data.get("name"), path=data.get("path"), acls=acls)

    def identifier(self, id_field: Optional[str]) -> str:
        """Human-readable identifier for reports/errors."""
        if id_field == "name":
            return self.name or "<unnamed>"
        if id_field == "path":
            return self.path or "<no-path>"
        return "<singleton>"


@dataclass
class MemberEntry:
    """A single group member: exactly one of user / service principal / nested group."""

    user: Optional[str] = None
    service_principal: Optional[str] = None
    group: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Any, where: str) -> "MemberEntry":
        if not isinstance(data, dict):
            raise ConfigError(f"{where} must be an object, got {type(data).__name__}")
        unknown = set(data) - _MEMBER_KEYS
        if unknown:
            raise ConfigError(
                f"{where} has unknown field(s): {', '.join(sorted(unknown))}. "
                f"Allowed: {', '.join(sorted(_MEMBER_KEYS))}"
            )
        entry = cls(
            user=data.get("user"),
            service_principal=data.get("service_principal"),
            group=data.get("group"),
        )
        _single_principal(entry, _MEMBER_FIELDS, where)
        return entry

    @property
    def member(self) -> Tuple[str, str]:
        """Return ``(member_type, member_name)`` for the single set member field."""
        for field_name in _MEMBER_FIELDS:
            value = getattr(self, field_name)
            if value:
                return field_name, value
        raise ConfigError("member entry has no principal")  # guarded by _single_principal

    @property
    def member_type(self) -> str:
        return self.member[0]

    @property
    def member_name(self) -> str:
        return self.member[1]


@dataclass
class GroupEntry:
    """A workspace/account group whose membership is governed.

    Example (``config/environments/dev/groups.json``)::

        {"groups": [
          {"name": "data-engineers", "members": [
            {"user": "alice@example.com"},
            {"service_principal": "1a2b3c4d-...-etl-runner"},
            {"group": "etl-oncall"}
          ]}
        ]}
    """

    name: str
    members: List[MemberEntry] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Any, where: str) -> "GroupEntry":
        if not isinstance(data, dict):
            raise ConfigError(f"{where} must be an object, got {type(data).__name__}")
        unknown = set(data) - _GROUP_KEYS
        if unknown:
            raise ConfigError(
                f"{where} has unknown field(s): {', '.join(sorted(unknown))}. "
                f"Allowed: {', '.join(sorted(_GROUP_KEYS))}"
            )
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{where} requires a non-empty 'name' (group display name)")
        members_raw = data.get("members", [])
        if not isinstance(members_raw, list):
            raise ConfigError(f"{where}.members must be a list, got {type(members_raw).__name__}")
        members = [MemberEntry.from_dict(item, f"{where}.members[{i}]") for i, item in enumerate(members_raw)]
        return cls(name=name, members=members)

    def identifier(self, id_field: Optional[str] = "name") -> str:
        return self.name


# One loaded config entry is either an object (ACL-governed) or a group (membership-governed).
ConfigEntry = Union[ObjectEntry, GroupEntry]

# An environment's full desired state: object-type key -> list of governed entries.
EnvironmentConfig = Dict[str, List[ConfigEntry]]
