"""GroupHandler plan and apply (create / membership / delete lifecycle)."""

import unittest

from governance.config.models import GroupEntry
from governance.handlers.groups import GroupHandler
from governance.resolve import IdentityIndex
from helpers import FakeWS, group, sp, user


def _index(**kw):
    ws = FakeWS(**kw)
    return ws, IdentityIndex(ws)


def _desired():
    return [
        GroupEntry.from_dict({"name": "data-engineers", "members": [
            {"user": "alice@x.com"}, {"user": "bob@x.com"},
            {"service_principal": "app-etl"}, {"group": "new-oncall"}]}, "x"),
        GroupEntry.from_dict({"name": "new-oncall", "members": [{"user": "carol@x.com"}]}, "x"),
    ]


def _world(**overrides):
    return dict(
        users=[user("u-alice", "alice@x.com"), user("u-bob", "bob@x.com"), user("u-carol", "carol@x.com")],
        service_principals=[sp("sp-etl", "app-etl")],
        groups=[
            group("g-de", "data-engineers", [("u-alice", "alice@x.com"), ("u-drift", "drift@x.com")]),
            group("g-legacy", "legacy-team"),
            group("g-admins", "admins"),
        ],
        **overrides,
    )


class GroupPlanTests(unittest.TestCase):
    def test_plan_create_delete_membership_protected(self):
        ws, index = _index(**_world())
        handler = GroupHandler(ws, index, protected_principals=["keep-me"], allow_deletion=True)
        plan = handler.plan(_desired())
        self.assertEqual(plan.to_create, ["new-oncall"])
        self.assertEqual(plan.to_delete, ["legacy-team"])
        self.assertIn("admins", plan.protected_from_delete)
        de = next(m for m in plan.membership if m.name == "data-engineers")
        # bob + app-etl + nested new-oncall added; drift removed
        self.assertTrue(any("bob@x.com" in a for a in de.add))
        self.assertTrue(any("new-oncall" in a for a in de.add))
        self.assertTrue(any("drift@x.com" in r for r in de.remove))

    def test_deletion_disabled(self):
        ws, index = _index(**_world())
        handler = GroupHandler(ws, index, allow_deletion=False)
        plan = handler.plan(_desired())
        self.assertEqual(plan.to_delete, [])

    def test_missing_member_raises(self):
        ws, index = _index(users=[], service_principals=[], groups=[group("g-de", "data-engineers")])
        handler = GroupHandler(ws, index)
        with self.assertRaises(Exception):
            handler.plan([GroupEntry.from_dict({"name": "data-engineers", "members": [{"user": "ghost@x.com"}]}, "x")])


class GroupApplyTests(unittest.TestCase):
    def test_apply_creates_patches_deletes_in_order(self):
        ws, index = _index(**_world())
        handler = GroupHandler(ws, index, allow_deletion=True)
        actions = handler.apply(_desired())
        self.assertIn("new-oncall", ws.groups.created)
        self.assertIn("g-legacy", ws.groups.deleted)
        self.assertNotIn("g-admins", ws.groups.deleted)          # protected builtin
        self.assertIn("g-de", [p[0] for p in ws.groups.patched])  # membership reconciled
        kinds = [a["action"] for a in actions]
        self.assertIn("create_group", kinds)
        self.assertIn("update_membership", kinds)
        self.assertIn("delete_group", kinds)

    def test_apply_no_delete_when_disabled(self):
        ws, index = _index(**_world())
        handler = GroupHandler(ws, index, allow_deletion=False)
        handler.apply(_desired())
        self.assertEqual(ws.groups.deleted, [])


if __name__ == "__main__":
    unittest.main()
