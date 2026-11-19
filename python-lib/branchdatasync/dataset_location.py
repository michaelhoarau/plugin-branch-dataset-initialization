"""Resolve dataset storage locations for comparison in macro reports."""
import logging


def get_dataset_location(project_key, dataset_name):
    """Read a resolved SQL table identifier or filesystem-like storage path.

    Args:
        project_key (str): Project owning the dataset to inspect.
        dataset_name (str): Dataset name within that project.

    Returns:
        str or None: Quoted resolved SQL table name, a fallback qualified name,
        or a storage path. None means the location is unsupported or unavailable.

    Uses Dataiku's metadata API with sensitive connection information disabled.
    No dataset contents are read or written. Lookup failures are logged and
    converted to None so the report can still display the other datasets.
    """
    try:
        import dataiku

        location = dataiku.Dataset(
            dataset_name, 
            project_key=project_key, 
            ignore_flow=True,
        ).get_location_info(sensitive_info=False)

        info = location.get("info", {})

        if location.get("locationInfoType") == "SQL":
            # Preferred way to get the resolved table name for a SQL dataset. We need 
            # a quoted name for a Snowflake dataset, which is required for cloning:
            if info.get("quotedResolvedTableName"):
                return info["quotedResolvedTableName"]

            # Reconstructs a qualified table name from the catalog, schema, and table 
            # keys when the resolved name is unavailable. This is a fallback for some 
            # datasets:
            if info.get("table"):
                return ".".join(str(info[key]) for key in ("catalog", "schema", "table") if info.get(key))

        return info.get("path") or None

    except Exception:
        logging.getLogger(__name__).warning(
            "Cannot read location for %s.%s", project_key, dataset_name, exc_info=True,
        )
        return None