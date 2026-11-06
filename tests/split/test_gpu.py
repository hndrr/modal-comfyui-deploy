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
            self.assertEqual(select_gpu(choice, executable_graph(graph, defs, ["2"]), defs)["gpu"], "L4")
