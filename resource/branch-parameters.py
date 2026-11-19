def do(payload, config, plugin_config, inputs):
    import dataiku
    from branchdatasync.project_family import discover_project_family

    # DSS supplies the current project context:
    project_key = dataiku.default_project_key()
    if not project_key:
        raise ValueError("Open this macro from a project to load its associated branches.")

    # Discover the project family (master + branches) and return the list of branches to the macro UI:
    family = discover_project_family(dataiku.api_client(), project_key)
    if family["master_key"] is None:
        raise ValueError("Unable to resolve the original master project. Check project access and the macro logs.")

    # Build the list of branches to display in the macro UI...
    choices = [
        {"value": key, "label": "{} — {} ({})".format(branch, name, key)}
        for branch, name, key in family["rows"]
        if key and key != project_key       # ... excluding the current project:
    ]
    choices.sort(key=lambda choice: choice["label"].casefold())

    return {
        "master_name": family["master_name"],
        "master_key": family["master_key"],
        "choices": choices,
        "warnings": family["warnings"],
    }