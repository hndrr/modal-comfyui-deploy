from copy import deepcopy
from uuid import uuid4
import unittest
from ambient_fixtures import workflow, validate_object_info, object_info, request
from ambient_comfyui.workflows import WorkflowRegistry, h3_metadata
from model_manifests import FAST8_MODEL_FILES, FAST8_MODES

T2V, I2V = "fasth3-8step-t2v", "fasth3-8step-i2v"

class FastEightTest(unittest.TestCase):
    def body(self, mode=I2V, image="anchor.png", revision=0):
        req = request(mode=mode, sessionId=str(uuid4()), workflowRevision=revision)
        graph = workflow(req, image, object_info=object_info())
        return {"prompt": graph, "extra_data": {"ambient": h3_metadata(req, graph)}}


    def test_t2v_and_i2v_bind_distinct_attention_to_the_same_eight_step_model(self):
        for mode in FAST8_MODES:
            for ratio in ("9:16", "16:9", "1:1"):
                graph = workflow(request(mode=mode, aspectRatio=ratio, sound="Drums and bass"),
                                 "anchor.png" if mode == I2V else None, object_info=object_info())
                self.assertEqual(graph["1"]["inputs"]["unet_name"], FAST8_MODEL_FILES["unet"])
                self.assertEqual(graph["17"]["inputs"]["shift_video"], 10)
                self.assertEqual(graph["17"]["inputs"]["shift_audio"], 3)
                self.assertEqual(graph["18"]["inputs"]["attention"], "comfy kitchen attention")
                sparse = graph["2"]["inputs"]
                self.assertEqual(sparse["selection"], "sol-attn" if mode == I2V else "vsa")
                self.assertEqual(sparse.get("selection.tau"), 1.3 if mode == I2V else None)
                self.assertEqual(sparse.get("selection.keep_percent"), 10 if mode == T2V else None)
                self.assertEqual((sparse["start_percent"], sparse["min_tokens"], sparse["extra_tokens"]), (.2, 12288, 256))
                self.assertEqual(graph["9"]["inputs"]["sampler_name"], "res_multistep")
                self.assertEqual(graph["10"]["inputs"], {"model": ["2", 0], "scheduler": "simple", "steps": 8, "denoise": 1.0})
                self.assertEqual("first_frame" in graph["6"]["inputs"], mode == I2V)
                self.assertIn("Drums and bass", graph["6"]["inputs"]["prompt"])
                self.assertNotIn("LoraLoaderModelOnly", {n["class_type"] for n in graph.values()})
                self.assertTrue(validate_object_info(object_info(), mode))


    def test_fixed_reference_live_reference_revision_and_stage_isolation(self):
        registry = WorkflowRegistry({})
        registry.prepare(self.body())
        registry.prepare(self.body(T2V, None))
        template = deepcopy(registry.describe()["stages"][I2V])
        template["bindings"]["reference"]["source"] = "workflow"
        template["graph"]["16"]["inputs"]["image"] = "fixed.png"
        registry.apply(I2V, template, 0, object_info())
        fixed = registry.prepare(self.body(image=None, revision=1))
        self.assertEqual(fixed["prompt"]["16"]["inputs"]["image"], "fixed.png")
        self.assertEqual(fixed["prompt"]["6"]["inputs"]["first_frame"], ["16", 0])
        old = registry.prepare(self.body(image="live.png", revision=0))
        self.assertEqual(old["prompt"]["16"]["inputs"]["image"], "live.png")
        t2v = registry.prepare(self.body(T2V, None, 1))
        self.assertNotIn("16", t2v["prompt"])
        template["bindings"]["reference"]["source"] = "ambient"
        registry.apply(I2V, template, 1, object_info())
        live = registry.prepare(self.body(image="new.png", revision=2))
        self.assertEqual(live["extra_data"]["ambient"]["effective"]["reference"], "new.png")
        with self.assertRaisesRegex(ValueError, "requires a first-frame"):
            registry.prepare(self.body(image=None, revision=2))
        broken = deepcopy(template)
        broken["graph"]["6"]["inputs"].pop("first_frame")
        with self.assertRaisesRegex(ValueError, "reference is disconnected"):
            registry.apply(I2V, broken, 2, object_info())
        broken = deepcopy(template)
        del broken["bindings"]["reference"]
        with self.assertRaisesRegex(ValueError, "Keep all Ambient input bindings"):
            registry.apply(I2V, broken, 2, object_info())
