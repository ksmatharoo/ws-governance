# Build, Run & Test

Operational guide for the Databricks Governance Framework (setuptools packaging). For the
design, see [design.md](design.md); for running it as a workspace job, see
[../deploy/README.md](../deploy/README.md).

---

## 1. Prerequisites

- **Python 3.9+** (3.12 is what this repo is developed and tested on)
- Dependencies, all declared in [`requirements.txt`](../requirements.txt) and installed
  with the package:
  - **`databricks-sdk>=0.139.0`** — workspace API client. 0.139.0 is the minimum because
    workspace assignment uses `WorkspaceClient.workspace_iam_v2`.
  - **`pyyaml`** — the config in this repo is YAML (`--format yaml`).
  - **`pytest`** — the test suite.
- Databricks workspace access with permission to manage ACLs / SCIM groups
  (workspace admin; account admin if using `group_scope: account`)

> **`governance lint` needs no workspace** — it validates config files offline. Only
> `validate` / `plan` / `apply` contact a workspace.

---

## 2. Build / install

### Dev environment (editable install, provides the `governance` command)

```bash
cd /path/to/ws-governance
python3.12 -m venv .venv              # on a uv-managed Python: uv venv --python 3.12 .venv
.venv/bin/python -m pip install -e .  # or: uv pip install --python .venv/bin/python -e .
.venv/bin/governance --help
```

> On a uv-managed standalone interpreter, the stock `python3.12 -m venv` produces a broken
> environment; create the venv with `uv venv --python 3.12 .venv` instead.

### Build the wheel

```bash
python -m pip install --upgrade setuptools wheel
python setup.py bdist_wheel
# -> dist/databricks_governance-0.1.0-py3-none-any.whl
pip install dist/databricks_governance-0.1.0-py3-none-any.whl
```

The command is identical on Windows CI agents. The workspace job installs this wheel with
`%pip install` (see [../deploy/README.md](../deploy/README.md)); serverless already has a
`databricks-sdk`, so the `>=0.139.0` pin is what makes pip upgrade it when it is older.

### Run from source without installing

```bash
export PYTHONPATH=src
python3 -m governance.cli --help
```

---

## 3. Configure authentication

Uses the SDK's unified auth. Configure a `.databrickscfg` profile per environment:

```ini
# ~/.databrickscfg
[govtest]
host = https://adb-xxxx.azuredatabricks.net
auth_type = databricks-cli          # from: databricks auth login --profile govtest

# only needed for group_scope: account
[account-admin]
host       = https://accounts.azuredatabricks.net
account_id = 00000000-0000-0000-0000-000000000000
```

Then reference these profiles in `config-govtest-yaml/settings.yaml`
(`environments.<env>.profile`, `account.profile`). Credentials are never stored in the
repo.

Alternatives to profiles: set `host` in settings and export env vars
(`DATABRICKS_TOKEN`, or `DATABRICKS_CLIENT_ID`/`DATABRICKS_CLIENT_SECRET` for OAuth). In a
workspace job, leave both `profile` and `host` out so the job uses its run-as identity
(see [../deploy/README.md](../deploy/README.md)).

---

## 4. Author config

```
config-govtest-yaml/
├── settings.yaml
├── templates/*.template.yaml
└── environments/
    └── govtest/
        ├── identity.yaml        # groups + entitlements
        ├── compute.yaml
        ├── jobs.yaml
        ├── secrets.yaml
        ├── workspace.yaml
        └── _inventory.yaml      # optional: offline existence check for lint
```

