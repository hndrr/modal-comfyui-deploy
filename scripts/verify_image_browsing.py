"""Run inside the split CPU container after the environment repair completes.

Checks published frontend delivery and output CRUD without waking a GPU.
Only files created by this check are renamed/deleted.
"""

import asyncio
import io
import json
import uuid
from pathlib import Path

import aiohttp
import modal
from PIL import Image


async def main():
    origin = "http://127.0.0.1:8000"
    gpu = modal.Function.from_name("comfyui-split", "gpu_worker")
    async with aiohttp.ClientSession() as client:
        async def get(path):
            async with client.get(origin + path) as response:
                assert response.status == 200, (path, response.status, await response.text())
                return await response.read()

        status = json.loads(await get("/split/status"))
        assert status["candidate"] is None and not status["busy"], status
        assert status["mode"] == "split", status
        node = Path("/environments") / status["environment"] / "comfy/custom_nodes/ComfyUI-Image-Browsing"
        assert 'version = "2.3.0"' in (node / "pyproject.toml").read_text()
        assert "version: 2.3.0" in (node / "web/version.yaml").read_text()
        extensions = json.loads(await get("/api/extensions"))
        assets = [path for path in extensions if "ComfyUI-Image-Browsing/" in path]
        assert assets, extensions
        for path in assets:
            assert await get(path)
        assert (await gpu.get_current_stats.aio()).num_total_runners == 0

        async def mutate(method, path, **kwargs):
            async with client.request(method, origin + path, **kwargs) as response:
                body = await response.json()
                assert response.status == 200 and body.get("success"), (response.status, body)

        folder = "split-image-browsing-" + uuid.uuid4().hex
        route = "/api/image-browsing/output/" + folder
        form = aiohttp.FormData()
        form.add_field("folders", folder, content_type="text/plain")
        await mutate("POST", "/api/image-browsing/output/", data=form)
        try:
            png = io.BytesIO()
            Image.new("RGB", (16, 16), (80, 140, 210)).save(png, format="PNG")
            form = aiohttp.FormData()
            form.add_field("files", png.getvalue(), filename="check.png", content_type="image/png")
            await mutate("POST", route, data=form)
            listing = json.loads(await get(route))
            assert any(item["name"] == "check.png" for item in listing["data"]), listing
            assert await get(route + "/check.png") == png.getvalue()
            preview = await get(route + "/check.png?preview=1")
            assert Image.open(io.BytesIO(preview)).format == "WEBP"
            await mutate("PUT", route + "/check.png", json={"filename": f"/output/{folder}/renamed.png"})
            assert await get(route + "/renamed.png") == png.getvalue()
            await mutate("DELETE", "/api/image-browsing/delete", json={"file_list": [f"/output/{folder}/renamed.png"]})
            assert json.loads(await get(route))["data"] == []
        finally:
            await mutate("DELETE", "/api/image-browsing/delete", json={"file_list": [f"/output/{folder}"]})

        # Existing generated results must also be accessible through this plugin.
        existing = json.loads(await get("/api/image-browsing/output/split-verification"))
        image = next(item for item in existing["data"] if item["name"].startswith("minimax_real_") and item["type"] == "image")
        assert await get("/api/image-browsing/output/split-verification/" + image["name"] + "?preview=1")
        assert (await gpu.get_current_stats.aio()).num_total_runners == 0
        print(json.dumps({"event": "image_browsing_verified", "environment": status["environment"],
                          "version": "2.3.0", "frontend_assets": assets,
                          "checks": ["listing", "upload", "preview", "rename", "delete", "generated_output"],
                          "gpu_runners": 0}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
