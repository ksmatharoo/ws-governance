"""Tests for the TEST-ONLY prerequisite check/provision helpers."""

import json
import pathlib
import sys
import unittest
from tempfile import TemporaryDirectory

# make the sibling helper modules importable as top-level (as they run in practice)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "prerequisite"))

import check  # noqa: E402
import inventory as inv_mod  # noqa: E402
import provision  # noqa: E402
from check import Context  # noqa: E402
from databricks.sdk.service import iam, workspace as ws  # noqa: E402


# --------------------------------------------------------------------------- #
# compact fake workspace client
# --------------------------------------------------------------------------- #
class _L:
    def __init__(self, items):
        self.items = list(items)

    def list(self, **kw):
        return iter(self.items)


class _Groups(_L):
    def create(self, *, display_name=None, **kw):
        g = iam.Group(id="g-new", display_name=display_name)
        self.items.append(g)
        return g


class _Creatable(_L):
    """A list-able API that also records create() calls (clusters/warehouses/policies/pools)."""

    def __init__(self, items=()):
        super().__init__(items)
        self.created = []

    def create(self, **kw):
        self.created.append(kw)
        return type("R", (), {})()


class _WS:
    def __init__(self, dirs):
        self.dirs = set(dirs)
        self.made = []

    def get_status(self, path):
        if path in self.dirs:
            return ws.ObjectInfo(path=path, object_id=1, object_type=ws.ObjectType.DIRECTORY)
        raise RuntimeError("RESOURCE_DOES_NOT_EXIST")

    def mkdirs(self, path):
        self.made.append(path)
        self.dirs.add(path)


class _Secrets:
    def __init__(self, scopes):
        self.scopes = list(scopes)
        self.created = []

    def list_scopes(self):
        return iter([ws.SecretScope(name=s) for s in self.scopes])

    def create_scope(self, *, scope, **kw):
        self.created.append(scope)
        self.scopes.append(scope)


class _Fake:
    def __init__(self, *, groups=(), scopes=(), dirs=()):
        self.users = _L([iam.User(id="u1", user_name="me@x.com")])
        self.service_principals = _L([iam.ServicePrincipal(id="s1", application_id="app-1")])
        self.groups = _Groups(groups)
        self.secrets = _Secrets(scopes)
        self.workspace = _WS(dirs)
        self.clusters = _Creatable([])
        self.warehouses = _Creatable([])
        self.cluster_policies = _Creatable([])
        self.instance_pools = _Creatable([])
        self.jobs = _L([])   # MANUAL type; resolves to empty -> MISSING


_INV = {
    "environment": "t",
    "identities": {
        "users": [{"user_name": "me@x.com"}],
        "service_principals": [{"application_id": "app-1"}],
        "groups": [{"name": "team-x"}],
    },
    "resources": {
        "directories": [{"path": "/Workspace/Shared/x"}],
        "secret-scopes": [{"name": "scope-x"}],
        "clusters": [{"name": "cl-x", "spec": {"spark_version": "x", "node_type_id": "n"}}],
        "warehouses": [{"name": "wh-x", "spec": {"cluster_size": "2X-Small"}}],
        "jobs": [{"name": "job-x"}],  # MANUAL (no creator)
        "tokens": {"note": "singleton"},
    },
}


class InventoryModuleTests(unittest.TestCase):
    def test_load(self):
        with TemporaryDirectory() as d:
            p = pathlib.Path(d) / "inv.json"
            p.write_text(json.dumps(_INV))
            inv = inv_mod.load_inventory(p)
            self.assertEqual(inv["environment"], "t")

    def test_identities_and_resources(self):
        self.assertEqual(set(inv_mod.identities(_INV)), {"users", "service_principals", "groups"})
        res = inv_mod.resources(_INV)
        self.assertIn("directories", res)
        self.assertNotIn("tokens", res)          # singleton excluded
        self.assertNotIn("description", res)

    def test_classification(self):
        self.assertIn("secret-scopes", inv_mod.CREATABLE_RESOURCES)
        self.assertIn("clusters", inv_mod.CREATABLE_WITH_SPEC)
        self.assertIn("warehouses", inv_mod.CREATABLE_WITH_SPEC)
        self.assertIn("jobs", inv_mod.MANUAL_RESOURCES)

    def test_id_extraction(self):
        self.assertEqual(inv_mod.identity_id("users", {"user_name": "a@b.com"}), "a@b.com")
        self.assertEqual(inv_mod.resource_id("path", {"path": "/x"}), "/x")