Copy a template from `config-govtest-yaml/templates/`, fill in real values, and edit
object names/paths and principals to match your workspace. To add another environment,
add it under `environments` in `settings.yaml` and create
`environments/<env>/*.yaml`. Allowed permission values and format rules are in
[config-schema.md](config-schema.md); the full schema is in
[design.md section 5](design.md#5-configuration-model).

---

## 5. Run

The CLI defaults to `--config config --format json`, so every command here passes the
YAML config explicitly. Always in this order the first time:
**lint → validate → plan → apply**.

```bash
C="--config config-govtest-yaml --format yaml"

# 0) Offline value/format check (no workspace) — the CI gate
governance lint $C --env govtest
governance lint $C                                       # all environments
# With environments/<env>/_inventory.yaml present, lint also checks existence offline.
# Generate one from a live workspace — see "Generating an inventory" in config-schema.md.

# 1) Check config against the live workspace (no changes)
governance validate $C --env govtest

# 2) Preview the diff reconcile would enact
governance plan $C --env govtest
governance plan $C --env govtest --type jobs,clusters    # subset

# 3) Apply (interactive confirmation)
governance apply $C --env govtest
governance apply $C --env govtest --yes                  # non-interactive
governance apply $C --env govtest --type entitlements    # subset
governance apply $C --env govtest --no-delete-groups     # disable group deletion this run
governance apply $C --env govtest --delete-groups        # enable group deletion this run
```

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | Success / no changes |
| `2` | `plan` only: drift detected (changes would be applied) |
| `1` | Error (config/auth/resolution), lint failure, or validation issues found |

**Audit log** — each `apply` writes `audit-logs/<env>-<UTCtimestamp>.audit.json` (or
under `--audit-dir`) listing every action taken.

### Typical local workflow

```bash
# edit config-govtest-yaml/environments/govtest/*.yaml
governance lint     $C --env govtest     # offline: catch bad values / formats
governance validate $C --env govtest     # live: catch typos / missing objects / bad levels
governance plan     $C --env govtest     # review the diff
git add config-govtest-yaml/ && git commit -m "governance: add analysts as consumers"
governance apply    $C --env govtest     # enforce, confirm when prompted
```

---

## 6. Test

```bash
# from the repo root (setup.cfg sets pythonpath = src and testpaths = tests)
.venv/bin/python -m pytest -q

# a single module / case
.venv/bin/python -m pytest -q tests/test_entitlements.py
.venv/bin/python -m pytest -q tests/test_engine.py -k apply
```

The standard-library runner works too: `PYTHONPATH=src python3 -m unittest discover -s tests`.

### What the tests cover

| File | Focus |
|---|---|
| `tests/test_config.py` | Model validation, loader merging/rejections, settings parsing |
| `tests/test_yaml_loader.py` | YAML loader parity with the JSON loader |
| `tests/test_schema.py` | Offline lint: permission enums, principal/identifier formats |
| `tests/test_inventory.py` | Offline existence checks against `_inventory` manifests |
| `tests/test_diff.py` | ACL & membership diff; owner/inherited exclusion; protected preservation |
| `tests/test_groups.py` | Group create/membership/delete lifecycle + guardrails |
| `tests/test_entitlements.py` | Entitlements reconcile and workspace assignment of account principals |
| `tests/test_handlers.py` | Permissions & secrets reconcile (plan vs apply, protected) |
| `tests/test_resolve.py` | Name/path → object id resolvers (incl. SQL/BI and pipelines) |
| `tests/test_engine.py` | End-to-end validate/plan/apply, ordering, `--type` filter |
| `tests/test_cli.py` | `require_ci` guard, provenance collection, audit-log stamping |
| `tests/test_prerequisite.py` | Test-only prerequisite check/provision helpers (`tests/prerequisite/`) |
| `tests/helpers.py` | In-memory fake SDK clients (no network) returning real SDK models |

Tests never touch a real workspace — they inject fake `WorkspaceClient`s that return real
SDK model objects, so the same production code paths run offline and deterministically.

---

## 7. CI usage (optional)

The CLI is CI-friendly. Two kinds of gate:

**On every PR — offline (no workspace/secrets needed):**

```bash
pip install -e .
python -m pytest -q                                              # unit tests
governance lint --config config-govtest-yaml --format yaml       # all envs
```

**Against a workspace (needs `DATABRICKS_*` / a profile):**

```bash
governance validate --config config-govtest-yaml --format yaml --env govtest
governance plan     --config config-govtest-yaml --format yaml --env govtest   # exit 2 => drift
```

- Run **tests + `governance lint`** on every PR (fast, no credentials).
- `governance validate` / `plan` on PR when workspace credentials are available
  (`plan` exit `2` flags drift for reviewers).
- `governance apply ... --yes` on merge to enforce — from the pipeline, or by running the
  workspace job with `action=apply` (see [../deploy/README.md](../deploy/README.md)).

This repo ships no pipeline definition; see [ci-cd.md](ci-cd.md) for the execution model.

---

## 8. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `lint` says `invalid permission` | Use a level allowed for that type — see [config-schema.md](config-schema.md). |
| `lint` says `not a valid email` / `not a valid UUID` | `user` must be an email; `service_principal` must be a UUID application id. |
| `lint` says `path must be ... absolute` | Notebook/directory/repo paths must start with `/`. |
| `lint` says `neither defined in config nor in the inventory` | Add the principal to `_inventory.yaml` (also for account principals that `apply` will assign via `entitlements`). |
| `Error: settings.json not found at ...` | The CLI defaulted to JSON / `config/`; pass `--config config-govtest-yaml --format yaml`. |
| `Environment 'X' not defined` | Add it under `environments` in `settings.yaml`. |
| `... not found` on plan/apply | Object name/path doesn't exist in the workspace — run `validate`. |
| `... not found in the workspace or its account` (entitlements) | The principal isn't in the account either; check the name / application id. |
| `invalid permission '...'` | Level not allowed for that object type — `validate` lists valid levels. |
| `Group '...' not found` (member) | The user/SP/nested group isn't in the workspace/account SCIM directory. |
| `'WorkspaceClient' object has no attribute 'workspace_iam_v2'` | `databricks-sdk` is older than 0.139.0 — upgrade it. |
| `Account-scope groups requested but no 'account' block` | Add the `account` block to `settings.yaml`. |
| Refuses to apply non-interactively | Pass `--yes` in scripts/CI. |
| Auth errors | Check the profile in `~/.databrickscfg` (`databricks auth login --profile <p>`) or `DATABRICKS_*` env vars. |
