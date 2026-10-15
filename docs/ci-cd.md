# CI/CD and the two-mode execution model

The framework runs the same binary in two modes, chosen **per environment** in
`settings.yaml`. Lower/test environments apply locally; higher environments apply only
through a pipeline.

| Mode | Environment example | Auth | Who applies | `apply` allowed from |
|---|---|---|---|---|
| **Local** | `govtest` | `.databrickscfg` `profile` | a human admin | anywhere |
| **CI-only** | `prod` | `host` + service-principal OAuth (env vars) | the pipeline SP | the pipeline only |

`lint`, `validate`, and `plan` run **anywhere** in both modes — developers always get a
full local preview of prod. Only `apply` to a CI-only environment is restricted.

> This repo ships **no pipeline definition** (no GitHub Actions / Azure DevOps files). It
> provides the wheel, the CLI, and the workspace job in [`../deploy/`](../deploy/README.md);
> wire them into your CI system as described below.

## How the mode is selected

Nothing changes about the commands — the environment's settings decide who authenticates
and where apply may run:

```yaml
environments:
  govtest:
    profile: govtest
  prod:
    host: https://prod.azuredatabricks.net
    require_ci: true
```

- **`profile`** → local `.databrickscfg` auth (preferred for dev/test).
- **`host`** (no profile) → the SDK reads service-principal creds from env vars
  (`DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET`), injected by CI.
- **neither** → SDK default auth; in a workspace job that is the job's run-as identity
  (this is how [`../deploy/run_governance.py`](../deploy/run_governance.py) runs).
- **`require_ci: true`** → `apply` refuses unless the env var `GOVERNANCE_CI=1` is set
  (only the apply pipeline sets it). Defense-in-depth on top of branch protection.

## The flow

```
edit config ─▶ open PR ─▶ PR pipeline (pytest + lint + validate + plan)
                              │  reviewers see the exact diff
                              ▼
                     code-owner review + branch protection
                              │
                         merge to main
                              ▼
          apply pipeline (apply --yes as SP, GOVERNANCE_CI=1, per-env lock)
             or: run the workspace job with action=apply
                              ▼
             workspace reconciled + audit log kept as artifact
```

Every governance command in the pipeline takes
`--config config-govtest-yaml --format yaml --env <env>`.

## Provenance / audit trail

Every applied run writes `audit-logs/<env>-<timestamp>.audit.json` (or under
`--audit-dir`) stamped with:

- `actor` — the acting principal (`whoami`): a human admin locally, the SP in CI.
- `git_sha` — from `GOVERNANCE_GIT_SHA` or `GITHUB_SHA`.
- `ci_run_url` — the pipeline run, from `GOVERNANCE_CI_RUN_URL` or the GitHub run vars.

On non-GitHub CI, set `GOVERNANCE_GIT_SHA` and `GOVERNANCE_CI_RUN_URL` yourself. This
ties a live-workspace change back to the reviewed commit and the pipeline run. Keep the
file as a build artifact; shipping it to immutable storage or a system table is the
recommended next step. The workspace job writes it to
`/Workspace/Shared/gov-test/audit-logs`.

## Prerequisites (out of band)

1. **Provision the automation service principal** with workspace-admin (and account-admin
   if any env uses `group_scope: account`), via Terraform/IdP.
2. **Add that SP to `protected_principals`** so reconcile never strips its own access.
   `apply` prints a warning if the acting identity is not protected.
3. **Store SP credentials as pipeline secrets** (`DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID`,
   `DATABRICKS_CLIENT_SECRET`) scoped to the prod environment. Prefer OIDC federation
   over a long-lived client secret where the workspace supports it.
4. **Enable branch protection on `main`**: required PR review, required status checks
   (`lint`, `plan`), no direct pushes. This is what actually enforces config-author ≠
   applier; the `require_ci` flag is the in-tool backstop.
5. **Define code owners** for `config-govtest-yaml/` with the real platform-team slug.

## Multiple workspaces

Each higher environment gets its own entry in `settings.yaml` (its own `host`) and its own
secret set. Run the apply stage as a matrix over environments, or one pipeline per
workspace, so each reconciles independently under its own concurrency lock.
