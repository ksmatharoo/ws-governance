"""CI-mode guard, provenance collection, and audit-log stamping."""

import json
import os
import unittest
from tempfile import TemporaryDirectory
from unittest import mock

from governance.cli import _ci_guard, _in_ci, _provenance
from governance.config.loader import EnvironmentSettings
from governance.report import write_audit_log


class CiGuardTests(unittest.TestCase):
    def test_local_env_never_blocked(self):
        env = EnvironmentSettings(profile="dev", require_ci=False)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(_ci_guard("dev", env))

    def test_ci_only_env_blocked_without_marker(self):
        env = EnvironmentSettings(host="https://p", require_ci=True)
        with mock.patch.dict(os.environ, {}, clear=True):
            msg = _ci_guard("prod", env)
        self.assertIsNotNone(msg)
        self.assertIn("prod", msg)

    def test_ci_only_env_allowed_in_ci(self):
        env = EnvironmentSettings(host="https://p", require_ci=True)
        with mock.patch.dict(os.environ, {"GOVERNANCE_CI": "1"}, clear=True):
            self.assertTrue(_in_ci())
            self.assertIsNone(_ci_guard("prod", env))


class ProvenanceTests(unittest.TestCase):
    def test_github_run_url_constructed(self):
        env = {
            "GITHUB_SHA": "abc123",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_REPOSITORY": "org/repo",
            "GITHUB_RUN_ID": "42",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            prov = _provenance()
        self.assertEqual(prov["git_sha"], "abc123")
        self.assertEqual(prov["ci_run_url"], "https://github.com/org/repo/actions/runs/42")

    def test_explicit_overrides_win(self):
        env = {"GOVERNANCE_GIT_SHA": "deadbeef", "GOVERNANCE_CI_RUN_URL": "https://ci/run/1"}
        with mock.patch.dict(os.environ, env, clear=True):
            prov = _provenance()
        self.assertEqual(prov["git_sha"], "deadbeef")
        self.assertEqual(prov["ci_run_url"], "https://ci/run/1")

    def test_empty_when_absent(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            prov = _provenance()
        self.assertIsNone(prov["git_sha"])
        self.assertIsNone(prov["ci_run_url"])


class AuditProvenanceTests(unittest.TestCase):
    def test_audit_log_records_provenance(self):
        with TemporaryDirectory() as d:
            path = write_audit_log(
                "prod", [{"op": "x"}], directory=d,
                actor="gov-sp", git_sha="abc123", ci_run_url="https://ci/run/1",
            )
            with open(path, encoding="utf-8") as fh:
                payload = json.loads(fh.read())
        self.assertEqual(payload["actor"], "gov-sp")
        self.assertEqual(payload["git_sha"], "abc123")
        self.assertEqual(payload["ci_run_url"], "https://ci/run/1")
        self.assertEqual(payload["action_count"], 1)

    def test_audit_log_provenance_optional(self):
        with TemporaryDirectory() as d:
            path = write_audit_log("dev", [], directory=d)
            with open(path, encoding="utf-8") as fh:
                payload = json.loads(fh.read())
        self.assertIsNone(payload["actor"])
        self.assertIsNone(payload["git_sha"])


if __name__ == "__main__":
    unittest.main()
