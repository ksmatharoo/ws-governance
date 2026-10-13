"""Command-line entrypoint: ``governance validate | plan | apply``.

Exit codes:
    0  success / no changes
    1  error (config, auth, resolution) or validation issues found
    2  ``plan`` only: drift detected (changes would be applied) -- useful in CI
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

from governance.changes import EnvironmentPlan
from governance.client import whoami
from governance.config import loader as json_loader
from governance.config import yaml_loader
from governance.config.loader import EnvironmentSettings
from governance.engine import GovernanceEngine
from governance.errors import ConfigError, GovernanceError
from governance import inventory as inventory_mod
from governance.objecttypes import all_keys
from governance.report import render_plan, render_validation, write_audit_log
from governance.schema import lint_environment


def _loader(args):
    """Select the config loader by --format (default json). Both share schema/validation."""
    return yaml_loader if getattr(args, "format", "json") == "yaml" else json_loader


def _flatten_types(raw: Optional[List[str]]) -> Optional[List[str]]:
    if not raw:
        return None
    types: List[str] = []
    for item in raw:
        types.extend(t.strip() for t in item.split(",") if t.strip())
    unknown = [t for t in types if t not in all_keys()]
    if unknown:
        raise GovernanceError(
            f"Unknown object type(s): {', '.join(unknown)}. Known: {', '.join(all_keys())}"
        )
    return types or None


def _load(args):
    ld = _loader(args)
    config_root = Path(args.config)
    settings = ld.load_settings(config_root)
    config = ld.load_environment(config_root, args.env)
    types = _flatten_types(args.type)
    engine = GovernanceEngine(settings, args.env)
    return settings, config, types, engine


def _resolve_deletion(settings, args) -> bool:
    override = getattr(args, "delete_groups", None)
    if override is None:
        return settings.allow_group_deletion
    return override


def _in_ci() -> bool:
    """True when running inside the governance pipeline (set by the CI workflow)."""
    return os.environ.get("GOVERNANCE_CI") == "1"


def _ci_guard(env: str, env_settings: EnvironmentSettings) -> Optional[str]:
    """Return a refusal message if this env is CI-only and we're not in CI, else None."""
    if env_settings.require_ci and not _in_ci():
        return (
            f"Environment '{env}' is marked require_ci: it can only be applied by the CI "
            f"pipeline, not from a local machine. Run 'plan'/'validate' locally to preview, "
            f"and merge to the protected branch to let the pipeline apply."
        )
    return None


def _provenance() -> dict:
    """Collect apply provenance from the environment (populated by the CI workflow)."""
    git_sha = os.environ.get("GOVERNANCE_GIT_SHA") or os.environ.get("GITHUB_SHA")
    ci_run_url = os.environ.get("GOVERNANCE_CI_RUN_URL")
    if not ci_run_url:
        server = os.environ.get("GITHUB_SERVER_URL")
        repo = os.environ.get("GITHUB_REPOSITORY")
        run_id = os.environ.get("GITHUB_RUN_ID")
        if server and repo and run_id:
            ci_run_url = f"{server}/{repo}/actions/runs/{run_id}"
    return {"git_sha": git_sha, "ci_run_url": ci_run_url}


def cmd_lint(args) -> int:
    """Offline (no workspace) value validation of the config, for CI."""
    ld = _loader(args)
    config_root = Path(args.config)
    settings = ld.load_settings(config_root)
    envs = [args.env] if args.env else sorted(settings.environments)
    if not envs:
        print("No environments defined in settings.", file=sys.stderr)
        return 1

    total_issues = 0
    for env in envs:
        try:
            config = ld.load_environment(config_root, env)
        except ConfigError as exc:
            print(f"[{env}] FAIL (structural): {exc}", file=sys.stderr)
            total_issues += 1
            continue
        issues = lint_environment(config)

        # Optional offline existence check against a provisioned-resource inventory.
        inv_note = ""
        inv_path = _inventory_path(args, config_root, env)
        if inv_path:
            try:
                inv = inventory_mod.load_inventory(inv_path)
                issues = issues + [f"inventory: {i}" for i in inventory_mod.check(config, inv)]
                inv_note = f"; inventory {inv_path.name} checked"
            except ConfigError as exc:
                issues = issues + [f"inventory: could not load {inv_path.name} ({exc})"]

        if issues:
            total_issues += len(issues)
            print(f"[{env}] FAIL — {len(issues)} issue(s):")
            for issue in issues:
                print(f"    - {issue}")
        else:
            n = sum(len(v) for v in config.values())
            print(f"[{env}] OK — {n} object(s) valid across {len(config)} type(s){inv_note}")
    return 1 if total_issues else 0


