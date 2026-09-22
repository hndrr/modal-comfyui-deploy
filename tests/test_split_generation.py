"""Duration migration and Split-only recipes, using public ComfyUI schemas."""
from copy import deepcopy
from uuid import uuid4
import unittest

from ambient.contracts import DEFAULT_BACKENDS, validate_request
from ambient.h3 import workflow as old_workflow
from ambient_fixtures import object_info
from comfy_split.ambient_workflows import WorkflowRegistry, h3_metadata, validate_template
from comfy_split.generation import MODES, NEW_MODES, REF_MODELS, workflow
from test_ambient import request


def objects():
    info = object_info()
    def node(inputs, outputs):
        return {"input": {"required": {k: [v] for k, v in inputs.items()}}, "output": outputs}
    info["UNETLoader"]["input"]["required"]["unet_name"][0].extend(REF_MODELS)
    info["MiniMaxH3ImageToVideo"]["input"]["required"]["length"] = ["INT", {"default": 124, "min": 5, "max": 3600}]
    info["VAEDecode"] = node({"samples": "LATENT", "vae": "VAE"}, ["IMAGE"])
    info["EasyCache"] = node({"model": "MODEL", "reuse_threshold": "FLOAT", "start_percent": "FLOAT", "end_percent": "FLOAT", "verbose": "BOOLEAN"}, ["MODEL"])
    info["SolAttnMiniMax"] = node({"model": "MODEL", "start_percent": "FLOAT", "end_percent": "FLOAT", "min_tokens": "INT", "sink_conditioning": ["exact_kv_and_rows"], "verbose": "BOOLEAN"}, ["MODEL"])
    info["SolAttnMiniMax"]["input"]["required"]["selection"] = ["COMFY_DYNAMICCOMBO_V3", {"options": [
        {"key": "VSA (FastVideo)", "inputs": {"required": {"vsa_keep_percent": ["FLOAT", {"default": 10.0}]}}}]}]
    ref = info["MiniMaxH3ReferenceToVideo"] = deepcopy(info["MiniMaxH3ImageToVideo"])
    ref["input"]["required"]["ref_image_size"] = [["match", "max"]]
    ref["input"]["optional"] = {"audio_vae": ["VAE"], "ref_images": ["COMFY_AUTOGROW_V3", {"template": {
        "prefix": "ref_image_", "min": 1, "max": 9, "input": {"optional": {"image": ["IMAGE"]}}}}]}
    return info


