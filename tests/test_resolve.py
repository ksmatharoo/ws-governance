"""Object-id resolvers, including the SQL/BI + pipelines types from Gap 1."""

import unittest

from governance.errors import ResolutionError
from governance.objecttypes import get_spec
from governance.resolve import resolve_object_id
from helpers import FakeWS, alert, cluster, dashboard, pipeline, query


class ResolverTests(unittest.TestCase):
    def _ws(self):
        return FakeWS(
            clusters=[cluster("c-1", "shared")],
            pipelines=[pipeline("p-1", "bronze-to-silver-dlt")],
            queries=[query("q-1", "daily-revenue-query")],
            alerts=[alert("a-1", "revenue-drop-alert")],
            lakeview=[dashboard("d-1", "Revenue Overview")],
        )

    def test_cluster(self):
        self.assertEqual(resolve_object_id(self._ws(), get_spec("clusters"), "shared"), "c-1")

    def test_pipeline(self):
        self.assertEqual(resolve_object_id(self._ws(), get_spec("pipelines"), "bronze-to-silver-dlt"), "p-1")

    def test_dashboard(self):
        self.assertEqual(resolve_object_id(self._ws(), get_spec("dashboards"), "Revenue Overview"), "d-1")

    def test_query(self):
        self.assertEqual(resolve_object_id(self._ws(), get_spec("queries"), "daily-revenue-query"), "q-1")

    def test_alert(self):
        self.assertEqual(resolve_object_id(self._ws(), get_spec("alerts"), "revenue-drop-alert"), "a-1")

    def test_missing_raises(self):
        for key, ident in (("pipelines", "nope"), ("dashboards", "nope"),
                           ("queries", "nope"), ("alerts", "nope")):
            with self.assertRaises(ResolutionError):
                resolve_object_id(self._ws(), get_spec(key), ident)


if __name__ == "__main__":
    unittest.main()
