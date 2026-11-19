"""Dataiku macro for previewing and initializing datasets in project branches."""

import dataiku

from dataiku.runnables import Runnable
from html import escape

from branchdatasync.project_family import discover_project_family
from branchdatasync.dataset_report import render_dataset_table
from branchdatasync.report_html import table_start, table_row, table_end, wrap_report


class MyRunnable(Runnable):
    """Report branch mappings and initialize Snowflake or Filesystem datasets.

    Dry run is enabled by default. Run mode delegates copying to type-specific
    handlers and reports each dataset's outcome alongside its storage mapping.
    """

    def __init__(self, project_key, config, plugin_config):
        """Store the execution context supplied by Dataiku DSS.

        Args:
            project_key (str): Current branch project, used as the copy target.
            config (dict): Macro parameters: ``branch_project_key`` selects the
                source project, ``dry_run`` defaults to True, and
                ``overwrite_existing`` defaults to False.
            plugin_config (dict): Plugin-level settings supplied by DSS.
        """
        self.project_key = project_key
        self.config = config
        self.plugin_config = plugin_config

    def get_progress_target(self):
        """Return None because this macro does not publish progress updates."""
        return None

    def run(self, progress_callback):
        """Validate the source selection and return the complete HTML report.

        Discover the original master and related branches, then delegate the
        dataset report and optional copy operations to ``render_dataset_table``.
        Dry run only reads metadata. Run mode clones eligible Snowflake output
        tables, skipping input-only datasets and shared locations. Existing
        targets are replaced only when ``overwrite_existing`` is explicitly True.
        Filesystem outputs are copied through Dataiku row streams, preserving
        matching partition layouts and synchronizing the target schema.

        Args:
            progress_callback (callable): DSS progress callback; currently unused.

        Returns:
            str: Styled HTML containing dataset mappings, copy results in run
            mode, and the original master and branch listing.

        Raises:
            ValueError: Run mode has no selected source, or the selected project
                is the current project or is not a discovered related project.
        """
        client = dataiku.api_client()
        project = client.get_project(self.project_key)
        
        # The following will recover the project Git branch ancestry:
        family = discover_project_family(client, self.project_key)
        
        # Collect the project key selected by the user and ensures that it's
        # actually usable as a source for the datasets of the new branch:
        selected_key = self.config.get("branch_project_key")
        dry_run = self.config.get("dry_run", True)
        if not dry_run and not selected_key:
            raise ValueError("Select a source branch project before copying datasets.")
        if selected_key and selected_key not in {
            key for _, _, key in family["rows"] if key and key != self.project_key
        }:
            raise ValueError("Select an associated branch project other than the current project.")
            
        # Generate and print the datasets table:
        heading = "<h2>Datasets in project {}</h2>".format(escape(self.project_key))
        source_project = client.get_project(selected_key) if selected_key else None
        overwrite_existing = self.config.get("overwrite_existing", False) is True
        datasets_html = render_dataset_table(
            project, source_project, dry_run=dry_run, overwrite_existing=overwrite_existing,
        )

        if dry_run:
            settings_html = "<p>Dry run: report only; no changes are made.</p>"
        else:
            settings_html = "<p>Run: Snowflake outputs use database-side cloning. Filesystem outputs are streamed through Dataiku.</p>"
            
        if selected_key:
            settings_html += "<p>Selected branch project: {}</p>".format(escape(selected_key))
            
        return wrap_report(settings_html + heading + datasets_html + self._branch_report(family))

    def _branch_report(self, family):
        """Format discovered master and branch information as an HTML fragment.

        Args:
            family (dict): Result of ``discover_project_family`` with
                ``master_key``, ``master_name``, ``rows`` containing
                (branch name, project name, project key) tuples, and ``warnings``.

        Returns:
            str: Escaped HTML showing the master and an alphabetically sorted
            branch table. Project-linked main/master branches appear first;
            rows without a project key appear last and are muted. The current
            project is highlighted, and discovery warnings follow the table.
            If the master cannot be resolved, an explanatory message is returned.

        This method only formats supplied data; it makes no API calls or writes.
        """
        master_key = family["master_key"]
        if master_key is None:
            return ("<h2>Original master project</h2>"
                    "<p>Unable to resolve the original master project. Check the macro logs and project access.</p>"
                    "<h2>Branches</h2><p>Unavailable because the original master could not be resolved.</p>")
        
        rows = family["rows"]
        html = "<h2>Original master project</h2><p>{} ({}){}</p>".format(
            escape(family["master_name"]), escape(master_key),
            " — current project" if master_key == self.project_key else "",
        )
        html += ("<h2>Branches</h2>"
                 "<p>Branches known to the master and related projects visible to the macro. "
                 "Remote refs reflect the last Git fetch.</p>")

        if rows:
            html += table_start(["Branch", "Project", "Project key"], "Project branches")

            sorted_rows = sorted(rows, key=lambda row: (
                not bool(row[2]), row[0].casefold() not in ("master", "main"),
                *(value.casefold() for value in row),
            ))

            for index, (branch, name, project_key) in enumerate(sorted_rows):
                html += table_row(
                    [branch, name, project_key], index,
                    current=project_key == self.project_key, monospace=(0, 2),
                    muted=not bool(project_key),
                )
            html += table_end()

        else:
            html += "<p>No branches found.</p>"

        for warning in family["warnings"]:
            html += "<p>{}</p>".format(escape(warning))

        return html
