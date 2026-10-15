"""Entitlements: config parsing, offline lint, inventory checks, handler and engine."""

import json
import tempfile
import unittest
from pathlib import Path

from databricks.sdk.service import iam, iamv2

from governance.config.loader import EnvironmentSettings, Settings, load_environment
from governance.config.models import EntitlementEntry, GroupEntry
from governance.engine import GovernanceEngine
from governance.errors import ConfigError
from governance.handlers.entitlements import EntitlementsHandler
from governance.inventory import Inventory, check
from governance.resolve import IdentityIndex
from governance.schema import lint_environment
from helpers import FakeWS, FakeWorkspaceIamV2, group, sp, user

SP = "11111111-1111-4111-8111-111111111111"
ACCT_SP = "22222222-2222-4222-8222-222222222222"


def _account_iam():
    """Account principals not yet assigned to the workspace."""
    return FakeWorkspaceIamV2(
        groups=[iamv2.Group(group_id="901", group_name="analysts")],
        users=[iamv2.User(username="bob@x.com", full_name=None, account_user_status=None, user_id="902")],
        service_principals=[iamv2.ServicePrincipal(
            display_name="bi", account_sp_status=None, application_id=ACCT_SP, service_principal_id="903")],
    )


def _entry(data):
    return EntitlementEntry.from_dict(data, "x")


def _ws():
    return FakeWS(
        users=[user("u-alice", "alice@x.com", ["workspace-access"])],
        service_principals=[sp("sp-etl", SP, ["workspace-access", "allow-cluster-create"])],
        groups=[
            group("g-de", "data-engineers", entitlements=["workspace-access", "allow-cluster-create"]),
            group("g-admins", "admins", entitlements=["workspace-access", "allow-cluster-create"]),
        ],
    )


def _handler(ws, protected=()):
    return EntitlementsHandler(ws, IdentityIndex(ws), protected)