def _inventory_path(args, config_root: Path, env: str):
    """Explicit --inventory wins; otherwise auto-detect _inventory.* in the env dir."""
    explicit = getattr(args, "inventory", None)
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise GovernanceError(f"--inventory path not found: {p}")
        return p
    return inventory_mod.find_inventory(config_root / "environments" / env)


def cmd_validate(args) -> int:
    _, config, types, engine = _load(args)
    issues = engine.validate(config, types)
    print(render_validation(issues, args.env))
    return 1 if issues else 0


def cmd_plan(args) -> int:
    _, config, types, engine = _load(args)
    plan, _ = engine.run(config, types, dry_run=True)
    print(render_plan(plan, dry_run=True))
    if plan.errors:
        return 1
    return 2 if plan.has_changes else 0


def cmd_apply(args) -> int:
    settings, config, types, engine = _load(args)

    # Enforce CI-only environments before touching the workspace.
    refusal = _ci_guard(args.env, engine.env_settings)
    if refusal:
        print(f"\n{refusal}", file=sys.stderr)
        return 1

    allow_deletion = _resolve_deletion(settings, args)

    # Preview first.
    plan, _ = engine.run(config, types, dry_run=True, allow_group_deletion=allow_deletion)
    print(render_plan(plan, dry_run=True))
    if plan.errors:
        print("\nResolve the errors above before applying.", file=sys.stderr)
        return 1
    if not plan.has_changes:
        return 0

    # Identify the acting principal (human admin locally, service principal in CI) for
    # the audit trail, and warn if reconcile might strip its own access.
    actor = None
    try:
        actor = whoami(engine.ws)
    except GovernanceError as exc:
        print(f"\nWarning: could not determine acting identity ({exc}).", file=sys.stderr)
    if actor and actor not in settings.protected_principals:
        print(
            f"\nNote: acting identity '{actor}' is not listed in protected_principals. "
            f"Workspace admins are retained by the platform, but if this principal relies on a "
            f"managed grant it could lose access. Add it to protected_principals to be safe.",
            file=sys.stderr,
        )

    if not args.yes:
        if not sys.stdin.isatty():
            print("\nRefusing to apply without confirmation (use --yes in non-interactive runs).", file=sys.stderr)
            return 1
        answer = input("\nApply these changes? Type 'yes' to proceed: ").strip()
        if answer != "yes":
            print("Aborted.")
            return 0

    result, audit = engine.run(config, types, dry_run=False, allow_group_deletion=allow_deletion)
    print("\n" + render_plan(result, dry_run=False))
    prov = _provenance()
    path = write_audit_log(
        args.env, audit, directory=args.audit_dir, actor=actor,
        git_sha=prov["git_sha"], ci_run_url=prov["ci_run_url"],
    )
    print(f"\nAudit log written to {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="governance", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("--env", required=True, help="environment name from settings")
        p.add_argument("--config", default="config", help="path to the config directory (default: config)")
        p.add_argument(
            "--format",
            choices=("json", "yaml"),
            default="json",
            help="config file format (default: json). yaml requires PyYAML.",
        )
        p.add_argument(
            "--type",
            action="append",
            metavar="TYPE",
            help="limit to object type(s); repeatable or comma-separated (e.g. --type jobs,clusters)",
        )

    p_lint = sub.add_parser("lint", help="offline value validation of config (CI); no workspace needed")
    p_lint.add_argument("--config", default="config", help="path to the config directory (default: config)")
    p_lint.add_argument("--env", default=None, help="environment to lint (default: all environments)")
    p_lint.add_argument(
        "--format", choices=("json", "yaml"), default="json",
        help="config file format (default: json). yaml requires PyYAML.",
    )
    p_lint.add_argument(
        "--inventory", default=None, metavar="PATH",
        help="offline existence check against a provisioned-resource inventory "
             "(default: auto-detect environments/<env>/_inventory.yaml|yml|json)",
    )
    p_lint.set_defaults(func=cmd_lint)

    p_validate = sub.add_parser("validate", help="check config against the workspace; no changes")
    add_common(p_validate)
    p_validate.set_defaults(func=cmd_validate)

    p_plan = sub.add_parser("plan", help="show the diff that apply would enact (dry run)")
    add_common(p_plan)
    p_plan.set_defaults(func=cmd_plan)

    p_apply = sub.add_parser("apply", help="reconcile the workspace to match config (drift-removal)")
    add_common(p_apply)
    p_apply.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    p_apply.add_argument("--audit-dir", default="audit-logs", help="where to write the JSON audit log")
    p_apply.add_argument(
        "--delete-groups", dest="delete_groups", action="store_true", default=None,
        help="enable the group delete phase for this run (overrides settings)",
    )
    p_apply.add_argument(
        "--no-delete-groups", dest="delete_groups", action="store_false",
        help="disable the group delete phase for this run (overrides settings)",
    )
    p_apply.set_defaults(func=cmd_apply)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except GovernanceError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover

    print("test")
    sys.exit(main())
