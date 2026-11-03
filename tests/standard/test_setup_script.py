"""Exercise setup.sh with fake CLIs; never contact a cloud account."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class SetupScriptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.checkout = self.directory / "checkout with spaces"
        (self.checkout / "scripts").mkdir(parents=True)
        (self.checkout / "worker" / "node_modules").mkdir(parents=True)
        for name in ("setup.sh", "modal.sh"):
            shutil.copy2(ROOT / "scripts" / name, self.checkout / "scripts" / name)
        shutil.copy2(ROOT / ".node-version", self.checkout / ".node-version")
        (self.checkout / ".env.example").write_text(
            "COMFYUI_REQUIRES_PROXY_AUTH=off\n", encoding="utf-8"
        )
        config = self.directory / "modal.toml"
        config.write_text("[pinned]\n[explicit]\n[shell]\n", encoding="utf-8")
        self.log = self.directory / "calls.jsonl"
        binaries = self.directory / "bin"
        binaries.mkdir()
        stub = f"#!{sys.executable}\n" + '''import json
import os
from pathlib import Path
import sys

tool = Path(sys.argv[0]).name
with open(os.environ["SETUP_TEST_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps({
        "tool": tool, "args": sys.argv[1:], "cwd": os.getcwd(),
        "profile": os.environ.get("MODAL_PROFILE", ""),
        "proxy_auth": os.environ.get("COMFYUI_REQUIRES_PROXY_AUTH", ""),
    }) + "\\n")
if tool + " " + " ".join(sys.argv[1:]) == os.environ.get("SETUP_TEST_FAIL"):
    sys.exit(37)
if tool == "node":
    print("v" + os.environ["SETUP_TEST_NODE_VERSION"])
'''
        for name in ("uv", "node", "npm"):
            binary = binaries / name
            binary.write_text(stub, encoding="utf-8")
            binary.chmod(0o755)
        self.environment = {
            **os.environ,
            "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
            "MODAL_CONFIG_PATH": str(config),
            "MODAL_PROFILE": "",
            "COMFYUI_REQUIRES_PROXY_AUTH": "off",
            "SETUP_TEST_LOG": str(self.log),
            "SETUP_TEST_NODE_VERSION": (ROOT / ".node-version").read_text().strip(),
            "SETUP_TEST_FAIL": "",
        }

    def run_script(self, *arguments, **environment):
        self.log.write_text("", encoding="utf-8")
        return subprocess.run(
            ["bash", str(self.checkout / "scripts" / "setup.sh"), *arguments],
            cwd=self.directory,
            env={**self.environment, **environment},
            capture_output=True,
            text=True,
            timeout=15,
        )

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_prepare_installs_locked_dependencies_and_preserves_existing_env(self):
        result = self.run_script("prepare")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [call for call in self.calls() if call["tool"] != "node"]
        self.assertEqual(
            [(call["tool"], call["args"]) for call in calls],
            [("uv", ["sync", "--locked", "--extra", "split-test"]), ("npm", ["ci"])],
        )
        self.assertEqual(calls[0]["cwd"], str(self.checkout))
        self.assertEqual(calls[1]["cwd"], str(self.checkout / "worker"))
        dotenv = self.checkout / ".env"
        self.assertEqual(dotenv.read_bytes(), (self.checkout / ".env.example").read_bytes())

        existing = b"COMFYUI_REQUIRES_PROXY_AUTH=off\nUNUSED=$(touch should-not-exist)\n"
        dotenv.write_bytes(existing)
        result = self.run_script("prepare")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(dotenv.read_bytes(), existing)
        self.assertFalse((self.checkout / "should-not-exist").exists())

    def test_modal_targets_preserve_profile_precedence_and_require_auth(self):
        (self.checkout / ".env").write_text("UNUSED=$(touch should-not-exist)\n")
        for target in ("standard", "split"):
            for arguments, shell, pin, expected in (
                ([], "", "", ""),
                ([], "", "pinned", "pinned"),
                ([], "shell", "pinned", "shell"),
                (["--profile", "explicit"], "shell", "pinned", "explicit"),
            ):
                with self.subTest(target=target, arguments=arguments, shell=shell, pin=pin):
                    pin_file = self.checkout / ".modal-profile"
                    pin_file.write_text(pin, encoding="utf-8")
                    config = Path(self.environment["MODAL_CONFIG_PATH"])
                    before = config.read_bytes()
                    result = self.run_script("deploy", target, *arguments, MODAL_PROFILE=shell)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    call, = self.calls()
                    app = "comfyapp.py" if target == "standard" else "splitapp.py"
                    self.assertEqual(
                        (call["tool"], call["args"]), ("uv", ["run", "modal", "deploy", app])
                    )
                    self.assertEqual(call["cwd"], str(self.checkout))
                    self.assertEqual(call["profile"], expected)
                    self.assertEqual(call["proxy_auth"], "on" if target == "standard" else "off")
                    self.assertEqual(pin_file.read_text(), pin)
                    self.assertEqual(config.read_bytes(), before)
                    self.assertFalse((self.checkout / "should-not-exist").exists())

    def test_worker_deploy_runs_only_after_tests_and_typecheck(self):
        result = self.run_script("deploy", "worker")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [call for call in self.calls() if call["tool"] == "npm"]
        self.assertEqual(
            [call["args"] for call in calls],
            [["test"], ["run", "typecheck"], ["run", "deploy"]],
        )
        self.assertTrue(all(call["cwd"] == str(self.checkout / "worker") for call in calls))

    def test_failures_stop_before_later_commands(self):
        for arguments, failure, expected in (
            (
                ("prepare",), "uv sync --locked --extra split-test",
                [("uv", ["sync", "--locked", "--extra", "split-test"])],
            ),
            (
                ("prepare",), "npm ci",
                [("uv", ["sync", "--locked", "--extra", "split-test"]), ("npm", ["ci"])],
            ),
            (("deploy", "worker"), "npm test", [("npm", ["test"])]),
            (
                ("deploy", "worker"), "npm run typecheck",
                [("npm", ["test"]), ("npm", ["run", "typecheck"])],
            ),
            (
                ("deploy", "split"), "uv run modal deploy splitapp.py",
                [("uv", ["run", "modal", "deploy", "splitapp.py"])],
            ),
        ):
            with self.subTest(failure=failure):
                result = self.run_script(*arguments, SETUP_TEST_FAIL=failure)
                self.assertEqual(result.returncode, 37, result.stderr)
                self.assertEqual(
                    [(call["tool"], call["args"]) for call in self.calls()
                     if call["tool"] != "node"],
                    expected,
                )

    def test_node_version_mismatch_stops_before_install_or_deploy(self):
        for arguments in (("prepare",), ("deploy", "worker")):
            with self.subTest(arguments=arguments):
                result = self.run_script(*arguments, SETUP_TEST_NODE_VERSION="0.0.0")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(".node-version", result.stderr)
                self.assertEqual([call["tool"] for call in self.calls()], ["node"])
                self.assertFalse((self.checkout / ".env").exists())

    def test_worker_without_dependencies_requests_prepare(self):
        (self.checkout / "worker" / "node_modules").rmdir()
        result = self.run_script("deploy", "worker")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("./scripts/setup.sh prepare", result.stderr)
        self.assertFalse(any(call["tool"] == "npm" for call in self.calls()))

    def test_invalid_arguments_never_call_external_tools(self):
        for arguments in (
            (), ("prepare", "split"), ("deploy",), ("deploy", "all"),
            ("deploy", "worker", "--profile", "explicit"),
            ("deploy", "standard", "--profile"),
            ("deploy", "split", "--profile", ""),
            ("deploy", "split", "--unknown", "explicit"),
            ("deploy", "split", "--profile", "explicit", "extra"),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_script(*arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("Usage:", result.stderr)
                self.assertEqual(self.calls(), [])
