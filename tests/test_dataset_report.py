from pathlib import Path
import re
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python-lib"))
from branchdatasync.dataset_report import render_dataset_table


class DatasetReportTests(unittest.TestCase):
    def setUp(self):
        self.current = Mock(spec=["list_datasets", "list_recipes", "project_key"])
        self.current.project_key = "CURRENT"
        self.current.list_recipes.return_value = []
        self.current.list_datasets.return_value = [SimpleNamespace(name="orders<&>", type="PostgreSQL")]
        self.source = Mock(spec=["list_datasets", "get_dataset", "project_key"])
        self.source.project_key = "SOURCE"
        self.source.list_datasets.return_value = [SimpleNamespace(name="orders<&>", type="Filesystem")]
        location_patch = patch("branchdatasync.dataset_report.get_dataset_location")
        self.location = location_patch.start()
        self.location.side_effect = lambda key, name: key + "." + name
        self.addCleanup(location_patch.stop)

    def test_table_uses_matching_source_type_and_current_type(self):
        html = render_dataset_table(self.current, self.source)
        self.assertIn('<table aria-label="Project datasets"', html)
        self.assertIn("orders&lt;&amp;&gt;", html)
        self.assertIn("PostgreSQL", html)
        self.assertNotIn("Filesystem", html)
        self.assertNotIn(">Source type</th>", html)
        self.assertNotIn("Original size", html)
        self.assertNotIn("Original rows", html)
        self.assertNotIn(">Status</th>", html)
        self.source.get_dataset.assert_not_called()
        self.assertEqual(html.count('<tr '), 2)
        self.location.assert_any_call("SOURCE", "orders<&>")
        self.location.assert_any_call("CURRENT", "orders<&>")

    def test_missing_dataset_does_not_request_metrics(self):
        self.source.list_datasets.return_value = []
        html = render_dataset_table(self.current, self.source)
        self.assertIn("Unavailable", html)
        self.source.get_dataset.assert_not_called()
        self.location.assert_called_once_with("CURRENT", "orders<&>")

    def test_missing_selection_still_shows_dataset_table(self):
        html = render_dataset_table(self.current)
        self.assertIn("PostgreSQL", html)
        self.assertIn("Unavailable", html)

    def test_input_dataset_has_no_location_row_or_location_lookup(self):
        self.current.list_recipes.return_value = [{
            "inputs": {"main": {"items": [{"ref": "orders<&>"}]}}, "outputs": {},
        }]
        html = render_dataset_table(self.current, self.source)
        self.assertIn(">input</td>", html)
        self.assertEqual(html.count('<tr '), 1)
        self.assertNotIn("Source location", html)
        self.location.assert_not_called()

    def test_source_access_error_keeps_table(self):
        self.source.list_datasets.side_effect = PermissionError("denied")
        with self.assertLogs(level="WARNING"):
            html = render_dataset_table(self.current, self.source)
        self.assertIn("Unable to read", html)
        self.assertIn("PostgreSQL", html)

    def test_input_only_usage_then_source_type_then_name(self):
        self.current.list_datasets.return_value = [
            SimpleNamespace(name=name, type="Filesystem")
            for name in ["z_input", "b_input", "a_input", "intermediate", "unused", "result"]
        ]
        self.source.list_datasets.return_value = [
            SimpleNamespace(name=name, type=source_type)
            for name, source_type in [("z_input", "Snowflake"), ("b_input", "Azure"),
                                      ("a_input", "Azure"), ("intermediate", "Snowflake"),
                                      ("unused", "Azure"), ("result", "Snowflake")]
        ]
        source_types = {dataset.name: dataset.type for dataset in self.source.list_datasets.return_value}
        for dataset in self.current.list_datasets.return_value:
            dataset.type = source_types[dataset.name]
        self.current.list_recipes.return_value = [
            {"inputs": {"main": {"items": [{"ref": "CURRENT.z_input"}, {"ref": "a_input"}]},
                        "lookup": {"items": [{"ref": "b_input"}]}},
             "outputs": {"main": {"items": [{"ref": "intermediate"}]}}},
            {"inputs": {"main": {"items": [{"ref": "intermediate"}]}},
             "outputs": {"main": {"items": [{"ref": "CURRENT.result"}]}}},
        ]
        html = render_dataset_table(self.current, self.source)
        expected_order = ["unused", "intermediate", "result", "a_input", "b_input", "z_input"]
        positions = [html.index(name) for name in expected_order]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(html.count(">input</td>"), 3)
        self.assertEqual(html.count(">output</td>"), 3)
        self.assertIn('<strong style="color:#1672b8;font-weight:700;">Snowflake</strong>', html)

    def test_foreign_references_do_not_classify_local_names_as_inputs(self):
        self.current.list_recipes.return_value = [
            {"inputs": {"main": {"items": [{"ref": "OTHER.orders<&>"}]}}, "outputs": {}}
        ]
        html = render_dataset_table(self.current, self.source)
        self.assertIn(">output</td>", html)
        self.assertNotIn(">input</td>", html)

    def test_dry_run_does_not_dispatch_copy_handlers(self):
        with patch("branchdatasync.dataset_report.copy_dataset") as dispatcher:
            html = render_dataset_table(self.current, self.source)
        dispatcher.assert_not_called()
        self.assertNotIn(">Rows copied</th>", html)

    def test_run_mode_displays_result_without_writing_data(self):
        self.current.list_datasets.return_value[0].type = "Filesystem"
        from branchdatasync.copy_operations import CopyResult
        with patch("branchdatasync.dataset_report.copy_dataset", return_value=CopyResult("Success", 12, "Filesystem data copied")):
            html = render_dataset_table(self.current, self.source, dry_run=False)
        self.assertIn(">Success</td>", html)
        self.assertIn(">12</td>", html)
        self.assertIn("Filesystem data copied", html)
        self.assertIn('colspan="6"', html)
        self.source.get_dataset.assert_not_called()

    def test_overwrite_setting_is_passed_to_copy_dispatcher(self):
        from branchdatasync.copy_operations import CopyResult
        with patch("branchdatasync.dataset_report.copy_dataset", return_value=CopyResult("Success", 1, "Copied")) as dispatcher:
            render_dataset_table(self.current, self.source, dry_run=False, overwrite_existing=True)
        self.assertTrue(dispatcher.call_args.args[0].overwrite_existing)

    def test_run_mode_skips_inputs_and_handles_missing_source(self):
        self.current.list_recipes.return_value = [{
            "inputs": {"main": {"items": [{"ref": "orders<&>"}]}}, "outputs": {},
        }]
        with patch("branchdatasync.dataset_report.copy_dataset") as dispatcher:
            html = render_dataset_table(self.current, self.source, dry_run=False)
        dispatcher.assert_not_called()
        self.assertIn(">Skipped</td>", html)
        self.location.assert_not_called()
        self.current.list_recipes.return_value = []
        self.source.list_datasets.return_value = []
        html = render_dataset_table(self.current, self.source, dry_run=False)
        self.assertIn(">Failure</td>", html)
        self.assertIn("No matching dataset", html)

    def test_result_colors_apply_only_to_dataset_cells(self):
        from branchdatasync.copy_operations import CopyResult
        cases = [
            (CopyResult("Success", 12, "Copied"), "#e8f5e9"),
            (CopyResult("Failure", None, "Copy failed"), "#ffebee"),
            (CopyResult("Skipped", 0, "Source unavailable", source_unavailable=True), "#fff3e0"),
            (CopyResult("Skipped", 0, "Target dataset already exists; overwrite is disabled.", target_exists=True), "#fff3e0"),
        ]
        for result, color in cases:
            with self.subTest(status=result.status):
                with patch("branchdatasync.dataset_report.copy_dataset", return_value=result):
                    html = render_dataset_table(self.current, self.source, dry_run=False)
                rows = re.findall(r'<tr style=.*?</tr>', html)
                self.assertEqual(rows[0].count("background-color:" + color), 6)
                self.assertNotIn(color, rows[1])
                dot_color = {"#e8f5e9": "#2e7d32", "#ffebee": "#c62828", "#fff3e0": "#e07800"}[color]
                self.assertIn("background-color:" + dot_color, rows[0])
                self.assertIn("border-radius:50%", rows[0])
                self.assertLess(rows[0].index("orders&lt;&amp;&gt;"), rows[0].index('role="img"'))
                self.assertIn("radial-gradient", rows[0])
                self.assertIn("box-shadow", rows[0])
                self.assertNotIn(dot_color, rows[1])

    def test_missing_source_is_orange_and_listing_error_is_red_in_dry_run(self):
        self.source.list_datasets.return_value = []
        html = render_dataset_table(self.current, self.source)
        self.assertEqual(html.count("background-color:#fff3e0"), 3)
        self.source.list_datasets.side_effect = PermissionError("denied")
        with self.assertLogs(level="WARNING"):
            html = render_dataset_table(self.current, self.source)
        self.assertEqual(html.count("background-color:#ffebee"), 3)

    def test_run_shared_location_warning_is_inside_details_without_second_row(self):
        from branchdatasync.copy_operations import CopyResult
        result = CopyResult("Skipped", 0, "Shared source and target location.", shared_location=True)
        with patch("branchdatasync.dataset_report.copy_dataset", return_value=result):
            html = render_dataset_table(self.current, self.source, dry_run=False)
        rows = re.findall(r'<tr style=.*?</tr>', html)
        self.assertEqual(len(rows), 1)
        cells = re.findall(r'<td[^>]*>(.*?)</td>', rows[0])
        self.assertIn("Shared source and target location.", cells[5])
        self.assertIn("Same location, nothing to change.", cells[5])
        self.assertLess(cells[5].index("Shared source"), cells[5].index("Same location"))
        self.assertIn('title="Source and target', cells[5])

    def test_dry_run_keeps_shared_location_warning_in_second_row(self):
        self.location.return_value = "/shared/data"
        self.location.side_effect = None
        html = render_dataset_table(self.current, self.source)
        self.assertEqual(len(re.findall(r'<tr style=.*?</tr>', html)), 2)
        self.assertIn('colspan="3"', html)
        self.assertIn("Same location, nothing to change.", html)



if __name__ == "__main__":
    unittest.main()
