"""Copy Filesystem datasets using bounded-memory Dataiku row streams."""
import os
from urllib.parse import unquote, urlsplit

from .file_copy import _has_files, stream_files


def _resolved_root(location):
    """Return a canonical local root, rejecting unsupported location metadata.

    Args:
        location (dict): Dataiku location information, with an FS type and path.

    Returns:
        str: Absolute real path, including resolution of symlink aliases.

    Raises:
        ValueError: Metadata is not a local filesystem location.
    """
    if location.get("locationInfoType") != "FS":
        raise ValueError("Source and target must both resolve to Filesystem locations.")

    path = location.get("info", {}).get("path")
    if not isinstance(path, str) or not path:
        raise ValueError("A filesystem storage path could not be resolved.")

    if path.startswith("file:"):
        uri = urlsplit(path)
        if uri.netloc not in ("", "localhost") or uri.query or uri.fragment:
            raise ValueError("Only local file URIs are supported for Filesystem datasets.")

        path = unquote(uri.path)

    if not os.path.isabs(path):
        raise ValueError("Filesystem storage paths must be absolute.")

    return os.path.realpath(path)


def stream_filesystem(context):
    """Copy local datasets with canonical-path and directory-overlap checks.

    Streams rows through Dataiku, synchronizing schema and matching partitions.
    Overwrite may clear existing data; failures can leave a partial target.
    """
    return stream_files(
        context, "Filesystem", _resolved_root,
        lambda source, target: os.path.commonpath([source, target]) in (source, target),
    )
