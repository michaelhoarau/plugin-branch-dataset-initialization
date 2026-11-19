"""Clone tables inside Snowflake through the target dataset's connection."""
import logging
import re

from .copy_operations import CopyResult


_CONTEXT_SQL = ("SELECT CURRENT_ACCOUNT() AS ACCOUNT_ID, CURRENT_REGION() AS REGION_ID, CURRENT_DATABASE() AS DB_NAME, "
                "CURRENT_SCHEMA() AS SCHEMA_NAME")
_IDENTIFIER = re.compile(r'\s*("(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*)\s*(\.|$)')


def _identifier_parts(identifier):
    """Parse a Snowflake identifier into unescaped component names.

    Args:
        identifier (str): One-, two-, or three-part resolved identifier.

    Returns:
        list[str]: Components with quoted spelling preserved, doubled quotes
        decoded, and unquoted components normalized to uppercase.

    Raises:
        ValueError: Invalid syntax, empty/control-character components, a trailing
            separator, or an unsupported number of components.

    Only identifier syntax is accepted; SQL fragments must not pass through.
    """
    parts, position = [], 0
    while position < len(identifier):
        match = _IDENTIFIER.match(identifier, position)
        if match is None:
            raise ValueError("Invalid resolved Snowflake table identifier.")

        token = match[1]
        part = token[1:-1].replace('""', '"') if token.startswith('"') else token.upper()
        if not part or any(ord(char) < 32 for char in part):
            raise ValueError("Invalid Snowflake identifier component.")

        parts.append(part)
        position = match.end()
        if match[2] == '.' and position == len(identifier):
            raise ValueError("Incomplete Snowflake table identifier.")

    if not 1 <= len(parts) <= 3:
        raise ValueError("Expected a Snowflake table identifier with one to three components.")

    return parts


def _qualified_table(info, session):
    """Resolve a table-backed dataset to a fully quoted three-part identifier.

    Args:
        info (dict): SQL location metadata containing ``table`` and
            ``quotedResolvedTableName``.
        session (dict): Connection defaults with ``DB_NAME`` and ``SCHEMA_NAME``.

    Returns:
        str: Quoted database.schema.table identifier with embedded quotes escaped.

    Raises:
        ValueError: The dataset is query-backed, its identifier is invalid,
            or required database/schema defaults cannot be resolved.
    """
    resolved = info.get("quotedResolvedTableName")
    if not info.get("table") or not resolved:
        raise ValueError("Snowflake cloning requires a table-backed dataset, not a SQL query.")

    parts = _identifier_parts(resolved)
    if len(parts) == 1:
        parts = [session.get("DB_NAME"), session.get("SCHEMA_NAME")] + parts

    elif len(parts) == 2:
        parts = [session.get("DB_NAME")] + parts

    if any(not isinstance(part, str) or not part for part in parts):
        raise ValueError("Unable to resolve the Snowflake database and schema.")

    return '.'.join('"{}"'.format(part.replace('"', '""')) for part in parts)


def _session_context(executor):
    """Read account, region, database, and schema for an executor's connection.

    Args:
        executor (SQLExecutor2): Executor bound to the dataset being resolved.

    Returns:
        dict: One context row with ACCOUNT_ID, REGION_ID, DB_NAME, and SCHEMA_NAME.

    Raises:
        ValueError: The result is not one row or has no account/region identity.
        Exception: SQL execution or result conversion fails.

    Executes a read-only context query and does not read dataset contents.
    """
    rows = executor.query_to_df(_CONTEXT_SQL).to_dict(orient="records")
    if len(rows) != 1 or (not rows[0].get("ACCOUNT_ID") or not rows[0].get("REGION_ID")):
        raise ValueError("Unable to determine the Snowflake account and session context.")

    return rows[0]


def _target_exists(executor, qualified_table):
    """Check whether the target table or view is visible in information schema.

    Args:
        executor (SQLExecutor2): Executor using the target dataset's connection.
        qualified_table (str): Fully qualified, three-part target identifier.

    Returns:
        bool: Whether a matching table/view is visible to the active role.

    Raises:
        ValueError: Identifier parsing or result conversion fails.
        Exception: The read-only metadata query fails.

    A False result is subject to metadata visibility. Plain CREATE still guards
    against hidden existing objects or targets created after this check.
    """
    database, schema, table = _identifier_parts(qualified_table)

    def literal(value):
        """Quote a string for Snowflake SQL, escaping backslashes and apostrophes."""
        return "'{}'".format(value.replace('\\', '\\\\').replace("'", "''"))

    catalog = '"{}"'.format(database.replace('"', '""'))
    query = (
        'SELECT COUNT(*) AS TARGET_EXISTS FROM {}."INFORMATION_SCHEMA"."TABLES" '
        'WHERE TABLE_CATALOG = {} AND TABLE_SCHEMA = {} AND TABLE_NAME = {}'
    ).format(catalog, literal(database), literal(schema), literal(table))
    rows = executor.query_to_df(query).to_dict(orient="records")

    return int(rows[0]["TARGET_EXISTS"]) > 0


