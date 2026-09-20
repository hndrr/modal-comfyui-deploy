from copy import deepcopy
import json
from pathlib import Path
import unittest
from uuid import uuid4

from aiohttp import web
from aiohttp.test_utils import TestServer

from ambient.tagging import SCHEMA, TaggingError, classify, recipe, result, schema_version
from comfy_split.ambient_workflows import WorkflowRegistry, validate_template


# Public node definitions recaptured after the 2026-09-20 Skill Choice update
# (upstream 12239dab9f03cd4c3dead4b043028ea602b8d43b). Interpret no longer
# accepts skill_strength; tagging uses its unchanged multi_choice/score contract.
OBJECTS = json.loads((Path(__file__).parent / "fixtures/jev_object_info.json").read_text())
VALUES = {"scene": ["ocean", "rain"], "sound": ["water"], "motion": 0.2, "warmth": 0.4, "dream": 0.6}


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

    def test_schema_version_tracks_connected_rubrics_but_not_generation_state(self):
        graph = recipe({"prompt": "One"}, str(uuid4()), 0)["prompt"]
        other = recipe({"prompt": "Two"}, str(uuid4()), 0)["prompt"]
        self.assertEqual(schema_version(graph), schema_version(other))
        other["20"] = {"class_type": "PrimitiveStringMultiline", "inputs": {"value": '["quiet", "loud"]'}}
        other["4"]["inputs"]["candidates_json"] = ["20", 0]
        version = schema_version(other)
        other["20"]["inputs"]["value"] = '["silent", "loud"]'
        self.assertNotEqual(version, schema_version(other))

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

    def test_legacy_result_contract_is_still_readable(self):
        execution = {"graph": recipe({}, str(uuid4()), 0)["prompt"], "meta": {"outputs": {"tags": "30"}, "revision": 0}}
        value = result({"outputs": {"30": {"text": [json.dumps(VALUES)]}}}, execution)
        self.assertEqual(value["tags"], ["scene:ocean", "scene:rain", "sound:water"])


class JevExecutionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.body = None
        self.errors = None
        self.values = deepcopy(VALUES)
        self.job_id = str(uuid4())
        self.registry = WorkflowRegistry({})
        self.registry.prepare(recipe({}, str(uuid4()), 0))
        # Mirror a user-edited graph with a different preview node and provider.
        saved = self.registry.describe()["stages"]["jev"]
        previous = saved["outputs"]["scene"]
        saved["graph"]["70"] = saved["graph"].pop(previous)
        saved["outputs"]["scene"] = "70"
        saved["graph"]["2"]["inputs"]["provider"] = "openrouter"
        self.registry.apply("jev", saved, 0, OBJECTS)
        async def handler(request):
            if request.path == "/prompt":
                self.body = self.registry.prepare(await request.json())
                return web.json_response({"prompt_id": self.job_id})
            if request.path == f"/history/{self.job_id}":
                if self.errors:
                    history = {"status": {"status_str": "error", "messages": [["execution_error", {"exception_message": self.errors}]]}}
                else:
                    contract = self.body["extra_data"]["ambient"]["outputs"]
                    history = {"status": {"status_str": "success", "completed": True},
                               "outputs": {contract[name]: {"text": [json.dumps(value)]} for name, value in self.values.items()}}
                return web.json_response({self.job_id: history})
            if request.path == f"/ambient/executions/{self.job_id}":
                return web.json_response({"executions": [{"meta": self.body["extra_data"]["ambient"], "graph": self.body["prompt"]}]})
            raise web.HTTPNotFound()
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", handler)
        self.server = TestServer(app)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.base = str(self.server.make_url("/")).rstrip("/")

    async def test_saved_outputs_are_resolved_and_scores_and_schema_are_persisted(self):
        value = await classify(self.base, {}, {"prompt": "Actual edited video", "sound": "Waves"}, str(uuid4()), 1)
        self.assertEqual(value["status"], "completed")
        self.assertEqual(value["tags"], ["scene:ocean", "scene:rain", "sound:water"])
        self.assertEqual(value["scores"], {"motion": 0.2, "warmth": 0.4, "dream": 0.6})
        self.assertEqual(value["workflowRevision"], 1)
        self.assertEqual(value["executionId"], self.job_id)
        self.assertEqual(value["schemaVersion"], schema_version(self.body["prompt"]))
        self.assertIn("Actual edited video", self.body["prompt"]["1"]["inputs"]["value"])

    async def test_failure_retains_execution_id_without_leaking_remote_inputs(self):
        self.errors = "Node 'JevResolve' not found. api_key=do-not-expose"
        with self.assertRaises(TaggingError) as caught:
            await classify(self.base, {}, {}, str(uuid4()), 1)
        self.assertEqual(caught.exception.execution_id, self.job_id)
        self.assertIn("JevResolve", str(caught.exception))
        self.assertNotIn("do-not-expose", str(caught.exception))

    async def test_malformed_result_is_never_saved_as_successful_tags(self):
        for name, value in (("motion", True), ("dream", float("nan")), ("scene", "ocean")):
            with self.subTest(name=name):
                self.values = {**VALUES, name: value}
                with self.assertRaises(ValueError):
                    await classify(self.base, {}, {}, str(uuid4()), 1)


if __name__ == "__main__":
    unittest.main()
