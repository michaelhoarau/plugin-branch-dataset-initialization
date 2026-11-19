"""Copy S3 objects inside S3; DSS handles only API calls and metadata."""
import logging
from contextlib import ExitStack
from urllib.parse import urlsplit

from .copy_operations import CopyResult


def _resolved_root(location):
    """Resolve an S3 URI to (bucket, prefix), retaining literal key spelling.

    S3 keys are not local paths: repeated slashes and percent escapes must not
    be normalized or decoded. Reject ambiguous URI and relative-path metadata.
    """
    if location.get("locationInfoType") != "FS":
        raise ValueError("Source and target must both resolve to S3 file locations.")
    path = location.get("info", {}).get("path")
    if not isinstance(path, str) or not path or "${" in path or any(ord(char) < 32 for char in path):
        raise ValueError("An S3 storage URI could not be resolved.")
    uri = urlsplit(path)
    if (uri.scheme != "s3" or not uri.netloc or uri.query or uri.fragment
            or any(char in uri.netloc for char in "@: ")
            or any(part in (".", "..") for part in uri.path.split("/"))):
        raise ValueError("Expected an absolute s3://bucket/prefix storage URI.")
    return uri.netloc.lower(), uri.path[1:].rstrip("/")


def _roots_overlap(source, target):
    """Detect nested dataset prefixes in the same bucket on key boundaries."""
    if source[0] != target[0]:
        return False
    return (not source[1] or not target[1]
            or source[1].startswith(target[1] + "/")
            or target[1].startswith(source[1] + "/"))


def _s3_client(api, connection_name, stack):
    """Use only DSS-resolved credentials, never the local AWS credential chain.

    Custom endpoints and advanced DSS SDK overrides are refused rather than
    silently sending requests to an unrelated service or ignoring its settings.
    """
    import boto3
    from botocore.config import Config

    try:
        info = api.get_connection(connection_name).get_info()
    except Exception as error:
        raise ValueError(
            "Cannot read S3 connection {!r} details ({}). "
            "Check connection details access for the macro's execution user.".format(
                connection_name, type(error).__name__,
            )
        ) from None

    try:
        credentials = info.get_aws_credential()
    except Exception as error:
        raise ValueError(
            "Dataiku did not return AWS credentials for S3 connection {!r} ({}). "
            "Check its authentication mode and credential resolution; "
            "access to connection details alone does not confirm credentials are available.".format(
                connection_name, type(error).__name__,
            )
        ) from None
    if (not isinstance(credentials, dict)
            or not all(isinstance(credentials.get(key), str) and credentials[key]
                       for key in ("accessKey", "secretKey"))):
        raise ValueError(
            "Dataiku returned incomplete AWS credentials for S3 connection {!r}; "
            "check its authentication mode and credential resolution.".format(connection_name)
        )

    try:
        try:
            # S3 responses may omit resolvedParams. The SDK indexes that key
            # directly, so `get_resolved_params() or ...` cannot handle it.
            params = info.get_resolved_params()
        except (KeyError, AttributeError):
            params = None
        params = params or info.get_params()
    except Exception as error:
        raise ValueError(
            "Cannot read S3 connection {!r} parameters ({}); "
            "AWS credentials were retrieved successfully.".format(connection_name, type(error).__name__)
        ) from None
    if not isinstance(params, dict):
        raise ValueError("Dataiku returned invalid S3 connection parameters.")

    region = params.get("regionOrEndpoint") or None
    if region and ("://" in region or "${" in region):
        raise ValueError("S3 server-side copy requires AWS region connections; custom endpoints are unsupported.")
    if params.get("dkuProperties"):
        raise ValueError("S3 server-side copy does not support custom DSS AWS SDK properties.")

    encryption = params.get("encryptionMode", "NONE")
    extra_args = {}
    if encryption == "SSE_S3":
        extra_args["ServerSideEncryption"] = "AES256"
    elif encryption == "SSE_KMS":
        extra_args["ServerSideEncryption"] = "aws:kms"
        if params.get("encryptionKeyId"):
            extra_args["SSEKMSKeyId"] = params["encryptionKeyId"]
    elif encryption != "NONE":
        raise ValueError("Unsupported S3 connection encryption mode.")

    client = boto3.client(
        "s3", aws_access_key_id=credentials["accessKey"],
        aws_secret_access_key=credentials["secretKey"],
        aws_session_token=credentials.get("sessionToken"), region_name=region,
        config=Config(s3={"addressing_style": "path" if params.get("usePathMode") else "auto"}),
    )
    stack.callback(client.close)
    return client, extra_args


def _prefix(root):
    """Use a directory boundary so a dataset prefix cannot include its siblings."""
    return root[1] + "/" if root[1] else ""


def _objects(client, root):
    """List all current objects, including zero-byte files and folder markers."""
    objects = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=root[0], Prefix=_prefix(root)):
        for item in page.get("Contents", []):
            # Validate the manifest before it can drive writes or deletions.
            if (not isinstance(item.get("Key"), str) or not item["Key"].startswith(_prefix(root))
                    or not isinstance(item.get("Size"), int) or item["Size"] < 0):
                raise ValueError("Invalid S3 object listing; no copy attempted.")
            objects.append(item)
    return objects


def _delete_objects(client, bucket, keys):
    """Delete stale destination objects in API-sized batches; inspect errors."""
    for offset in range(0, len(keys), 1000):
        response = client.delete_objects(
            Bucket=bucket, Delete={"Objects": [{"Key": key} for key in keys[offset:offset + 1000]], "Quiet": True},
        )
        if response.get("Errors"):
            raise RuntimeError("S3 could not delete some stale destination objects.")