def clone_snowflake(context):
    """Create or replace a Snowflake table clone using the target connection.

    Args:
        context (CopyContext): Source and target projects, shared dataset name,
            and the explicit ``overwrite_existing`` setting.

    Returns:
        CopyResult: Success after confirmed clone execution, with the target
        row count when available; Skipped for shared physical locations or an
        existing target when overwrite is disabled; Failure for preparation or
        SQL execution errors. Unconfirmed execution has an unknown row count.

    Reads fresh dataset locations, validates Snowflake table backends, resolves
    identifiers in each connection's session context, and rejects cross-account
    or cross-region clones. Run mode executes CREATE TABLE CLONE, or CREATE OR
    REPLACE TABLE CLONE when overwrite is enabled, followed by a target COUNT.
    A failed count does not turn a completed clone into Failure. This function
    can modify database tables and must never be called during dry run. It does
    not mark datasets as built or change their DSS definitions.
    """
    import dataiku

    logger = logging.getLogger(__name__)
    try:
        source_dataset = dataiku.Dataset(context.dataset_name, project_key=context.source_project_key, ignore_flow=True)
        target_dataset = dataiku.Dataset(context.dataset_name, project_key=context.target_project_key, ignore_flow=True)
        source_location = source_dataset.get_location_info(sensitive_info=False)
        target_location = target_dataset.get_location_info(sensitive_info=False)
        for location in (source_location, target_location):
            if (location.get("locationInfoType") != "SQL"
                    or location.get("info", {}).get("databaseType", "").casefold() != "snowflake"):
                raise ValueError("Source and target must both resolve to Snowflake SQL tables.")

        source_executor = dataiku.SQLExecutor2(dataset=source_dataset)
        target_executor = dataiku.SQLExecutor2(dataset=target_dataset)
        source_session = _session_context(source_executor)
        target_session = _session_context(target_executor)
        if (source_session["ACCOUNT_ID"], source_session["REGION_ID"]) != (target_session["ACCOUNT_ID"], target_session["REGION_ID"]):
            return CopyResult("Failure", 0, "Source and target connections use different Snowflake accounts; cloning is unavailable.")

        source_table = _qualified_table(source_location["info"], source_session)
        target_table = _qualified_table(target_location["info"], target_session)
        if source_table == target_table:
            return CopyResult("Skipped", 0, "Shared source and target location.", shared_location=True)

        if not context.overwrite_existing and _target_exists(target_executor, target_table):
            return CopyResult("Skipped", 0, "Target dataset already exists; overwrite is disabled.", target_exists=True)

    except Exception as error:
        logger.exception("Cannot prepare Snowflake clone for %s", context.dataset_name)
        return CopyResult("Failure", 0, "Cannot prepare Snowflake clone: {}".format(str(error)[:500]))

    # Plain CREATE also protects against a target created after the existence
    # check. Only an explicit overwrite opt-in enables OR REPLACE.
    create = "CREATE OR REPLACE TABLE" if context.overwrite_existing else "CREATE TABLE"
    clone_sql = "{} {} CLONE {}".format(create, target_table, source_table)
    logger.info("Cloning Snowflake table: %s", clone_sql)

    try:
        # Run the clone after a pre-query so the main query always yields a consistent small result set:
        target_executor.query_to_df(
            "SELECT 1 AS CLONE_COMPLETE", pre_queries=[clone_sql], post_queries=["COMMIT"],
        )

    except Exception as error:
        if not context.overwrite_existing and "already exists" in str(error).casefold():
            try:
                if _target_exists(target_executor, target_table):
                    return CopyResult("Skipped", 0, "Target dataset already exists; overwrite is disabled.", target_exists=True)

            except Exception:
                logger.warning("Could not recheck target existence after clone failure", exc_info=True)

        logger.exception("Snowflake clone failed for %s", context.dataset_name)

        return CopyResult(
            "Failure", None,
            "Clone failed or completion could not be confirmed; check whether the target exists. " +
            ("Overwrite was enabled. " if context.overwrite_existing else "Existing tables are not replaced. ")
            + str(error)[:500],
        )

    # Close is succeful for a completed clone, but the row count may be 
    # unavailable if the target is empty or the user lacks SELECT privileges:
    row_count = None
    try:
        rows = target_executor.query_to_df(
            "SELECT COUNT(*) AS ROWS_COPIED FROM {}".format(target_table),
        ).to_dict(orient="records")
        row_count = int(rows[0]["ROWS_COPIED"])

        if row_count < 0:
            raise ValueError("Invalid target row count")

    except Exception:
        row_count = None
        logger.warning("Clone created but row count unavailable for %s", context.dataset_name, exc_info=True)

    message = "Snowflake zero-copy clone created successfully."
    message += " Row count read from target." if row_count is not None else " Target row count is unavailable."

    return CopyResult("Success", row_count, message)
