"""End-to-end engine orchestration: validate / plan / apply across all kinds."""

import unittest

from databricks.sdk.service import iam

from governance.config.loader import EnvironmentSettings, Settings
from governance.config.models import GroupEntry, ObjectEntry
from governance.engine import GovernanceEngine
from governance.resolve import IdentityIndex
from helpers import FakePermissions, FakeSecrets, FakeWS, acr, aclitem, cluster, group, sp, user


def _settings(**kw):
    base = dict(
        environments={"dev": EnvironmentSettings(profile="x", group_scope="workspace")},
        protected_principals=["admins"],
        allow_group_deletion=True,
    )
    base.update(kw)
    return Settings(**base)


def _ws():
    perms = FakePermissions(state={
        ("clusters", "c1"): [
            acr([iam.PermissionLevel.CAN_MANAGE], group_name="data-engineers"),
            acr([iam.PermissionLevel.CAN_ATTACH_TO], user_name="drift@x.com"),
        ],
        ("authorization", "tokens"): [],
    })
    secrets = FakeSecrets(acls={"etl": [aclitem("data-engineers", "MANAGE"), aclitem("old@x.com", "READ")]})
    return FakeWS(
        permissions=perms,
        secrets=secrets,
        clusters=[cluster("c1", "shared")],
        users=[user("u-alice", "alice@x.com"), user("u-bob", "bob@x.com")],
        service_principals=[sp("sp-etl", "app-etl")],
        groups=[
            group("g-de", "data-engineers", [("u-alice", "alice@x.com"), ("u-drift", "drift@x.com")]),
            group("g-legacy", "legacy-team"),
            group("g-admins", "admins"),
        ],
    )


def _config():
    return {
        "groups": [GroupEntry.from_dict({"name": "data-engineers", "members": [
            {"user": "alice@x.com"}, {"user": "bob@x.com"}]}, "x")],
        "clusters": [ObjectEntry.from_dict({"name": "shared", "acls": [
            {"group": "data-engineers", "permission": "CAN_MANAGE"},
            {"group": "data-analysts", "permission": "CAN_ATTACH_TO"}]}, "x")],
        "secret-scopes": [ObjectEntry.from_dict({"name": "etl", "acls": [
            {"group": "data-engineers", "permission": "MANAGE"},
            {"service_principal": "app-etl", "permission": "READ"}]}, "x")],
        "tokens": [ObjectEntry.from_dict({"acls": [
            {"group": "admins", "permission": "CAN_USE"}]}, "x")],
    }


def _engine(ws):
    e = GovernanceEngine(_settings(), "dev")
    e._ws = ws
    e._ws_index = IdentityIndex(ws)
    return e


class EnginePlanTests(unittest.TestCase):
    def test_plan_makes_no_changes(self):
        ws = _ws()
        plan, audit = _engine(ws).run(_config(), dry_run=True, allow_group_deletion=True)
        self.assertTrue(plan.has_changes)
        self.assertEqual(audit, [])
        self.assertEqual(ws.permissions.set_calls, [])   # no side effects on plan
        self.assertEqual(ws.secrets.puts, [])
        self.assertEqual(ws.groups.created, [])
        self.assertEqual(ws.groups.deleted, [])

    def test_groups_reconciled_first(self):
        # canonical order places groups before clusters/secrets/tokens
        plan, _ = _engine(_ws()).run(_config(), dry_run=True, allow_group_deletion=True)
        kinds = [c.kind for c in plan.changes]
        self.assertLess(kinds.index("groups"), kinds.index("clusters"))


class EngineApplyTests(unittest.TestCase):
    def test_apply_side_effects_and_audit(self):
        ws = _ws()
        result, audit = _engine(ws).run(_config(), dry_run=False, allow_group_deletion=True)
        # permissions: clusters + tokens
        self.assertEqual({(t, i) for t, i, _ in ws.permissions.set_calls},
                         {("clusters", "c1"), ("authorization", "tokens")})
        # secrets
        self.assertIn(("etl", "app-etl", "READ"), ws.secrets.puts)
        self.assertIn(("etl", "old@x.com"), ws.secrets.deletes)
        # groups
        self.assertIn("g-legacy", ws.groups.deleted)
        self.assertNotIn("g-admins", ws.groups.deleted)
        self.assertGreaterEqual(len(audit), 4)

    def test_type_filter(self):
        ws = _ws()
        _engine(ws).run(_config(), types=["secret-scopes"], dry_run=False, allow_group_deletion=True)
        self.assertEqual(ws.permissions.set_calls, [])   # only secrets touched
        self.assertNotEqual(ws.secrets.puts, [])


class EngineValidateTests(unittest.TestCase):
    def test_validate_clean(self):
        cfg = {
            "clusters": [ObjectEntry.from_dict({"name": "shared", "acls": [
                {"group": "data-engineers", "permission": "CAN_MANAGE"}]}, "x")],
            "groups": [GroupEntry.from_dict({"name": "data-engineers", "members": [{"user": "alice@x.com"}]}, "x")],
        }
        issues = _engine(_ws()).validate(cfg)
        self.assertEqual(issues, [])

    def test_validate_flags_bad_permission_and_principal(self):
        cfg = {"clusters": [ObjectEntry.from_dict({"name": "shared", "acls": [
            {"group": "ghost-group", "permission": "NOT_A_LEVEL"}]}, "x")]}
        issues = _engine(_ws()).validate(cfg)
        self.assertTrue(any("invalid permission" in i for i in issues))
        self.assertTrue(any("ghost-group" in i for i in issues))

    def test_validate_flags_missing_object(self):
        cfg = {"clusters": [ObjectEntry.from_dict({"name": "does-not-exist", "acls": []}, "x")]}
        issues = _engine(_ws()).validate(cfg)
        self.assertTrue(any("does-not-exist" in i for i in issues))


if __name__ == "__main__":
    unittest.main()
