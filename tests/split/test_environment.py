import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from comfy_split import runtime
from comfy_split.build_constraints import build_constraints, core_pins, wheel_pins, wheel_roots
from comfy_split.check_environment import check_core_requirements, check_pins


class EnvironmentTests(unittest.TestCase):
    def test_discovers_new_remote_and_local_wheels_without_a_package_list(self):
        def installed(name, origin):
            return Mock(metadata={"Name": name}, read_text=Mock(return_value=origin))
        packages = [
            installed("remote", '{"url":"https://example.invalid/kernel.whl#sha256=abc"}'),
            installed("local", '{"url":"file:///opt/wheels/another.whl"}'),
            installed("ordinary", None), installed("source", '{"url":"file:///src/pkg","dir_info":{}}'),
        ]
        with patch("comfy_split.build_constraints.distributions", return_value=packages):
            self.assertEqual(set(wheel_roots()), {"remote", "local"})

    def test_base_wheels_protect_transitive_runtime_and_requested_extras(self):
        base = {"new-kernel": Mock(version="1", requires=["torch", "cuda[driver]", "uninstalled; python_version<'3'"]),
                "torch": Mock(version="2.10.0+cu130", requires=["triton", "cuda"]),
                "cuda": Mock(version="13", requires=["nv-lib; extra=='driver'", "optional; extra=='dev'"]),
                "nv-lib": Mock(version="13.1", requires=[]), "triton": Mock(version="3", requires=["torch"])}
        pins = wheel_pins(["new_kernel"], base.__getitem__)
        self.assertEqual(set(pins), {"new-kernel==1", "torch==2.10.0+cu130", "cuda==13", "nv-lib==13.1", "triton==3"})
        with self.assertRaisesRegex(RuntimeError, "wheel origins"):
            wheel_pins([])
        with patch("comfy_split.build_constraints.wheel_pins", return_value=pins):
            for frontend in ("1.57.4", "2.0"):
                requirements = f"torch>=2.11\ncomfyui-frontend-package=={frontend}\ncomfy-kitchen==1.0"
                for manager in ("4.2.2", "5.0.0"):
                    self.assertEqual(set(build_constraints(requirements, f"comfyui_manager == {manager}")),
                                     {*pins, f"comfyui-frontend-package=={frontend}", "comfy-kitchen==1.0",
                                      f"comfyui-manager=={manager}"})

    def test_core_pins_accept_requirement_spelling_comments_and_active_markers(self):
        requirements = """
            # Core requirements may use any normalized package spelling.
            Comfy_Kitchen == 1.0 ; python_version >= '3'  # pinned kitchen
            comfy.kitchen == 0.9 ; python_version < '3'
            ComfyUI_frontend_package == 1.57.4  # matching frontend
        """
        self.assertEqual(list(core_pins(requirements)),
                         ["comfy-kitchen==1.0", "comfyui-frontend-package==1.57.4"])
        installed = {"comfy-kitchen": "1.0", "comfyui-frontend-package": "1.57.4"}
        check_core_requirements(requirements, installed.__getitem__)

    def test_core_pins_leave_pip_directives_and_unrelated_sources_to_pip(self):
        requirements = (
            "--extra-index-url https://example.invalid/simple\n"
            "-r optional.txt\n-e git+https://example.invalid/node.git#egg=node\n"
            "./dist/node.whl\nhttps://example.invalid/node.whl\n"
            "comfy-kitchen == \\\n 1.0\ncomfyui-frontend-package==1.57.4\n"
        )
        self.assertEqual(list(core_pins(requirements)),
                         ["comfy-kitchen==1.0", "comfyui-frontend-package==1.57.4"])
        with self.assertRaisesRegex(RuntimeError, "must pin comfyui-manager"):
            list(core_pins("comfyui_manager>=5", ("comfyui-manager",)))

    def test_core_pins_require_an_active_exact_version(self):
        for spec in (">=1.0", "==1.*", "@ https://example.invalid/kitchen.whl",
                     "==1.0 ; python_version < '3'"):
            with self.subTest(spec=spec), self.assertRaisesRegex(RuntimeError, "must pin comfy-kitchen"):
                list(core_pins(f"comfy-kitchen {spec}\ncomfyui-frontend-package==1.57.4"))

    def test_core_upgrade_rejects_a_stale_frontend_even_if_internal_constraints_match(self):
        requirements = "comfyui-frontend-package==1.57.4\ncomfy-kitchen==1.0"
        installed = {"comfyui-frontend-package": "1.52.7", "comfy-kitchen": "1.0"}
        with self.assertRaisesRegex(RuntimeError, "comfyui-frontend-package"):
            check_core_requirements(requirements, installed.__getitem__)
        installed["comfyui-frontend-package"] = "1.57.4"
        check_core_requirements(requirements, installed.__getitem__)

    def test_candidate_rejects_install_scripts_that_override_protected_packages(self):
        check_pins("torch==2.10.0+cu130\n", lambda _: "2.10.0+cu130")
        with self.assertRaisesRegex(RuntimeError, "固定依存の競合"):
            check_pins("torch==2.10.0+cu130\n", lambda _: "2.11.0")

    def test_new_base_copies_nodes_without_copying_core(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "template"
            (template / "custom_nodes/pack").mkdir(parents=True)
            (template / "custom_nodes/pack/__init__.py").write_text("node")
            (template / "main.py").write_text("core")
            with patch.object(runtime, "TEMPLATE", template), patch.object(runtime, "ENVIRONMENTS", root / "envs"), \
                 patch.object(runtime, "USER", root / "user"), patch.object(runtime.subprocess, "run", Mock()):
                runtime.initialize_environment()
            self.assertFalse((root / "envs/base/comfy/main.py").exists())
            self.assertTrue((root / "envs/base/comfy/custom_nodes/pack/__init__.py").exists())

    def test_repair_clones_active_environment_and_preserves_other_nodes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environments = root / "environments"
            template = root / "template"
            source = environments / "env-old"
            node = "comfy/custom_nodes/ComfyUI-Image-Browsing"
            (source / node).mkdir(parents=True)
            (source / node / "pyproject.toml").write_text('version = "2.3.1"')
            other = source / "comfy/custom_nodes/comfyui-gguf"
            other.mkdir()
            (other / "__init__.py").write_text("# installed node")
            (source / "venv/bin").mkdir(parents=True)
            (source / "venv/bin/pip").write_text(f"#!{source}/venv/bin/python\n")
            bundled = template / "custom_nodes/ComfyUI-Image-Browsing"
            (bundled / "web").mkdir(parents=True)
            (bundled / "pyproject.toml").write_text('version = "2.3.0"')
            (bundled / "web/version.yaml").write_text("version: 2.3.0")
            (bundled / "web/manager.js").write_text("// bundled frontend")
            with patch.object(runtime, "ENVIRONMENTS", environments), patch.object(runtime, "TEMPLATE", template):
                version = runtime.create_environment("env-old", restore_image_browsing=True)
                unchanged = runtime.create_environment("env-old")
            repaired = environments / version
            self.assertIn("2.3.0", (repaired / node / "pyproject.toml").read_text())
            self.assertTrue((repaired / node / "web/manager.js").is_file())
            self.assertEqual((repaired / "comfy/custom_nodes/comfyui-gguf/__init__.py").read_text(), "# installed node")
            self.assertIn(str(repaired), (repaired / "venv/bin/pip").read_text())
            self.assertIn("2.3.1", (source / node / "pyproject.toml").read_text())
            self.assertIn("2.3.1", (environments / unchanged / node / "pyproject.toml").read_text())
