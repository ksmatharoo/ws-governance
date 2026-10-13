# Workspace Deployment

How to deploy the governance framework into a Databricks workspace and run it as a job.
Everything here uses the Databricks REST API through two helper scripts in this folder —
no Databricks CLI required.

| Script | Purpose |
|--------|---------|
| `deploy_to_workspace.py` | Upload a file **or** directory (wheel, notebook, config tree) to a workspace path (`POST /api/2.0/workspace/{mkdirs,import}`). |
| `create_job.py` | Create (and optionally run) the governance job (`POST /api/2.1/jobs/create`, `run-now`). |
| `run_governance.py` | The notebook the job runs; `%pip install`s the wheel and calls `governance lint|plan|apply`. |

### Auth

Every script takes either `--host` + `--token`, or a `.databrickscfg` `--profile`
(falling back to env vars / default). Examples below use `--profile govtest`.

### Target layout in the workspace

```
/Workspace/Shared/gov-test/
├── dist/databricks_governance-0.1.0-py3-none-any.whl   # the wheel
├── config-govtest-yaml/                                 # the YAML config tree
│   ├── settings.yaml                                    # (deployed copy has NO profile)
│   └── environments/govtest/*.yaml
├── run_governance                                       # the notebook
└── audit-logs/                                          # written by `apply`
```

---

## Deployment steps (in order)

### 1. Build the wheel

```bash
python setup.py bdist_wheel
# -> dist/databricks_governance-0.1.0-py3-none-any.whl
```

### 2. Upload the wheel

```bash
python deploy/deploy_to_workspace.py --profile govtest
# default --src = newest dist/*.whl, default --dest-dir = /Workspace/Shared/gov-test/dist
# -> /Workspace/Shared/gov-test/dist/databricks_governance-0.1.0-py3-none-any.whl
```

### 3. Upload the config

```bash
python deploy/deploy_to_workspace.py --profile govtest \
  --src config-govtest-yaml --dest-dir /Workspace/Shared/gov-test
# -> /Workspace/Shared/gov-test/config-govtest-yaml/... (tree preserved)
```

> **Important — settings must have no `profile` in the workspace.** The engine builds its
> client from `settings.yaml`: a `profile` there makes it look up `.databrickscfg`, which
> does not exist on the job cluster. The repo's `config-govtest-yaml/settings.yaml` keeps
> `profile: govtest` for **local** CLI use, so after step 3 overwrite the deployed
> `settings.yaml` with a profile-less copy (no `profile`/`host` → the job uses its run-as
> identity):
>
> ```bash
> cat > /tmp/settings.yaml <<'EOF'
> environments:
>   govtest:
>     group_scope: workspace
> protected_principals:
>   - admins
> allow_group_deletion: false
> EOF
> python deploy/deploy_to_workspace.py --profile govtest \
>   --src /tmp/settings.yaml --dest-dir /Workspace/Shared/gov-test/config-govtest-yaml
> ```

### 4. Upload the notebook

```bash
python deploy/deploy_to_workspace.py --profile govtest \
  --src deploy/run_governance.py --dest-dir /Workspace/Shared/gov-test
# format=AUTO imports it as a PYTHON notebook, stripping the .py extension:
# -> /Workspace/Shared/gov-test/run_governance
```

The notebook targets the YAML config:
`governance <action> --config /Workspace/Shared/gov-test/config-govtest-yaml --env govtest --format yaml`.

### 5. Create the job

```bash
python deploy/create_job.py --profile govtest
# -> Created job_id=<id>  (serverless, notebook task, action base-parameter defaults to "plan")
```

`POST /api/2.1/jobs/create` does not enforce unique names — creating repeatedly makes
duplicate jobs. Delete the stale one (`POST /api/2.1/jobs/delete`) or reuse the existing
`job_id`.

### 6. Run and verify

```bash
# trigger immediately on create:
python deploy/create_job.py --profile govtest --run-now

# or run an existing job (action = lint | plan | apply):
#   POST /api/2.1/jobs/run-now  {"job_id": <id>, "notebook_params": {"action": "plan"}}
```

A successful `plan` returns:

```
governance plan -> exit 0 (OK)
== PLAN (dry run) :: environment 'govtest' ==
No changes — workspace matches config.
```

---

## Deployment log — govtest (2026-10-13)

Target workspace: `https://adb-7405609408355473.13.azuredatabricks.net` (profile `govtest`).

| # | Step | Command | Result |
|---|------|---------|--------|
| 1 | Build wheel | `python setup.py bdist_wheel` | `dist/databricks_governance-0.1.0-py3-none-any.whl` (42 KB) |
| 2 | Upload wheel | `deploy_to_workspace.py --profile govtest` | `FILE /Workspace/Shared/gov-test/dist/...whl` |
| 3 | Upload config | `deploy_to_workspace.py --src config-govtest-yaml --dest-dir /Workspace/Shared/gov-test` | 6 files under `config-govtest-yaml/` |
| 3b | Profile-less settings | re-uploaded `settings.yaml` with no `profile` | job uses run-as identity |
| 4 | Upload notebook | `deploy_to_workspace.py --src deploy/run_governance.py --dest-dir /Workspace/Shared/gov-test` | `NOTEBOOK /Workspace/Shared/gov-test/run_governance` (PYTHON) |
| 5 | Create job | `create_job.py --profile govtest` | `job_id=1080839984342098` (old duplicate `555431354763973` deleted) |
| 6 | Run (plan) | `jobs/run-now {action: plan}` | run `566803765674849` → **SUCCESS**, no drift |
