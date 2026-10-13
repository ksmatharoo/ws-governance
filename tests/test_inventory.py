"""Offline existence checks against an environment inventory."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from governance.config.models import AclEntry, GroupEntry, MemberEntry, ObjectEntry
from governance.errors import ConfigError
from governance.inventory import Inventory, check, find_inventory, load_inventory

REPO = Path(__file__).resolve().parents[1]


def _config():
    return {
        "groups": [
            GroupEntry(
                name="data-engineers",
                members=[MemberEntry(user="alice@x.com"), MemberEntry(group="bi-viewers")],
            )
        ],
        "clusters": [
            ObjectEntry(
                name="c1",
                acls=[
                    AclEntry(permission="CAN_MANAGE", group="data-engineers"),
                    AclEntry(permission="CAN_RESTART", user="alice@x.com"),
                    AclEntry(permission="CAN_RESTART", service_principal="sp-1"),
                ],
            )
        ],
    }


def _inventory():
    return Inventory.from_dict(
        {
            "identities": {
                "users": [{"user_name": "alice@x.com"}],
                "service_principals": [{"application_id": "sp-1", "display_name": "etl"}],
                "groups": [{"name": "bi-viewers"}],  # externally provisioned
            },
            "resources": {"clusters": [{"name": "c1"}]},
        }
    )


class CheckTests(unittest.TestCase):
    def test_all_present_no_issues(self):
        self.assertEqual(check(_config(), _inventory()), [])

    def test_missing_object_reported(self):
        inv = _inventory()
        inv.resources["clusters"] = {"other"}
        issues = check(_config(), inv)
        self.assertTrue(any("c1" in i and "not found" in i for i in issues))

    def test_missing_user_reported(self):
        inv = _inventory()
        inv.users.discard("alice@x.com")
        issues = check(_config(), inv)
        self.assertTrue(any("alice@x.com" in i for i in issues))

    def test_missing_service_principal_reported(self):
        inv = _inventory()
        inv.service_principals.clear()
        issues = check(_config(), inv)
        self.assertTrue(any("sp-1" in i for i in issues))

    def test_sp_matched_by_display_name(self):
        cfg = _config()
        cfg["clusters"][0].acls[2] = AclEntry(permission="CAN_RESTART", service_principal="etl")
        self.assertEqual(check(cfg, _inventory()), [])

    def test_unknown_group_member_reported(self):
        inv = _inventory()
        inv.groups.clear()  # bi-viewers no longer provisioned and not defined in config
        issues = check(_config(), inv)
        self.assertTrue(any("bi-viewers" in i for i in issues))

    def test_config_defined_group_is_valid_target(self):
        # An ACL granting a group the config itself creates needs no inventory entry.
        cfg = _config()
        cfg["clusters"][0].acls[0] = AclEntry(permission="CAN_MANAGE", group="data-engineers")
        self.assertEqual(check(cfg, _inventory()), [])

    def test_account_group_not_assigned_reported_distinctly(self):
        # bi-viewers exists in the account but is not assigned to this workspace.
        inv = Inventory.from_dict(
            {
                "identities": {
                    "users": [{"user_name": "alice@x.com"}],
                    "service_principals": [{"application_id": "sp-1"}],
                    "groups": [{"name": "bi-viewers", "assigned": False}],
                },
                "resources": {"clusters": [{"name": "c1"}]},
            }
        )
        # Grant the unassigned group a permission directly.
        cfg = _config()
        cfg["clusters"][0].acls[0] = AclEntry(permission="CAN_MANAGE", group="bi-viewers")
        cfg["groups"][0].members = [MemberEntry(user="alice@x.com")]  # drop the nested ref
        issues = check(cfg, inv)
        self.assertTrue(any("not assigned to this workspace" in i and "bi-viewers" in i for i in issues))

    def test_assigned_group_is_ok(self):
        inv = Inventory.from_dict(
            {
                "identities": {
                    "users": [{"user_name": "alice@x.com"}],
                    "service_principals": [{"application_id": "sp-1"}],
                    "groups": [{"name": "bi-viewers"}],  # assigned (default)
                },
                "resources": {"clusters": [{"name": "c1"}]},
            }
        )
        self.assertEqual(inv.unassigned_groups, set())
        self.assertIn("bi-viewers", inv.groups)

    def test_singleton_token_skips_existence(self):
        cfg = {"tokens": [ObjectEntry(acls=[AclEntry(permission="CAN_USE", user="alice@x.com")])]}
        # No 'tokens' in inventory resources, but singleton has no identifier to check.
        self.assertEqual(check(cfg, _inventory()), [])


class LoadTests(unittest.TestCase):
    def test_load_json_inventory(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "_inventory.json"
            p.write_text(json.dumps({"resources": {"clusters": ["a", {"name": "b"}]}}), encoding="utf-8")
            inv = load_inventory(p)
            self.assertEqual(inv.resources["clusters"], {"a", "b"})

    def test_find_inventory_prefers_present_file(self):
        with TemporaryDirectory() as d:
            env = Path(d)
            self.assertIsNone(find_inventory(env))
            (env / "_inventory.json").write_text("{}", encoding="utf-8")
            self.assertEqual(find_inventory(env).name, "_inventory.json")

    def test_bundled_yaml_inventories_are_discoverable(self):
        # File-existence only (no parsing) — works even without PyYAML.
        for env in ("govtest",):
            found = find_inventory(REPO / "config-govtest-yaml" / "environments" / env)
            self.assertIsNotNone(found, f"no inventory found for {env}")
            self.assertEqual(found.name, "_inventory.yaml")


if __name__ == "__main__":
    unittest.main()
