"""Exercise server-side copy without any dataset or S3 content streams."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python-lib"))
from branchdatasync.copy_operations import CopyContext, copy_dataset
from branchdatasync.s3_copy import _resolved_root


class S3CopyTests(unittest.TestCase):
    def setUp(self):
        self.context = CopyContext("SOURCE", "TARGET", "orders", "S3", "S3", "s3://bucket/source", "s3://bucket/target")
        self.source, self.target = Mock(), Mock()
        for dataset, prefix, connection in [(self.source, "source", "read"), (self.target, "target", "write")]:
            dataset.get_location_info.return_value = {
                "locationInfoType": "FS", "info": {"path": "s3://bucket/" + prefix, "connectionName": connection},
            }
        self.columns = [{"name": "name", "type": "string"}]
        self.source.read_schema.return_value = self.columns
        settings = {"type": "S3", "formatType": "csv", "formatParams": {"separator": ","},
                    "partitioning": {"dimensions": [], "filePathPattern": ".*"}, "params": {"path": "source"}}
        self.source_settings = deepcopy(settings)
        self.target_settings = deepcopy(settings)
        self.target_settings["params"]["path"] = "target"
        self.source_api, self.target_api = Mock(), Mock()
        self.source_api.get_settings.return_value.get_raw.return_value = self.source_settings
        self.target_api.get_settings.return_value.get_raw.return_value = self.target_settings
        self.api = Mock()
        self.api.get_project.side_effect = lambda key: types.SimpleNamespace(
            get_dataset=lambda name: self.source_api if key == "SOURCE" else self.target_api)
        self.connection_params = {"read": {"regionOrEndpoint": "eu-west-1"}, "write": {"regionOrEndpoint": "eu-west-1"}}
        self.credentials = {name: {"accessKey": name + "-key", "secretKey": name + "-secret", "sessionToken": name + "-token"}
                            for name in ("read", "write")}
        self.connections = {}
        for name in ("read", "write"):
            info = Mock()
            info.get_aws_credential.return_value = self.credentials[name]
            info.get_resolved_params.return_value = self.connection_params[name]
            self.connections[name] = types.SimpleNamespace(get_info=lambda info=info: info)
        self.api.get_connection.side_effect = self.connections.__getitem__
        self.source_s3, self.target_s3 = Mock(), Mock()
        self.source_objects = [self.object("source/part-0.csv", 123), self.object("source/part-1.csv", 0)]
        self.target_objects = []
        self.source_s3.get_paginator.return_value.paginate.side_effect = lambda **kwargs: [{"Contents": self.source_objects}]
        self.target_s3.get_paginator.return_value.paginate.side_effect = lambda **kwargs: [{"Contents": self.target_objects}]
        self.copied = {}
        def head(Bucket, Key):
            if Key in self.copied:
                return {"ContentLength": self.copied[Key], "ETag": '"target-etag"'}
            item = next(item for item in self.source_objects if item["Key"] == Key)
            return {"ContentLength": item["Size"], "ETag": item["ETag"]}
        self.target_s3.head_object.side_effect = head
        def copy(copy_source, bucket, key, **kwargs):
            item = next(item for item in self.source_objects if item["Key"] == copy_source["Key"])
            self.copied[key] = item["Size"]
        self.target_s3.copy.side_effect = copy
        self.target_s3.delete_objects.return_value = {}
        self.boto3 = types.ModuleType("boto3")
        self.boto3.client = Mock(side_effect=lambda service, **kwargs: self.source_s3 if kwargs["aws_access_key_id"] == "read-key" else self.target_s3)
        self.config = Mock(side_effect=lambda **kwargs: kwargs)
        modules = {
            "dataiku": types.SimpleNamespace(
                Dataset=Mock(side_effect=lambda name, project_key, ignore_flow: self.source if project_key == "SOURCE" else self.target),
                api_client=lambda: self.api),
            "boto3": self.boto3, "botocore": types.ModuleType("botocore"),
            "botocore.config": types.SimpleNamespace(Config=self.config),
        }
        self.modules = patch.dict(sys.modules, modules)
        self.modules.start()
        self.addCleanup(self.modules.stop)

    @staticmethod
    def object(key, size):
        return {"Key": key, "Size": size, "ETag": '"source-etag"'}

    def assert_no_writes(self):
        self.target_s3.copy.assert_not_called()
        self.target_s3.delete_objects.assert_not_called()
        self.target.write_schema.assert_not_called()
        self.target_api.clear.assert_not_called()

    def test_copies_objects_and_zero_byte_files_without_streaming_data(self):
        result = copy_dataset(self.context)
        self.assertEqual(result.status, "Success")
        self.assertIsNone(result.rows_copied)
        self.assertIn("2 objects", result.message)
        self.assertEqual(self.copied, {"target/part-0.csv": 123, "target/part-1.csv": 0})
        self.target_s3.copy.assert_any_call(
            {"Bucket": "bucket", "Key": "source/part-0.csv"}, "bucket", "target/part-0.csv",
            SourceClient=self.source_s3, ExtraArgs={"CopySourceIfMatch": '"source-etag"'},
        )
        self.target.write_schema.assert_called_once_with(self.columns)
        for dataset in (self.source, self.target):
            dataset.iter_tuples.assert_not_called()
            dataset.get_writer.assert_not_called()
            dataset.get_files_info.assert_not_called()
        for client in (self.source_s3, self.target_s3):
            client.get_object.assert_not_called()
            client.download_file.assert_not_called()
            client.upload_file.assert_not_called()
            client.close.assert_called_once()
        self.target_api.clear.assert_not_called()

    def test_managed_copy_handles_large_objects(self):
        self.source_objects[0]["Size"] = 6 * 1024 ** 3
        self.assertEqual(copy_dataset(self.context).status, "Success")
        # boto3's managed copy chooses multipart, rather than a single CopyObject.
        self.assertEqual(self.copied["target/part-0.csv"], 6 * 1024 ** 3)
        self.target_s3.copy_object.assert_not_called()

    def test_uses_resolved_connection_credentials_including_session_token(self):
        copy_dataset(self.context)
        for call, name in zip(self.boto3.client.call_args_list, ("read", "write")):
            self.assertEqual(call.kwargs["aws_access_key_id"], name + "-key")
            self.assertEqual(call.kwargs["aws_secret_access_key"], name + "-secret")
            self.assertEqual(call.kwargs["aws_session_token"], name + "-token")
            self.assertEqual(call.kwargs["region_name"], "eu-west-1")

    def test_connection_names_fall_back_to_dataset_settings(self):
        for dataset, settings, name in (
                (self.source, self.source_settings, "read"),
                (self.target, self.target_settings, "write")):
            dataset.get_location_info.return_value["info"].pop("connectionName")
            settings["params"]["connection"] = name
        result = copy_dataset(self.context)
        self.assertEqual(result.status, "Success")
        self.assertEqual([call.args[0] for call in self.api.get_connection.call_args_list], ["read", "write"])
        for dataset in (self.source, self.target):
            dataset.get_location_info.assert_called_once_with(sensitive_info=False)

    def test_connection_name_fallback_can_be_used_on_only_one_side(self):
        self.target.get_location_info.return_value["info"]["connectionName"] = None
        self.target_settings["params"]["connection"] = "write"
        self.assertEqual(copy_dataset(self.context).status, "Success")
        self.assertEqual([call.args[0] for call in self.api.get_connection.call_args_list], ["read", "write"])

    def test_missing_connection_name_identifies_the_dataset_side(self):
        for dataset, side in ((self.source, "Source"), (self.target, "Target")):
            with self.subTest(side=side):
                info = dataset.get_location_info.return_value["info"]
                name = info.pop("connectionName")
                with self.assertLogs(level="ERROR"):
                    result = copy_dataset(self.context)
                info["connectionName"] = name
                self.assertEqual((result.status, result.rows_copied), ("Failure", 0))
                self.assertIn(side + " S3 connection name", result.message)
                self.assertIn("params.connection", result.message)
                self.assert_no_writes()

    def test_unreadable_credentials_fail_without_using_local_aws_credentials(self):
        self.connections["write"].get_info().get_aws_credential.side_effect = PermissionError("sensitive-secret")
        with self.assertLogs(level="ERROR") as logs:
            result = copy_dataset(self.context)
        self.assertEqual((result.status, result.rows_copied), ("Failure", 0))
        self.assertNotIn("sensitive-secret", result.message + str(logs.output))
        self.assertIn("connection details", result.message)
        self.source_s3.close.assert_called_once()
        self.assert_no_writes()

    def test_target_encryption_and_path_style_are_honored(self):
        self.connection_params["write"].update(encryptionMode="SSE_KMS", encryptionKeyId="target-kms", usePathMode=True)
        self.assertEqual(copy_dataset(self.context).status, "Success")
        self.assertEqual(self.target_s3.copy.call_args.kwargs["ExtraArgs"]["SSEKMSKeyId"], "target-kms")
        self.assertEqual(self.target_s3.copy.call_args.kwargs["ExtraArgs"]["ServerSideEncryption"], "aws:kms")
        self.assertEqual(self.boto3.client.call_args.kwargs["config"]["s3"]["addressing_style"], "path")

    def test_custom_endpoint_is_refused_before_s3_calls(self):
        self.connection_params["write"]["regionOrEndpoint"] = "https://other-s3.example.com"
        with self.assertLogs(level="ERROR"):
            self.assertEqual(copy_dataset(self.context).status, "Failure")
        self.assert_no_writes()

    def test_shared_or_nested_roots_never_write(self):
        for path, status in [("s3://bucket/source/", "Skipped"), ("s3://bucket/source/child", "Failure"), ("s3://bucket/", "Failure")]:
            with self.subTest(path=path):
                self.target.get_location_info.return_value["info"]["path"] = path
                result = copy_dataset(replace(self.context, overwrite_existing=True))
                self.assertEqual(result.status, status)
                self.assertEqual(result.shared_location, status == "Skipped")
        self.assert_no_writes()
        self.boto3.client.assert_not_called()

    def test_different_bucket_and_sibling_prefix_are_supported(self):
        self.target.get_location_info.return_value["info"]["path"] = "s3://other/source-sibling"
        self.assertEqual(copy_dataset(self.context).status, "Success")
        self.assertEqual(self.target_s3.copy.call_args.args[1:3], ("other", "source-sibling/part-1.csv"))

    def test_invalid_locations_fail_before_writes(self):
        for path in ("/bucket/path", "s3:///path", "s3://bucket/path?query", "s3://bucket/path#fragment",
                     "s3://bucket/a/../target", "s3://user@bucket/path", "s3://bucket/a\nb"):
            with self.subTest(path=path), self.assertLogs(level="ERROR"):
                self.target.get_location_info.return_value["info"]["path"] = path
                self.assertEqual(copy_dataset(self.context).status, "Failure")
        self.assert_no_writes()

    def test_existing_target_skips_without_overwrite(self):
        self.target_objects.append(self.object("target/old.csv", 1))
        result = copy_dataset(self.context)
        self.assertEqual((result.status, result.target_exists), ("Skipped", True))
        self.assert_no_writes()

    def test_overwrite_removes_only_stale_objects_after_copy_and_schema(self):
        self.target_objects.extend([self.object("target/old.csv", 1), self.object("target/part-0.csv", 2)])
        events = Mock()
        events.attach_mock(self.target_s3.copy, "copy")
        events.attach_mock(self.target.write_schema, "schema")
        events.attach_mock(self.target_s3.delete_objects, "delete")
        self.assertEqual(copy_dataset(replace(self.context, overwrite_existing=True)).status, "Success")
        self.target_s3.delete_objects.assert_called_once_with(
            Bucket="bucket", Delete={"Objects": [{"Key": "target/old.csv"}], "Quiet": True})
        self.assertEqual([call[0] for call in events.mock_calls], ["copy", "copy", "schema", "delete"])

    def test_empty_or_marker_only_source_leaves_target_untouched(self):
        for objects in ([], [self.object("source/", 0)]):
            with self.subTest(objects=objects):
                self.source_objects = objects
                self.target_objects = [self.object("target/old.csv", 1)]
                result = copy_dataset(replace(self.context, overwrite_existing=True))
                self.assertEqual((result.status, result.source_unavailable), ("Skipped", True))
        self.assert_no_writes()

    def test_listing_error_is_not_an_empty_source(self):
        self.source_s3.get_paginator.return_value.paginate.side_effect = PermissionError("Denied")
        with self.assertLogs(level="ERROR"):
            result = copy_dataset(replace(self.context, overwrite_existing=True))
        self.assertEqual((result.status, result.rows_copied), ("Failure", 0))
        self.assert_no_writes()

    def test_target_listing_error_is_not_an_empty_target(self):
        self.target_s3.get_paginator.return_value.paginate.side_effect = PermissionError("Denied")
        with self.assertLogs(level="ERROR"):
            self.assertEqual(copy_dataset(self.context).status, "Failure")
        self.assert_no_writes()

    def test_all_pages_and_partition_relative_keys_are_copied(self):
        partitioning = {"dimensions": [{"name": "day", "type": "time"}], "filePathPattern": "%Y/%M/%D/.*"}
        self.source_settings["partitioning"] = deepcopy(partitioning)
        self.target_settings["partitioning"] = deepcopy(partitioning)
        self.source_objects = [self.object("source/2026/11/19/part.csv", 100), self.object("source/2026/11/20/a %20.csv", 10)]
        self.source_s3.get_paginator.return_value.paginate.side_effect = lambda **kwargs: [
            {"Contents": [self.source_objects[0]]}, {}, {"Contents": [self.source_objects[1]]}]
        self.assertEqual(copy_dataset(self.context).status, "Success")
        self.assertEqual(set(self.copied), {"target/2026/11/19/part.csv", "target/2026/11/20/a %20.csv"})
        self.source_s3.get_paginator.return_value.paginate.assert_called_once_with(Bucket="bucket", Prefix="source/")

    def test_incompatible_formats_or_partition_layouts_fail_before_writes(self):
        for field, value in [("type", "Filesystem"), ("formatType", "parquet"), ("formatParams", {"separator": ";"}),
                             ("partitioning", {"dimensions": [], "filePathPattern": "other"})]:
            with self.subTest(field=field), self.assertLogs(level="ERROR"):
                original = self.target_settings[field]
                self.target_settings[field] = value
                self.assertEqual(copy_dataset(self.context).status, "Failure")
                self.target_settings[field] = original
        self.assert_no_writes()

    def test_dynamic_paths_and_single_file_datasets_are_rejected(self):
        self.target.get_location_info.return_value["info"]["path"] = "s3://bucket/target/${partition}"
        with self.assertLogs(level="ERROR"):
            self.assertEqual(copy_dataset(self.context).status, "Failure")
        self.target.get_location_info.return_value["info"]["path"] = "s3://bucket/target"
        self.target.get_location_info.return_value["info"]["isSingleFile"] = True
        with self.assertLogs(level="ERROR"):
            self.assertEqual(copy_dataset(self.context).status, "Failure")
        self.assert_no_writes()

    def test_project_variables_are_supported_after_location_resolution(self):
        self.source_settings["params"]["path"] = "${projectKey}/orders"
        self.target_settings["params"]["path"] = "${projectKey}/orders"
        self.assertEqual(copy_dataset(self.context).status, "Success")

    def test_destination_identity_must_read_source_before_mutation(self):
        self.target_s3.head_object.side_effect = PermissionError("Cannot read source")
        with self.assertLogs(level="ERROR"):
            result = copy_dataset(replace(self.context, overwrite_existing=True))
        self.assertEqual((result.status, result.rows_copied), ("Failure", 0))
        self.assert_no_writes()

    def test_source_version_is_pinned_when_available(self):
        old_head = self.target_s3.head_object.side_effect
        def head(**kwargs):
            result = old_head(**kwargs)
            if kwargs["Key"].startswith("source/"):
                result["VersionId"] = "source-version"
            return result
        self.target_s3.head_object.side_effect = head
        self.assertEqual(copy_dataset(self.context).status, "Success")
        self.assertEqual(self.target_s3.copy.call_args.args[0]["VersionId"], "source-version")

    def test_copy_failure_reports_partial_and_preserves_stale_objects(self):
        original_copy = self.target_s3.copy.side_effect
        def copy(*args, **kwargs):
            if self.copied:
                raise RuntimeError("Copy failed")
            return original_copy(*args, **kwargs)
        self.target_s3.copy.side_effect = copy
        self.target_objects.append(self.object("target/old.csv", 1))
        with self.assertLogs(level="ERROR"):
            result = copy_dataset(replace(self.context, overwrite_existing=True))
        self.assertEqual(result.status, "Failure")
        self.assertIsNone(result.rows_copied)
        self.assertIn("after 1 verified objects", result.message)
        self.target_s3.delete_objects.assert_not_called()
        self.target.write_schema.assert_not_called()
        self.target_s3.close.assert_called_once()

    def test_failed_size_verification_is_not_success(self):
        self.target_s3.copy.side_effect = lambda source, bucket, key, **kwargs: self.copied.update({key: 999})
        with self.assertLogs(level="ERROR"):
            self.assertEqual(copy_dataset(self.context).status, "Failure")
        self.target_s3.delete_objects.assert_not_called()
        self.target.write_schema.assert_not_called()

    def test_schema_failure_preserves_stale_objects(self):
        self.target_objects.append(self.object("target/old.csv", 1))
        self.target.write_schema.side_effect = RuntimeError("Schema update failed")
        with self.assertLogs(level="ERROR"):
            self.assertEqual(copy_dataset(replace(self.context, overwrite_existing=True)).status, "Failure")
        self.target_s3.delete_objects.assert_not_called()

    def test_delete_batches_are_limited_to_1000_keys_and_partial_errors_fail(self):
        self.target_objects = [self.object("target/old-{}.csv".format(i), 1) for i in range(1001)]
        self.assertEqual(copy_dataset(replace(self.context, overwrite_existing=True)).status, "Success")
        self.assertEqual([len(call.kwargs["Delete"]["Objects"]) for call in self.target_s3.delete_objects.call_args_list], [1000, 1])
        self.target_s3.delete_objects.return_value = {"Errors": [{"Code": "AccessDenied"}]}
        with self.assertLogs(level="ERROR"):
            self.assertEqual(copy_dataset(replace(self.context, overwrite_existing=True)).status, "Failure")

    def test_source_change_during_preflight_prevents_target_writes(self):
        self.target_s3.head_object.side_effect = lambda **kwargs: {"ContentLength": 999, "ETag": '"changed"'}
        with self.assertLogs(level="ERROR"):
            self.assertEqual(copy_dataset(replace(self.context, overwrite_existing=True)).status, "Failure")
        self.assert_no_writes()

    def test_listing_cannot_escape_the_dataset_prefix(self):
        self.source_objects.append(self.object("source-sibling/file.csv", 3))
        with self.assertLogs(level="ERROR"):
            self.assertEqual(copy_dataset(self.context).status, "Failure")
        self.assert_no_writes()

    def test_keys_retain_literal_slashes_percent_escapes_and_case(self):
        self.assertEqual(
            _resolved_root({"locationInfoType": "FS", "info": {"path": "s3://bucket/A//b%20c/"}}),
            ("bucket", "A//b%20c"),
        )


if __name__ == "__main__":
    unittest.main()
