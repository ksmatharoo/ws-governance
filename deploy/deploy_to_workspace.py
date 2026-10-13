#!/usr/bin/env python3
"""Upload a file or directory to a Databricks workspace path, via the Workspace REST API.

Copies a local file (e.g. the governance ``.whl`` or the ``run_governance`` notebook) or a
whole directory tree (e.g. a config dir) into a workspace directory. For a directory the
tree is recreated under ``--dest-dir``. Uses ``POST /api/2.0/workspace/mkdirs`` to ensure
each parent exists, then ``POST /api/2.0/workspace/import`` (``format=AUTO``, base64
content, ``overwrite``) per file — no SDK Workspace client, per requirement.

Auth: pass ``--host`` and ``--token`` directly, or fall back to the SDK's unified
``Config`` (``--profile`` -> .databrickscfg, else env vars / default).

Note: the import endpoint caps content at ~10 MB; the governance wheel is ~40 KB.

Usage:
    python3 deploy/deploy_to_workspace.py --profile govtest                 # newest dist/*.whl
    python3 deploy/deploy_to_workspace.py --profile govtest \
        --src config-govtest-yaml --dest-dir /Workspace/Shared/gov-test     # upload a directory
    python3 deploy/deploy_to_workspace.py --host $DBX_HOST --token $DBX_TOKEN \
        --src dist/databricks_governance-0.1.0-py3-none-any.whl \
        --dest-dir /Workspace/Shared/gov-test/dist
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import sys
import urllib.error
import urllib.request


def _request(method: str, url: str, headers: dict, payload: dict | None = None) -> dict:
    """Make one JSON REST call and return the parsed response (raises on HTTP error)."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:  # surface the API error body, which is informative
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"HTTP {exc.code} from {url}:\n{detail}") from exc
    return json.loads(body) if body.strip() else {}


def resolve_auth(host: str | None, token: str | None, profile: str | None):
    """Return (host, headers). Prefer explicit --host/--token, else the SDK's Config."""
    if host or token:
        if not (host and token):
            raise SystemExit("--host and --token must be given together")
        return host.rstrip("/"), {"Authorization": f"Bearer {token}"}
    from databricks.sdk.core import Config  # lazy: --host/--token path needs no SDK
    cfg = Config(profile=profile) if profile else Config()
    return cfg.host.rstrip("/"), dict(cfg.authenticate())


def _resolve_source(explicit: str | None) -> str:
    """Return the local file/dir to upload: the explicit one, else the newest dist/*.whl."""
    if explicit:
        if not os.path.exists(explicit):
            raise SystemExit(f"source not found: {explicit}")
        return explicit
    candidates = sorted(glob.glob("dist/*.whl"), key=os.path.getmtime)
    if not candidates:
        raise SystemExit("no wheel in dist/ — build one first: python setup.py bdist_wheel")
    return candidates[-1]


def _iter_files(source: str):
    """Yield every file under source (recursively) if it's a dir, else source itself."""
    if os.path.isdir(source):
        for dirpath, _, filenames in os.walk(source):
            for fn in sorted(filenames):
                yield os.path.join(dirpath, fn)
    else:
        yield source


def _upload_file(host: str, headers: dict, local: str, dest_path: str,
                 overwrite: bool, made_dirs: set) -> None:
    """mkdirs the parent (once) and import one local file to dest_path via REST."""
    parent = dest_path.rsplit("/", 1)[0]
    if parent and parent not in made_dirs:
        _request("POST", f"{host}/api/2.0/workspace/mkdirs", headers, {"path": parent})
        made_dirs.add(parent)
    with open(local, "rb") as fh:
        content_b64 = base64.b64encode(fh.read()).decode("ascii")
    _request("POST", f"{host}/api/2.0/workspace/import", headers, {
        "path": dest_path,
        "format": "AUTO",
        "content": content_b64,
        "overwrite": overwrite,
    })
    print(f"Uploaded {dest_path}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Upload the governance wheel to a workspace path")
    ap.add_argument("--host", default=None, help="workspace URL, e.g. https://adb-123.azuredatabricks.net")
    ap.add_argument("--token", default=None, help="personal access token (use with --host)")
    ap.add_argument("--profile", default=None,
                    help=".databrickscfg profile used when --host/--token are not given")
    ap.add_argument("--src", "--wheel", dest="src", default=None,
                    help="local file OR directory to upload (default: newest dist/*.whl)")
    ap.add_argument("--dest-dir", default="/Workspace/Shared/gov-test/dist",
                    help="workspace directory to upload into")
    ap.add_argument("--no-overwrite", action="store_true", help="fail if a target already exists")
    args = ap.parse_args(argv)

    source = _resolve_source(args.src)
    dest_dir = args.dest_dir.rstrip("/")
    overwrite = not args.no_overwrite

    host, auth = resolve_auth(args.host, args.token, args.profile)
    headers = {**auth, "Content-Type": "application/json"}

    # Destination path for each file = dest_dir + its path relative to the source's parent.
    # For a single file this is dest_dir/<basename>; for a directory the tree is preserved
    # under dest_dir/<dirname>/...
    base_parent = os.path.dirname(os.path.normpath(source)) or "."
    made_dirs: set = set()
    print(f"Uploading {source} -> {dest_dir} on {host} ...", file=sys.stderr)
    count = 0
    for local in _iter_files(source):
        rel = os.path.relpath(local, base_parent).replace(os.sep, "/")
        _upload_file(host, headers, local, f"{dest_dir}/{rel}", overwrite, made_dirs)
        count += 1
    print(f"Done — {count} file(s) uploaded.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
