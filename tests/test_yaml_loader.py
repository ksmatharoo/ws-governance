"""YAML loader + the config-govtest-yaml sample (group-managing / workspace-only model).

This sample manages its own groups (identity.yaml defines gov-test-team and its members),
so unlike the IdP-groups model the config carries a `groups:` block. Tests here validate
the YAML loads and that the sample is internally consistent against its own inventory. They
are skipped where PyYAML is absent (e.g. locked-down offline environments); the
missing-dependency test runs everywhere.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from governance import inventory as inventory_mod
from governance.config import yaml_loader
from governance.errors import ConfigError

REPO = Path(__file__).resolve().parents[1]
YAML_ROOT = REPO / "config-govtest-yaml"
ENV = "govtest"
HAVE_YAML = yaml_loader._HAVE_YAML


class YamlDependencyTests(unittest.TestCase):
    def test_clear_error_when_pyyaml_missing(self):
        if HAVE_YAML:
            self.skipTest("PyYAML is installed; missing-dependency path not exercised")
        with self.assertRaises(ConfigError) as cm:
            yaml_loader.load_settings(YAML_ROOT)
        self.assertIn("PyYAML", str(cm.exception))


@unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
class YamlConfigTests(unittest.TestCase):
    def test_settings_are_workspace_only(self):
        s = yaml_loader.load_settings(YAML_ROOT)
        self.assertEqual(s.environments[ENV].profile, "govtest")
        # workspace-only model: no account scope, no account block
        self.assertEqual(s.environments[ENV].group_scope, "workspace")
        self.assertIsNone(s.account)
        self.assertIn("admins", s.protected_principals)

    def test_config_manages_groups(self):
        # This sample owns its groups: identity.yaml defines gov-test-team + members.
        cfg = yaml_loader.load_environment(YAML_ROOT, ENV)
        self.assertIn("groups", cfg, f"{ENV} must manage groups in this model")
        names = {g.name for g in cfg["groups"]}
        self.assertIn("gov-test-team", names)

    def test_config_validates_clean_against_its_inventory(self):
        # Every group / user / SP / object an ACL references is backed by _inventory.yaml.
        cfg = yaml_loader.load_environment(YAML_ROOT, ENV)
        inv_path = inventory_mod.find_inventory(YAML_ROOT / "environments" / ENV)
        self.assertIsNotNone(inv_path, f"no inventory for {ENV}")
        issues = inventory_mod.check(cfg, inventory_mod.load_inventory(inv_path))
        self.assertEqual(issues, [], f"{ENV} inventory gaps: {issues}")

    def test_missing_settings_raises(self):
        with TemporaryDirectory() as d:
            with self.assertRaises(ConfigError):
                yaml_loader.load_settings(Path(d))

    def test_underscore_files_skipped_in_merge(self):
        # _inventory.yaml must not be parsed as object config.
        cfg = yaml_loader.load_environment(YAML_ROOT, ENV)
        self.assertNotIn("identities", cfg)
        self.assertNotIn("resources", cfg)


if __name__ == "__main__":
    unittest.main()
