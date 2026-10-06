"""Observable CLI generation and standalone deployment checks."""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
OPTIONAL_FUNCTIONS = {
    "skill_context.py", "lite_subagent_registry.py", "previous_tool_context.py",
    "history_cleanup.py", "tool_call_filter.py", "subagent_context.py", "tool_call_tombstone_context.py",
}
FUNCTIONS = (
    "lite_handoff_router.py", "skill_context.py", "lite_subagent_registry.py",
    "previous_tool_context.py", "history_cleanup.py", "tool_call_filter.py", "subagent_context.py",
    "router_preparation.py",
)
SKILL_ID_CONSUMERS = {"lite_handoff_router.py", "skill_context.py", "lite_subagent_registry.py", "router_preparation.py"}


class SkillGenerationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "handoff_router"
        for name in (
            "shared/skill_preparation.py", "shared/request_runtime.py", "shared/tool_history.py", "shared/tool_context.py",
            "shared/registry_preparation.py", "shared/previous_tool_context.py", "shared/history_cleanup.py",
            "tools/generate_skill_preparation.py",
            *FUNCTIONS, "lite_delegate.py", "tool_call_tombstone_context.py",
        ):
            target = self.function_path(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            source = ROOT.parent / "optional_filters" / name if name in OPTIONAL_FUNCTIONS else ROOT / name
            shutil.copyfile(source, target)

    def function_path(self, name):
        directory = self.root.parent / "optional_filters" if name in OPTIONAL_FUNCTIONS else self.root
        return directory / name

    def run_generator(self, *args):
        return subprocess.run(
            [sys.executable, "-B", str(self.root / "tools/generate_skill_preparation.py"), *args],
            capture_output=True, text=True, check=False,
        )

    def outputs(self):
        return {name: self.function_path(name).read_bytes() for name in FUNCTIONS}

    def test_generation_is_reproducible_and_check_is_read_only(self):
        before = self.outputs()
        mtimes = {name: self.function_path(name).stat().st_mtime_ns for name in before}
        checked = self.run_generator("--check")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(self.outputs(), before)
        self.assertEqual({name: self.function_path(name).stat().st_mtime_ns for name in before}, mtimes)
        for _ in range(2):
            generated = self.run_generator()
            self.assertEqual(generated.returncode, 0, generated.stderr)
            self.assertEqual(self.outputs(), before)

    def test_normalization_source_update_refreshes_all_skill_id_lookups(self):
        source = self.root / "shared/skill_preparation.py"
        source.write_text(source.read_text().replace(
            '    result = []', '    # Changed normalization source\n    result = []', 1,
        ))
        before = self.outputs()
        checked = self.run_generator("--check")
        self.assertEqual(checked.returncode, 1)
        for name in SKILL_ID_CONSUMERS:
            self.assertIn(name, checked.stderr)
        self.assertEqual(self.outputs(), before)
        self.assertEqual(self.run_generator().returncode, 0)
        for name, output in self.outputs().items():
            if name not in SKILL_ID_CONSUMERS:
                self.assertEqual(output, before[name])
                continue
            self.assertNotEqual(output, before[name])
        self.assertEqual(self.run_generator("--check").returncode, 0)

    def test_source_change_requires_generation_and_check_never_writes(self):
        source = self.root / "shared/skill_preparation.py"
        source.write_text(source.read_text() + "\n# Updated authoritative source\n")
        before = self.outputs()
        checked = self.run_generator("--check")
        self.assertEqual(checked.returncode, 1, checked.stderr)
        self.assertIn("lite_handoff_router.py", checked.stderr)
        self.assertIn("skill_context.py", checked.stderr)
        self.assertEqual(self.outputs(), before)
        generated = self.run_generator()
        self.assertEqual(generated.returncode, 0, generated.stderr)
        regenerated = self.outputs()
        self.assertNotEqual(regenerated, before)
        self.assertEqual(self.run_generator("--check").returncode, 0)
        self.assertEqual(self.run_generator().returncode, 0)
        self.assertEqual(self.outputs(), regenerated)

    def test_generation_preserves_unrelated_deployment_code(self):
        router_path = self.root / "lite_handoff_router.py"
        router_path.write_text(router_path.read_text() + "\n# Role-specific local change\n")
        before = self.outputs()
        self.assertEqual(self.run_generator().returncode, 0)
        self.assertEqual(self.outputs(), before)

    def test_lifecycle_source_change_refreshes_every_function(self):
        source = self.root / "shared/request_runtime.py"
        source.write_text(source.read_text() + "\n# Updated lifecycle source\n")
        before = self.outputs()
        checked = self.run_generator("--check")
        self.assertEqual(checked.returncode, 1, checked.stderr)
        for name in FUNCTIONS:
            self.assertIn(name, checked.stderr)
        self.assertEqual(self.outputs(), before)
        self.assertEqual(self.run_generator().returncode, 0)
        for name, output in self.outputs().items():
            self.assertNotEqual(output, before[name])
        self.assertEqual(self.run_generator("--check").returncode, 0)

    def test_tool_history_source_change_refreshes_only_history_consumers(self):
        source = self.root / "shared/tool_history.py"
        source.write_text(source.read_text() + "\n# Updated Tool history source\n")
        before = self.outputs()
        consumers = {"lite_handoff_router.py", "tool_call_filter.py", "subagent_context.py", "previous_tool_context.py", "router_preparation.py"}
        checked = self.run_generator("--check")
        self.assertEqual(checked.returncode, 1, checked.stderr)
        for name in consumers:
            self.assertIn(name, checked.stderr)
        self.assertEqual(self.outputs(), before)
        self.assertEqual(self.run_generator().returncode, 0)
        for name, output in self.outputs().items():
            if name in consumers:
                self.assertNotEqual(output, before[name])
            else:
                self.assertEqual(output, before[name])
        self.assertEqual(self.run_generator("--check").returncode, 0)

    def test_tool_context_source_change_refreshes_only_projection_consumers(self):
        source = self.root / "shared/tool_context.py"
        source.write_text(source.read_text() + "\n# Updated Tool context source\n")
        before = self.outputs()
        consumers = {"tool_call_filter.py", "subagent_context.py", "lite_handoff_router.py"}
        checked = self.run_generator("--check")
        self.assertEqual(checked.returncode, 1, checked.stderr)
        for name in consumers:
            self.assertIn(name, checked.stderr)
        self.assertEqual(self.outputs(), before)
        self.assertEqual(self.run_generator().returncode, 0)
        for name, output in self.outputs().items():
            if name in consumers:
                self.assertNotEqual(output, before[name])
            else:
                self.assertEqual(output, before[name])
        self.assertEqual(self.run_generator("--check").returncode, 0)

    def test_check_detects_a_stale_deployment_copy_and_generation_repairs_it(self):
        before = self.outputs()
        for name in FUNCTIONS:
            with self.subTest(filename=name):
                path = self.function_path(name)
                path.write_text(path.read_text().replace(
                    "Authoritative local request lifecycle for independently uploaded Functions.",
                    "Stale request lifecycle.",
                ))
                stale = self.outputs()
                self.assertNotEqual(stale, before)
                checked = self.run_generator("--check")
                self.assertEqual(checked.returncode, 1)
                self.assertIn(name, checked.stderr)
                self.assertEqual(self.outputs(), stale)
                self.assertEqual(self.run_generator().returncode, 0)
                self.assertEqual(self.outputs(), before)

    def test_router_stage_source_changes_refresh_composed_and_standalone_outputs(self):
        for stage, standalone in (
            ("registry_preparation.py", "lite_subagent_registry.py"),
            ("previous_tool_context.py", "previous_tool_context.py"),
            ("history_cleanup.py", "history_cleanup.py"),
        ):
            with self.subTest(stage=stage):
                source = self.root / "shared" / stage
                source.write_text(source.read_text() + "\n# Updated Router stage\n")
                before = self.outputs()
                checked = self.run_generator("--check")
                self.assertEqual(checked.returncode, 1, checked.stderr)
                self.assertIn("router_preparation.py", checked.stderr)
                self.assertIn(standalone, checked.stderr)
                self.assertEqual(self.outputs(), before)
                self.assertEqual(self.run_generator().returncode, 0)
                for name, output in self.outputs().items():
                    if name in {standalone, "router_preparation.py"}:
                        self.assertNotEqual(output, before[name])
                    else:
                        self.assertEqual(output, before[name])
                self.assertEqual(self.run_generator("--check").returncode, 0)

    def test_each_supported_artifact_imports_without_neighbouring_runtime_modules(self):
        script = '''
import importlib.util
import sys
import types
from unittest.mock import AsyncMock

for name, attributes in {
            "open_webui.config": {"BYPASS_ADMIN_ACCESS_CONTROL": False},
            "open_webui.env": {"BYPASS_MODEL_ACCESS_CONTROL": False},
            "open_webui.models.models": {"Models": types.SimpleNamespace()},
            "open_webui.models.skills": {"Skills": types.SimpleNamespace()},
            "open_webui.models.users": {"Users": types.SimpleNamespace()},
            "open_webui.utils.chat": {"generate_chat_completion": AsyncMock()},
            "open_webui.utils.filter": {"get_filter_functions": AsyncMock(), "process_filter_functions": AsyncMock()},
            "open_webui.utils.misc": {"remove_system_message": lambda messages: messages},
            "open_webui.utils.models": {"check_model_access": AsyncMock()},
            "open_webui.utils.tools": {
                "get_attached_knowledge": lambda *args: [],
                "get_builtin_tools": AsyncMock(), "get_tools": AsyncMock(),
            },
}.items():
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    sys.modules[name] = module
spec = importlib.util.spec_from_file_location("standalone_function", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
getattr(module, sys.argv[2])()
'''
        for filename, entrypoint in (
            ("lite_handoff_router.py", "Pipe"), ("skill_context.py", "Filter"), ("lite_subagent_registry.py", "Filter"),
            ("previous_tool_context.py", "Filter"), ("history_cleanup.py", "Filter"),
            ("tool_call_filter.py", "Filter"), ("subagent_context.py", "Filter"),
            ("router_preparation.py", "Filter"), ("lite_delegate.py", "Tools"),
            ("tool_call_tombstone_context.py", "Filter"),
        ):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / filename
                shutil.copyfile(self.function_path(filename), target)
                imported = subprocess.run(
                    [sys.executable, "-I", "-B", "-c", script, str(target), entrypoint],
                    cwd=directory, capture_output=True, text=True, check=False,
                )
                self.assertEqual(imported.returncode, 0, imported.stderr)
