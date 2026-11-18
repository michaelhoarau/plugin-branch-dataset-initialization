from html import unescape
from pathlib import Path
import re
import sys
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python-lib"))
from branchtablesinitialization.dataset_location import get_dataset_location
from branchtablesinitialization.report_html import location_row


class DatasetLocationTests(unittest.TestCase):
    def setUp(self):
        self.dataiku = types.ModuleType("dataiku")
        self.dataiku.Dataset = Mock()
        self.location_info = self.dataiku.Dataset.return_value.get_location_info
        module_patch = patch.dict(sys.modules, {"dataiku": self.dataiku})
        module_patch.start()
        self.addCleanup(module_patch.stop)

    def test_resolved_sql_location_uses_explicit_project_context(self):
        self.location_info.return_value = {
            "locationInfoType": "SQL",
            "info": {"quotedResolvedTableName": '"DB"."SCHEMA"."BRANCH_TABLE"', "table": "${projectKey}_TABLE"},
        }
        self.assertEqual(get_dataset_location("BRANCH", "orders"), '"DB"."SCHEMA"."BRANCH_TABLE"')
        self.dataiku.Dataset.assert_called_once_with("orders", project_key="BRANCH", ignore_flow=True)
        self.location_info.assert_called_once_with(sensitive_info=False)

    def test_storage_path(self):
        self.location_info.return_value = {"locationInfoType": "FS", "info": {"path": "s3://bucket/branch/orders"}}
        self.assertEqual(get_dataset_location("BRANCH", "orders"), "s3://bucket/branch/orders")

    def test_unavailable_location(self):
        self.location_info.side_effect = PermissionError("denied")
        with self.assertLogs(level="WARNING"):
            self.assertIsNone(get_dataset_location("BRANCH", "orders"))

    def test_complete_sql_identifier_is_highlighted(self):
        source = "DB.PUBLIC.TSHIRTS_REVENUE-PREDICTION-SNOWFLAKE"
        target = "DB.PUBLIC.TSHIRTS_TEST1_REVENUE-PREDICTION-SNOWFLAKE"
        html = location_row(source, target, 0)
        self.assertIn('color:#8995a3;">DB.PUBLIC.</span>', html)
        self.assertIn('font-weight:700;">TSHIRTS_TEST1_REVENUE-PREDICTION-SNOWFLAKE</strong>', html)
        plain = unescape(re.sub(r"<[^>]+>", "", html))
        self.assertIn(source, plain)
        self.assertIn(target, plain)
        self.assertIn('colspan="4"', html)

    def test_s3_project_rename_highlights_whole_components(self):
        prefix = "s3://ai-workbench-dss-data/design/dataiku/"
        suffix = "/web_last_month_enriched_copy"
        html = location_row(prefix + "DKU_TSHIRTS" + suffix,
                            prefix + "DATAIKU_TSHIRTS_BRANCH_1" + suffix, 0)
        changes = re.findall(r"<strong[^>]*>(.*?)</strong>", html)
        self.assertEqual(changes, ["DKU_TSHIRTS", "DATAIKU_TSHIRTS_BRANCH_1"])
        self.assertIn('background-color:#fef3c7', html)
        self.assertIn('background-color:#dceefb', html)
        self.assertIn('>Source location</span>', html)
        self.assertIn('>Target location</span>', html)
        self.assertIn('grid-template-columns:max-content minmax(0,1fr)', html)
        plain = unescape(re.sub(r"<[^>]+>", "", html))
        self.assertIn(prefix + "DKU_TSHIRTS" + suffix, plain)
        self.assertIn(prefix + "DATAIKU_TSHIRTS_BRANCH_1" + suffix, plain)

    def test_identical_locations_show_overlap_warning_and_tooltip(self):
        html = location_row("same_table", "same_table", 0)
        self.assertIn("Same location, nothing to change.", html)
        self.assertIn('color:#c62828', html)
        self.assertIn('title="Source and target datasets point to the same location.', html)
        self.assertIn("can overwrite", html)
        self.assertNotIn("Source location &gt; Target location", html)

    def test_location_text_is_escaped_and_muted_rows_stay_grey(self):
        html = location_row('<script>alert("x")</script>', "target", 0, muted=True)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;", html)
        self.assertIn("background:#f3f4f6", html)
        self.assertNotIn("#1672b8", html)

    def test_missing_source_keeps_target_readable(self):
        html = location_row(None, "target", 0)
        self.assertIn("Unavailable", html)
        self.assertIn('font-weight:700;">target</strong>', html)
        self.assertNotIn("Same location", html)

    def test_similar_but_different_locations_do_not_show_overlap_warning(self):
        html = location_row("TSHIRTS_TABLE", "TSHIRTS_TEST1_TABLE", 0)
        self.assertNotIn("Same location", html)
        self.assertNotIn("can overwrite", html)

    def test_unavailable_locations_are_not_treated_as_overlapping(self):
        self.assertNotIn("Same location", location_row(None, None, 0))


if __name__ == "__main__":
    unittest.main()
