from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python-lib"))
from branchtablesinitialization.copy_operations import CopyContext, CopyResult, copy_dataset


class CopyOperationsTests(unittest.TestCase):
    def setUp(self):
        self.context = CopyContext("SOURCE", "TARGET", "orders", "Snowflake", "Snowflake", "SOURCE_TABLE", "TARGET_TABLE")

    def test_filesystem_dispatches_to_streaming_handler(self):
        context = replace(self.context, source_type="Filesystem", target_type="Filesystem")
        with patch("branchtablesinitialization.filesystem_copy.stream_filesystem", return_value=CopyResult("Success", 2, "Copied")) as handler:
            result = copy_dataset(context)
        handler.assert_called_once_with(context)
        self.assertEqual(result.status, "Success")
        self.assertEqual(result.rows_copied, 2)

    def test_dispatch_supplies_the_complete_context_and_supports_success_contract(self):
        handler = Mock(return_value=CopyResult("Success", 123, "Copied and verified."))
        with patch.dict("branchtablesinitialization.copy_operations.COPY_HANDLERS", {("snowflake", "snowflake"): handler}):
            result = copy_dataset(self.context)
        handler.assert_called_once_with(self.context)
        self.assertEqual(result.rows_copied, 123)
        self.assertEqual(result.status, "Success")

    def test_shared_or_unknown_locations_do_not_reach_handlers(self):
        handler = Mock()
        context = replace(self.context, source_type="Filesystem", target_type="Filesystem")
        with patch.dict("branchtablesinitialization.copy_operations.COPY_HANDLERS", {("filesystem", "filesystem"): handler}):
            shared = copy_dataset(replace(context, target_location="SOURCE_TABLE"))
            unavailable = copy_dataset(replace(context, target_location=None))
        handler.assert_not_called()
        self.assertEqual(shared.status, "Skipped")
        self.assertEqual(unavailable.status, "Failure")

    def test_unsupported_types_and_cross_type_copies_are_explicit(self):
        for kind in ["Filesystem", "PostgreSQL"]:
            result = copy_dataset(replace(self.context, target_type=kind))
            self.assertEqual(result.status, "Not implemented")
            self.assertEqual(result.rows_copied, 0)

    def test_handler_failure_does_not_invent_a_row_count(self):
        handler = Mock(side_effect=RuntimeError("Example handler failure"))
        with patch.dict("branchtablesinitialization.copy_operations.COPY_HANDLERS", {("snowflake", "snowflake"): handler}), self.assertLogs(level="ERROR"):
            result = copy_dataset(self.context)
        self.assertEqual(result.status, "Failure")
        self.assertIsNone(result.rows_copied)


if __name__ == "__main__":
    unittest.main()
