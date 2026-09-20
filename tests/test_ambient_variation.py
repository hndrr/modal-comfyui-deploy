from copy import deepcopy
import json
import unittest
from pathlib import Path
from uuid import uuid4
from types import SimpleNamespace
from ambient.contracts import validate_request, fingerprint, prompt_text, MUSIC_DIRECTION
from ambient.processing import run_job
from ambient.library import Library
from ambient.service import JobService
from ambient.tagging import SCHEMA, recipe, schema_version
from comfy_split.ambient_workflows import field_value, set_field, WorkflowRegistry
from test_ambient import Store, request
from test_ambient_library import MemoryVolume


def context():
    return {"version": 1, "scenePreset": "scene-chrome", "soundPresets": ["sound-chrome"],
            "lookPreset": "look-chrome", "family": "uk-garage", "source": "manual",
            "features": {"tags": ["scene:abstract", "sound:drums", "sound:bass"], "scores": {"motion": .8}}}


class VariationTest(unittest.TestCase):
    def test_optional_context_does_not_change_legacy_fingerprint_and_is_bounded(self):
        req = request()
        old = validate_request(req)
        self.assertNotIn("generationContext", old)
        self.assertEqual(fingerprint(old), fingerprint(validate_request(req)))
        good = validate_request({**req, "generationContext": context()})
        self.assertEqual(good["generationContext"], context())
        for patch in [{"version": 2}, {"features": {"scores": {"motion": float('nan')}}},
                      {"soundPresets": ["x"] * 5}, {"source": "invented"}, {"family": "x" * 81}]:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validate_request({**req, "generationContext": {**context(), **patch}})

    def test_context_is_saved_with_video_even_when_tagging_fails(self):
        jobs, volume = Store(), MemoryVolume()
        library = Library(Store(), volume)
        service = JobService(jobs, lambda _: None)
        req = request(saveToLibrary=True, generationContext=context())
        service.submit(req)
        def generate(request, image, source, cancelled, progress):
            source.write_bytes(b"video")
            return {"effective": {"prompt": "Chrome", "sound": "2-step drums"}}
        storage = SimpleNamespace(prepare_anchor=lambda *_: None,
            publish_clip=lambda _, id: {"id": id, "bytes": 5},
            download_clip=lambda _, path: path.write_bytes(b"video"))
        def fail(*_):
            raise RuntimeError("tagger offline")
        run_job(req["requestId"], jobs, storage, {("h3", "comfyui"): generate}, library=library, tag_dispatch=fail)
        self.assertEqual(service.get(req["requestId"])["status"], "completed")
        self.assertEqual(library.list()["clips"][0]["generation"]["context"], context())

    def test_music_follows_audio_and_workflow_music_edits_survive_binding_updates(self):
        req = request(sound="UK garage drums and bass")
        value = prompt_text(req)
        self.assertNotIn('non_diegetic_music: N/A', value)
        graph = {"1": {"inputs": {"prompt": value}}}
        prompt = {"node": "1", "input": "prompt", "part": "prompt"}
        sound = {**prompt, "part": "sound"}
        set_field(graph, prompt, "New scene")
        self.assertIn('UK garage drums and bass', graph['1']['inputs']['prompt'])
        graph['1']['inputs']['prompt'] = graph['1']['inputs']['prompt'].replace(MUSIC_DIRECTION, 'Live guitar and drums')
        set_field(graph, prompt, "Other scene")
        set_field(graph, sound, "Percussion and bass")
        self.assertTrue(graph['1']['inputs']['prompt'].endswith('non_diegetic_music: Live guitar and drums'))
        self.assertIn('Live guitar and drums', field_value(graph, sound))
        self.assertEqual(graph['1']['inputs']['prompt'].count('Live guitar and drums'), 1)
        # With workflow-owned sound, a prompt-only update retains explicit silence too.
        graph['1']['inputs']['prompt'] = prompt_text(req).replace(MUSIC_DIRECTION, 'N/A')
        set_field(graph, prompt, 'Visual only edit')
        self.assertTrue(graph['1']['inputs']['prompt'].endswith('N/A'))
        set_field(graph, sound, 'Drums')
        self.assertTrue(graph['1']['inputs']['prompt'].endswith(MUSIC_DIRECTION))

    def test_jev_schema_and_bridge_allow_rhythm_without_changing_output_contract(self):
        self.assertIn('waveform', SCHEMA['scene']['criteria'])
        self.assertIn('percussion', SCHEMA['sound']['criteria'])
        current = recipe({}, str(uuid4()), 0)
        old = deepcopy(current)
        old['prompt']['101']['inputs']['value'] = json.dumps(['rain', 'wind'])
        self.assertNotEqual(schema_version(current['prompt']), schema_version(old['prompt']))
        templates = Path('ambient/bridge_templates.json').read_text()
        self.assertNotIn('ゆっくり変化する環境動画', templates)
        registry = WorkflowRegistry({})
        registry.prepare(old)
        registry.prepare(current)
        self.assertIn('percussion', json.dumps(registry.describe()['stages']['jev']))
        self.assertEqual(registry.snapshot()['revision'], 0)
