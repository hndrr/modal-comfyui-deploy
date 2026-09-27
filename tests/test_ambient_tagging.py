from copy import deepcopy
from uuid import uuid4
import unittest
import json
from pathlib import Path
from ambient_comfyui.workflows import WorkflowRegistry, validate_template
from ambient_comfyui.tagging import recipe
from ambient_comfyui.tagging import SCHEMA

OBJECTS = json.loads((Path(__file__).parent / "fixtures/jev_object_info.json").read_text())

class JevWorkflowTest(unittest.TestCase):
    def test_current_installed_node_contract_and_all_judgments_use_effective_state(self):
        state = {"prompt": "Edited shoreline", "sound": "Waves", "settings": {"seed": 17}}
        body = recipe(state, str(uuid4()), 0)
        registry = WorkflowRegistry({})
        registry.prepare(body)
        template = registry.describe()["stages"]["jev"]
        validate_template(template, template, OBJECTS)
        self.assertEqual(set(template["outputs"]), set(SCHEMA))
        for name, output in template["outputs"].items():
            node, slot = template["graph"][output]["inputs"]["source"]
            inputs = template["graph"][node]["inputs"]
            self.assertEqual(slot, 0)  # result, not raw response_json
            self.assertEqual(inputs["task"], SCHEMA[name]["type"])
            candidates = template["graph"][inputs["candidates_json"][0]]["inputs"]["value"]
            self.assertEqual(json.loads(candidates), list(SCHEMA[name]["criteria"]))
            state_node = template["graph"][inputs["state"][0]]
            self.assertEqual(json.loads(state_node["inputs"]["value"]), state)


    def test_refresh_removed_jev_default_preserves_saved_versions_and_accepted_body(self):
        registry = WorkflowRegistry({})
        legacy = recipe({}, str(uuid4()), 0)
        legacy["prompt"] = {
            "1": {"class_type": "JevInterpret", "inputs": {"state": "{}", "schema_json": "{}"}},
            "2": {"class_type": "JevResolve", "inputs": {"judgments": ["1", 0]}},
            "3": {"class_type": "PreviewAny", "inputs": {"source": ["2", 2]}},
        }
        legacy["extra_data"]["ambient"].update(
            bindings={"state": {"node": "1", "input": "state", "source": "ambient"}}, outputs={"tags": "3"})
        accepted = registry.prepare(legacy)
        saved = deepcopy(registry.describe()["stages"]["jev"])
        registry.state["versions"]["1"] = {"jev": saved}
        registry.state["revision"] = 1
        registry.prepare(recipe({}, str(uuid4()), 0))
        self.assertEqual(set(registry.describe(0)["stages"]["jev"]["outputs"]), set(SCHEMA))
        self.assertEqual(registry.describe(1)["stages"]["jev"], saved)
        self.assertEqual(accepted["prompt"], legacy["prompt"])