class CheckTests(unittest.TestCase):
    def test_all_exist(self):
        fake = _Fake(groups=[iam.Group(id="g", display_name="team-x")], scopes=["scope-x"],
                     dirs=["/Workspace/Shared/x"])
        recs = check.check_all(Context(fake), _INV)
        # 'big-cluster' is MANUAL and unresolved -> MISSING, everything else EXISTS
        by = {(r.kind, r.identifier): r.status for r in recs}
        self.assertEqual(by[("users", "me@x.com")], check.EXISTS)
        self.assertEqual(by[("groups", "team-x")], check.EXISTS)
        self.assertEqual(by[("directories", "/Workspace/Shared/x")], check.EXISTS)
        self.assertEqual(by[("secret-scopes", "scope-x")], check.EXISTS)

    def test_missing_flagged(self):
        fake = _Fake(groups=[], scopes=[], dirs=["/Workspace/Shared/x"])
        recs = check.check_all(Context(fake), _INV)
        by = {(r.kind, r.identifier): r.status for r in recs}
        self.assertEqual(by[("groups", "team-x")], check.MISSING)
        self.assertEqual(by[("secret-scopes", "scope-x")], check.MISSING)


class ProvisionTests(unittest.TestCase):
    def test_dry_run_creates_nothing(self):
        fake = _Fake(groups=[], scopes=[], dirs=["/Workspace/Shared/x"])
        recs = provision.plan_and_apply(Context(fake), _INV, do_create=False)
        statuses = {(k, i): s for k, i, s, _ in recs}
        self.assertEqual(statuses[("groups", "team-x")], provision.PLAN)
        self.assertEqual(statuses[("secret-scopes", "scope-x")], provision.PLAN)
        self.assertEqual(statuses[("clusters", "cl-x")], provision.PLAN)      # spec present
        self.assertEqual(statuses[("warehouses", "wh-x")], provision.PLAN)
        self.assertEqual(statuses[("jobs", "job-x")], provision.MANUAL)       # no creator
        self.assertEqual(fake.clusters.created, [])                           # nothing created
        self.assertEqual(fake.groups.items, [])

    def test_create_makes_missing(self):
        fake = _Fake(groups=[], scopes=[], dirs=[])
        recs = provision.plan_and_apply(Context(fake), _INV, do_create=True)
        statuses = {(k, i): s for k, i, s, _ in recs}
        self.assertEqual(statuses[("groups", "team-x")], provision.CREATED)
        self.assertEqual(statuses[("secret-scopes", "scope-x")], provision.CREATED)
        self.assertEqual(statuses[("directories", "/Workspace/Shared/x")], provision.CREATED)
        self.assertEqual(statuses[("clusters", "cl-x")], provision.CREATED)
        self.assertEqual(statuses[("warehouses", "wh-x")], provision.CREATED)
        self.assertEqual(statuses[("jobs", "job-x")], provision.MANUAL)       # never auto-created
        self.assertIn("team-x", [g.display_name for g in fake.groups.items])
        self.assertIn("scope-x", fake.secrets.created)
        self.assertEqual(len(fake.clusters.created), 1)
        self.assertEqual(len(fake.warehouses.created), 1)

    def test_spec_required_for_compute(self):
        # a cluster entry with no spec cannot be created
        inv = {"resources": {"clusters": [{"name": "no-spec-cluster"}]}}
        fake = _Fake()
        recs = provision.plan_and_apply(Context(fake), inv, do_create=True)
        statuses = {(k, i): (s, d) for k, i, s, d in recs}
        status, detail = statuses[("clusters", "no-spec-cluster")]
        self.assertEqual(status, provision.MANUAL)
        self.assertIn("spec", detail)
        self.assertEqual(fake.clusters.created, [])


if __name__ == "__main__":
    unittest.main()
