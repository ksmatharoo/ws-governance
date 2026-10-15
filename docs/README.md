# Documentation

- **[design.md](design.md)** — complete design of the framework: goals, architecture,
  components, data flow, reconcile semantics, config model, and how to extend it.
- **[build-run-test.md](build-run-test.md)** — how to install, configure, run
  (`lint` / `validate` / `plan` / `apply`), and test the framework, including CI usage.
- **[config-schema.md](config-schema.md)** — allowed permission values per object type,
  principal/identifier format rules, entitlements and workspace assignment, and how the
  offline `lint` CI gate works.
- **[pre-apply-runbook.md](pre-apply-runbook.md)** — what to create/verify in a new
  workspace before the first `apply`: prerequisites, discovery commands, the config
  artifact to author, validation gates, and rollback.
- **[ci-cd.md](ci-cd.md)** — the two-mode (local vs CI-only) execution model, provenance
  in audit logs, and pipeline prerequisites.
- **[../deploy/README.md](../deploy/README.md)** — deploying the wheel, config and notebook
  to a workspace and running governance as a job.

This repo uses **YAML** config: the sample lives in
[`../config-govtest-yaml/`](../config-govtest-yaml/), with a template per config set in
[`../config-govtest-yaml/templates/`](../config-govtest-yaml/templates/). Every CLI
command therefore takes `--config config-govtest-yaml --format yaml` (the CLI defaults are
`config` and `json`).

For packaging and a quick start, see the top-level [../README.md](../README.md).
