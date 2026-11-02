import unittest
from unittest.mock import call, patch

from comfy_split import release


class ReleaseTests(unittest.TestCase):
    def test_resolves_tag_not_moving_target_branch(self):
        sha = "a" * 40
        data = {"tag_name": "stable/1", "target_commitish": "master", "draft": False, "prerelease": False}
        with patch.object(release, "github_json", side_effect=[data, {"sha": sha}]) as api:
            self.assertEqual(release.comfy_revision(local=True), sha)
        self.assertEqual(api.call_args_list, [call("releases/latest"), call("commits/refs%2Ftags%2Fstable%2F1")])

    def test_unusable_releases_stop_the_deployment(self):
        stable = {"tag_name": "v1", "draft": False, "prerelease": False}
        for responses in ([OSError("offline")], [dict(stable, draft=True)], [dict(stable, prerelease=True)],
                          [stable, OSError("tag missing")], [stable, {"sha": "master"}]):
            with self.subTest(responses=responses), patch.object(release, "github_json", side_effect=responses):
                with self.assertRaises((RuntimeError, ValueError)):
                    release.comfy_revision(local=True)

    def test_container_without_image_revision_fails_offline(self):
        with patch.dict(release.os.environ, {}, clear=True), patch.object(release, "github_json") as api:
            with self.assertRaises(ValueError):
                release.comfy_revision(local=False)
            api.assert_not_called()
