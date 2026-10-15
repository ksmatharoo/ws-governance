# Config Schema & Value Reference

The configs are **constrained**, not free-form: permission levels come from a fixed enum
per object type, and principals must be well-formed. This is enforced offline by
`governance lint` (see below) so CI can gate every commit **without a workspace**.

Templates for each config set live in
[`config-govtest-yaml/templates/`](../config-govtest-yaml/templates/).

> **YAML in this repo.** The config here is YAML (`--format yaml`, loaded by
> `governance.config.yaml_loader`, which needs PyYAML — installed via `requirements.txt`).
> The CLI also accepts JSON (`--format json`, the CLI default) with exactly the same
> schema; the YAML loader reuses the JSON loader's models and validation and differs only
> in the parse step.

## Allowed permission levels per object type

| Object type | Identifier | `kind` | Allowed permission values |
|---|---|---|---|
| `groups` | `name` | groups | — (membership only, no permissions) |
| `entitlements` | principal (`group` / `user` / `service_principal`) | entitlements | `workspace-access`, `workspace-consume`, `databricks-sql-access`, `allow-cluster-create`, `allow-instance-pool-create` |
| `clusters` | `name` | permissions | `CAN_ATTACH_TO`, `CAN_RESTART`, `CAN_MANAGE` |
| `cluster-policies` | `name` | permissions | `CAN_USE` |
| `instance-pools` | `name` | permissions | `CAN_ATTACH_TO`, `CAN_MANAGE` |
| `warehouses` | `name` | permissions | `CAN_USE`, `CAN_MONITOR`, `CAN_MANAGE` |
| `jobs` | `name` | permissions | `CAN_VIEW`, `CAN_MANAGE_RUN`, `CAN_MANAGE` |
| `pipelines` | `name` | permissions | `CAN_VIEW`, `CAN_RUN`, `CAN_MANAGE` |
| `notebooks` | `path` | permissions | `CAN_READ`, `CAN_RUN`, `CAN_EDIT`, `CAN_MANAGE` |
| `directories` | `path` | permissions | `CAN_READ`, `CAN_RUN`, `CAN_EDIT`, `CAN_MANAGE` |
| `repos` | `path` | permissions | `CAN_READ`, `CAN_RUN`, `CAN_EDIT`, `CAN_MANAGE` |
| `experiments` | `name` | permissions | `CAN_READ`, `CAN_EDIT`, `CAN_MANAGE` |
| `serving-endpoints` | `name` | permissions | `CAN_VIEW`, `CAN_QUERY`, `CAN_MANAGE` |
| `dashboards` (Lakeview AI/BI) | `name` | permissions | `CAN_READ`, `CAN_RUN`, `CAN_EDIT`, `CAN_MANAGE` |
| `queries` (DBSQL) | `name` | permissions | `CAN_VIEW`, `CAN_RUN`, `CAN_EDIT`, `CAN_MANAGE` |
| `alerts` (DBSQL) | `name` | permissions | `CAN_VIEW`, `CAN_RUN`, `CAN_EDIT`, `CAN_MANAGE` |
| `secret-scopes` | `name` | secrets | `READ`, `WRITE`, `MANAGE` |
| `tokens` | — (singleton) | tokens | `CAN_USE`, `CAN_MANAGE` |

> Source of truth: `ObjectTypeSpec.allowed_permissions` in `src/governance/objecttypes.py`.
> The live `validate` command additionally cross-checks levels against the workspace via
> `permissions.get_permission_levels`.

## Principal & identifier format rules

Values are validated for shape, so typos and free-form text are rejected:

| Field | Rule |
|---|---|
| `user` | Must be an email address (`name@domain.tld`). |
| `service_principal` | Must be a UUID application id (`8-4-4-4-12` hex). |
| `group` (ACL principal, member, or group name) | Clean, single-line, non-empty string (spaces allowed, e.g. `account users`). |
| `name` (object identifier) | Clean, single-line, non-empty string. |
| `path` (notebooks/directories/repos) | Clean absolute path starting with `/`. |
| all strings | No empty/whitespace-only values; no control characters or newlines. |

Every ACL entry must set **exactly one** of `group` / `user` / `service_principal` plus a
`permission`. Group members set **exactly one** of `user` / `service_principal` / `group`.
Unknown fields are rejected. (Structural rules are enforced by the loader; value rules by
the linter.)

## Entitlements

Each `entitlements` entry names **exactly one** principal and its **complete** entitlement
set; `entitlements` is required (use `[]` for none) and each principal may appear once.

```yaml
entitlements:
  - group: data-engineers
    entitlements: [workspace-access, databricks-sql-access]
  - service_principal: "00000000-0000-0000-0000-000000000000"
    entitlements: []
```

- **Authoritative per listed principal:** `apply` adds missing entitlements and removes
  extra ones. Principals not listed are never read or changed.
- **Never removed from** the `admins` group or any `protected_principals` entry
  (additions still apply; the plan tags them `protected: kept`).
- **Direct only:** entitlements a user inherits through group membership are not shown or
  changed; govern them on the group.
- Always applied through the **workspace** SCIM API, even when `group_scope` is `account`.
- **Workspace assignment:** if the principal is an account-level user, group or service
  principal that is not yet in the workspace, `apply` assigns it to the workspace with
  exactly the listed entitlements (Workspace Assignment API, Beta, called on the
  workspace host). A workspace admin is enough; no account admin is needed. The plan tags
  it `ASSIGN TO WORKSPACE`. Example of a consumer-only group:

  ```yaml
  entitlements:
    - group: business-analysts
      entitlements: [workspace-consume]
  ```

  - Principals are **never unassigned** from the workspace by this tool.
  - An unassigned principal listed with `[]` is left alone (the API rejects an empty set).
  - Consumer-only is about **effective** entitlements: `workspace-consume` does not take
    away `workspace-access` / `databricks-sql-access` inherited from other groups (e.g.
    `users` or a migration clone group in older workspaces). Check those groups too.
