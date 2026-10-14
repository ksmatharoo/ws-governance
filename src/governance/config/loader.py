"""Load and validate governance config from the on-disk JSON tree (stdlib only).

Layout::

    config/
      settings.json                # environments -> auth; protected_principals
      environments/
        <env>/*.json               # one or more files; merged by top-level key

Config files under an environment folder are merged by their top-level object-type
key, so file organisation is a matter of taste (e.g. ``compute.json`` may hold both
``clusters`` and ``warehouses``). A given object-type key may only appear once
across the environment's files.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from governance.config.models import EntitlementEntry, EnvironmentConfig, GroupEntry, ObjectEntry
from governance.errors import ConfigError
from governance.objecttypes import all_keys, get_spec


# --------------------------------------------------------------------------- #
# settings.json
# --------------------------------------------------------------------------- #
_GROUP_SCOPES = ("workspace", "account")


@dataclass
class AccountSettings:
    """Account-console connection, used when any environment governs groups at account scope."""

    account_id: Optional[str] = None
    host: Optional[str] = None
    profile: Optional[str] = None

    @property
    def describe(self) -> str:
        if self.profile:
            return f"account profile={self.profile}"
        parts = []
        if self.host:
            parts.append(f"host={self.host}")
        if self.account_id:
            parts.append(f"account_id={self.account_id}")
        return "account " + (", ".join(parts) if parts else "default SDK auth")


@dataclass
class EnvironmentSettings:
    profile: Optional[str] = None
    """`.databrickscfg` profile name (preferred for local CLI use)."""

    host: Optional[str] = None
    """Workspace URL; used with SDK unified auth (env vars / OAuth) when no profile."""

    group_scope: str = "workspace"
    """Where group membership is governed: ``workspace`` (WorkspaceClient) or ``account`` (AccountClient)."""

    require_ci: bool = False
    """If true, ``apply`` refuses to run unless it detects a CI context (env var
    ``GOVERNANCE_CI=1``). Use for higher environments (e.g. prod) so they can only be
    reconciled by the pipeline, never from a laptop. Lower/test envs leave this off and
    apply locally via a ``.databrickscfg`` profile. ``lint``/``validate``/``plan`` are
    unaffected and remain runnable anywhere."""

    @property
    def describe(self) -> str:
        if self.profile:
            return f"profile={self.profile}"
        if self.host:
            return f"host={self.host}"
        return "default SDK auth"


@dataclass
class Settings:
    environments: Dict[str, EnvironmentSettings] = field(default_factory=dict)
    protected_principals: List[str] = field(default_factory=list)
    """Principal names never revoked during reconcile / deleted (plus owner/admins)."""

    account: Optional[AccountSettings] = None
    """Account-console connection; required if any environment uses ``group_scope: account``."""

    allow_group_deletion: bool = False
    """Master switch for the group delete phase. Off by default (destructive);
    turn on to let reconcile delete workspace/account groups not present in config.
    A per-run CLI flag can further disable it."""

    def environment(self, env: str) -> EnvironmentSettings:
        if env not in self.environments:
            known = ", ".join(sorted(self.environments)) or "<none>"
            raise ConfigError(f"Environment '{env}' not defined in settings.json (known: {known})")
        return self.environments[env]

    @classmethod
    def from_dict(cls, data: Any) -> "Settings":
        if not isinstance(data, dict):
            raise ConfigError("settings.json must contain an object at the top level")
        allowed = {"environments", "protected_principals", "account", "allow_group_deletion"}
        unknown = set(data) - allowed
        if unknown:
            raise ConfigError(f"settings.json has unknown field(s): {', '.join(sorted(unknown))}")

        envs_raw = data.get("environments", {})
        if not isinstance(envs_raw, dict):
            raise ConfigError("settings.json 'environments' must be an object")
        environments: Dict[str, EnvironmentSettings] = {}
        for name, env_data in envs_raw.items():
            if not isinstance(env_data, dict):
                raise ConfigError(f"environments.{name} must be an object")
            unknown_env = set(env_data) - {"profile", "host", "group_scope", "require_ci"}
            if unknown_env:
                raise ConfigError(
                    f"environments.{name} has unknown field(s): {', '.join(sorted(unknown_env))}"
                )
            scope = env_data.get("group_scope", "workspace")
            if scope not in _GROUP_SCOPES:
                raise ConfigError(
                    f"environments.{name}.group_scope must be one of {_GROUP_SCOPES}, got '{scope}'"
                )
            require_ci = env_data.get("require_ci", False)
            if not isinstance(require_ci, bool):
                raise ConfigError(f"environments.{name}.require_ci must be true or false")
            environments[name] = EnvironmentSettings(
                profile=env_data.get("profile"),
                host=env_data.get("host"),
                group_scope=scope,
                require_ci=require_ci,
            )

        protected = data.get("protected_principals", [])
        if not isinstance(protected, list) or not all(isinstance(p, str) for p in protected):
            raise ConfigError("settings.json 'protected_principals' must be a list of strings")

        account = None
        acct_raw = data.get("account")
        if acct_raw is not None:
            if not isinstance(acct_raw, dict):
                raise ConfigError("settings.json 'account' must be an object")
            unknown_acct = set(acct_raw) - {"account_id", "host", "profile"}
            if unknown_acct:
                raise ConfigError(f"settings.json 'account' has unknown field(s): {', '.join(sorted(unknown_acct))}")
            account = AccountSettings(
                account_id=acct_raw.get("account_id"),
                host=acct_raw.get("host"),
                profile=acct_raw.get("profile"),
            )

        allow_del = data.get("allow_group_deletion", False)
        if not isinstance(allow_del, bool):
            raise ConfigError("settings.json 'allow_group_deletion' must be true or false")

        # Validate that account-scope environments have an account block.
        for name, env in environments.items():
            if env.group_scope == "account" and account is None:
                raise ConfigError(
                    f"environments.{name} uses group_scope 'account' but no 'account' block is defined"
                )

        return cls(
            environments=environments,
            protected_principals=protected,
            account=account,
            allow_group_deletion=allow_del,
        )


def load_settings(config_root: Path) -> Settings:
    path = Path(config_root) / "settings.json"
    if not path.exists():
        raise ConfigError(f"settings.json not found at {path}")
    return Settings.from_dict(_read_json(path))


# --------------------------------------------------------------------------- #
# environment object configs
# --------------------------------------------------------------------------- #
def load_environment(config_root: Path, env: str) -> EnvironmentConfig:
    """Load, merge, and validate all object configs for one environment."""
    env_dir = Path(config_root) / "environments" / env
    if not env_dir.is_dir():
        raise ConfigError(f"No config directory for environment '{env}' at {env_dir}")

    # Skip "_"-prefixed files (e.g. _inventory.json): meta files, not object config.
    files = sorted(
        p for p in env_dir.iterdir() if p.suffix == ".json" and not p.name.startswith("_")
    )
    if not files:
        raise ConfigError(f"No .json config files found in {env_dir}")

    # Merge top-level keys across files, tracking origin for good error messages.
    merged: Dict[str, Any] = {}
    origin: Dict[str, Path] = {}
    for path in files:
        doc = _read_json(path)
        if doc is None:
            continue
        if not isinstance(doc, dict):
            raise ConfigError(f"{path} must contain an object mapping object-type -> list")
        for key, value in doc.items():
            if key in merged:
                raise ConfigError(
                    f"Object type '{key}' defined in both {origin[key].name} and "
                    f"{path.name}; define each object type in a single file."
                )
            merged[key] = value
            origin[key] = path

    return _validate_merged(merged, origin)


def _validate_merged(merged: Dict[str, Any], origin: Dict[str, Path]) -> EnvironmentConfig:
    result: EnvironmentConfig = {}
    known = set(all_keys())

    for key, value in merged.items():
        where = origin[key].name
        if key not in known:
            raise ConfigError(
                f"Unknown object type '{key}' in {where}. Known types: {', '.join(all_keys())}"
            )
        spec = get_spec(key)
        if not isinstance(value, list):
            raise ConfigError(f"'{key}' in {where} must be a list, got {type(value).__name__}")

        if spec.kind == "groups":
            result[key] = _parse_groups(spec.key, value, where)
        elif spec.kind == "entitlements":
            result[key] = _parse_entitlements(spec.key, value, where)
        else:
            result[key] = _parse_objects(spec.key, spec.id_field, value, where)

    return result


def _parse_objects(key: str, id_field: Optional[str], value: list, where: str) -> List[ObjectEntry]:
    entries: List[ObjectEntry] = []
    for idx, item in enumerate(value):
        entry = ObjectEntry.from_dict(item, f"{where}:{key}[{idx}]")
        _check_identifier(key, id_field, entry, idx, where)
        entries.append(entry)
    _check_unique_identifiers(key, id_field, entries, where)
    return entries


def _parse_groups(key: str, value: list, where: str) -> List[GroupEntry]:
    entries: List[GroupEntry] = []
    seen = set()
    for idx, item in enumerate(value):
        group = GroupEntry.from_dict(item, f"{where}:{key}[{idx}]")
        if group.name in seen:
            raise ConfigError(f"Duplicate group name '{group.name}' for '{key}' in {where}")
        seen.add(group.name)
        entries.append(group)
    return entries


def _parse_entitlements(key: str, value: list, where: str) -> List[EntitlementEntry]:
    entries: List[EntitlementEntry] = []
    seen = set()
    for idx, item in enumerate(value):
        entry = EntitlementEntry.from_dict(item, f"{where}:{key}[{idx}]")
        if entry.principal in seen:
            raise ConfigError(f"Duplicate principal '{entry.identifier()}' for '{key}' in {where}")
        seen.add(entry.principal)
        entries.append(entry)
    return entries


def _check_identifier(key: str, id_field: Optional[str], entry: ObjectEntry, idx: int, where: str) -> None:
    if id_field == "name":
        if not entry.name:
            raise ConfigError(f"'{key}[{idx}]' in {where} requires a 'name'")
        if entry.path:
            raise ConfigError(f"'{key}[{idx}]' in {where} must not set 'path' (identified by name)")
    elif id_field == "path":
        if not entry.path:
            raise ConfigError(f"'{key}[{idx}]' in {where} requires a 'path'")
        if entry.name:
            raise ConfigError(f"'{key}[{idx}]' in {where} must not set 'name' (identified by path)")
    else:  # singleton, e.g. tokens
        if entry.name or entry.path:
            raise ConfigError(f"'{key}[{idx}]' in {where} is a singleton and takes no 'name'/'path'")


def _check_unique_identifiers(key: str, id_field: Optional[str], entries: List[ObjectEntry], where: str) -> None:
    if id_field is None:
        if len(entries) > 1:
            raise ConfigError(f"'{key}' in {where} is a singleton but has {len(entries)} entries")
        return
    seen = set()
    for entry in entries:
        ident = entry.identifier(id_field)
        if ident in seen:
            raise ConfigError(f"Duplicate {id_field} '{ident}' for '{key}' in {where}")
        seen.add(ident)


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            text = fh.read()
        if not text.strip():
            return None
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Failed to parse JSON {path}: {exc}") from exc
