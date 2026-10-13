"""Uniform result types produced by handlers and rendered by the report layer.

A :class:`Change` describes what reconcile would do (plan) or did (apply) to one
governed object or group. The engine collects them into an :class:`EnvironmentPlan`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Change:
    kind: str
    """Object-type key, e.g. ``clusters`` or ``groups``."""

    identifier: str
    """Human-readable object identifier (name / path / scope / group name)."""

    adds: List[str] = field(default_factory=list)
    """Grants/members that would be added (human-readable)."""

    removes: List[str] = field(default_factory=list)
    """Grants/members that would be removed (drift)."""

    unchanged: int = 0
    create: bool = False
    delete: bool = False
    protected: bool = False
    """Informational: an object skipped from deletion because it is protected/built-in."""

    error: Optional[str] = None
    """Set when the object could not be resolved/processed (shown, not fatal to the run)."""

    @property
    def changed(self) -> bool:
        return bool(self.adds or self.removes or self.create or self.delete)


@dataclass
class EnvironmentPlan:
    env: str
    changes: List[Change] = field(default_factory=list)

    @property
    def changed(self) -> List[Change]:
        return [c for c in self.changes if c.changed]

    @property
    def errors(self) -> List[Change]:
        return [c for c in self.changes if c.error]

    @property
    def has_changes(self) -> bool:
        return bool(self.changed)
