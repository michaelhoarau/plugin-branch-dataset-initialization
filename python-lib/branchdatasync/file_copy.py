"""Shared bounded-memory Dataiku copying for file-backed datasets."""
import logging
import re

from contextlib import closing

from .copy_operations import CopyResult


def _has_files(file_info):
    """Return whether a Dataiku file listing contains any dataset files.

    Args:
        file_info (dict): Result of Dataset.get_files_info(), across partitions.

    Returns:
        bool: True for any file, including an empty or header-only data file.

    Raises:
        ValueError: File metadata is missing or malformed. Unknown is not empty.
    """
    if not isinstance(file_info, dict) or not {"globalPaths", "pathsByPartition"}.intersection(file_info):
        raise ValueError("Unable to determine whether the target already contains files.")

    return bool(file_info.get("globalPaths")) or any(file_info.get("pathsByPartition", {}).values())


def _missing_root_error(error):
    """Return whether an exception identifies Dataiku's missing dataset root.

    Other listing errors, including permission failures, are not considered
    missing storage and must still be reported as failures.
    """
    message = str(error)
    return bool(
        "ERR_FSPROVIDER_ROOT_PATH_DOES_NOT_EXIST" in message
        or re.search(r"root path of the dataset(?: [^\r\n]*?)? does not exist", message, re.IGNORECASE)
    )


def _target_has_files(target):
    """Inspect target contents, allowing a not-yet-created dataset root.

    Args:
        target (dataiku.Dataset): Destination dataset whose files are inspected.

    Returns:
        bool: Whether files exist. Dataiku's missing-root error means False;
        the target writer will create its storage when copying starts.

    Raises:
        Exception: Listing fails for another reason, such as missing permissions.
            These errors must not be mistaken for an empty destination.
    """
    try:
        return _has_files(target.get_files_info())

    except Exception as error:
        if not _missing_root_error(error):
            raise

        logging.getLogger(__name__).info("Target dataset root does not exist yet; treating it as empty.")

        return False


def stream_files(context, backend, resolve_root, roots_overlap):
    """Stream a validated file-backed dataset, preserving schema and partitions.

    Backend-specific callbacks resolve physical roots and detect overlapping
    storage. Preparation leaves target data untouched. Success is reported only
    after all writers close; write failures may leave a partial target.
    """
    import dataiku

    logger = logging.getLogger(__name__)
    try:
        source = dataiku.Dataset(context.dataset_name, project_key=context.source_project_key, ignore_flow=True)
        target = dataiku.Dataset(context.dataset_name, project_key=context.target_project_key, ignore_flow=True)
        source_root = resolve_root(source.get_location_info(sensitive_info=False))
        target_root = resolve_root(target.get_location_info(sensitive_info=False))
        if source_root == target_root:
            return CopyResult("Skipped", 0, "Shared source and target location.", shared_location=True)
        if roots_overlap(source_root, target_root):
            return CopyResult("Failure", 0, "Source and target storage roots overlap; copy refused.")

        client = dataiku.api_client()
        source_api = client.get_project(context.source_project_key).get_dataset(context.dataset_name)
        target_api = client.get_project(context.target_project_key).get_dataset(context.dataset_name)
        source_settings = source_api.get_settings().get_raw()
        target_settings = target_api.get_settings().get_raw()
        if any(settings.get("type", "").casefold() != backend.casefold()
               for settings in (source_settings, target_settings)):
            raise ValueError("This copy handler requires {} datasets on both sides.".format(backend))

        source_dimensions = source_settings.get("partitioning", {}).get("dimensions", [])
        target_dimensions = target_settings.get("partitioning", {}).get("dimensions", [])
        if source_dimensions != target_dimensions:
            raise ValueError("Source and target partition dimensions must match before copying.")

        target_has_files = _target_has_files(target)
        if target_has_files and not context.overwrite_existing:
            return CopyResult("Skipped", 0, "Target dataset already contains files; overwrite is disabled.", target_exists=True)

        # Verify the source is readable before modifying the target schema/data.
        try:
            source_files = source.get_files_info()

        except Exception as error:
            if _missing_root_error(error):
                return CopyResult("Skipped", 0, "Source dataset {}.{} has no storage root; target left unchanged.".format(
                    context.source_project_key, context.dataset_name,
                ), source_unavailable=True)
            raise ValueError("Source dataset {}.{} could not be read: {}".format(
                context.source_project_key, context.dataset_name, error,
            )) from error

        if not isinstance(source_files, dict):
            raise ValueError("Source dataset files could not be listed.")
        if "globalPaths" in source_files and source_files["globalPaths"] is None:
            return CopyResult("Skipped", 0, "Source dataset has no storage root; target left unchanged.", source_unavailable=True)
        if not _has_files(source_files):
            return CopyResult("Skipped", 0, "Source dataset has no data files; target left unchanged.", source_unavailable=True)

        columns = source.read_schema()
        partitions = source.list_partitions(raise_if_empty=False) if source_dimensions else [None]

    except Exception as error:
        logger.exception("Cannot prepare %s copy for %s", backend, context.dataset_name)
        return CopyResult("Failure", 0, "Cannot prepare {} copy: {}".format(backend, str(error)[:500]))

    # Copy the source rows to the target, counting them as they are written:
    rows_copied = 0
    try:
        # Write the source schema to the target, 
        # then clear any existing files if requested:
        target.write_schema(columns)
        if context.overwrite_existing and target_has_files:
            target_api.clear()
        target.spec_item["appendMode"] = False

        # Stream each source partition to the target, counting rows as they are written:
        for partition in partitions:
            reader = source
            if partition is not None:
                # A fresh handle prevents read partitions accumulating between loops.
                reader = dataiku.Dataset(context.dataset_name, project_key=context.source_project_key, ignore_flow=True)
                reader.add_read_partitions(partition)
                target.set_write_partition(partition)

            # Dataiku maps head + no limit to a full, unsampled read.
            with closing(reader.iter_tuples(sampling="head", limit=None)) as rows:
                with target.get_writer() as writer:
                    for row in rows:
                        writer.write_tuple(row)
                        rows_copied += 1

        return CopyResult("Success", rows_copied, "{} data copied through Dataiku; target schema synchronized.".format(backend))

    except Exception as error:
        logger.exception("%s copy failed for %s", backend, context.dataset_name)
        return CopyResult("Failure", None, "{} copy failed; target may be partially updated. {}".format(backend, str(error)[:500]))
