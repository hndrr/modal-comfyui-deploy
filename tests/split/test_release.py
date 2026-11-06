import unittest
import runpy
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import call, patch

from comfy_split import release


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(release.os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in (release.REVISION_ENV, "GH_TOKEN", "GITHUB_TOKEN"):
            release.os.environ.pop(key, None)

    def test_resolves_tag_not_moving_target_branch(self):
        sha = "a" * 40
        data = {"tag_name": "stable/1", "target_commitish": "master", "draft": False, "prerelease": False}
        for env in ({}, {release.REVISION_ENV: ""}, {release.REVISION_ENV: " \t"}):
            with self.subTest(env=env), patch.dict(release.os.environ, env), \
                 patch.object(release, "github_json", side_effect=[data, {"sha": sha}]) as api:
                self.assertEqual(release.comfy_revision(local=True), sha)
            self.assertEqual(api.call_args_list, [call("releases/latest"), call("commits/refs%2Ftags%2Fstable%2F1")])

    def test_unusable_releases_stop_the_deployment(self):
        stable = {"tag_name": "v1", "draft": False, "prerelease": False}
        for responses in ([OSError("offline")], [HTTPError(release.API, 401, "Unauthorized", {}, None)],
                          [dict(stable, draft=True)], [dict(stable, prerelease=True)],
                          [stable, OSError("tag missing")], [stable, {"sha": "master"}]):
            with self.subTest(responses=responses), patch.object(release, "github_json", side_effect=responses):
                with self.assertRaises((RuntimeError, ValueError)):
                    release.comfy_revision(local=True)

    def test_container_without_image_revision_fails_offline(self):
        for env in ({}, {release.REVISION_ENV: ""}, {release.REVISION_ENV: " \t"}):
            with self.subTest(env=env), patch.dict(release.os.environ, env, clear=True), \
                 patch.object(release, "github_json") as api:
                with self.assertRaises(ValueError):
                    release.comfy_revision(local=False)
                api.assert_not_called()

    def test_explicit_revision_is_reused_locally_and_remotely_without_github(self):
        for local in (True, False):
            for sha in ("a" * 40, "main", "A" * 40):
                with self.subTest(local=local, sha=sha), \
                     patch.dict(release.os.environ, {release.REVISION_ENV: sha}), \
                     patch.object(release, "github_json") as api:
                    if sha == "a" * 40:
                        self.assertEqual(release.comfy_revision(local=local), sha)
                    else:
                        with self.assertRaises(ValueError):
                            release.comfy_revision(local=local)
                    api.assert_not_called()

    def test_github_uses_available_token_without_requiring_one(self):
        for key in (None, "GH_TOKEN", "GITHUB_TOKEN"):
            with self.subTest(key=key), patch.dict(release.os.environ, {key: "test-token"} if key else {}), \
                 patch.object(release, "urlopen") as request, \
                 patch.object(release.json, "load", return_value={"ok": True}):
                self.assertEqual(release.github_json("releases/latest"), {"ok": True})
                sent = request.call_args.args[0]
                self.assertEqual(sent.get_header("Authorization"), "Bearer test-token" if key else None)

    def test_gpu_verification_requires_a_revision_before_resolving_an_image(self):
        script = Path(__file__).resolve().parents[2] / "scripts/verify_split_gpus.py"
        for env in ({}, {release.REVISION_ENV: ""}, {release.REVISION_ENV: " \t"}):
            with self.subTest(env=env), patch.dict(release.os.environ, env), \
                 patch("modal.is_local", return_value=True), \
                 patch.object(release, "github_json", side_effect=AssertionError("Unexpected GitHub lookup")), \
                 self.assertRaisesRegex(ValueError, release.REVISION_ENV):
                runpy.run_path(str(script))
