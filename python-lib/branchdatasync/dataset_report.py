"""Build dataset comparison tables and coordinate optional run-mode copies."""

import logging

from .report_html import table_start, table_row, table_end, location_row
from .dataset_location import get_dataset_location
from .copy_operations import CopyContext, CopyResult, copy_dataset


def _input_only_datasets(project):
    """Find local datasets consumed by recipes but never produced by recipes.

    Args:
        project (DSSProject): Project whose recipe input/output roles are read.

    Returns:
        set[str]: Input references absent from all recipe output references.
        Project-qualified references are normalized, and foreign references
        are excluded. Datasets used as both inputs and outputs are not included.

    Recipe lookup errors propagate to the caller rather than silently assigning
    incorrect usage labels. Only recipe metadata is read.
    """
    inputs, outputs = set(), set()

    # Loops through all recipes in the project:
    for recipe in project.list_recipes():
        # Loops through all inputs, and then all ouputs:
        for direction, refs in (("inputs", inputs), ("outputs", outputs)):
            # Each input and output section can have several roles, each 
            # of which can have several items. Each item has a "ref" key 
            # that is the dataset reference:
            for role in recipe.get(direction, {}).values():
                for item in role.get("items", []):
                    ref = item.get("ref", "")
                    # Check if the reference is project-qualified:
                    if "." in ref:
                        owner, ref = ref.split(".", 1)

                        # Check if the reference belongs to the current project:
                        if owner != project.project_key:
                            continue

                    refs.add(ref)

    return inputs - outputs


def render_dataset_table(project, source_project=None, dry_run=True, overwrite_existing=False):
    """Render dataset mappings and, optionally, execute type-specific copies.

    Args:
        project (DSSProject): Current branch project and copy destination.
        source_project (DSSProject or None): Related source project whose
            datasets are matched to current datasets by name.
        dry_run (bool): If True, read metadata and render mappings without
            invoking copy handlers. Defaults to True.
        overwrite_existing (bool): Allow handlers to replace existing targets
            in run mode. Defaults to False and has no write effect in dry run.

    Returns:
        str: Styled HTML table, or an empty-project message. Output datasets
        appear first, followed by muted input-only datasets, with type and name
        as secondary sort keys. Outputs include location comparisons. Run mode
        adds result, row-count, and explanation columns.

    Run mode delegates eligible outputs to handlers that may create or replace
    tables or files. Inputs are skipped. Source-list failures are logged and displayed;
    current-project dataset and recipe lookup errors propagate to the caller.
    """
    datasets = sorted(project.list_datasets(), key=lambda dataset: dataset.name.casefold())
    if not datasets:
        return "<p>No datasets found in this project.</p>"
    input_only = _input_only_datasets(project)

    # Build a mapping of source datasets by name, and log any lookup failures:
    source_by_name = {}
    source_error = False
    if source_project is not None:
        try:
            source_by_name = {dataset.name: dataset for dataset in source_project.list_datasets()}
        except Exception:
            logging.getLogger(__name__).warning("Cannot list source project datasets", exc_info=True)
            source_error = True

    html = "<p>Dataset count: {}</p>".format(len(datasets))
    if source_project is not None:
        html += "<p>Source datasets are matched by name in the selected branch project.</p>"
    if source_error:
        html += "<p>Unable to read the selected project's dataset list.</p>"

    # We can now build a table of datasets with usage, type, and optional copy results:
    table_rows = []
    for dataset in datasets:
        source = source_by_name.get(dataset.name)
        source_type = source.type if source is not None else "—"
        usage = "input" if dataset.name in input_only else "output"
        table_rows.append((dataset, usage, source_type, source is None))

    table_rows.sort(key=lambda row: (
        row[1] == "input", row[0].type.casefold(), row[0].name.casefold(),
    ))

    # Adjust table headers depending on the run mode:
    headers = ["Dataset", "Usage", "Type"]
    if not dry_run:
        headers += ["Result", "Rows copied", "Details"]
    html += table_start(headers, "Project datasets")

    # Loop through the sorted datasets, resolve locations, and invoke copy handlers when in run mode:
    for index, (dataset, usage, source_type, source_missing) in enumerate(table_rows):
        source_location = target_location = None
        if usage != "input":
            if source_project is not None and not source_missing:
                source_location = get_dataset_location(source_project.project_key, dataset.name)
            target_location = get_dataset_location(project.project_key, dataset.name)

        # Build the table row with dataset name, usage, type, and optional copy results:
        cells = [dataset.name, usage, dataset.type]
        result = None
        if not dry_run:
            if usage == "input":
                result = CopyResult("Skipped", 0, "Input-only dataset.")

            elif source_project is None or source_missing:
                result = CopyResult("Failure", 0, "No matching dataset available in the selected source project.", source_unavailable=not source_error)

            else:
                result = copy_dataset(CopyContext(
                    source_project_key=source_project.project_key,
                    target_project_key=project.project_key,
                    dataset_name=dataset.name,
                    source_type=source_type,
                    target_type=dataset.type,
                    source_location=source_location,
                    target_location=target_location,
                    overwrite_existing=overwrite_existing,
                ))

            cells += [
                result.status, "{:,}".format(result.rows_copied) 
                if result.rows_copied is not None 
                else "Unknown", result.message
            ]

        tone = None
        if usage != "input":
            if source_error:
                tone = "error"
            elif source_project is not None and (source_missing or source_location is None):
                tone = "warning"
            elif result is not None:
                if result.source_unavailable or result.target_exists:
                    tone = "warning"
                elif result.status == "Success":
                    tone = "success"
                elif result.status == "Failure":
                    tone = "error"
        shared_location = result is not None and result.status == "Skipped" and result.shared_location
        html += table_row(
            cells, index, monospace=(0,), numeric=(4,) if not dry_run else (),
            emphasized=(2,) if dataset.type.casefold() == "snowflake" else (),
            muted=usage == "input",
            tone=tone,
            shared_location_column=5 if shared_location else None,
            nowrap=(1, 2, 3, 4) if not dry_run else (1, 2),
            word_wrap=(5,) if not dry_run else (),
        )

        if usage == "input" or shared_location:
            continue

        html += location_row(source_location, target_location, index, columns=len(headers))

    return html + table_end()
