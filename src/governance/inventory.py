"""Environment inventory — the resources/identities provisioned in an environment,
used to validate governance config **offline** (no workspace needed).

`governance lint` normally checks config *values* (permission enums, principal formats).
With an inventory it also checks *existence*: that every object an ACL targets, and every
user / service principal / externally-provisioned group a config references, actually
exists in the environment. This is the same class of check `validate` runs against a live
workspace, but resolved against a static manifest instead — so it can gate CI/local runs
with no credentials.

Convention: a file named ``_inventory.yaml`` (or ``.yml`` / ``.json``) lives in the
environment's config directory. The leading underscore keeps it out of the object-config
merge (the YAML loader skips ``_``-prefixed files). YAML inventories require PyYAML; JSON
inventories need nothing beyond the standard library.

Manifest shape::

    environment: dev
    identities:
      users:              [ {user_name: alice@corp.com} | "alice@corp.com" ]
      service_principals: [ {application_id: "...", display_name: etl-runner} ]
      groups:             [ {name: bi-viewers} ]      # externally provisioned only
    resources:
      clusters:    [ {name: shared-interactive} | "shared-interactive" ]
      directories: [ {path: /Workspace/Shared/x} | "/Workspace/Shared/x" ]
      ...
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from governance.config.models import EnvironmentConfig, GroupEntry, ObjectEntry
from governance.errors import ConfigError
from governance.objecttypes import get_spec

_YAML_SUFFIXES = (".yaml", ".yml")
_INVENTORY_STEMS = ("_inventory",)


def _as_identifier(item: Any, keys: tuple, where: str) -> Optional[str]:
    """A manifest entry is either a bare string or a dict carrying one of ``keys``."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for k in keys:
            if item.get(k):
                return str(item[k])
        return None
    raise ConfigError(f"{where}: expected a string or object, got {type(item).__name__}")


@dataclass
class Inventory:
    environment: Optional[str] = None
    users: Set[str] = field(default_factory=set)
    # both application_id and display_name go in, so config may reference SPs either way
    service_principals: Set[str] = field(default_factory=set)
    groups: Set[str] = field(default_factory=set)  # provisioned AND assigned to this workspace
    # groups that exist in the account but are NOT yet assigned to this workspace
    # (identities.groups entries marked `assigned: false`). Referencing one is a
    # prerequisite gap: the group must be assigned to the workspace before it can be granted
    # permissions. The framework reports this; assigning is an account-admin action.
    unassigned_groups: Set[str] = field(default_factory=set)
    resources: Dict[str, Set[str]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Any) -> "Inventory":
        if not isinstance(data, dict):
            raise ConfigError("inventory must be a mapping at the top level")
        inv = cls(environment=data.get("environment"))

        identities = data.get("identities") or {}
        if not isinstance(identities, dict):
            raise ConfigError("inventory 'identities' must be a mapping")
        for item in identities.get("users") or []:
            name = _as_identifier(item, ("user_name", "name"), "identities.users")
            if name:
                inv.users.add(name)
        for item in identities.get("service_principals") or []:
            if isinstance(item, dict):
                for k in ("application_id", "display_name", "name"):
                    if item.get(k):
                        inv.service_principals.add(str(item[k]))
            elif isinstance(item, str):
                inv.service_principals.add(item)
        for item in identities.get("groups") or []:
            name = _as_identifier(item, ("name",), "identities.groups")
            if not name:
                continue
            # `assigned: false` marks an account group not yet attached to this workspace.
            if isinstance(item, dict) and item.get("assigned") is False:
                inv.unassigned_groups.add(name)
            else:
                inv.groups.add(name)

        resources = data.get("resources") or {}
        if not isinstance(resources, dict):
            raise ConfigError("inventory 'resources' must be a mapping")
        for key, items in resources.items():
            if items is None or isinstance(items, dict):
                # e.g. tokens: {note: ...} — singleton, no identifiers to track
                inv.resources.setdefault(key, set())
                continue
            if not isinstance(items, list):
                raise ConfigError(f"inventory resources.{key} must be a list")
            bucket = inv.resources.setdefault(key, set())
            for item in items:
                ident = _as_identifier(item, ("name", "path"), f"resources.{key}")
                if ident:
                    bucket.add(ident)
        return inv


def find_inventory(env_dir: Path) -> Optional[Path]:
    """Locate the ``_inventory.*`` manifest in an environment dir, if present."""
    for stem in _INVENTORY_STEMS:
        for suffix in (*_YAML_SUFFIXES, ".json"):
            candidate = env_dir / f"{stem}{suffix}"
            if candidate.exists():
                return candidate
    return None


def load_inventory(path: Path) -> Inventory:
    """Parse an inventory manifest (YAML needs PyYAML; JSON is stdlib)."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix in _YAML_SUFFIXES:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - offline-only path
            raise ConfigError(
                f"PyYAML is required to read a YAML inventory ({path}); "
                f"pip install pyyaml, or use a .json inventory."
            ) from exc
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ConfigError(f"Failed to parse YAML inventory {path}: {exc}") from exc
    else:
        try:
            data = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Failed to parse JSON inventory {path}: {exc}") from exc
    return Inventory.from_dict(data)


def check(config: EnvironmentConfig, inventory: Inventory) -> List[str]:
    """Return existence issues: config references not backed by the inventory."""
    issues: List[str] = []

    # Groups the framework creates this run are valid targets even if not pre-provisioned.
    config_groups: Set[str] = set()
    for key, entries in config.items():
        if get_spec(key).kind == "groups":
            config_groups |= {g.name for g in entries}
    known_groups = config_groups | inventory.groups

    def check_principal(ptype: str, name: str, ctx: str) -> None:
        if ptype == "group":
            if name in known_groups:
                return
            if name in inventory.unassigned_groups:
                issues.append(
                    f"{ctx}: group '{name}' exists in the account but is not assigned to this "
                    f"workspace — assign it (account admin) before granting permissions"
                )
            else:
                issues.append(f"{ctx}: group '{name}' is neither defined in config nor in the inventory")
        elif ptype == "user":
            if name not in inventory.users:
                issues.append(f"{ctx}: user '{name}' is not in the inventory")
        elif ptype == "service_principal":
            if name not in inventory.service_principals:
                issues.append(f"{ctx}: service principal '{name}' is not in the inventory")

    for key, entries in config.items():
        spec = get_spec(key)
        if spec.kind == "groups":
            for group in entries:  # type: GroupEntry
                for member in group.members:
                    check_principal(member.member_type, member.member_name, f"groups '{group.name}' member")
            continue

        available = inventory.resources.get(key, set())
        for entry in entries:  # type: ObjectEntry
            ident = entry.identifier(spec.id_field)
            if spec.id_field is not None and ident not in available:
                issues.append(f"{key} '{ident}': not found in the inventory for this environment")
            for acl in entry.acls:
                check_principal(acl.principal_type, acl.principal_name, f"{key} '{ident}'")

    return issues
