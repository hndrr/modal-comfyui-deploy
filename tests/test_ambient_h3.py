import unittest

from ambient.h3 import workflow
from ambient.readiness import validate_object_info
from ambient_fixtures import object_info
from test_ambient import request


class UpstreamContractTest(unittest.TestCase):
    def test_save_video_nested_codec_follows_live_container_schema(self):
        info = object_info()
        codec = ["COMFY_DYNAMICCOMBO_V3", {"options": [
            {"key": "h264", "inputs": {"required": {}, "optional": {
                "encoding": ["COMFY_DYNAMICCOMBO_V3", {"options": [
                    {"key": "auto", "inputs": {"required": {}}},
                ]}],
            }}},
        ]}]
        inputs = info["SaveVideo"]["input"]
        del inputs["required"]["codec"]
        inputs["required"]["format"] = ["COMFY_DYNAMICCOMBO_V3", {"options": [
            {"key": "mp4", "inputs": {"required": {"codec": codec}}},
        ]}]
        # Upstream also exposes a hidden, optional legacy codec at the root.
        inputs["optional"] = {"codec": codec}
        for mode in ("h3", "fasth3"):
            with self.subTest(mode=mode):
                graph = workflow({**request(), "mode": mode}, object_info=info)
                saved = graph["15"]["inputs"]
                self.assertEqual(saved["format"], "mp4")
                self.assertEqual(saved["format.codec"], "h264")
                self.assertNotIn("codec", saved)
                self.assertNotIn("format.codec.encoding", saved)
                self.assertTrue(validate_object_info(info, mode))

    def test_input_defaults_and_reordered_outputs_follow_the_server(self):
        info = object_info()
        info["MiniMaxH3ImageToVideo"]["output"] = ["LATENT", "CONDITIONING"]
        info["SamplerCustomAdvanced"]["output_name"] = ["denoised_output", "output"]
        info["MiniMaxH3ImageToVideo"]["input"]["required"]["upstream_option"] = [
            "FLOAT",
            {"default": 0.75},
        ]
        graph = workflow(request(), "ambient/uploaded.png", object_info=info)
        self.assertEqual(graph["6"]["inputs"]["upstream_option"], 0.75)
        self.assertEqual(graph["7"]["inputs"]["conditioning"], ["6", 1])
        self.assertEqual(graph["11"]["inputs"]["latent_image"], ["6", 0])
        self.assertEqual(graph["12"]["inputs"]["samples"], ["11", 1])
        self.assertEqual(graph["13"]["inputs"]["samples"], ["11", 1])
        self.assertEqual(graph["6"]["inputs"]["first_frame"], ["16", 0])
        self.assertTrue(validate_object_info(info))

    def test_contract_breaks_are_explicit_instead_of_guessed(self):
        for change in (
            "missing_node",
            "renamed_input",
            "required_input",
            "ambiguous_output",
            "missing_sampler",
            "missing_model",
            "not_output",
        ):
            with self.subTest(change=change):
                info = object_info()
                node = info["MiniMaxH3ImageToVideo"]
                if change == "missing_node":
                    del info["MiniMaxH3ImageToVideo"]
                elif change == "renamed_input":
                    del node["input"]["optional"]["first_frame"]
                elif change == "required_input":
                    node["input"]["required"]["new_embedding"] = ["EMBEDDING"]
                elif change == "ambiguous_output":
                    node["output"].append("LATENT")
                elif change == "missing_sampler":
                    info["KSamplerSelect"]["input"]["required"]["sampler_name"] = [["euler"]]
                elif change == "missing_model":
                    info["VAELoader"]["input"]["required"]["vae_name"] = [[]]
                else:
                    info["SaveVideo"]["output_node"] = False
                with self.assertRaisesRegex(ValueError, "ComfyUI"):
                    validate_object_info(info)


if __name__ == "__main__":
    unittest.main()
