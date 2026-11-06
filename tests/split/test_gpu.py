import copy
import json
import unittest
from pathlib import Path

from comfy_split.gpu import DEFAULT, select_gpu
from comfy_split.mcp_policy import executable_graph


class MeasuredRoutingTests(unittest.TestCase):
    def test_only_measured_model_and_workload_use_the_calibrated_gpu(self):
        fixture = Path(__file__).resolve().parents[2] / "scripts/verify_split_gpu_workflow.json"
        graph = json.loads(fixture.read_text())
        definitions = {n["class_type"]: {"python_module": "nodes"} for n in graph.values()}
        definitions["EmptyFlux2LatentImage"]["python_module"] = "comfy_extras.nodes_flux"
        for size in (512, 1024):
            graph["6"]["inputs"].update(width=size, height=size)
            self.assertEqual(select_gpu("auto", graph, definitions)["gpu"], "L4")
            self.assertEqual(select_gpu("auto-speed", graph, definitions)["gpu"], DEFAULT)
        for node, field, value in [
            ("1", "unet_name", "other.safetensors"), ("6", "batch_size", 2),
            ("6", "width", ["other", 0]), ("7", "steps", 30),
        ]:
            with self.subTest(field=field):
                changed = copy.deepcopy(graph)
                changed[node]["inputs"][field] = value
                self.assertEqual(select_gpu("auto", changed, definitions)["gpu"], DEFAULT)

    def test_browser_partial_run_only_counts_requested_outputs(self):
        light = {"class_type": "EmptyImage", "inputs": {"width": 64, "height": 64, "batch_size": 1}}
        graph = {"1": light, "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
                 "3": {"class_type": "CustomVideoOutput", "inputs": {}}}
        defs = {node["class_type"]: {"python_module": "nodes", "output_node": key != "1"}
                for key, node in graph.items()}
        for choice in ("auto", "auto-speed"):
            self.assertEqual(select_gpu(choice, executable_graph(graph, defs), defs)["gpu"], DEFAULT)
            self.assertEqual(select_gpu(choice, executable_graph(graph, defs, ["2"]), defs)["gpu"], "CPU")

    def test_encoded_video_io_is_light_but_frame_processing_is_unmeasured(self):
        graph = {"1": {"class_type": "LoadVideo", "inputs": {"file": "movie.mp4"}},
                 "2": {"class_type": "SaveVideo", "inputs": {"video": ["1", 0], "format": "auto"}}}
        definitions = {name: {"python_module": "comfy_extras.nodes_video"}
                       for name in ("LoadVideo", "SaveVideo", "GetVideoComponents")}
        self.assertEqual(select_gpu("auto", graph, definitions)["gpu"], "CPU")
        graph["3"] = {"class_type": "GetVideoComponents", "inputs": {"video": ["1", 0]}}
        self.assertEqual(select_gpu("auto", graph, definitions)["gpu"], DEFAULT)

    def test_cpu_precedes_manual_gpu_but_requires_separate_registration(self):
        graph = {"1": {"class_type": "GeminiNode", "inputs": {"prompt": "hello"}}}
        defs = {"GeminiNode": {"python_module": "comfy_api_nodes.nodes_gemini", "api_node": True}}
        for choice in ("auto", "auto-speed", "L4", "L40S", DEFAULT):
            self.assertEqual(select_gpu(choice, graph, defs)["gpu"], "CPU")
        # Neither API_NODE nor a familiar class name alone confers CPU eligibility.
        for name, module in (("NewAPINode", "comfy_api_nodes.nodes_gemini"), ("GeminiNode", "custom_nodes.other")):
            unregistered = {"1": {"class_type": name, "inputs": {}}}
            definitions = {name: {"python_module": module, "api_node": True}}
            selected = select_gpu("auto", unregistered, definitions)
            self.assertEqual(selected["gpu"], DEFAULT)
            self.assertIn(name, selected["gpu_reason"])
            self.assertEqual(select_gpu("CPU", unregistered, definitions)["gpu"], "CPU")
            self.assertEqual(select_gpu("L40S", unregistered, definitions)["gpu"], "L40S")