- Reconciled right after `groups`, so a group created in the same run can be entitled.

## Linting in CI

`lint` performs the checks above with **no network / workspace access** — ideal for a
pull-request gate:

```bash
# lint every environment defined in settings.yaml
PYTHONPATH=src python3 -m governance.cli lint --config config-govtest-yaml --format yaml

# or via the installed console script
governance lint --config config-govtest-yaml --format yaml                 # all environments
governance lint --config config-govtest-yaml --format yaml --env govtest   # one environment
```

Exit code `0` = clean, `1` = one or more issues (structural or value). Example CI step:

```yaml
- name: Validate governance configs
  run: |
    pip install -e .
    governance lint --config config-govtest-yaml --format yaml
```

`lint` vs `validate`:

| Command | Network | Checks |
|---|---|---|
| `lint` | none (offline) | structure + **allowed values / formats** (+ existence, with an inventory) |
| `validate` | live workspace | object existence, principal existence, live permission levels |

Run `lint` in CI on every commit; run `validate` before `apply` against the target workspace.

## Offline existence checks — the environment inventory

`lint` can also verify **existence** offline (no workspace) when an environment inventory is
present — bringing much of `validate`'s value to CI/local with no credentials. Put a file
named `_inventory.yaml` (or `.yml` / `.json`) in the environment's config directory; `lint`
auto-detects it (or pass `--inventory PATH`). The leading `_` keeps it out of the
object-config merge.

The manifest lists what's **provisioned** in the environment; `lint` then flags any config
reference not backed by it:

```yaml
environment: prod
identities:
  users:              [ {user_name: alice@corp.com} ]
  service_principals: [ {application_id: "…", display_name: etl-runner} ]
  groups:
    - name: bi-viewers                 # in the account AND assigned to this workspace
    - {name: finance-readers, assigned: false}   # in the account, NOT yet assigned
resources:
  warehouses: [ {name: prod-bi-wh} ]
  clusters:   [ {name: prod-etl-cluster} ]
  # …one list per object-type key, entries by name or path
```

What it reports:

| Reference in config | Result |
|---|---|
| Object (by name/path) missing from `resources.<type>` | **fail** — object not provisioned |
| User / service principal missing from `identities` | **fail** — identity not synced |
| Group neither defined in config nor in the inventory | **fail** — unknown group |
| Group in `identities.groups` with `assigned: false` | **fail** — in account but not assigned to this workspace (assign it, account admin) |
| Group defined in config (framework-created) or assigned | ok |

> **Entitlements and unassigned principals.** `apply` can assign an account principal
> listed under `entitlements` (see [Entitlements](#entitlements)), but `lint` still
> checks it against the inventory: an `assigned: false` group, or a user / service
> principal missing from `identities`, is reported. List it in the inventory as a
> regular entry (without `assigned: false`) before committing the config.

Service principals match by either `application_id` or `display_name`. Singleton `tokens`
has no identifier, so only its principals are checked. This is the model behind
`config-govtest-yaml/environments/govtest/_inventory.yaml`.

### Generating an inventory from a live workspace

Rather than hand-author the manifest, export it from the target workspace with
[`governance.export_inventory`](../src/governance/export_inventory.py). It queries the
workspace over the SDK and writes a manifest in exactly the shape above — resource keys
match the object-type registry (`cluster-policies`, `secret-scopes`, …), `directories`
keyed by `path`, everything else by `name`. The output is YAML or JSON, inferred from the
`--out` extension unless `--format` is given.

```bash
# Auth uses the SDK's unified auth: --profile reads ~/.databrickscfg, else env vars.
.venv/bin/python -m governance.export_inventory \
  --profile <profile> --env <env> \
  --out config-govtest-yaml/environments/<env>/_inventory.yaml
```

Options:

| Flag | Default | Purpose |
|---|---|---|
| `--profile` | (default auth) | `.databrickscfg` profile for the workspace |
| `--env` | *(required)* | environment name stamped into the manifest |
| `--out` | *(required)* | output path for the inventory |
| `--format` | `auto` | `auto` (from the `--out` extension: `.yaml`/`.yml` → YAML, else JSON), `json`, or `yaml` |
| `--dir-roots` | `/Workspace/Shared` | comma-separated roots to walk for `directories` |
| `--max-depth` | `4` | recursion depth bound for the directory walk |

Each object type is queried best-effort: a type that errors (e.g. SQL not enabled) is skipped
and noted under a `_warnings` key rather than failing the whole export. Only non-empty buckets
are written, so the manifest asserts only what was actually observed. Because it widens
`directories` by walking the workspace tree, pass `--dir-roots` if you govern directories
outside `/Workspace/Shared`.

Once written as `_inventory.yaml` in the env dir, `lint` auto-detects it:

```bash
governance lint --config config-govtest-yaml --format yaml --env <env>
# -> [<env>] OK — N object(s) valid across M type(s); inventory _inventory.yaml checked
```

Re-run the exporter to refresh the manifest whenever the workspace changes — e.g. as a CI
step against the target workspace — rather than editing it by hand. Note this is a *snapshot*:
the committed manifest is only as current as the last export, so a stale inventory can mask
drift that live `validate` would still catch.
