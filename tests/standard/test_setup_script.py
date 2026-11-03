"""Check deployment safety with local Python validation and mocked cloud CLIs."""

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
        """Create an isolated checkout whose deployment commands only write a log."""
        temporary = tempfile.TemporaryDirectory(suffix=" with spaces")
        self.addCleanup(temporary.cleanup)
        self.checkout = Path(temporary.name).resolve()
        for name in ("scripts", "bin", "worker/node_modules"):
            (self.checkout / name).mkdir(parents=True)
        shutil.copy2(ROOT / "scripts/setup.sh", self.checkout / "scripts/setup.sh")
        for name in (".node-version", ".env.example"):
            shutil.copy2(ROOT / name, self.checkout / name)
        stub = f"#!{sys.executable}\n" + '''import os, sys
from pathlib import Path
tool = Path(sys.argv[0]).name
if tool == "node":
    print("v" + Path(".node-version").read_text().strip())
elif tool == "uv" and sys.argv[1:4] == ["run", "--locked", "python"]:
    os.execv(sys.executable, [sys.executable, *sys.argv[4:]])
else:
    with open(os.environ["SETUP_TEST_LOG"], "a") as stream:
        stream.write(" ".join([tool, *sys.argv[1:]]) + "\\n")
    if " ".join(sys.argv[1:]) == os.environ.get("SETUP_TEST_FAIL"):
        sys.exit(37)
'''
        for name in ("bin/uv", "bin/node", "bin/npm", "scripts/modal.sh"):
            binary = self.checkout / name
            binary.write_text(stub, encoding="utf-8")
            binary.chmod(0o755)
        self.log = self.checkout / "calls.log"
        self.environment = {
            **os.environ,
            "PATH": str(self.checkout / "bin") + os.pathsep + os.environ["PATH"],
            "SETUP_TEST_LOG": str(self.log),
            "SETUP_TEST_FAIL": "",
        }
        self.environment.pop("COMFYUI_REQUIRES_PROXY_AUTH", None)

    def run_script(self, *arguments, **environment):
        """Run the helper and collect cloud commands without executing them."""
        self.log.write_text("")
        result = subprocess.run(
            ["bash", str(self.checkout / "scripts/setup.sh"), *arguments],
            cwd=self.checkout,
            env={**self.environment, **environment},
            capture_output=True, text=True, timeout=15,
        )
        return result, self.log.read_text().splitlines()

    def test_standard_requires_persistent_auth_and_rejects_shell_override(self):
        """Block transient or disabled auth before forwarding a standard deployment."""
        dotenv = self.checkout / ".env"
        for saved, override in (("off", "on"), ("on", "off")):
            with self.subTest(saved=saved, override=override):
                dotenv.write_text(f"COMFYUI_REQUIRES_PROXY_AUTH={saved}\n")
                result, calls = self.run_script(
                    "deploy", "standard", COMFYUI_REQUIRES_PROXY_AUTH=override
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("COMFYUI_REQUIRES_PROXY_AUTH", result.stderr)
                self.assertEqual(calls, [])
        result, calls = self.run_script("deploy", "standard", "--profile", "setup-test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["modal.sh --profile setup-test deploy comfyapp.py"])
        self.assertEqual(dotenv.read_text(), "COMFYUI_REQUIRES_PROXY_AUTH=on\n")

    def test_worker_never_deploys_after_failed_checks(self):
        """Preserve each failed check's exit code and prevent the deployment."""
        for failure in ("test", "run typecheck"):
            with self.subTest(failure=failure):
                result, calls = self.run_script("deploy", "worker", SETUP_TEST_FAIL=failure)
                self.assertEqual(result.returncode, 37, result.stderr)
                self.assertNotIn("npm run deploy", calls)

    def test_prepare_preserves_existing_dotenv(self):
        """Keep existing configuration when preparing a checkout again."""
        dotenv = self.checkout / ".env"
        existing = b"COMFYUI_REQUIRES_PROXY_AUTH=on\nCOMFYUI_GPU_PROFILE=h100\n"
        dotenv.write_bytes(existing)
        result, _ = self.run_script("prepare")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(dotenv.read_bytes(), existing)
