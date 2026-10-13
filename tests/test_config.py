"""Config loading, model validation, and settings parsing."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from governance.config.loader import Settings, load_environment, load_settings
from governance.config.models import AclEntry, GroupEntry, MemberEntry, ObjectEntry
from governance.errors import ConfigError


def _write(root: Path, rel: str, obj):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


class AclEntryTests(unittest.TestCase):
    def test_valid_each_principal_type(self):
        for data in (
            {"group": "g", "permission": "CAN_MANAGE"},
            {"user": "u@x.com", "permission": "CAN_VIEW"},
            {"service_principal": "app", "permission": "CAN_MANAGE_RUN"},
        ):
            entry = AclEntry.from_dict(data, "x")
            self.assertEqual(entry.principal_name, data.get("group") or data.get("user") or data.get("service_principal"))

    def test_two_principals_rejected(self):
        with self.assertRaises(ConfigError):
            AclEntry.from_dict({"group": "g", "user": "u", "permission": "CAN_VIEW"}, "x")

    def test_no_principal_rejected(self):
        with self.assertRaises(ConfigError):
            AclEntry.from_dict({"permission": "CAN_VIEW"}, "x")

    def test_missing_permission_rejected(self):
        with self.assertRaises(ConfigError):
            AclEntry.from_dict({"group": "g"}, "x")

    def test_unknown_field_rejected(self):
        with self.assertRaises(ConfigError):
            AclEntry.from_dict({"group": "g", "perm": "x"}, "x")


class GroupModelTests(unittest.TestCase):
    def test_member_exactly_one(self):
        with self.assertRaises(ConfigError):
            MemberEntry.from_dict({"user": "u", "group": "g"}, "m")
        with self.assertRaises(ConfigError):
            MemberEntry.from_dict({}, "m")

    def test_group_requires_name(self):
        with self.assertRaises(ConfigError):
            GroupEntry.from_dict({"members": []}, "g")

    def test_group_members_must_be_list(self):
        with self.assertRaises(ConfigError):
            GroupEntry.from_dict({"name": "g", "members": "nope"}, "g")

    def test_group_ok(self):
        g = GroupEntry.from_dict({"name": "g", "members": [{"user": "u@x.com"}, {"group": "nested"}]}, "g")
        self.assertEqual(g.name, "g")
        self.assertEqual([(m.member_type, m.member_name) for m in g.members],
                         [("user", "u@x.com"), ("group", "nested")])


class LoaderTests(unittest.TestCase):
    def _base(self, root):
        _write(root, "settings.json", {"environments": {"dev": {"profile": "dev"}}})

    def test_load_full_environment(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            self._base(root)
            _write(root, "environments/dev/compute.json", {
                "clusters": [{"name": "c", "acls": [{"group": "g", "permission": "CAN_MANAGE"}]}]})
            _write(root, "environments/dev/identity.json", {
                "groups": [{"name": "g", "members": [{"user": "u@x.com"}]}]})
            _write(root, "environments/dev/secrets.json", {
                "secret-scopes": [{"name": "s", "acls": [{"group": "g", "permission": "READ"}]}],
                "tokens": [{"acls": [{"group": "g", "permission": "CAN_USE"}]}]})
            cfg = load_environment(root, "dev")
            self.assertEqual(set(cfg), {"clusters", "groups", "secret-scopes", "tokens"})
            self.assertIsInstance(cfg["groups"][0], GroupEntry)
            self.assertIsInstance(cfg["clusters"][0], ObjectEntry)

    def test_name_object_rejects_path(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            self._base(root)
            _write(root, "environments/dev/x.json", {
                "clusters": [{"path": "/p", "acls": []}]})
            with self.assertRaises(ConfigError):
                load_environment(root, "dev")

    def test_path_object_requires_path(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            self._base(root)
            _write(root, "environments/dev/x.json", {"notebooks": [{"name": "n", "acls": []}]})
            with self.assertRaises(ConfigError):
                load_environment(root, "dev")

    def test_duplicate_key_across_files_rejected(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            self._base(root)
            _write(root, "environments/dev/a.json", {"clusters": []})
            _write(root, "environments/dev/b.json", {"clusters": []})
            with self.assertRaises(ConfigError):
                load_environment(root, "dev")

    def test_unknown_object_type_rejected(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            self._base(root)
            _write(root, "environments/dev/x.json", {"widgets": []})
            with self.assertRaises(ConfigError):
                load_environment(root, "dev")

    def test_tokens_singleton_rejects_multiple(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            self._base(root)
            _write(root, "environments/dev/x.json", {"tokens": [{"acls": []}, {"acls": []}]})
            with self.assertRaises(ConfigError):
                load_environment(root, "dev")


class SettingsTests(unittest.TestCase):
    def test_account_and_scope(self):
        s = Settings.from_dict({
            "environments": {"dev": {"profile": "d"}, "prod": {"profile": "p", "group_scope": "account"}},
            "account": {"profile": "acct", "account_id": "abc"},
            "allow_group_deletion": True,
        })
        self.assertEqual(s.environments["prod"].group_scope, "account")
        self.assertTrue(s.allow_group_deletion)
        self.assertEqual(s.account.profile, "acct")

    def test_account_scope_requires_account_block(self):
        with self.assertRaises(ConfigError):
            Settings.from_dict({"environments": {"prod": {"group_scope": "account"}}})

    def test_invalid_group_scope(self):
        with self.assertRaises(ConfigError):
            Settings.from_dict({"environments": {"dev": {"group_scope": "galaxy"}}})

    def test_allow_group_deletion_must_be_bool(self):
        with self.assertRaises(ConfigError):
            Settings.from_dict({"environments": {}, "allow_group_deletion": "yes"})

    def test_default_group_scope_is_workspace(self):
        s = Settings.from_dict({"environments": {"dev": {"profile": "d"}}})
        self.assertEqual(s.environments["dev"].group_scope, "workspace")
        self.assertFalse(s.allow_group_deletion)

    def test_require_ci_defaults_false_and_parses(self):
        s = Settings.from_dict({
            "environments": {
                "dev": {"profile": "d"},
                "prod": {"host": "https://p", "require_ci": True},
            }
        })
        self.assertFalse(s.environments["dev"].require_ci)
        self.assertTrue(s.environments["prod"].require_ci)

    def test_require_ci_must_be_bool(self):
        with self.assertRaises(ConfigError):
            Settings.from_dict({"environments": {"prod": {"require_ci": "yes"}}})


if __name__ == "__main__":
    unittest.main()
