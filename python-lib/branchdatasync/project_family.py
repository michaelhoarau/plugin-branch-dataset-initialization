"""Discover project ancestry and branch information through read-only APIs."""
import logging


def discover_project_family(client, current_project_key):
    """Resolve the original master and discover its visible branch family.

    Args:
        client (DSSClient): Authenticated Dataiku API client.
        current_project_key (str): Project from which the macro is invoked.

    Returns:
        dict: Master key and display name under ``master_key`` and
        ``master_name``; ``rows`` of (branch name, project name, project key)
        tuples; and sorted ``warnings``. Git refs without a discovered project
        have empty project fields. An unresolved master has None identifiers
        and an empty row list.

    Follows originProjectKey links, handles cycles, and caches Git status within
    this call. Discovery is limited to readable projects and known Git refs;
    it does not fetch remotes or modify project state. API failures are logged
    and represented by partial results and warnings.
    """
    # Cache Git status because resolving ancestry can visit a project repeatedly.
    statuses = {}
    warnings = set()
    logger = logging.getLogger(__name__)

    def status(project_key):
        """Return cached Git status for a project key, or None on read failure.

        Failures are logged and add a warning to the enclosing discovery result.
        """
        if project_key not in statuses:
            try:
                statuses[project_key] = client.get_project(project_key).get_project_git().get_status()

            except Exception:
                logger.warning("Cannot read Git status for %s", project_key, exc_info=True)
                statuses[project_key] = None
                warnings.add("Some project ancestry could not be read; the branch list may be incomplete.")

        return statuses[project_key]

    def root(project_key):
        """Follow a project key's ancestry and return its original master key.

        Return None if status cannot be read or an ancestry cycle is detected;
        append warnings through the enclosing discovery state.
        """
        visited = set()
        while project_key not in visited:
            visited.add(project_key)
            project_status = status(project_key)
            if project_status is None:
                return None

            origin = project_status.get("originProjectKey")
            if not origin:
                return project_key

            project_key = origin
        warnings.add("A cycle was found in project ancestry; the original master could not be resolved.")

        return None

    master_key = root(current_project_key)
    if master_key is None:
        return {"master_key": None, "master_name": None, "rows": [],
                "warnings": sorted(warnings)}

    try:
        master_name = client.get_project(master_key).get_metadata().get("label") or master_key

    except Exception:
        logger.warning("Cannot read master project metadata", exc_info=True)
        master_name = master_key
        warnings.add("The master project's display name could not be read; its key is shown instead.")

    try:
        projects = client.list_projects()

    except Exception:
        logger.warning("Cannot list projects", exc_info=True)
        projects = []
        warnings.add("Projects could not be listed; duplicated branches may be missing.")

    projects_by_key = {item["projectKey"]: item for item in projects}
    projects_by_key.setdefault(current_project_key, {"projectKey": current_project_key})
    projects_by_key.setdefault(master_key, {"projectKey": master_key, "name": master_name})
    rows = []
    branch_names = set()
    for project_key, info in projects_by_key.items():
        if root(project_key) != master_key:
            continue

        project_status = status(project_key)
        branch = project_status.get("currentBranch") or "(no branch reported)"
        branch_names.add(branch)
        name = info.get("name") or info.get("label") or project_key
        rows.append((branch, name, project_key))

    # A duplicated project's branch may not exist in the master's local repo.
    # Combine project-per-branch information with the master's known Git refs.
    try:
        git_branches = client.get_project(master_key).get_project_git().list_branches(remote=True)
        rows.extend((branch, "", "") for branch in set(git_branches) - branch_names)

    except Exception:
        logger.warning("Cannot list master Git branches", exc_info=True)
        warnings.add("The master's Git branches could not be read; only discovered project branches are shown.")

    return {
        "master_key": master_key, 
        "master_name": master_name,
        "rows": rows, 
        "warnings": sorted(warnings)
    }