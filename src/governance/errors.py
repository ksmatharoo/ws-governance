"""Exception types for the governance framework."""

from __future__ import annotations


class GovernanceError(Exception):
    """Base class for all framework errors (used for clean CLI exit)."""


class ConfigError(GovernanceError):
    """Raised for malformed or invalid governance config."""


class ResolutionError(GovernanceError):
    """Raised when a configured object or principal cannot be resolved in the workspace."""