def copy_s3_objects(context):
    """Copy a dataset's prefix with S3 managed (including multipart) copies.

    Metadata comes from DSS and only metadata crosses the DSS process. Objects
    retain their relative keys and bytes, so file formats and partition layouts
    must match. Destination credentials must read the source as well as write
    the target; SourceClient only controls the SDK's source HEAD requests.

    Existing targets require overwrite. Stale target objects are removed only
    after every copy verifies by size and schema synchronization succeeds.
    This is not an atomic snapshot: stop concurrent writers during the copy.
    Missing/file-less sources leave targets untouched. Rows are never read or
    counted; successful results have an unknown row count.
    """
    import dataiku

    logger = logging.getLogger(__name__)
    # Closing clients also happens on every early return and failure path.
    with ExitStack() as stack:
        try:
            source = dataiku.Dataset(context.dataset_name, project_key=context.source_project_key, ignore_flow=True)
            target = dataiku.Dataset(context.dataset_name, project_key=context.target_project_key, ignore_flow=True)
            source_location = source.get_location_info(sensitive_info=False)
            target_location = target.get_location_info(sensitive_info=False)
            source_root, target_root = _resolved_root(source_location), _resolved_root(target_location)
            if source_root == target_root:
                return CopyResult("Skipped", 0, "Shared source and target location.", shared_location=True)
            if _roots_overlap(source_root, target_root):
                return CopyResult("Failure", 0, "Source and target S3 prefixes overlap; copy refused.")
            if any(location["info"].get("isSingleFile") for location in (source_location, target_location)):
                raise ValueError("S3 server-side copy requires directory-backed datasets.")

            api = dataiku.api_client()
            settings = [api.get_project(key).get_dataset(context.dataset_name).get_settings().get_raw()
                        for key in (context.source_project_key, context.target_project_key)]
            if any(item.get("type", "").casefold() != "s3" for item in settings):
                raise ValueError("This copy handler requires S3 datasets on both sides.")
            if not settings[0].get("formatType"):
                raise ValueError("Source file format is unavailable.")
            for field in ("formatType", "formatParams", "partitioning"):
                if settings[0].get(field) != settings[1].get(field):
                    raise ValueError("Source and target {} must match for byte-preserving S3 copies.".format(field))
            clients = []
            for side, location, dataset_settings in zip(
                    ("Source", "Target"), (source_location, target_location), settings):
                # Location metadata may omit the connection name, especially
                # when requested without sensitive connection information.
                name = (location["info"].get("connectionName")
                        or dataset_settings.get("params", {}).get("connection"))
                if not isinstance(name, str) or not name.strip():
                    raise ValueError(
                        "{} S3 connection name could not be resolved. "
                        "Check the dataset connection in its settings (params.connection).".format(side)
                    )
                clients.append(_s3_client(api, name, stack))
            (source_client, _), (target_client, extra_args) = clients
            target_objects = _objects(target_client, target_root)
            if target_objects and not context.overwrite_existing:
                return CopyResult("Skipped", 0, "Target dataset already contains S3 objects; overwrite is disabled.", target_exists=True)
            source_objects = _objects(source_client, source_root)
            if not any(not item["Key"].endswith("/") for item in source_objects):
                return CopyResult("Skipped", 0, "Source dataset has no S3 data objects; target left unchanged.", source_unavailable=True)
            columns = source.read_schema()

            # Preflight read permission using the identity that will sign COPY,
            # and pin each source to its current version or ETag before writes.
            manifest = []
            for item in source_objects:
                head = target_client.head_object(Bucket=source_root[0], Key=item["Key"])
                if head["ContentLength"] != item["Size"] or head.get("ETag") != item.get("ETag"):
                    raise ValueError("Source objects changed during preparation; retry with source writers stopped.")
                copy_source = {"Bucket": source_root[0], "Key": item["Key"]}
                if head.get("VersionId"):
                    copy_source["VersionId"] = head["VersionId"]
                target_key = _prefix(target_root) + item["Key"][len(_prefix(source_root)):]
                manifest.append((copy_source, target_key, item["Size"], head["ETag"]))

        except Exception as error:
            # Do not echo SDK/connection exception bodies which may contain
            # credential information. Validation errors here use fixed text.
            logger.error("Cannot prepare S3 copy for %s (%s)", context.dataset_name, type(error).__name__)
            detail = str(error) if isinstance(error, ValueError) else "Check S3 access, connection details, and the plugin's boto3 environment."
            return CopyResult("Failure", 0, "Cannot prepare S3 server-side copy. " + detail)

        copied = 0
        try:
            for copy_source, target_key, size, etag in manifest:
                target_client.copy(
                    copy_source, target_root[0], target_key, SourceClient=source_client,
                    ExtraArgs=dict(extra_args, CopySourceIfMatch=etag),
                )
                if target_client.head_object(Bucket=target_root[0], Key=target_key)["ContentLength"] != size:
                    raise RuntimeError("Copied S3 object size verification failed.")
                copied += 1
            target.write_schema(columns)
            target_keys = {entry[1] for entry in manifest}
            stale = [item["Key"] for item in target_objects if item["Key"] not in target_keys]
            if context.overwrite_existing:
                _delete_objects(target_client, target_root[0], stale)
            return CopyResult(
                "Success", None,
                "S3 server-side copy completed and verified: {} objects. Target schema synchronized; row count unavailable.".format(copied),
            )
        except Exception as error:
            logger.error("S3 copy failed for %s after %s verified objects (%s)", context.dataset_name, copied, type(error).__name__)
            return CopyResult(
                "Failure", None,
                "S3 server-side copy failed after {} verified objects; target may be partially updated. "
                "Check S3 read/write, KMS, and delete permissions and retry with concurrent writers stopped.".format(copied),
            )
