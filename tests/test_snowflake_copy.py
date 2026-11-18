from dataclasses import replace
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python-lib"))
from branchtablesinitialization.copy_operations import CopyContext, copy_dataset
from branchtablesinitialization.snowflake_copy import _identifier_parts, _CONTEXT_SQL


def frame(rows):
    value = Mock()
    value.to_dict.return_value = rows
    return value


class SnowflakeCopyTests(unittest.TestCase):
    def setUp(self):
        self.context = CopyContext("SOURCE", "TARGET", "orders", "Snowflake", "Snowflake", "source_table", "target_table")
        self.source = Mock()
        self.target = Mock()
        self.source_info = {"databaseType": "Snowflake", "table": "SOURCE-TABLE", "quotedResolvedTableName": '"DB"."SCHEMA"."SOURCE-TABLE"'}
        self.target_info = {"databaseType": "Snowflake", "table": "TARGET-TABLE", "quotedResolvedTableName": '"DB"."SCHEMA"."TARGET-TABLE"'}
        self.source.get_location_info.return_value = {"locationInfoType": "SQL", "info": self.source_info}
        self.target.get_location_info.return_value = {"locationInfoType": "SQL", "info": self.target_info}
        self.source_executor, self.target_executor = Mock(), Mock()
        self.source_session = {"ACCOUNT_ID": "ACCOUNT", "REGION_ID": "AWS_EU_WEST_1", "DB_NAME": "SOURCE_DB", "SCHEMA_NAME": "PUBLIC"}
        self.target_session = {"ACCOUNT_ID": "ACCOUNT", "REGION_ID": "AWS_EU_WEST_1", "DB_NAME": "TARGET_DB", "SCHEMA_NAME": "PUBLIC"}
        self.source_executor.query_to_df.side_effect = lambda query, **kwargs: frame([self.source_session])
        self.target_executor.query_to_df.side_effect = self.target_query
        self.dataiku = types.ModuleType("dataiku")
        self.dataiku.Dataset = Mock(side_effect=lambda name, project_key, ignore_flow: self.source if project_key == "SOURCE" else self.target)
        self.dataiku.SQLExecutor2 = Mock(side_effect=lambda dataset: self.source_executor if dataset is self.source else self.target_executor)
        self.dataiku.api_client = Mock()
        modules = patch.dict(sys.modules, {"dataiku": self.dataiku})
        modules.start()
        self.addCleanup(modules.stop)

    def target_query(self, query, **kwargs):
        if query == _CONTEXT_SQL:
            return frame([self.target_session])
        if query.startswith("SELECT COUNT(*)"):
            if "TARGET_EXISTS" in query:
                return frame([{"TARGET_EXISTS": 0}])
            return frame([{"ROWS_COPIED": 1234}])
        return frame([{"CLONE_COMPLETE": 1}])

    def clone_calls(self):
        return [call for call in self.target_executor.query_to_df.call_args_list if call.kwargs.get("pre_queries")]

    def test_creates_clone_on_target_connection_and_returns_target_count(self):
        result = copy_dataset(self.context)
        self.assertEqual(result.status, "Success")
        self.assertEqual(result.rows_copied, 1234)
        self.assertEqual(self.clone_calls()[0].kwargs, {
            "pre_queries": ['CREATE TABLE "DB"."SCHEMA"."TARGET-TABLE" CLONE "DB"."SCHEMA"."SOURCE-TABLE"'],
            "post_queries": ["COMMIT"],
        })
        self.source_executor.query_to_df.assert_called_once_with(_CONTEXT_SQL)
        self.target_executor.query_to_df.assert_any_call('SELECT COUNT(*) AS ROWS_COPIED FROM "DB"."SCHEMA"."TARGET-TABLE"')
        self.source.get_location_info.assert_called_once_with(sensitive_info=False)
        self.dataiku.api_client.assert_not_called()

    def test_existing_target_failure_does_not_replace_or_retry(self):
        def query(sql, **kwargs):
            if kwargs.get("pre_queries"):
                raise RuntimeError("Table already exists")
            return self.target_query(sql, **kwargs)
        self.target_executor.query_to_df.side_effect = query
        with self.assertLogs(level="ERROR"):
            result = copy_dataset(self.context)
        self.assertEqual(result.status, "Failure")
        self.assertIsNone(result.rows_copied)
        self.assertIn("already exists", result.message)
        self.assertEqual(len(self.clone_calls()), 1)
        self.assertNotIn("REPLACE", self.clone_calls()[0].kwargs["pre_queries"][0])
        self.dataiku.api_client.assert_not_called()

    def test_clone_success_is_preserved_if_row_count_fails(self):
        def query(sql, **kwargs):
            if sql.startswith("SELECT COUNT(*) AS ROWS_COPIED"):
                raise RuntimeError("Count unavailable")
            return self.target_query(sql, **kwargs)
        self.target_executor.query_to_df.side_effect = query
        with self.assertLogs(level="WARNING"):
            result = copy_dataset(self.context)
        self.assertEqual(result.status, "Success")
        self.assertIsNone(result.rows_copied)
        self.assertIn("row count is unavailable", result.message)

    def test_different_accounts_do_not_execute_ddl(self):
        self.source_session["ACCOUNT_ID"] = "OTHER_ACCOUNT"
        result = copy_dataset(self.context)
        self.assertEqual(result.status, "Failure")
        self.assertEqual(self.clone_calls(), [])

    def test_same_physical_table_is_skipped_after_resolution(self):
        self.target_info["quotedResolvedTableName"] = self.source_info["quotedResolvedTableName"]
        result = copy_dataset(self.context)
        self.assertEqual(result.status, "Skipped")
        self.assertEqual(self.clone_calls(), [])
        self.dataiku.api_client.assert_not_called()

    def test_same_display_names_in_different_databases_are_cloned(self):
        self.source_info["quotedResolvedTableName"] = self.target_info["quotedResolvedTableName"] = '"PUBLIC"."ORDERS"'
        result = copy_dataset(replace(self.context, source_location="PUBLIC.ORDERS", target_location="PUBLIC.ORDERS"))
        self.assertEqual(result.status, "Success")
        self.assertIn('"TARGET_DB"."PUBLIC"."ORDERS" CLONE "SOURCE_DB"."PUBLIC"."ORDERS"', self.clone_calls()[0].kwargs["pre_queries"][0])

    def test_query_dataset_and_injected_sql_are_rejected_before_ddl(self):
        for identifier in [None, '"DB"."SCHEMA"."TABLE"; DROP TABLE X', 'DB.SCHEMA.TABLE.']:
            with self.subTest(identifier=identifier):
                self.target_info["quotedResolvedTableName"] = identifier
                with self.assertLogs(level="ERROR"):
                    result = copy_dataset(self.context)
                self.assertEqual(result.status, "Failure")
        self.assertEqual(self.clone_calls(), [])

    def test_identifier_parser_preserves_quoted_dots_and_quotes(self):
        self.assertEqual(_identifier_parts('db."schema.with.dot"."a""b"'), ["DB", "schema.with.dot", 'a"b'])

    def test_existing_target_is_skipped_without_clone_or_marking(self):
        def query(sql, **kwargs):
            if "TARGET_EXISTS" in sql:
                return frame([{"TARGET_EXISTS": 1}])
            return self.target_query(sql, **kwargs)
        self.target_executor.query_to_df.side_effect = query
        result = copy_dataset(self.context)
        self.assertEqual(result.status, "Skipped")
        self.assertEqual(result.rows_copied, 0)
        self.assertIn("already exists", result.message)
        self.assertEqual(self.clone_calls(), [])
        self.assertTrue(result.target_exists)
        self.dataiku.api_client.assert_not_called()

    def test_overwrite_uses_create_or_replace(self):
        result = copy_dataset(replace(self.context, overwrite_existing=True))
        self.assertEqual(result.status, "Success")
        sql = self.clone_calls()[0].kwargs["pre_queries"][0]
        self.assertTrue(sql.startswith('CREATE OR REPLACE TABLE "DB"."SCHEMA"."TARGET-TABLE" CLONE '))

    def test_table_created_between_check_and_clone_is_skipped(self):
        checks = []
        def query(sql, **kwargs):
            if "TARGET_EXISTS" in sql:
                checks.append(sql)
                return frame([{"TARGET_EXISTS": int(len(checks) > 1)}])
            if kwargs.get("pre_queries"):
                raise RuntimeError("Table already exists")
            return self.target_query(sql, **kwargs)
        self.target_executor.query_to_df.side_effect = query
        result = copy_dataset(self.context)
        self.assertEqual(result.status, "Skipped")
        self.assertNotIn("REPLACE", self.clone_calls()[0].kwargs["pre_queries"][0])
        self.dataiku.api_client.assert_not_called()

    def test_existence_check_failure_prevents_clone(self):
        def query(sql, **kwargs):
            if "TARGET_EXISTS" in sql:
                raise RuntimeError("Metadata unavailable")
            return self.target_query(sql, **kwargs)
        self.target_executor.query_to_df.side_effect = query
        with self.assertLogs(level="ERROR"):
            result = copy_dataset(self.context)
        self.assertEqual(result.status, "Failure")
        self.assertEqual(self.clone_calls(), [])


if __name__ == "__main__":
    unittest.main()