class SplitGenerationTest(unittest.TestCase):
    def setUp(self):
        self.info = objects()

    def body(self, mode="h3", revision=0, frames=124, names=None, image=None):
        req = request(mode=mode, frames=frames, sessionId=str(uuid4()), workflowRevision=revision)
        if names is not None:
            req["referenceNames"] = names
        graph = workflow(req, image, object_info=self.info)
        return {"prompt": graph, "extra_data": {"ambient": h3_metadata(req, graph)}}

    def test_old_recipes_and_api_modes_are_preserved(self):
        for mode in DEFAULT_BACKENDS:
            req = request(mode=mode, sessionId=str(uuid4()), workflowRevision=0)
            self.assertEqual(workflow(req, object_info=self.info), old_workflow(req, object_info=self.info))
        for mode in NEW_MODES:
            with self.assertRaisesRegex(ValueError, "Invalid generation mode"):
                validate_request(request(mode=mode))

    def test_frame_boundaries_all_modes_and_optional_default(self):
        for mode in MODES:
            for frames in (5, 39, 73, 124, 243, 3592):
                body = self.body(mode, frames=frames, image=None if mode == "h3-ref2v" else "first.png" if "i2v" in mode else None)
                self.assertEqual(body["prompt"]["6"]["inputs"]["length"], frames)
                self.assertEqual(body["extra_data"]["ambient"]["bindings"]["length"]["source"], "ambient")
            for frames in (0, -12, 124.0, True, 123, 3609):
                with self.assertRaisesRegex(ValueError, "Length"):
                    self.body(mode, frames=frames)

    def test_reference_recipe_order_limits_and_available_ref2va(self):
        body = self.body("h3-ref2v", names=["b.png", "a.png"])
        graph = body["prompt"]
        self.assertEqual(graph["1"]["inputs"]["unet_name"], REF_MODELS[0])
        self.assertEqual(graph["6"]["inputs"]["ref_image_size"], "match")
        self.assertEqual(graph["6"]["inputs"]["ref_images.ref_image_1"], ["ambient_ref_1", 0])
        self.assertEqual(graph["ambient_ref_0"]["inputs"]["image"], "b.png")
        self.assertEqual(graph["17"]["class_type"], "EasyCache")
        self.assertEqual(graph["10"]["inputs"]["steps"], 20)
        template = {"graph": graph, **{k: body["extra_data"]["ambient"][k] for k in ("bindings", "outputs")}}
        validate_template(template, template, self.info)
        self.info["UNETLoader"]["input"]["required"]["unet_name"][0].remove(REF_MODELS[0])
        self.assertEqual(self.body("h3-ref2v")["prompt"]["1"]["inputs"]["unet_name"], REF_MODELS[1])
        self.body("h3-ref2v", names=[f"{i}.png" for i in range(9)])
        for names in ([], ["a.png"] * 10, [None]):
            with self.assertRaisesRegex(ValueError, "1–9"):
                self.body("h3-ref2v", names=names)

    def test_vsa_modes_use_distinct_nodes_and_schedules(self):
        eight = self.body(NEW_MODES[1], image="first.png")["prompt"]
        four = self.body(NEW_MODES[2], image="first.png")["prompt"]
        self.assertEqual(eight["1"], four["1"])
        self.assertEqual((eight["17"]["inputs"]["shift_video"], four["17"]["inputs"]["shift_video"]), (10, 12))
        self.assertEqual(eight["2"]["class_type"], "BlockSparseAttention")
        self.assertEqual(eight["2"]["inputs"]["selection"], "vsa")
        self.assertEqual(eight["10"]["inputs"]["steps"], 8)
        self.assertEqual(four["2"]["class_type"], "SolAttnMiniMax")
        self.assertEqual(four["9"]["inputs"]["sampler_name"], "euler")
        self.assertEqual(four["10"]["inputs"]["sigmas"], "0.9999166, 0.9728326, 0.9230769, 0.8, 0.0")
        for mode in NEW_MODES[1:]:
            registry = WorkflowRegistry({})
            with self.assertRaisesRegex(ValueError, "requires a first-frame"):
                registry.prepare(self.body(mode))
            registry.prepare(self.body(mode, image="first.png"))

    def test_duration_migration_preserves_history_layout_and_explicit_ownership(self):
        registry = WorkflowRegistry({})
        registry.prepare(self.body())
        old = registry.describe()["stages"]["h3"]
        del old["bindings"]["length"]
        old["graph"]["10"]["inputs"]["steps"] = 13
        old["bindings"]["seed"]["source"] = "workflow"
        old["workflow"] = {"nodes": [{"id": 6, "pos": [12, 34]}]}
        registry.apply("h3", old, 0, self.info)
        accepted = self.body(revision=1)
        frozen = deepcopy(accepted)
        self.assertTrue(registry.upgrade_lengths())
        self.assertFalse(registry.upgrade_lengths())
        self.assertEqual(registry.snapshot(1)["stages"]["h3"], old)
        current = registry.describe()["stages"]["h3"]
        self.assertEqual(current["workflow"], old["workflow"])
        self.assertEqual(current["graph"], old["graph"])
        self.assertEqual(registry.prepare(self.body(revision=2, frames=243))["prompt"]["6"]["inputs"]["length"], 243)
        self.assertEqual(registry.prepare(accepted)["prompt"]["6"]["inputs"]["length"], 124)
        self.assertEqual(accepted, frozen)
        current["bindings"]["length"]["source"] = "workflow"
        current["graph"]["6"]["inputs"]["length"] = 73
        registry.apply("h3", current, 2, self.info)
        self.assertFalse(registry.upgrade_lengths())
        self.assertEqual(registry.prepare(self.body(revision=3, frames=243))["prompt"]["6"]["inputs"]["length"], 73)

    def test_saved_ref_workflow_replaces_all_images_when_order_and_count_change(self):
        registry = WorkflowRegistry({})
        registry.prepare(self.body("h3-ref2v", names=["old-a.png", "old-b.png", "old-c.png"]))
        template = registry.describe()["stages"]["h3-ref2v"]
        template["graph"]["17"]["inputs"]["reuse_threshold"] = 0.4
        registry.apply("h3-ref2v", template, 0, self.info)
        graph = registry.prepare(self.body("h3-ref2v", revision=1, names=["new-b.png", "new-a.png"]))["prompt"]
        inputs = graph["6"]["inputs"]
        self.assertNotIn("ref_images.ref_image_2", inputs)
        self.assertEqual([graph[inputs[f"ref_images.ref_image_{i}"][0]]["inputs"]["image"] for i in range(2)], ["new-b.png", "new-a.png"])
        self.assertEqual(graph["17"]["inputs"]["reuse_threshold"], 0.4)


if __name__ == "__main__":
    unittest.main()
