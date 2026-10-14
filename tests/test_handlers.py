"""PermissionsHandler and SecretsHandler reconcile logic."""

import unittest

from databricks.sdk.service import iam

from governance.config.models import ObjectEntry
from governance.handlers.permissions import PermissionsHandler
from governance.handlers.secrets import SecretsHandler
from governance.objecttypes import get_spec
from helpers import FakePermissions, FakeSecrets, FakeWS, acr, aclitem, cluster, pipeline


class PermissionsHandlerTests(unittest.TestCase):
    def _ws(self):
        perms = FakePermissions(state={
            ("clusters", "c1"): [
                acr([iam.PermissionLevel.CAN_MANAGE], group_name="data-engineers"),
                acr([iam.PermissionLevel.CAN_ATTACH_TO], user_name="drift@x.com"),
            ],
            ("authorization", "tokens"): [],
        })
        return FakeWS(permissions=perms, clusters=[cluster("c1", "shared")])

    def test_plan_computes_drift(self):
        ws = self._ws()
        h = PermissionsHandler(ws)
        spec = get_spec("clusters")
        entry = ObjectEntry.from_dict({"name": "shared", "acls": [
            {"group": "data-engineers", "permission": "CAN_MANAGE"},
            {"group": "data-analysts", "permission": "CAN_ATTACH_TO"}]}, "x")
        change, audit = h.reconcile(spec, entry, dry_run=True)
        self.assertTrue(any("data-analysts" in a for a in change.adds))
        self.assertTrue(any("drift@x.com" in r for r in change.removes))
        self.assertEqual(audit, [])                      # dry run: no writes
        self.assertEqual(ws.permissions.set_calls, [])

    def test_apply_calls_set_with_effective(self):
        ws = self._ws()
        h = PermissionsHandler(ws, protected_principals=["admins"])
        spec = get_spec("clusters")
        entry = ObjectEntry.from_dict({"name": "shared", "acls": [
            {"group": "data-engineers", "permission": "CAN_MANAGE"}]}, "x")
        change, audit = h.reconcile(spec, entry, dry_run=False)
        self.assertEqual(len(ws.permissions.set_calls), 1)
        self.assertEqual(len(audit), 1)
        # drift removed on apply
        t, i, acl = ws.permissions.set_calls[0]
        self.assertEqual((t, i), ("clusters", "c1"))

    def test_apply_resends_owner(self):
        # Jobs/pipelines reject set() without exactly one owner; it must ride along unchanged.
        perms = FakePermissions(state={("pipelines", "p1"): [
            acr([iam.PermissionLevel.IS_OWNER], user_name="owner@x.com"),
        ]})
        ws = FakeWS(permissions=perms, pipelines=[pipeline("p1", "etl")])
        h = PermissionsHandler(ws)
        entry = ObjectEntry.from_dict({"name": "etl", "acls": [
            {"group": "data-engineers", "permission": "CAN_VIEW"}]}, "x")
        change, _ = h.reconcile(get_spec("pipelines"), entry, dry_run=False)
        self.assertEqual(change.removes, [])             # owner never shows as drift
        _, _, acl = ws.permissions.set_calls[0]
        self.assertIn(("owner@x.com", iam.PermissionLevel.IS_OWNER),
                      [(r.user_name, r.permission_level) for r in acl])

    def test_tokens_use_fixed_id(self):
        ws = self._ws()
        h = PermissionsHandler(ws)
        spec = get_spec("tokens")
        entry = ObjectEntry.from_dict({"acls": [{"group": "admins", "permission": "CAN_USE"}]}, "x")
        self.assertEqual(h.object_id(spec, entry), "tokens")
        self.assertEqual(h.identifier(spec, entry), "tokens")


class SecretsHandlerTests(unittest.TestCase):
    def _ws(self):
        return FakeWS(secrets=FakeSecrets(acls={"etl": [
            aclitem("data-engineers", "MANAGE"),
            aclitem("olduser@x.com", "READ"),
        ]}))

    def test_add_change_remove(self):
        ws = self._ws()
        h = SecretsHandler(ws)
        spec = get_spec("secret-scopes")
        entry = ObjectEntry.from_dict({"name": "etl", "acls": [
            {"group": "data-engineers", "permission": "MANAGE"},       # unchanged
            {"service_principal": "app-etl", "permission": "READ"}]}, "x")   # new
        change, audit = h.reconcile(spec, entry, dry_run=False)
        self.assertTrue(any("app-etl" in a for a in change.adds))
        self.assertTrue(any("olduser@x.com" in r for r in change.removes))
        self.assertIn(("etl", "app-etl", "READ"), ws.secrets.puts)
        self.assertIn(("etl", "olduser@x.com"), ws.secrets.deletes)
        self.assertEqual(len(audit), 1)

    def test_permission_change_puts_not_deletes(self):
        ws = self._ws()
        h = SecretsHandler(ws)
        spec = get_spec("secret-scopes")
        entry = ObjectEntry.from_dict({"name": "etl", "acls": [
            {"group": "data-engineers", "permission": "READ"},          # MANAGE -> READ
            {"user": "olduser@x.com", "permission": "READ"}]}, "x")      # keep
        change, _ = h.reconcile(spec, entry, dry_run=False)
        self.assertIn(("etl", "data-engineers", "READ"), ws.secrets.puts)
        self.assertEqual(ws.secrets.deletes, [])

    def test_protected_principal_preserved(self):
        ws = self._ws()
        h = SecretsHandler(ws, protected_principals=["olduser@x.com"])
        spec = get_spec("secret-scopes")
        entry = ObjectEntry.from_dict({"name": "etl", "acls": [
            {"group": "data-engineers", "permission": "MANAGE"}]}, "x")
        h.reconcile(spec, entry, dry_run=False)
        self.assertEqual(ws.secrets.deletes, [])   # protected principal not removed


if __name__ == "__main__":
    unittest.main()
