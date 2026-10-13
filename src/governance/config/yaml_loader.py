"""Load governance config from a YAML tree — a parallel to :mod:`governance.config.loader`.

This is deliberately separate code from the JSON loader: the JSON path stays stdlib-only
with zero third-party dependencies, while this path requires **PyYAML** (``pip install
pyyaml``). Both produce the exact same :class:`~governance.config.loader.Settings` and
:class:`~governance.config.models.EnvironmentConfig` objects and run the same validation —
only the on-disk file format differs.

Layout mirrors the JSON tree, with ``.yaml``/``.yml`` files::

    config-yaml/
      settings.yaml                # environments -> auth; protected_principals
      environments/
        <env>/*.yaml               # merged by top-level object-type key

Because the parsed structure is identical to the JSON one, this module reuses the model
constructors and the merged-config validator from the JSON loader rather than duplicating
them; that keeps the two formats behaviourally identical by construction.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from governance.config.loader import Settings, _validate_merged
from governance.config.models import EnvironmentConfig
from governance.errors import ConfigError

try:  # PyYAML is an optional dependency; only this loader needs it.
    import yaml

    _HAVE_YAML = True
except ImportError:  # pragma: no cover - exercised only where PyYAML is absent
    yaml = None
    _HAVE_YAML = False


_SUFFIXES = (".yaml", ".yml")


def _require_yaml() -> None:
    if not _HAVE_YAML:
        raise ConfigError(
            "PyYAML is required to load YAML config (pip install pyyaml). "
            "Use the JSON loader (governance.config.loader) for a dependency-free path."
        )


def _read_yaml(path: Path) -> Any:
    _require_yaml()
    try:
        with path.open("r", encoding="utf-8") as fh:
            text = fh.read()
        if not text.strip():
            return None
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:  # type: ignore[union-attr]
        raise ConfigError(f"Failed to parse YAML {path}: {exc}") from exc


def _settings_path(config_root: Path) -> Path:
    for suffix in _SUFFIXES:
        candidate = config_root / f"settings{suffix}"
        if candidate.exists():
            return candidate
    raise ConfigError(f"settings.yaml (or .yml) not found in {config_root}")


def load_settings(config_root: Path) -> Settings:
    """Read ``settings.yaml`` and build a validated :class:`Settings` (same as JSON)."""
    config_root = Path(config_root)
    data = _read_yaml(_settings_path(config_root))
    return Settings.from_dict(data)


def load_environment(config_root: Path, env: str) -> EnvironmentConfig:
    """Load, merge, and validate all YAML object configs for one environment."""
    _require_yaml()
    env_dir = Path(config_root) / "environments" / env
    if not env_dir.is_dir():
        raise ConfigError(f"No config directory for environment '{env}' at {env_dir}")

    # Skip "_"-prefixed files (e.g. _inventory.yaml): meta files, not object config.
    files = sorted(
        p for p in env_dir.iterdir() if p.suffix in _SUFFIXES and not p.name.startswith("_")
    )
    if not files:
        raise ConfigError(f"No .yaml/.yml config files found in {env_dir}")

    # Merge top-level keys across files, tracking origin for good error messages
    # (identical rules to the JSON loader).
    merged: Dict[str, Any] = {}
    origin: Dict[str, Path] = {}
    for path in files:
        doc = _read_yaml(path)
        if doc is None:
            continue
        if not isinstance(doc, dict):
            raise ConfigError(f"{path} must contain a mapping of object-type -> list")
        for key, value in doc.items():
            if key in merged:
                raise ConfigError(
                    f"Object type '{key}' defined in both {origin[key].name} and "
                    f"{path.name}; define each object type in a single file."
                )
            merged[key] = value
            origin[key] = path

    return _validate_merged(merged, origin)
