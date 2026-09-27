from copy import deepcopy
from uuid import uuid4
import unittest
from ambient_fixtures import workflow, validate_object_info, object_info, request
from ambient_comfyui.workflows import WorkflowRegistry, h3_metadata
from model_manifests import FUSED_MODEL_FILES, MODEL_FILES
from ambient_comfyui.contracts import H3_FOUR_STEP_MODES

class H3FourStepTest(unittest.TestCase):
    def body(self, mode, image=None, revision=0):
        req = request(mode=mode, sessionId=str(uuid4()), workflowRevision=revision)
        graph = workflow(req, image, object_info=object_info())
        return {"prompt": graph, "extra_data": {"ambient": h3_metadata(req, graph)}}


    def test_four_step_routes_keep_audio_and_optional_first_frame(self):
        for mode in H3_FOUR_STEP_MODES:
            for image in (None, "first.png"):
                with self.subTest(mode=mode, image=image):
                    info = object_info()
                    if mode == "h3-fused-4step":
                        # Fused can execute even when no LoRA loader/asset exists.
                        del info["LoraLoaderModelOnly"]
                    graph = workflow(request(mode=mode, aspectRatio="9:16", sound="2-step drums and bass"),
                                     image, object_info=info)
                    self.assertEqual(graph["10"]["inputs"]["steps"], 4)
                    self.assertEqual(graph["9"]["inputs"]["sampler_name"], "res_multistep")
                    model_link = graph["7"]["inputs"]["model"]
                    self.assertEqual(model_link, graph["10"]["inputs"]["model"])
                    shift = graph[model_link[0]]["inputs"]
                    self.assertEqual((shift["shift_video"], shift["shift_audio"]), (12, 3))
                    kinds = [n["class_type"] for n in graph.values()]
                    self.assertEqual(kinds.count("LoraLoaderModelOnly"), 0 if mode == "h3-fused-4step" else 1)
                    expected = FUSED_MODEL_FILES if mode == "h3-fused-4step" else MODEL_FILES
                    self.assertEqual(graph["1"]["inputs"]["unet_name"], expected["unet"])
                    inputs = graph["6"]["inputs"]
                    self.assertEqual((inputs["width"], inputs["height"]), (576, 1024))
                    self.assertIn("2-step drums and bass", inputs["prompt"])
                    self.assertEqual("first_frame" in inputs, image is not None)
                    self.assertEqual(graph["14"]["inputs"]["audio"], ["13", 0])
                    self.assertTrue(validate_object_info(info, mode))
        old = workflow(request(), object_info=object_info())
        self.assertEqual(old["10"]["inputs"]["steps"], 8)
        self.assertNotIn("17", old)


    def test_optional_reference_and_applied_drafts_are_isolated_by_mode(self):
        registry = WorkflowRegistry({})
        for mode in ("h3", *H3_FOUR_STEP_MODES):
            registry.prepare(self.body(mode, "original.png"))
        mode = "h3-fused-4step"
        template = deepcopy(registry.describe()["stages"][mode])
        template["bindings"]["reference"]["source"] = "workflow"
        template["graph"]["16"]["inputs"]["image"] = "fixed.png"
        registry.apply(mode, template, 0, object_info())
        applied = registry.prepare(self.body(mode, None, 1))
        self.assertEqual(applied["prompt"]["16"]["inputs"]["image"], "fixed.png")
        # The older revision and other modes remain valid T2V workflows.
        for other, revision in ((mode, 0), ("h3-turbo-4step", 1), ("h3", 1)):
            prepared = registry.prepare(self.body(other, None, revision))
            self.assertNotIn("first_frame", prepared["prompt"]["6"]["inputs"])
            self.assertTrue(prepared["extra_data"]["ambient"]["bindings"]["reference"]["optional"])
