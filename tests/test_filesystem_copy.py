from dataclasses import replace
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python-lib"))
from branchdatasync.copy_operations import CopyContext, copy_dataset
from branchdatasync.filesystem_copy import _has_files


class FilesystemCopyTests(unittest.TestCase):
    def setUp(self):
        self.context = CopyContext("SOURCE", "TARGET", "orders", "Filesystem", "Filesystem", "/source", "/target")
        self.source, self.target = Mock(), Mock()
        self.target.spec_item = {}
        for dataset, path in [(self.source, "/source"), (self.target, "/target")]:
            dataset.get_location_info.return_value = {"locationInfoType": "FS", "info": {"path": path}}
            dataset.get_files_info.return_value = {"globalPaths": [], "pathsByPartition": {}}
        self.source.get_files_info.return_value = {"globalPaths": [{"path": "data.csv"}], "pathsByPartition": {}}
        self.columns = [{"name": "name", "type": "string"}, {"name": "amount", "type": "double"}]
        self.source.read_schema.return_value = self.columns
        self.data = [("café", 1.5), ("", None)]
        self.source.iter_tuples.side_effect = lambda **kwargs: (row for row in self.data)
        self.writer = MagicMock()
        self.writer.__enter__.return_value = self.writer
        self.target.get_writer.return_value = self.writer
        self.source_api, self.target_api = Mock(), Mock()
        self.source_settings = {"type": "Filesystem", "partitioning": {"dimensions": []}}
        self.target_settings = {"type": "Filesystem", "partitioning": {"dimensions": []}}
        self.source_api.get_settings.return_value.get_raw.return_value = self.source_settings
        self.target_api.get_settings.return_value.get_raw.return_value = self.target_settings
        self.client = Mock()
        self.client.get_project.side_effect = lambda key: types.SimpleNamespace(get_dataset=lambda name: self.source_api if key == "SOURCE" else self.target_api)
        self.dataiku = types.ModuleType("dataiku")
        self.dataiku.Dataset = Mock(side_effect=lambda name, project_key, ignore_flow: self.source if project_key == "SOURCE" else self.target)
        self.dataiku.api_client = lambda: self.client
        modules = patch.dict(sys.modules, {"dataiku": self.dataiku})
        modules.start()
        self.addCleanup(modules.stop)

    def test_streams_all_rows_with_schema_and_closes_writer(self):
        result = copy_dataset(self.context)
        self.assertEqual((result.status, result.rows_copied), ("Success", 2))
        self.source.iter_tuples.assert_called_once_with(sampling="head", limit=None)
        self.assertEqual([call.args[0] for call in self.writer.write_tuple.call_args_list], self.data)
        self.target.write_schema.assert_called_once_with(self.columns)
        self.writer.__exit__.assert_called_once()
        self.target_api.clear.assert_not_called()
        self.assertFalse(self.target.spec_item["appendMode"])

    def test_empty_source_still_opens_and_closes_writer(self):
        self.data = []
        result = copy_dataset(self.context)
        self.assertEqual((result.status, result.rows_copied), ("Success", 0))
        self.writer.write_tuple.assert_not_called()
        self.writer.__exit__.assert_called_once()

    def test_missing_target_root_can_be_created_by_the_writer(self):
        self.target.get_files_info.side_effect = Exception(
            "Failed to get the dataset files info: b'Root path of the dataset orders does not exist'"
        )
        result = copy_dataset(self.context)
        self.assertEqual((result.status, result.rows_copied), ("Success", 2))
        self.target.get_writer.assert_called_once()
        self.target_api.clear.assert_not_called()

    def test_missing_target_root_with_error_code_is_allowed_with_overwrite(self):
        self.target.get_files_info.side_effect = Exception("ERR_FSPROVIDER_ROOT_PATH_DOES_NOT_EXIST")
        result = copy_dataset(replace(self.context, overwrite_existing=True))
        self.assertEqual(result.status, "Success")
        self.target_api.clear.assert_not_called()

    def test_missing_source_root_error_is_not_treated_as_an_empty_source(self):
        self.source.get_files_info.side_effect = Exception("Root path of the dataset orders does not exist")
        result = copy_dataset(self.context)
        self.assertEqual((result.status, result.rows_copied), ("Skipped", 0))
        self.assertIn("Source dataset SOURCE.orders", result.message)
        self.assertTrue(result.source_unavailable)
        self.assertIn("target left unchanged", result.message)
        self.target.write_schema.assert_not_called()
        self.target.get_writer.assert_not_called()

    def test_populated_target_is_skipped_without_overwrite(self):
        self.target.get_files_info.return_value = {"globalPaths": [{"path": "old.csv", "size": 0}]}
        result = copy_dataset(self.context)
        self.assertEqual((result.status, result.rows_copied), ("Skipped", 0))
        self.assertIn("already contains files", result.message)
        self.target.write_schema.assert_not_called()
        self.target.get_writer.assert_not_called()
        self.target_api.clear.assert_not_called()

    def test_overwrite_clears_existing_data_and_does_not_append(self):
        self.target.get_files_info.return_value = {"globalPaths": [{"path": "old.csv"}]}
        result = copy_dataset(replace(self.context, overwrite_existing=True))
        self.assertEqual(result.status, "Success")
        self.target_api.clear.assert_called_once_with()
        self.assertFalse(self.target.spec_item["appendMode"])

    def test_nested_and_aliased_roots_are_protected(self):
        for path, status in [("/source/child", "Failure"), ("/source/../source", "Skipped"), ("/", "Failure")]:
            with self.subTest(path=path):
                self.target.get_location_info.return_value["info"]["path"] = path
                result = copy_dataset(replace(self.context, overwrite_existing=True))
                self.assertEqual(result.status, status)
        self.target_api.clear.assert_not_called()
        self.target.write_schema.assert_not_called()

    def test_file_uri_alias_is_recognized(self):
        self.target.get_location_info.return_value["info"]["path"] = "file:///source"
        self.assertEqual(copy_dataset(self.context).status, "Skipped")
        self.target.get_writer.assert_not_called()

    def test_incompatible_partitions_fail_before_writes(self):
        self.source_settings["partitioning"]["dimensions"] = [{"name": "day", "type": "time"}]
        with self.assertLogs(level="ERROR"):
            result = copy_dataset(replace(self.context, overwrite_existing=True))
        self.assertEqual(result.status, "Failure")
        self.target_api.clear.assert_not_called()
        self.target.write_schema.assert_not_called()

    def test_each_partition_is_read_once_and_written_to_its_matching_partition(self):
        dimensions = [{"name": "region", "type": "value"}]
        self.source_settings["partitioning"]["dimensions"] = dimensions
        self.target_settings["partitioning"]["dimensions"] = dimensions
        self.source.list_partitions.return_value = ["FR", "GB"]
        readers = []
        def dataset(name, project_key, ignore_flow):
            if project_key == "TARGET":
                return self.target
            if not readers:
                readers.append(self.source)
                return self.source
            reader = Mock()
            reader.iter_tuples.side_effect = lambda **kwargs: (row for row in [(str(len(readers)),)])
            readers.append(reader)
            return reader
        self.dataiku.Dataset.side_effect = dataset
        result = copy_dataset(self.context)
        self.assertEqual((result.status, result.rows_copied), ("Success", 2))
        readers[1].add_read_partitions.assert_called_once_with("FR")
        readers[2].add_read_partitions.assert_called_once_with("GB")
        self.assertEqual([call.args[0] for call in self.target.set_write_partition.call_args_list], ["FR", "GB"])
        self.source.iter_tuples.assert_not_called()

    def test_reader_failure_closes_stream_and_reports_partial_copy(self):
        closed = []
        def rows(**kwargs):
            try:
                yield ("first", 1)
                raise RuntimeError("Source read failed")
            finally:
                closed.append(True)
        self.source.iter_tuples.side_effect = rows
        with self.assertLogs(level="ERROR"):
            result = copy_dataset(self.context)
        self.assertEqual(result.status, "Failure")
        self.assertIsNone(result.rows_copied)
        self.assertIn("partially updated", result.message)
        self.assertTrue(closed)
        self.writer.__exit__.assert_called_once()

    def test_writer_close_failure_is_not_success(self):
        self.writer.__exit__.side_effect = RuntimeError("Commit failed")
        with self.assertLogs(level="ERROR"):
            result = copy_dataset(self.context)
        self.assertEqual(result.status, "Failure")
        self.assertIsNone(result.rows_copied)

    def test_unreadable_source_prevents_target_changes(self):
        self.source.get_files_info.side_effect = PermissionError("Source unreadable")
        with self.assertLogs(level="ERROR"):
            result = copy_dataset(replace(self.context, overwrite_existing=True))
        self.assertEqual((result.status, result.rows_copied), ("Failure", 0))
        self.target.write_schema.assert_not_called()
        self.target_api.clear.assert_not_called()

    def test_missing_source_storage_prevents_target_changes(self):
        self.source.get_files_info.return_value = {"globalPaths": None, "pathsByPartition": {}}
        result = copy_dataset(replace(self.context, overwrite_existing=True))
        self.assertEqual(result.status, "Skipped")
        self.target.write_schema.assert_not_called()
        self.target_api.clear.assert_not_called()

    def test_source_with_schema_but_no_files_leaves_existing_target_untouched(self):
        self.source.get_files_info.return_value = {"globalPaths": [], "pathsByPartition": {}}
        self.target.get_files_info.return_value = {"globalPaths": [{"path": "existing.csv"}]}
        result = copy_dataset(replace(self.context, overwrite_existing=True))
        self.assertEqual((result.status, result.rows_copied), ("Skipped", 0))
        self.assertIn("no data files", result.message)
        self.source.iter_tuples.assert_not_called()
        self.target.write_schema.assert_not_called()
        self.target_api.clear.assert_not_called()
        self.target.get_writer.assert_not_called()

    def test_unknown_target_contents_are_not_treated_as_empty(self):
        self.target.get_files_info.side_effect = PermissionError("Target unreadable")
        with self.assertLogs(level="ERROR"):
            result = copy_dataset(self.context)
        self.assertEqual(result.status, "Failure")
        self.target.get_writer.assert_not_called()
        with self.assertRaises(ValueError):
            _has_files({})


if __name__ == "__main__":
    unittest.main()
