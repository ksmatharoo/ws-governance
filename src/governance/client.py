"""Build a Databricks ``WorkspaceClient`` from environment settings.

Uses the SDK's unified authentication. Preference order:

1. ``profile`` -> the named ``.databrickscfg`` profile (recommended for local CLI).
2. ``host``    -> explicit workspace URL; credentials come from env vars / OAuth
                  (``DATABRICKS_TOKEN``, ``DATABRICKS_CLIENT_ID``/``SECRET``, etc.).
3. neither     -> default SDK auth (env vars, then ``DEFAULT`` profile).

No credentials are ever read from or written to the governance config.
"""

from __future__ import annotations

from databricks.sdk import AccountClient, WorkspaceClient

from governance.config.loader import AccountSettings, EnvironmentSettings
from governance.errors import GovernanceError


def make_client(env_settings: EnvironmentSettings) -> WorkspaceClient:
    try:
        if env_settings.profile:
            return WorkspaceClient(profile=env_settings.profile)
        if env_settings.host:
            return WorkspaceClient(host=env_settings.host)
        return WorkspaceClient()
    except Exception as exc:  # SDK config/auth construction error
        raise GovernanceError(
            f"Failed to build Databricks client ({env_settings.describe}): {exc}"
        ) from exc


def make_account_client(account_settings: AccountSettings) -> AccountClient:
    """Build an ``AccountClient`` for account-level (SCIM) group governance.

    Both ``WorkspaceClient`` and ``AccountClient`` expose identically-shaped
    ``.groups`` / ``.users`` / ``.service_principals`` SCIM APIs, so either can back
    an :class:`~governance.resolve.IdentityIndex`.
    """
    try:
        if account_settings.profile:
            return AccountClient(profile=account_settings.profile)
        kwargs = {}
        if account_settings.host:
            kwargs["host"] = account_settings.host
        if account_settings.account_id:
            kwargs["account_id"] = account_settings.account_id
        return AccountClient(**kwargs)
    except Exception as exc:
        raise GovernanceError(
            f"Failed to build Databricks account client ({account_settings.describe}): {exc}"
        ) from exc


def whoami(client: WorkspaceClient) -> str:
    """Return the authenticated principal (used to confirm connectivity)."""
    try:
        me = client.current_user.me()
        return me.user_name or me.display_name or "<unknown>"
    except Exception as exc:
        raise GovernanceError(f"Could not reach the workspace / authenticate: {exc}") from exc
