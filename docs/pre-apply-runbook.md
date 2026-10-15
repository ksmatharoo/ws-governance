# Pre-Apply Runbook — preparing a new workspace for governance

Follow this before running `governance apply` against a freshly-provisioned workspace.
It lists what must exist first, how to discover real object names, the **config artifact
you create**, and the safe apply sequence.

## What the framework does and does not create

| The framework **changes** | The framework **does NOT create** |
|---|---|
| Object ACLs (Permissions API `set`) | Users (come from your IdP / SCIM) |
| Secret-scope ACLs | Service principals (create in workspace/account settings) |
| Token (PAT) ACL | The governed objects themselves (clusters, jobs, pipelines, warehouses, notebooks, dirs, repos, experiments, serving endpoints, dashboards, queries, alerts, secret scopes) |
| Group membership | — |
| Groups (create / delete, if enabled) | |
| Entitlements of principals already in the workspace | Workspace assignment (account principals must be assigned to the workspace first) |

**Implication:** every principal you reference (user / service principal / nested group)
and every object you govern (except groups, which the tool can create) **must already
exist** in the workspace when you apply — otherwise resolution fails (safely, with an error).

---

## 1. Prerequisites checklist

- [ ] **Runner identity has admin rights** — workspace admin (and **account admin** if any
      environment uses `group_scope: account`), with permission to manage ACLs and SCIM groups.
- [ ] **Auth profile is valid** — a `~/.databrickscfg` profile (or `DATABRICKS_*` env vars)
      for the workspace; verify with `databricks current-user me -p <profile>`.
- [ ] **Users exist** — every `user` referenced (ACLs, group members) is provisioned via
      your IdP/SCIM.
- [ ] **Service principals exist** — every `service_principal` referenced exists; you have
      its **application id (UUID)**. Create the deploy/automation SP now.
- [ ] **Governed objects exist** — the clusters, jobs, pipelines, warehouses, notebooks,
      directories, repos, experiments, serving endpoints, dashboards, queries, alerts, and
      secret scopes you intend to govern have been created (by you or your platform team).
- [ ] **Break-glass principals identified** — the admin group(s) / SPs that must never be
      revoked, to list under `protected_principals`.
- [ ] **Python + the package** installed where you run the CLI (`pip install -e .` or the
      wheel; pulls `databricks-sdk` and `pyyaml` from `requirements.txt`).

---

## 2. Discover real object & identity names

Governance resolves objects by their **name** (or **path**). Enumerate what exists so you
use the exact names in config (replace `<profile>`):

```bash
# identity
databricks current-user me            -p <profile>   # confirm who you are + your groups
databricks groups list                -p <profile>
databricks service-principals list    -p <profile>   # application ids (UUIDs) for SP grants

# compute
databricks clusters list              -p <profile>
databricks cluster-policies list      -p <profile>
databricks instance-pools list        -p <profile>
databricks warehouses list            -p <profile>

# workflows & code
databricks jobs list                  -p <profile>
databricks pipelines list-pipelines   -p <profile>
databricks workspace list /Workspace  -p <profile>   # notebooks / directories
databricks repos list                 -p <profile>

# ML + SQL/BI
databricks serving-endpoints list     -p <profile>
databricks experiments search-experiments -p <profile>
databricks lakeview list              -p <profile>   # dashboards (display names)
databricks queries list               -p <profile>
databricks alerts list                -p <profile>

# secrets
databricks secrets list-scopes        -p <profile>
```

To see an object's **current** ACL (useful so your config doesn't accidentally revoke a
needed grant), e.g. for a cluster:

```bash
databricks permissions get clusters <cluster-id> -p <profile>
```

---

## 3. The config artifact to create

This is the artifact governance consumes. Create it under `config-govtest-yaml/` (copy
from `config-govtest-yaml/templates/`), using the real names from Step 2. Allowed
permission values and formats are in [config-schema.md](config-schema.md).

### 3a. `config-govtest-yaml/settings.yaml`

```yaml
environments:
  <ENV>:
    profile: <DATABRICKSCFG_PROFILE>
    group_scope: workspace
protected_principals:
  - admins
allow_group_deletion: false
```

- Start with `allow_group_deletion: false`.
- Put your break-glass admin group(s)/SP(s) in `protected_principals` so reconcile never
  revokes them.
- Use `group_scope: "account"` only if you manage groups at the account (then add an
  `account` block — see config-schema/design docs).

For the deployed job, the uploaded copy of `settings.yaml` must have **no `profile`** (the
job uses its run-as identity) — see [../deploy/README.md](../deploy/README.md) step 3.

### 3b. `config-govtest-yaml/environments/<ENV>/*.yaml`

One or more files, merged by top-level object-type key. Start **small** — govern a couple
of real objects and your core groups first, then expand. Skeleton:

```yaml
# identity.yaml
groups:
  - name: <real-group>
    members:
      - user: real.user@yourco.com
      - service_principal: "<sp-application-uuid>"

entitlements:
  - group: <real-group>              # must already be in the workspace
    entitlements: [workspace-access, databricks-sql-access]

# compute.yaml
clusters:
  - name: <real-cluster-name>
    acls:
      - {group: admins, permission: CAN_MANAGE}
      - {group: <real-group>, permission: CAN_RESTART}
```

> **Drift-removal reminder:** for each object you list, the config becomes the *complete*
> desired ACL. Any live grant on that object **not** in your config will be **revoked** on
> apply. Include every grant you want to keep (owners/admins are retained automatically).

---

## 4. Pre-apply validation gates (all safe)

```bash
C="--config config-govtest-yaml --format yaml"
governance lint     $C --env <ENV>   # offline: values/formats (+ existence, with _inventory.yaml)
governance validate $C --env <ENV>   # live: objects/principals exist
governance plan     $C --env <ENV>   # live: shows the diff, writes nothing
```

Proceed only when:
- `lint` is clean,
- `validate` reports no missing objects/principals/permission levels,
- `plan` shows **exactly** the changes you intend (review every `-` removal carefully).

---

## 5. First apply — scoped and reversible

```bash
# scope to ONE object type for the first run; keep group deletion off
governance apply --config config-govtest-yaml --format yaml --env <ENV> --type clusters
```

- It reprints the plan and requires you to type `yes`.
- Confirm the audit log at `audit-logs/<ENV>-<timestamp>.audit.json`.
- Widen scope (`--type ...` or drop it) once you trust the output.

---

## 6. Rollback / recovery

- **Config is the source of truth and it's in git** — to revert a change, restore the prior
  config (`git revert` / checkout the previous version) and `apply` again.
- The **audit log** records every grant added/removed and group created/deleted per run.
- Group deletion is off by default; enable it (`allow_group_deletion` or `--delete-groups`)
  only after you're confident the group config is complete, or you may delete groups other
  teams rely on.

---

## Ready-to-apply checklist

- [ ] Workspace provisioned; runner identity is admin
- [ ] Auth profile valid (`databricks current-user me`)
- [ ] Users & service principals referenced in config exist
- [ ] Governed objects exist (names captured from Step 2)
- [ ] `config-govtest-yaml/settings.yaml` created (`protected_principals` set, deletion off)
- [ ] `config-govtest-yaml/environments/<ENV>/*.yaml` created with real names
- [ ] `lint` clean → `validate` clean → `plan` shows intended diff
- [ ] First `apply` scoped with `--type`, audit log reviewed
