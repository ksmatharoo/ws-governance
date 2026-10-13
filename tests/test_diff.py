"""Pure diff logic (ACL grants and group membership)."""

import unittest

from databricks.sdk.service import iam

from governance.config.models import AclEntry
from governance.diff import (
    AclDiff,
    MembershipDiff,
    current_grants,
    current_member_ids,
    desired_grants,
)
from helpers import acr, group


class DesiredGrantsTests(unittest.TestCase):
    def test_normalizes_entries(self):
        entries = [
            AclEntry.from_dict({"group": "de", "permission": "CAN_MANAGE"}, "x"),
            AclEntry.from_dict({"user": "a@x.com", "permission": "CAN_VIEW"}, "x"),
        ]
        self.assertEqual(
            desired_grants(entries),
            {("group", "de", "CAN_MANAGE"), ("user", "a@x.com", "CAN_VIEW")},
        )


class CurrentGrantsTests(unittest.TestCase):
    def test_excludes_owner_and_inherited(self):
        perms = iam.ObjectPermissions(access_control_list=[
            acr([iam.PermissionLevel.CAN_MANAGE], group_name="de"),
            acr([iam.PermissionLevel.IS_OWNER], user_name="owner@x.com"),
            iam.AccessControlResponse(
                group_name="inh",
                all_permissions=[iam.Permission(permission_level=iam.PermissionLevel.CAN_MANAGE, inherited=True)],
            ),
        ])
        self.assertEqual(current_grants(perms), {("group", "de", "CAN_MANAGE")})


class AclDiffTests(unittest.TestCase):
    def test_add_remove_unchanged(self):
        current = {("group", "de", "CAN_MANAGE"), ("user", "drift@x.com", "CAN_VIEW")}
        desired = {("group", "de", "CAN_MANAGE"), ("user", "new@x.com", "CAN_VIEW")}
        d = AclDiff.compute(current, desired)
        self.assertEqual(d.to_add, {("user", "new@x.com", "CAN_VIEW")})
        self.assertEqual(d.to_remove, {("user", "drift@x.com", "CAN_VIEW")})
        self.assertEqual(d.unchanged, {("group", "de", "CAN_MANAGE")})
        self.assertTrue(d.changed)

    def test_protected_principal_preserved(self):
        current = {("group", "admins", "CAN_MANAGE")}
        desired = set()
        d = AclDiff.compute(current, desired, protected_principals=["admins"])
        self.assertNotIn(("group", "admins", "CAN_MANAGE"), d.to_remove)
        self.assertIn(("group", "admins", "CAN_MANAGE"), d.effective_desired)
        self.assertFalse(d.changed)

    def test_no_changes(self):
        grants = {("group", "de", "CAN_MANAGE")}
        self.assertFalse(AclDiff.compute(grants, grants).changed)


class MembershipDiffTests(unittest.TestCase):
    def test_compute(self):
        d = MembershipDiff.compute(current_ids={"a", "b"}, desired_ids={"b", "c"})
        self.assertEqual(d.to_add, {"c"})
        self.assertEqual(d.to_remove, {"a"})
        self.assertEqual(d.unchanged, {"b"})

    def test_current_member_ids(self):
        g = group("g1", "grp", [("u-a", "alice"), ("u-b", "bob")])
        self.assertEqual(current_member_ids(g), {"u-a", "u-b"})


if __name__ == "__main__":
    unittest.main()
