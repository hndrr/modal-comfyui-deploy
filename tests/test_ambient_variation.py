import unittest
from ambient_fixtures import request
from ambient_comfyui.contracts import prompt_text, MUSIC_DIRECTION
from ambient_comfyui.workflows import field_value, set_field

class VariationTest(unittest.TestCase):
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
