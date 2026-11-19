"""Type-specific copy entry points for run mode."""
import logging

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CopyContext:
    """Immutable inputs passed to a dataset copy handler.

    Attributes:
        source_project_key (str): Project containing the original dataset.
        target_project_key (str): Current branch receiving the copy.
        dataset_name (str): Dataset name shared by both projects.
        source_type (str): Source dataset backend used for handler selection.
        target_type (str): Target dataset backend used for handler selection.
        source_location (str or None): Resolved source table or storage path.
        target_location (str or None): Resolved target table or storage path.
        overwrite_existing (bool): Whether an existing target may be replaced.
    """
    source_project_key: str
    target_project_key: str
    dataset_name: str
    source_type: str
    target_type: str
    source_location: Optional[str]
    target_location: Optional[str]
    overwrite_existing: bool = False


@dataclass(frozen=True)
class CopyResult:
    """Outcome displayed for one dataset in the run-mode report.

    Attributes:
        status (str): Success, Failure, Skipped, or Not implemented.
        rows_copied (int or None): Known row count, or None when unavailable.
            For Snowflake clones, this is the row count read from the target.
        message (str): Human-readable explanation, escaped by the HTML renderer.
        source_unavailable (bool): Whether missing source data explains the
            outcome, allowing the report to distinguish it from other failures.
        shared_location (bool): Whether the copy was skipped because both
            datasets refer to the same physical location.
        target_exists (bool): Whether an existing target prevented the copy
            because overwrite was disabled.
    """
    # Future implementations return Success only after verifying the copy.
    status: str
    rows_copied: Optional[int]
    message: str
    source_unavailable: bool = False
    shared_location: bool = False
    target_exists: bool = False


def copy_snowflake(context):
    """Delegate a Snowflake copy to the database-side clone implementation.

    Args:
        context (CopyContext): Source, target, and overwrite configuration.

    Returns:
        CopyResult: Clone outcome, row count when available, and explanation.

    This function can create or replace a target table; call only in run mode.
    """
    from .snowflake_copy import clone_snowflake

    return clone_snowflake(context)


def copy_filesystem(context):
    """Delegate a Filesystem copy to the Dataiku streaming implementation.

    Args:
        context (CopyContext): Source, target, and overwrite configuration.

    Returns:
        CopyResult: Copy outcome, completed row count when known, and explanation.

    Can update target schema and files. Call only in run mode.
    """
    from .filesystem_copy import stream_filesystem

    return stream_filesystem(context)


def copy_s3(context):
    """Copy S3 objects server-side; call only in run mode."""
    from .s3_copy import copy_s3_objects

    return copy_s3_objects(context)


COPY_HANDLERS = {
    ("snowflake", "snowflake"): copy_snowflake,
    ("filesystem", "filesystem"): copy_filesystem,
    ("s3", "s3"): copy_s3,
}


def copy_dataset(context):
    """Validate a copy context and dispatch by source and target backend.

    Args:
        context (CopyContext): Copy inputs prepared for an output dataset.

    Returns:
        CopyResult: Handler outcome, Skipped for shared projects or non-Snowflake
        locations, Failure for missing locations or unhandled handler errors,
        or Not implemented when no backend pair is registered.

    Snowflake location equality is checked inside its handler after resolving
    database defaults. Handler exceptions are logged and converted to Failure
    with an unknown row count. The caller must exclude input-only datasets and
    must not invoke this function during dry run, because handlers may write.
    """
    if context.source_project_key == context.target_project_key:
        return CopyResult("Skipped", 0, "Source and target projects are the same.")

    if not context.source_location:
        return CopyResult("Failure", 0, "Source location is unavailable; no copy attempted.", source_unavailable=True)
    if not context.target_location:
        return CopyResult("Failure", 0, "Target location is unavailable; no copy attempted.")

    snowflake = context.source_type.casefold() == context.target_type.casefold() == "snowflake"

    # Snowflake names may omit the database; compare fully resolved identities
    # in the handler rather than assuming matching display strings overlap.
    if context.source_location == context.target_location and not snowflake:
        return CopyResult("Skipped", 0, "Shared source and target location.", shared_location=True)

    handler = COPY_HANDLERS.get((context.source_type.casefold(), context.target_type.casefold()))
    if handler is None:
        return CopyResult(
            "Not implemented", 0,
            "No copy handler for {} to {}.".format(context.source_type, context.target_type),
        )

    try:
        return handler(context)

    except Exception:
        logging.getLogger(__name__).exception("Copy handler failed for %s", context.dataset_name)

        # A future handler may have written some rows before failing.
        return CopyResult("Failure", None, "Copy handler failed; see macro logs for details.")
