"""Static (offline) config value linting."""

import unittest

from governance.config.models import GroupEntry, ObjectEntry
from governance.schema import is_abs_path, is_email, is_uuid, lint_environment, principal_issue


class FormatTests(unittest.TestCase):
    def test_email(self):
        self.assertTrue(is_email("a@b.com"))
        self.assertFalse(is_email("not-an-email"))
        self.assertFalse(is_email(" a@b.com "))
        self.assertFalse(is_email("a@b"))

    def test_uuid(self):
        self.assertTrue(is_uuid("11111111-1111-4111-8111-111111111111"))
        self.assertFalse(is_uuid("etl-runner"))
        self.assertFalse(is_uuid("1111"))

    def test_abs_path(self):
        self.assertTrue(is_abs_path("/Workspace/Shared/x"))
        self.assertFalse(is_abs_path("relative/path"))
        self.assertFalse(is_abs_path("/bad\npath"))

    def test_principal_issue(self):
        self.assertIsNone(principal_issue("user", "a@b.com"))
        self.assertIsNone(principal_issue("service_principal", "11111111-1111-4111-8111-111111111111"))
        self.assertIsNone(principal_issue("group", "account users"))
        self.assertIsNotNone(principal_issue("user", "nope"))
        self.assertIsNotNone(principal_issue("service_principal", "nope"))
        self.assertIsNotNone(principal_issue("group", "  "))


class LintEnvironmentTests(unittest.TestCase):
    def test_clean_config_passes(self):
        cfg = {
            "clusters": [ObjectEntry.from_dict({"name": "c", "acls": [
                {"group": "data-engineers", "permission": "CAN_MANAGE"},
                {"user": "a@b.com", "permission": "CAN_ATTACH_TO"},
                {"service_principal": "11111111-1111-4111-8111-111111111111", "permission": "CAN_RESTART"}]}, "x")],
            "groups": [GroupEntry.from_dict({"name": "data-engineers", "members": [{"user": "a@b.com"}]}, "x")],
            "tokens": [ObjectEntry.from_dict({"acls": [{"group": "admins", "permission": "CAN_USE"}]}, "x")],
        }
        self.assertEqual(lint_environment(cfg), [])

    def test_invalid_permission_flagged(self):
        cfg = {"clusters": [ObjectEntry.from_dict({"name": "c", "acls": [
            {"group": "g", "permission": "CAN_QUERY"}]}, "x")]}   # not valid for clusters
        issues = lint_environment(cfg)
        self.assertTrue(any("invalid permission 'CAN_QUERY'" in i for i in issues))

    def test_bad_user_and_sp_flagged(self):
        cfg = {"jobs": [ObjectEntry.from_dict({"name": "j", "acls": [
            {"user": "not-email", "permission": "CAN_VIEW"},
            {"service_principal": "not-uuid", "permission": "CAN_MANAGE"}]}, "x")]}
        issues = lint_environment(cfg)
        self.assertTrue(any("not a valid email" in i for i in issues))
        self.assertTrue(any("not a valid UUID" in i for i in issues))

    def test_bad_path_flagged(self):
        cfg = {"notebooks": [ObjectEntry.from_dict({"path": "relative/x", "acls": [
            {"group": "g", "permission": "CAN_READ"}]}, "x")]}
        issues = lint_environment(cfg)
        self.assertTrue(any("absolute path" in i for i in issues))

    def test_secret_permission_enum(self):
        cfg = {"secret-scopes": [ObjectEntry.from_dict({"name": "s", "acls": [
            {"group": "g", "permission": "CAN_MANAGE"}]}, "x")]}   # secrets use READ/WRITE/MANAGE
        issues = lint_environment(cfg)
        self.assertTrue(any("invalid permission" in i for i in issues))

    def test_group_member_format(self):
        cfg = {"groups": [GroupEntry.from_dict({"name": "g", "members": [
            {"user": "bad"}, {"service_principal": "bad"}]}, "x")]}
        issues = lint_environment(cfg)
        self.assertEqual(len(issues), 2)


if __name__ == "__main__":
    unittest.main()
