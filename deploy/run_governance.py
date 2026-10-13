# Databricks notebook source
# MAGIC %md
# MAGIC # Run governance in-workspace
# MAGIC Installs the governance wheel and runs `lint` / `plan` / `apply` against the uploaded
# MAGIC config, as a serverless job. Auth = this job's run-as identity (SDK default auth —
# MAGIC the uploaded settings.json must have NO `profile`).
# MAGIC
# MAGIC Source of truth for the notebook uploaded to
# MAGIC `/Workspace/Shared/gov-test/run_governance`. See `Steps_deploy_job.md`.

# COMMAND ----------

# MAGIC %pip install /Workspace/Shared/gov-test/dist/databricks_governance-0.1.0-py3-none-any.whl

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

import sys

dbutils.widgets.text("action", "plan")      # "lint" | "plan" | "apply"
action = dbutils.widgets.get("action").strip()

config_dir = "/Workspace/Shared/gov-test/config-govtest-yaml"
audit_dir = "/Workspace/Shared/gov-test/audit-logs"   # explicit, durable workspace path
argv = ["governance", action, "--config", config_dir, "--env", "govtest", "--format", "yaml"]
if action == "apply":
    argv += ["--yes", "--audit-dir", audit_dir]

sys.argv = argv

import io
import contextlib

from governance.cli import main

buf = io.StringIO()
rc = 0
try:
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        main()
except SystemExit as e:
    rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)

output = buf.getvalue()
print(output)
print(f">>> exit code: {rc}")

# Surface the audit log path (apply prints "Audit log written to <path>").
audit_line = next((ln for ln in output.splitlines() if "Audit log written to" in ln), None)

# lint: 0 = ok. plan: 0 = no drift, 2 = drift (both fine). apply: 0 = success.
ok = (rc in (0, 2)) if action == "plan" else (rc == 0)
header = f"governance {action} -> exit {rc} ({'OK' if ok else 'FAILED'})"
if audit_line:
    header += f"\n{audit_line.strip()}"
summary = f"{header}\n\n{output}"
if not ok:
    raise SystemExit(summary)

dbutils.notebook.exit(summary)
