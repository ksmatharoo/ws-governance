"""Module 1 (TEST-ONLY): check whether inventory-declared resources exist.

Usage:
    python3 tests/prerequisite/check.py --inventory prerequisite/govtest-env/inventory.json
    python3 tests/prerequisite/check.py --inventory <file> --profile <profile-override>

Exit 0 if everything exists, 1 if anything is missing (or on error).
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

# --- make governance (src/) and the sibling 'inventory' module importable ---
_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "src"))
sys.path.insert(0, str(_HERE.parent))

import inventory as inv_mod  # noqa: E402  (sibling module)
from governance.client import make_client  # noqa: E402
from governance.config.loader import EnvironmentSettings  # noqa: E402
from governance.errors import GovernanceError, ResolutionError  # noqa: E402
from governance.objecttypes import get_spec  # noqa: E402
from governance.resolve import IdentityIndex, resolve_object_id  # noqa: E402

EXISTS = "EXISTS"
MISSING = "MISSING"
ERROR = "ERROR"


@dataclass
class Record:
    category: str      # "identity" | "resource"
    kind: str          # users | groups | clusters | directories | ...
    identifier: str
    status: str        # EXISTS | MISSING | ERROR
    detail: str = ""


class Context:
    """Cached workspace lookups shared by check and provision."""

    def __init__(self, client):
        self.client = client
        self.index = IdentityIndex(client)
        self._scopes: Optional[Set[str]] = None

    @property
    def scope_names(self) -> Set[str]:
        if self._scopes is None:
            self._scopes = {s.name for s in self.client.secrets.list_scopes() if s.name}
        return self._scopes

    # -- existence checks --
    def identity_exists(self, kind: str, identifier: str) -> bool:
        if not identifier:
            return False
        try:
            if kind == "users":
                self.index.user(identifier)
            elif kind == "service_principals":
                self.index.service_principal(identifier)
            elif kind == "groups":
                self.index.group(identifier)
            else:
                return False
            return True
        except ResolutionError:
            return False

    def resource_exists(self, key: str, identifier: str) -> bool:
        if not identifier:
            return False
        if key == "secret-scopes":
            return identifier in self.scope_names
        try:
            resolve_object_id(self.client, get_spec(key), identifier)
            return True
        except ResolutionError:
            return False


def check_all(context: Context, inv: dict) -> List[Record]:
    records: List[Record] = []

    for kind, entries in inv_mod.identities(inv).items():
        for entry in entries:
            ident = inv_mod.identity_id(kind, entry)
            ok = context.identity_exists(kind, ident)
            records.append(Record("identity", kind, ident or "<missing-id>", EXISTS if ok else MISSING))

    for key, entries in inv_mod.resources(inv).items():
        try:
            spec = get_spec(key)
        except KeyError:
            for entry in entries:
                records.append(Record("resource", key, str(entry), ERROR, "unknown object type"))
            continue
        for entry in entries:
            ident = inv_mod.resource_id(spec.id_field, entry)
            ok = context.resource_exists(key, ident)
            records.append(Record("resource", key, ident or "<missing-id>", EXISTS if ok else MISSING))

    return records


def render(records: List[Record]) -> str:
    lines = []
    for r in records:
        mark = "OK  " if r.status == EXISTS else "MISS"
        extra = f"  ({r.detail})" if r.detail else ""
        lines.append(f"  [{mark}] {r.category}/{r.kind}: {r.identifier}{extra}")
    missing = [r for r in records if r.status != EXISTS]
    lines.append("")
    if missing:
        lines.append(f"{len(missing)} of {len(records)} declared item(s) missing.")
    else:
        lines.append(f"All {len(records)} declared item(s) exist.")
    return "\n".join(lines)


def build_context(inv: dict, profile_override: Optional[str]) -> Context:
    profile = profile_override or inv.get("profile")
    client = make_client(EnvironmentSettings(profile=profile))
    return Context(client)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check inventory-declared resources exist (test-only).")
    parser.add_argument("--inventory", required=True, help="path to inventory.json")
    parser.add_argument("--profile", default=None, help="override the profile in the inventory")
    args = parser.parse_args(argv)

    try:
        inv = inv_mod.load_inventory(args.inventory)
        context = build_context(inv, args.profile)
        records = check_all(context, inv)
    except (GovernanceError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"== prerequisite check :: environment '{inv.get('environment', '?')}' ==")
    print(render(records))
    return 0 if all(r.status == EXISTS for r in records) else 1


if __name__ == "__main__":
    sys.exit(main())
