import io
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from PIL import Image

from ambient.api import create_api
from ambient.contracts import ASPECT_RATIOS, RESOLUTIONS, generation_size, validate_request
from ambient.h3 import workflow
from ambient.service import Conflict, JobService
from ambient.storage import AmbientStorage, frame_path, image_path
from ambient_fixtures import object_info
from test_ambient import Store, Volume, request


class AspectRatioTests(unittest.TestCase):
    def test_both_models_generate_exact_ratios_on_the_h3_grid(self):
        for aspect, sizes in ASPECT_RATIOS.items():
            a, b = map(int, aspect.split(":"))
            for quality, size in sizes.items():
                for mode in ("h3", "fasth3"):
                    with self.subTest(aspect=aspect, quality=quality, mode=mode):
                        req = validate_request(request(mode=mode, resolution=quality, aspectRatio=aspect))
                        graph = workflow(req, object_info=object_info())
                        inputs = graph["6"]["inputs"]
                        self.assertEqual((inputs["width"], inputs["height"]), size)
                        self.assertEqual(size[0] * b, size[1] * a)
                        self.assertEqual((size[0] % 32, size[1] % 32), (0, 0))

    def test_legacy_jobs_keep_their_size_and_aspect_changes_conflict_on_retry(self):
        req = request()
        dispatches = []
        service = JobService(Store(), lambda key: dispatches.append(key) or "mock")
        service.submit(req)
        service.submit(req)
        self.assertEqual(len(dispatches), 1)
        self.assertEqual(generation_size(req), RESOLUTIONS["preview"])
        self.assertNotIn("aspectRatio", validate_request(req))
        with self.assertRaises(Conflict):
            service.submit({**req, "aspectRatio": "9:16"})
        for value in (None, [], {}, 1, "original", "__proto__", "100:1"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "aspect ratio"):
                validate_request(request(aspectRatio=value))

    def test_capabilities_publish_only_selectable_ratios(self):
        with TestClient(create_api(None, lambda: {}, None, None)) as client:
            response = client.get("/capabilities")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["aspectRatios"]["9:16"]["preview"], [576, 1024])
            self.assertNotIn("original", response.json()["aspectRatios"])

    def test_references_and_parent_frames_crop_to_the_selected_portrait_without_stretching(self):
        inputs = Volume()
        source = io.BytesIO()
        image = Image.new("RGB", (900, 900), "red")
        image.paste((0, 255, 0), (195, 0, 705, 900))
        image.save(source, format="PNG")
        with tempfile.TemporaryDirectory() as directory:
            for field, path in (("imageId", image_path), ("parentClipId", frame_path)):
                req = request(aspectRatio="9:16")
                req[field] = req["requestId"]
                inputs.files[path(req[field])] = source.getvalue()
                anchor = AmbientStorage(inputs, None).prepare_anchor(req, Path(directory))
                with Image.open(anchor) as result:
                    self.assertEqual(result.size, (576, 1024))
                    self.assertEqual(result.getpixel((0, 500)), (0, 255, 0))
                    self.assertEqual(result.getpixel((575, 500)), (0, 255, 0))