class EntitlementConfigTests(unittest.TestCase):
    def test_each_principal_type(self):
        for key, name in (("group", "g"), ("user", "a@x.com"), ("service_principal", SP)):
            e = _entry({key: name, "entitlements": ["workspace-access"]})
            self.assertEqual(e.principal, (key, name))
            self.assertEqual(e.identifier(), f"{key}:{name}")

    def test_empty_list_allowed(self):
        self.assertEqual(_entry({"group": "g", "entitlements": []}).entitlements, [])

    def test_entitlements_key_required(self):
        with self.assertRaises(ConfigError):
            _entry({"group": "g"})

    def test_entitlements_must_be_list(self):
        with self.assertRaises(ConfigError):
            _entry({"group": "g", "entitlements": "workspace-access"})

    def test_exactly_one_principal(self):
        with self.assertRaises(ConfigError):
            _entry({"group": "g", "user": "a@x.com", "entitlements": []})
        with self.assertRaises(ConfigError):
            _entry({"entitlements": []})

    def test_unknown_field_rejected(self):
        with self.assertRaises(ConfigError):
            _entry({"group": "g", "entitlements": [], "permission": "CAN_USE"})

    def test_loader_parses_and_rejects_duplicate_principal(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "environments" / "dev"
            env.mkdir(parents=True)
            doc = {"entitlements": [{"group": "g", "entitlements": ["workspace-access"]}]}
            (env / "identity.json").write_text(json.dumps(doc))
            cfg = load_environment(Path(tmp), "dev")
            self.assertIsInstance(cfg["entitlements"][0], EntitlementEntry)

            doc["entitlements"].append({"group": "g", "entitlements": []})
            (env / "identity.json").write_text(json.dumps(doc))
            with self.assertRaisesRegex(ConfigError, "Duplicate principal 'group:g'"):
                load_environment(Path(tmp), "dev")


class EntitlementLintTests(unittest.TestCase):
    def test_clean(self):
        cfg = {"entitlements": [_entry({"group": "g", "entitlements": ["workspace-access"]})]}
        self.assertEqual(lint_environment(cfg), [])

    def test_invalid_duplicate_and_non_string_values(self):
        cfg = {"entitlements": [_entry({"group": "g", "entitlements": [
            "workspace-acess", "workspace-access", "workspace-access", {"x": 1}]})]}
        issues = lint_environment(cfg)
        self.assertTrue(any("invalid entitlement 'workspace-acess'" in i for i in issues))
        self.assertTrue(any("listed more than once" in i for i in issues))
        self.assertTrue(any("must be strings" in i for i in issues))

    def test_malformed_principal(self):
        cfg = {"entitlements": [_entry({"service_principal": "etl-sp", "entitlements": []})]}
        self.assertTrue(any("not a valid UUID" in i for i in lint_environment(cfg)))


class EntitlementInventoryTests(unittest.TestCase):
    def test_principals_checked(self):
        inv = Inventory.from_dict({"identities": {"users": [{"user_name": "a@x.com"}], "groups": [{"name": "g"}]}})
        cfg = {"entitlements": [
            _entry({"group": "g", "entitlements": []}),
            _entry({"user": "a@x.com", "entitlements": []}),
            _entry({"service_principal": SP, "entitlements": []}),
        ]}
        issues = check(cfg, inv)
        self.assertEqual(len(issues), 1)
        self.assertIn(SP, issues[0])


class EntitlementsHandlerTests(unittest.TestCase):
    def test_plan_add_and_remove_without_writes(self):
        ws = _ws()
        change, audit = _handler(ws).reconcile(
            _entry({"group": "data-engineers", "entitlements": ["workspace-access", "databricks-sql-access"]}),
            dry_run=True,
        )
        self.assertEqual(change.adds, ["databricks-sql-access"])
        self.assertEqual(change.removes, ["allow-cluster-create"])
        self.assertEqual(change.unchanged, 1)
        self.assertEqual((audit, ws.groups.patched), ([], []))

    def test_apply_patches_scim(self):
        ws = _ws()
        _, audit = _handler(ws).reconcile(
            _entry({"group": "data-engineers", "entitlements": ["workspace-access", "databricks-sql-access"]}),
            dry_run=False,
        )
        gid, ops = ws.groups.patched[0]
        self.assertEqual(gid, "g-de")
        self.assertEqual([(o.op, o.path, o.value) for o in ops], [
            (iam.PatchOp.ADD, "entitlements", [{"value": "databricks-sql-access"}]),
            (iam.PatchOp.REMOVE, 'entitlements[value eq "allow-cluster-create"]', None),
        ])
        self.assertEqual(audit[0]["action"], "update_entitlements")

    def test_no_op_when_in_sync(self):
        ws = _ws()
        change, audit = _handler(ws).reconcile(
            _entry({"user": "alice@x.com", "entitlements": ["workspace-access"]}), dry_run=False)
        self.assertFalse(change.changed)
        self.assertEqual((audit, ws.users.patched), ([], []))

    def test_service_principal_uses_sp_api(self):
        ws = _ws()
        _handler(ws).reconcile(_entry({"service_principal": SP, "entitlements": []}), dry_run=False)
        sp_id, ops = ws.service_principals.patched[0]
        self.assertEqual(sp_id, "sp-etl")
        self.assertEqual(len(ops), 2)  # both entitlements removed

    def test_protected_principal_keeps_entitlements(self):
        ws = _ws()
        change, _ = _handler(ws, protected=["data-engineers"]).reconcile(
            _entry({"group": "data-engineers", "entitlements": ["databricks-sql-access"]}), dry_run=False)
        self.assertEqual(change.removes, [])
        self.assertEqual(change.adds, ["databricks-sql-access"])
        self.assertTrue(change.protected)
        _, ops = ws.groups.patched[0]
        self.assertEqual([o.op for o in ops], [iam.PatchOp.ADD])

    def test_admins_always_protected(self):
        ws = _ws()
        change, _ = _handler(ws).reconcile(_entry({"group": "admins", "entitlements": []}), dry_run=False)
        self.assertEqual(change.removes, [])
        self.assertEqual(ws.groups.patched, [])

    def test_group_created_this_run_planned_as_all_adds(self):
        change, _ = _handler(_ws()).reconcile(
            _entry({"group": "new-team", "entitlements": ["workspace-access"]}),
            dry_run=True, pending_groups=["new-team"])
        self.assertEqual(change.adds, ["workspace-access"])


class WorkspaceAssignmentTests(unittest.TestCase):
    def _ws(self):
        ws = _ws()
        ws.workspace_iam_v2 = _account_iam()
        return ws

    def test_plan_assigns_account_group_without_writes(self):
        ws = self._ws()
        change, audit = _handler(ws).reconcile(
            _entry({"group": "analysts", "entitlements": ["workspace-consume"]}), dry_run=True)
        self.assertTrue(change.create)
        self.assertEqual(change.adds, ["workspace-consume"])
        self.assertEqual((audit, ws.workspace_iam_v2.assigned), ([], []))

    def test_apply_assigns_each_principal_type_as_consumer(self):
        ws = self._ws()
        handler = _handler(ws)
        for key, name in (("group", "analysts"), ("user", "bob@x.com"), ("service_principal", ACCT_SP)):
            _, audit = handler.reconcile(_entry({key: name, "entitlements": ["workspace-consume"]}), dry_run=False)
            self.assertEqual(audit[0]["action"], "assign_to_workspace")
        self.assertEqual(
            [(a.principal_id, a.entitlements) for a in ws.workspace_iam_v2.assigned],
            [(pid, [iamv2.Entitlement.WORKSPACE_CONSUME]) for pid in (901, 902, 903)],
        )

    def test_entitlement_names_mapped_to_api_enum(self):
        ws = self._ws()
        _handler(ws).reconcile(_entry({"group": "analysts", "entitlements": [
            "workspace-access", "databricks-sql-access", "allow-cluster-create"]}), dry_run=False)
        self.assertEqual(ws.workspace_iam_v2.assigned[0].entitlements, [
            iamv2.Entitlement.ALLOW_CLUSTER_CREATE,
            iamv2.Entitlement.DATABRICKS_SQL_ACCESS,
            iamv2.Entitlement.WORKSPACE_ACCESS,
        ])

    def test_empty_entitlements_leaves_unassigned_principal_alone(self):
        ws = self._ws()
        change, audit = _handler(ws).reconcile(_entry({"group": "analysts", "entitlements": []}), dry_run=False)
        self.assertFalse(change.changed)
        self.assertEqual((audit, ws.workspace_iam_v2.assigned), ([], []))

    def test_assigned_principal_still_uses_scim(self):
        ws = self._ws()
        _handler(ws).reconcile(_entry({"group": "data-engineers", "entitlements": ["workspace-consume"]}),
                               dry_run=False)
        self.assertEqual(ws.workspace_iam_v2.assigned, [])
        self.assertEqual(ws.groups.patched[0][0], "g-de")

    def test_unknown_everywhere_is_an_error(self):
        with self.assertRaisesRegex(Exception, "not found in the workspace or its account"):
            _handler(self._ws()).reconcile(_entry({"group": "ghost", "entitlements": []}), dry_run=True)


class EntitlementsEngineTests(unittest.TestCase):
    def _engine(self, ws):
        settings = Settings(
            environments={"dev": EnvironmentSettings(profile="x", group_scope="workspace")},
            protected_principals=["admins"],
            allow_group_deletion=False,
        )
        e = GovernanceEngine(settings, "dev")
        e._ws = ws
        e._ws_index = IdentityIndex(ws)
        return e

    def _config(self):
        return {
            "groups": [GroupEntry.from_dict({"name": "new-team", "members": [{"user": "alice@x.com"}]}, "x")],
            "entitlements": [
                _entry({"group": "new-team", "entitlements": ["workspace-access"]}),
                _entry({"group": "ghost", "entitlements": []}),
            ],
        }

    def test_plan_orders_after_groups_and_reports_errors(self):
        plan, audit = self._engine(_ws()).run(self._config(), dry_run=True)
        kinds = [c.kind for c in plan.changes]
        self.assertLess(kinds.index("groups"), kinds.index("entitlements"))
        new_team = next(c for c in plan.changes if c.identifier == "group:new-team")
        self.assertEqual(new_team.adds, ["workspace-access"])
        ghost = next(c for c in plan.changes if c.identifier == "group:ghost")
        self.assertIn("not found", ghost.error)
        self.assertEqual(audit, [])

    def test_apply_entitles_group_created_this_run(self):
        ws = _ws()
        _, audit = self._engine(ws).run(self._config(), dry_run=False)
        self.assertIn("new-team", ws.groups.created)
        self.assertIn("g-new-team", [gid for gid, _ in ws.groups.patched])
        self.assertTrue(any(a["action"] == "update_entitlements" for a in audit))

    def test_validate_flags_bad_value_and_unknown_principal(self):
        issues = self._engine(_ws()).validate({"entitlements": [
            _entry({"group": "ghost", "entitlements": ["nope"]})]})
        self.assertTrue(any("invalid entitlement 'nope'" in i for i in issues))
        self.assertTrue(any("ghost" in i for i in issues))

    def test_validate_accepts_unassigned_account_principal(self):
        ws = _ws()
        ws.workspace_iam_v2 = _account_iam()
        issues = self._engine(ws).validate({"entitlements": [
            _entry({"group": "analysts", "entitlements": ["workspace-consume"]})]})
        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()
