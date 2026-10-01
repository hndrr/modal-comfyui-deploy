import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from comfy_split import node_packs, runtime
from comfy_split.config import NODE_PACKS, Settings
from comfy_split.extension_sources import NODE_SOURCES
from comfy_split.state import write_json

PINNED = [source for source in NODE_SOURCES.values() if "revision" in source]
MANAGED_SETTINGS = Settings(node_packs=tuple(NODE_PACKS))


class ManagedRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.environments = self.root / "environments"
        self.source = self.environments / "base"
        (self.source / "comfy/custom_nodes/user-node").mkdir(parents=True)
        (self.source / "comfy/custom_nodes/user-node/__init__.py").write_text("# user node\n")
        (self.source / "venv/bin").mkdir(parents=True)
        (self.source / "venv/bin/python").symlink_to(sys.executable)
        (self.source / "venv/bin/pip").write_text(f"#!{self.source}/venv/bin/python\n")
        write_json(self.source / "catalog.json", {"objects": {"GPUOnly": {}}})
        self.run = subprocess.run
        self.remotes = {}
        for repo in node_packs.REPOSITORIES:
            name = repo.split("/")[1]
            remote = self.root / name
            self.run(["git", "init", "-q", "-b", "main", str(remote)], check=True)
            (remote / "__init__.py").write_text("# first version\n")
            (remote / "requirements.txt").write_text("jsonschema>=4.23,<5\n")
            self.commit(remote, "initial")
            self.remotes[repo] = remote
        self.calls = []
        self.fail_dependency = False
        self.fail_clone = False
        for source in PINNED:
            sha = subprocess.check_output(["git", "-C", str(self.remotes[source["repository"]]), "rev-parse", "HEAD"], text=True).strip()
            self.enterContext(patch.dict(source, revision=sha))
        self.enterContext(patch.object(runtime, "ENVIRONMENTS", self.environments))
        self.enterContext(patch.dict(os.environ, {**MANAGED_SETTINGS.environment(), node_packs.TOKEN_ENV: "test-token",
                              "GIT_TRACE_CURL": "1", "GIT_CURL_VERBOSE": "1"}))
        self.enterContext(patch.object(node_packs.subprocess, "run", side_effect=self.execute))

    def commit(self, remote, message):
        self.run(["git", "-C", str(remote), "add", "."], check=True)
        self.run(["git", "-C", str(remote), "-c", "user.name=Test", "-c",
                  "user.email=test@example.invalid", "commit", "-qm", message], check=True)

    def execute(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        if command[0] == "git":
            command = list(command)
            if "ls-remote" in command or "fetch" in command:
                if self.fail_clone:
                    raise subprocess.CalledProcessError(128, command)
                self.assertNotIn("GIT_TRACE_CURL", kwargs["env"])
                self.assertNotIn("GIT_CURL_VERBOSE", kwargs["env"])
                askpass = Path(kwargs["env"]["GIT_ASKPASS"])
                self.assertNotIn("test-token", askpass.read_text())
                response = self.run([str(askpass), "Password for 'https://github.com':"],
                                    env=kwargs["env"], check=True, capture_output=True, text=True)
                self.assertEqual(response.stdout.strip(), "test-token")
                index = next(i for i, value in enumerate(command)
                             if value.startswith("https://github.com/"))
                repo = command[index].removeprefix("https://github.com/").removesuffix(".git")
                command[index] = self.remotes[repo].as_uri()
            return self.run(command, **kwargs)
        self.assertNotIn(node_packs.TOKEN_ENV, kwargs["env"])
        if self.fail_dependency:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)

    def test_latest_default_branches_are_published_as_one_immutable_snapshot(self):
        version = node_packs.prepare_environment("base")
        target = self.environments / version
        manifest = json.loads((target / node_packs.MANIFEST).read_text())
        self.assertEqual(set(manifest), node_packs.NODE_NAMES)
        self.assertFalse((self.source / node_packs.DIRECTORY).exists())
        self.assertTrue((target / "comfy/custom_nodes/user-node/__init__.py").exists())
        self.assertEqual(json.loads((target / "catalog.json").read_text()),
                         {"objects": {"GPUOnly": {}}})
        pip = next(command for command, _ in self.calls if "pip" in command)
        self.assertEqual(pip.count("-r"), len(NODE_PACKS))
        self.assertIn("/opt/split-constraints.txt", pip)
        self.calls.clear()
        self.assertIsNone(node_packs.prepare_environment(version))
        self.assertEqual(sum("ls-remote" in command for command, _ in self.calls), len(NODE_PACKS) - len(PINNED))
        self.assertFalse(any("fetch" in command or "pip" in command for command, _ in self.calls))
        self.assertEqual(len(list(self.environments.iterdir())), 2)
        remote = self.remotes[NODE_PACKS["skills-loader"]]
        (remote / "__init__.py").write_text("# latest version\n")
        self.commit(remote, "update")
        self.calls.clear()
        updated = node_packs.prepare_environment(version)
        self.assertEqual(sum("fetch" in command for command, _ in self.calls), 1)
        name = remote.name
        self.assertEqual((target / node_packs.DIRECTORY / name / "__init__.py").read_text(),
                         "# first version\n")
        self.assertEqual((self.environments / updated / node_packs.DIRECTORY / name
                          / "__init__.py").read_text(), "# latest version\n")
        self.assertNotEqual(manifest[name], json.loads(
            (self.environments / updated / node_packs.MANIFEST).read_text())[name])
        self.assertTrue(all("test-token" not in " ".join(command) for command, _ in self.calls))

    def test_pins_ignore_later_default_branch_changes(self):
        version = node_packs.prepare_environment("base")
        for pin in PINNED:
            repo = self.remotes[pin["repository"]]
            (repo / "__init__.py").write_text("# incompatible future main")
            self.commit(repo, "advance unselected main")
        self.calls.clear()
        self.assertIsNone(node_packs.prepare_environment(version))
        self.assertFalse(any("fetch" in cmd for cmd, _ in self.calls))

    def test_declared_conflicts_fail_before_copying_candidate(self):
        selected = list(NODE_SOURCES.items())[:2]
        markers = ["fixture_a/nodes.py", "fixture_b/nodes.py"]
        conflicts = [{"markers": markers, "message": "incompatible fixture nodes"}]
        for (_, source), marker in zip(selected, markers):
            repo = self.remotes[source["repository"]]
            nodes = repo / marker
            nodes.parent.mkdir(parents=True)
            nodes.write_text("# duplicate IDs")
            self.commit(repo, "duplicate nodes")
            revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            self.enterContext(patch.dict(source, revision=revision))
        with patch.object(node_packs, "NODE_CONFLICTS", conflicts):
            with self.assertRaisesRegex(RuntimeError, "incompatible fixture nodes"):
                node_packs.prepare_environment("base")
            self.assertEqual(list(self.environments.iterdir()), [self.source])
            self.assertFalse(any("pip" in cmd for cmd, _ in self.calls))
            # A disabled pack must not participate in the candidate conflict check.
            name, source = selected[0]
            with patch.dict(os.environ, {"SPLIT_NODE_PACKS": name}):
                version = node_packs.prepare_environment("base")
        self.assertEqual(set(json.loads((self.environments / version / node_packs.MANIFEST).read_text())),
                         {source["repository"].split("/")[1]})

    def test_saved_snapshot_can_be_read_without_token_or_git(self):
        version = node_packs.prepare_environment("base")
        self.calls.clear()
        with patch.dict(os.environ, {node_packs.TOKEN_ENV: ""}):
            self.assertEqual(set(node_packs.snapshot_revisions(version)), node_packs.NODE_NAMES)
        self.assertEqual(self.calls, [])
        target = self.environments / version
        (target / node_packs.DIRECTORY / next(iter(node_packs.NODE_NAMES)) / "__init__.py").unlink()
        self.assertIsNone(node_packs.snapshot_revisions(version))

    def test_individual_selection_reuses_legacy_volume_and_fetches_only_selected_pack(self):
        version = node_packs.prepare_environment("base")
        original = self.environments / version
        (original / node_packs.DIRECTORY).rename(original / node_packs.LEGACY_DIRECTORY)
        (original / node_packs.MANIFEST).rename(original / node_packs.LEGACY_MANIFEST)
        legacy = (original / node_packs.LEGACY_MANIFEST).read_bytes()
        selected = "ComfyUI-GeminiTools"
        self.calls.clear()
        with patch.dict(os.environ, {"SPLIT_NODE_PACKS": "gemini", "SPLIT_EXTENSIONS": ""}):
            with patch.dict(os.environ, {node_packs.TOKEN_ENV: ""}):
                self.assertEqual(set(node_packs.snapshot_revisions(version)), {selected})
            self.assertEqual(self.calls, [])
            remote = self.remotes[NODE_PACKS["gemini"]]
            (remote / "__init__.py").write_text("# selected update\n")
            self.commit(remote, "update selected pack")
            updated = node_packs.prepare_environment(version)
            self.assertEqual(set(node_packs.snapshot_revisions(updated)), {selected})
        self.assertEqual(sum("ls-remote" in cmd for cmd, _ in self.calls), 1)
        self.assertEqual(sum("fetch" in cmd for cmd, _ in self.calls), 1)
        self.assertEqual(next(cmd for cmd, _ in self.calls if "pip" in cmd).count("-r"), 1)
        self.assertEqual((original / node_packs.LEGACY_MANIFEST).read_bytes(), legacy)
        target = self.environments / updated
        self.assertEqual({p.name for p in (target / node_packs.DIRECTORY).iterdir()}, {selected})
        self.assertEqual({p.name for p in (target / node_packs.LEGACY_DIRECTORY).iterdir()}, node_packs.NODE_NAMES)

    def test_fetch_pins_checked_revision_even_if_branch_advances(self):
        original_execute = self.execute
        updated = False
        first_repo = NODE_PACKS["skills-loader"]
        first_name = first_repo.split("/")[1]

        def advance_branch(command, **kwargs):
            nonlocal updated
            if "fetch" in command and not updated:
                remote = self.remotes[first_repo]
                (remote / "__init__.py").write_text("# later version\n")
                self.commit(remote, "advance after HEAD query")
                updated = True
            return original_execute(command, **kwargs)

        with patch.object(node_packs.subprocess, "run", side_effect=advance_branch):
            version = node_packs.prepare_environment("base")
        self.assertEqual((self.environments / version / node_packs.DIRECTORY
                          / first_name / "__init__.py").read_text(), "# first version\n")

    def test_corrupt_manifest_is_repaired_without_mutating_active_snapshot(self):
        version = node_packs.prepare_environment("base")
        target = self.environments / version
        (target / node_packs.MANIFEST).write_text("{")
        self.assertIsNone(node_packs.snapshot_revisions(version))
        repaired = node_packs.prepare_environment(version)
        self.assertIsNotNone(node_packs.snapshot_revisions(repaired))
        self.assertEqual((target / node_packs.MANIFEST).read_text(), "{")

    def test_failed_fetch_or_missing_token_never_copies_the_active_environment(self):
        self.fail_clone = True
        with self.assertRaises(subprocess.CalledProcessError):
            node_packs.prepare_environment("base")
        self.assertEqual(list(self.environments.iterdir()), [self.source])
        with (patch.dict(os.environ, {node_packs.TOKEN_ENV: ""}),
              self.assertRaisesRegex(RuntimeError, node_packs.TOKEN_ENV)):
            node_packs.prepare_environment("base")
        self.assertEqual(list(self.environments.iterdir()), [self.source])

    def test_failed_dependency_install_leaves_source_untouched(self):
        self.fail_dependency = True
        with self.assertRaises(subprocess.CalledProcessError):
            node_packs.prepare_environment("base")
        self.assertFalse((self.source / node_packs.MANIFEST).exists())
        self.assertFalse((self.source / node_packs.DIRECTORY).exists())
        self.assertEqual((self.source / "venv/bin/pip").read_text(),
                         f"#!{self.source}/venv/bin/python\n")

    def test_manager_candidate_retains_node_pack_snapshot(self):
        version = node_packs.prepare_environment("base")
        candidate = runtime.create_environment(version)
        original = self.environments / version
        copied = self.environments / candidate
        self.assertEqual((copied / node_packs.MANIFEST).read_text(),
                         (original / node_packs.MANIFEST).read_text())
        self.assertEqual({p.name for p in (copied / node_packs.DIRECTORY).iterdir()},
                         node_packs.NODE_NAMES)
