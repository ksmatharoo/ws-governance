# databricks-governance (setuptools packaging)

Classic `setup.py` + `requirements.txt` src-layout packaging of the Databricks governance
framework, for CI agents (including Windows) that build the wheel with setuptools.

## Layout

```
.
├── setup.py                # install_requires read from requirements.txt
├── setup.cfg               # bdist_wheel + pytest options
├── requirements.txt        # all dependencies (single source of truth)
├── config-govtest-yaml/    # the govtest YAML config sample
├── deploy/                 # workspace deployment (wheel, config, notebook, job)
├── docs/                   # design, run/test guide, config schema, runbook, CI/CD
├── tests/                  # unit + prerequisite tests
└── src/
    └── governance/         # the package
```

## Build the wheel

```bash
python -m pip install --upgrade setuptools wheel
python setup.py bdist_wheel
# -> dist/databricks_governance-0.1.0-py3-none-any.whl
```

On Windows agents the command is identical (`python setup.py bdist_wheel`).

## Install

```bash
pip install dist/databricks_governance-0.1.0-py3-none-any.whl
```

All dependencies (databricks-sdk, pyyaml, pytest) are declared in requirements.txt and
install with the wheel. Provides the `governance` CLI entry point.

## Set up a dev environment

Create a Python 3.12 virtual environment and editable-install the package:

```bash
python3.12 -m venv .venv            # on this machine use: uv venv --python 3.12 .venv
.venv/bin/python -m pip install -e .  # or: uv pip install --python .venv/bin/python -e .
```

> Note: the local `python3.12` is a uv-managed standalone interpreter whose stock
> `python3.12 -m venv` produces a broken environment. On this machine create the venv with
> `uv venv --python 3.12 .venv` instead. On a normal CPython install, `python3.12 -m venv`
> works as written above.

## Test

```bash
.venv/bin/python -m pytest -q    # pythonpath=src is set in setup.cfg
```

Expected: `120 passed, 1 skipped` (the skip is the PyYAML-absent path, inactive when PyYAML
is installed).

## Documentation

See [docs/README.md](docs/README.md) — design, build/run/test guide, config schema
(including entitlements), pre-apply runbook, and CI/CD model.
Workspace deployment is in [deploy/README.md](deploy/README.md).

## Export a workspace inventory

`governance.export_inventory` queries a live Databricks workspace and writes an
`_inventory` manifest in JSON or YAML. The format is taken from `--format` when given,
otherwise inferred from the `--out` extension (`.yaml`/`.yml` -> YAML, else JSON). YAML
output requires PyYAML.

```bash
# YAML (auto-detected from the .yaml extension)
.venv/bin/python -m governance.export_inventory \
  --profile govtest --env govtest \
  --out config-govtest-yaml/environments/govtest/_inventory.generated.yaml

# JSON (auto-detected from the .json extension)
.venv/bin/python -m governance.export_inventory \
  --profile govtest --env govtest \
  --out config-govtest-yaml/environments/govtest/_inventory.json

# Force a format regardless of extension
.venv/bin/python -m governance.export_inventory \
  --profile govtest --env govtest --format yaml --out /tmp/inv.txt
```

Options: `--profile` (`.databrickscfg` profile, else env-var/default auth), `--env`
(environment name stamped into the manifest), `--out` (output path), `--format`
(`auto`|`json`|`yaml`, default `auto`), `--dir-roots` (comma-separated workspace roots to
walk for directories, default `/Workspace/Shared`), `--max-depth` (directory recursion
depth, default `4`). Each query is best-effort — a type that errors (e.g. SQL not enabled)
is skipped with a warning rather than failing the whole export.

Confirm the exported file round-trips through the inventory loader:

```bash
.venv/bin/python -c "from governance import inventory as i; \
m = i.load_inventory('config-govtest-yaml/environments/govtest/_inventory.generated.yaml'); \
print('OK:', sorted(m.resources))"
```

### End-to-end: export + verify + test

```bash
cd /Users/kulwinder.matharoo/github/governance-setuptools

# 1. Export the live govtest workspace to a YAML inventory
.venv/bin/python -m governance.export_inventory \
  --profile govtest --env govtest \
  --out config-govtest-yaml/environments/govtest/_inventory.generated.yaml

# 2. Confirm it round-trips through the inventory loader
.venv/bin/python -c "from governance import inventory as i; \
m = i.load_inventory('config-govtest-yaml/environments/govtest/_inventory.generated.yaml'); \
print('OK:', sorted(m.resources))"

# 3. Run the test suite
.venv/bin/python -m pytest -q
```

