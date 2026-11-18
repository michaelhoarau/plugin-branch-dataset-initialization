import json
import inspect
from pathlib import Path
import runpy
import sys
import types
import unittest
from unittest.mock import Mock, patch


COMPONENT = Path(__file__).resolve().parents[1] / "python-runnables/copy-content"
sys.path.insert(0, str(COMPONENT.parents[1] / "python-lib"))


class MacroTests(unittest.TestCase):
    def setUp(self):
        self.dataiku = types.ModuleType("dataiku")
        self.client = Mock(spec=["get_project", "list_projects"])
        self.dataiku.api_client = lambda: self.client
        self.dataiku.default_project_key = lambda: "BRANCH"
        self.dataiku.Dataset = Mock()
        self.dataiku.Dataset.return_value.get_location_info.return_value = {
            "locationInfoType": "FS", "info": {"path": "/data/example"},
        }
        self.module_patch = patch.dict(sys.modules, {"dataiku": self.dataiku})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        runnables = types.ModuleType("dataiku.runnables")
        runnables.Runnable = object
        with patch.dict(sys.modules, {"dataiku": self.dataiku, "dataiku.runnables": runnables}):
            self.macro_class = runpy.run_path(str(COMPONENT / "runnable.py"))["MyRunnable"]
            self.setup_form = runpy.run_path(str(COMPONENT.parents[1] / "resource/branch-parameters.py"))["do"]
        self.projects = {}
        self.client.get_project.side_effect = self.projects.__getitem__
        self.add_project("MASTER", "Original <master>", "main")
        self.add_project("BRANCH", "First branch", "feature", "MASTER")
        self.add_project("SIBLING", "Sibling", "sibling", "MASTER")
        self.add_project("CHILD", "Nested branch", "nested", "BRANCH")
        self.add_project("OTHER", "Unrelated", "unrelated")
        self.projects["MASTER"].get_project_git().list_branches.return_value = ["main", "remote/release"]

    def add_project(self, key, name, branch, origin=None):
        project = Mock(spec=["get_project_git", "get_metadata", "list_datasets", "get_dataset", "list_recipes", "project_key"])
        project.project_key = key
        project.list_recipes.return_value = []
        project.get_metadata.return_value = {"label": name}
        project.list_datasets.return_value = [types.SimpleNamespace(name="<data>", type="Filesystem")]
        project.get_dataset.return_value.get_settings.return_value.get_raw.return_value = {}
        project.get_dataset.return_value.get_last_metric_values.return_value.get_global_data.side_effect = KeyError("No metrics")
        git = Mock(spec=["get_status", "list_branches"])
        git.get_status.return_value = {"currentBranch": branch, "originProjectKey": origin}
        git.list_branches.return_value = []
        project.get_project_git.return_value = git
        self.projects[key] = project
        self.client.list_projects.return_value = [
            {"projectKey": k, "name": p.get_metadata()["label"]}
            for k, p in self.projects.items()
        ]

    def run_macro(self, key="BRANCH"):
        return self.macro_class(key, {}, {}).run(None)

    def test_family_includes_siblings_nested_and_git_refs(self):
        html = self.run_macro()
        self.assertLess(html.index("&lt;data&gt;"), html.index("Original master project"))
        self.assertIn("Original &lt;master&gt; (MASTER)", html)
        for text in ["feature", "sibling", "nested", "remote/release", "Current project", "BRANCH"]:
            self.assertIn(text, html)
        self.assertNotIn("Unrelated", html)
        for project in self.projects.values():
            project.get_project_git().get_status.assert_called_once()

    def test_empty_datasets_still_shows_master_and_branches(self):
        self.projects["BRANCH"].list_datasets.return_value = []
        html = self.run_macro("CHILD")
        self.assertIn("Original &lt;master&gt; (MASTER)", html)
        html = self.run_macro()
        self.assertIn("No datasets found", html)
        self.assertIn("Sibling", html)

    def test_run_in_master(self):
        self.assertIn("Original &lt;master&gt; (MASTER) — current project", self.run_macro("MASTER"))

    def test_unreadable_ancestor_preserves_dataset_result(self):
        self.projects["MASTER"].get_project_git().get_status.side_effect = PermissionError("denied")
        with self.assertLogs(level="WARNING"):
            html = self.run_macro()
        self.assertIn("&lt;data&gt;", html)
        self.assertIn("Unable to resolve", html)

    def test_partial_discovery_is_explicit(self):
        self.client.list_projects.side_effect = PermissionError("denied")
        with self.assertLogs(level="WARNING"):
            html = self.run_macro()
        self.assertIn("feature", html)
        self.assertIn("duplicated branches may be missing", html)

    def test_ancestry_cycle_does_not_hang(self):
        self.projects["MASTER"].get_project_git().get_status.return_value["originProjectKey"] = "BRANCH"
        self.assertIn("Unable to resolve", self.run_macro())

    def test_descriptor(self):
        descriptor = json.loads((COMPONENT / "runnable.json").read_text())
        self.assertEqual(descriptor["resultType"], "HTML")
        self.assertTrue(descriptor["params"][0]["defaultValue"])
        self.assertEqual(descriptor["paramsTemplate"], "branch-parameters.html")
        self.assertTrue((COMPONENT.parents[1] / "resource" / descriptor["paramsPythonSetup"]).is_file())

    def test_parameter_choices_exclude_current_and_unrelated_projects(self):
        data = self.setup_form({}, {}, {}, [])
        self.assertEqual(data["master_name"], "Original <master>")
        self.assertEqual(data["master_key"], "MASTER")
        self.assertEqual({choice["value"] for choice in data["choices"]}, {"MASTER", "SIBLING", "CHILD"})

    def test_parameter_choices_exclude_master_when_run_in_master(self):
        self.dataiku.default_project_key = lambda: "MASTER"
        data = self.setup_form({}, {}, {}, [])
        self.assertEqual({choice["value"] for choice in data["choices"]}, {"BRANCH", "SIBLING", "CHILD"})

    def test_selected_project_is_checked_again_at_run_time(self):
        for key in ["BRANCH", "OTHER", "DELETED"]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.macro_class("BRANCH", {"branch_project_key": key}, {}).run(None)
        html = self.macro_class("BRANCH", {"branch_project_key": "SIBLING", "dry_run": False}, {}).run(None)
        self.assertIn("Selected branch project: SIBLING", html)
        self.assertIn("Run: Snowflake outputs use database-side cloning.", html)
        self.assertIn(">Rows copied</th>", html)
        self.assertIn("Dry run: report only", self.run_macro())

    def test_run_mode_requires_source_selection(self):
        with self.assertRaisesRegex(ValueError, "Select a source branch"):
            self.macro_class("BRANCH", {"dry_run": False}, {}).run(None)

    def test_parameter_error_does_not_return_a_misleading_master(self):
        self.projects["MASTER"].get_project_git().get_status.side_effect = PermissionError("denied")
        with self.assertLogs(level="WARNING"), self.assertRaises(ValueError):
            self.setup_form({}, {}, {}, [])

    def test_setup_entry_point_matches_dss_loader(self):
        descriptor = json.loads((COMPONENT / "runnable.json").read_text())
        setup_path = COMPONENT.parents[1] / "resource" / descriptor["paramsPythonSetup"]
        namespace = {}
        exec(compile(setup_path.read_text(), str(setup_path), "exec"), namespace, namespace)
        functions = [value for value in namespace.values() if inspect.isfunction(value)]
        entry_point = functions[0] if len(functions) == 1 else namespace.get("do")
        self.assertIs(entry_point, namespace["do"])
        self.assertEqual(len(inspect.signature(entry_point).parameters), 4)
        self.assertEqual(entry_point({}, {}, {}, [])["master_key"], "MASTER")


if __name__ == "__main__":
    unittest.main()
